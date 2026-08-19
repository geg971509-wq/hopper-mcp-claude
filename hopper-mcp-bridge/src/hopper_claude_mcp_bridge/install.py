"""Wire the running HTTP bridge into Claude Code and macOS launchd.

Claude Code keeps its MCP servers in ``~/.claude.json`` under the top-level
``mcpServers`` object, as ``{"type": "http", "url": "...", "timeout": <ms>}``. We edit
that file atomically (and under an advisory lock) so the rest of Claude Code's state is
preserved untouched. A launchd agent keeps the bridge itself running across logins.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .bridge import (
    DEFAULT_HOST,
    DEFAULT_LOG_PATH,
    DEFAULT_MOUNT_PATH,
    DEFAULT_PORT,
    DEFAULT_TOOL_TIMEOUT_SEC,
    find_hopper_server,
)


DEFAULT_SERVER_NAME = "hopper"
DEFAULT_LABEL = "io.github.hopper-claude-mcp-http-bridge"


def _normalize_mount_path(value: str) -> str:
    value = value.strip() or DEFAULT_MOUNT_PATH
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/"


def _mcp_url(host: str, port: int, mount_path: str) -> str:
    return f"http://{host}:{port}{_normalize_mount_path(mount_path)}/"


@dataclass(frozen=True)
class InstallSettings:
    server_name: str = DEFAULT_SERVER_NAME
    label: str = DEFAULT_LABEL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mount_path: str = DEFAULT_MOUNT_PATH
    tool_timeout_sec: int = DEFAULT_TOOL_TIMEOUT_SEC
    hopper_server_path: str | None = None
    claude_config_path: Path = Path("~/.claude.json").expanduser()
    launch_agent_path: Path = Path(
        f"~/Library/LaunchAgents/{DEFAULT_LABEL}.plist"
    ).expanduser()
    log_path: Path = DEFAULT_LOG_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "mount_path", _normalize_mount_path(self.mount_path))
        object.__setattr__(
            self, "claude_config_path", self.claude_config_path.expanduser()
        )
        object.__setattr__(
            self, "launch_agent_path", self.launch_agent_path.expanduser()
        )
        object.__setattr__(self, "log_path", self.log_path.expanduser())
        object.__setattr__(
            self, "hopper_server_path", find_hopper_server(self.hopper_server_path)
        )

    @property
    def url(self) -> str:
        return _mcp_url(self.host, self.port, self.mount_path)

    @property
    def tool_timeout_ms(self) -> int:
        return self.tool_timeout_sec * 1000


# --------------------------------------------------------------------------- #
# ~/.claude.json editing (atomic, non-destructive, advisory-locked)
# --------------------------------------------------------------------------- #
@contextmanager
def _config_lock(path: Path):
    """Serialize concurrent bridge edits of the config via an advisory lock file.

    Note: this only guards against other bridge processes. Claude Code itself does not
    take this lock, so run ``install``/``uninstall`` while Claude Code is closed.
    """
    lock_path = path.with_name(path.name + ".hopper-bridge.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load_claude_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".hopper-bridge.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _set_server_entry(data: dict, name: str, url: str, timeout_ms: int) -> None:
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers[name] = {"type": "http", "url": url, "timeout": timeout_ms}


def _remove_server_entry(data: dict, name: str) -> bool:
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and name in servers:
        del servers[name]
        return True
    return False


# --------------------------------------------------------------------------- #
# launchd agent
# --------------------------------------------------------------------------- #
def _launch_agent_plist(settings: InstallSettings) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{settings.label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>hopper_claude_mcp_bridge</string>
        <string>serve</string>
        <string>--host</string>
        <string>{settings.host}</string>
        <string>--port</string>
        <string>{settings.port}</string>
        <string>--path</string>
        <string>{_normalize_mount_path(settings.mount_path)}</string>
        <string>--hopper-path</string>
        <string>{settings.hopper_server_path}</string>
        <string>--tool-timeout-sec</string>
        <string>{settings.tool_timeout_sec}</string>
        <string>--log-path</string>
        <string>{settings.log_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>
    <key>StandardOutPath</key>
    <string>{settings.log_path}.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{settings.log_path}.stderr.log</string>
</dict>
</plist>
"""


def _run_launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _user_uid() -> int:
    return int(subprocess.check_output(["id", "-u"], text=True).strip())


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def install(settings: InstallSettings, *, load_agent: bool = True) -> list[str]:
    """Install the bridge. Returns a list of human-readable warnings (may be empty)."""
    warnings: list[str] = []
    if not os.path.exists(settings.hopper_server_path or ""):
        warnings.append(
            f"HopperMCPServer not found at {settings.hopper_server_path}. The MCP server "
            "requires Hopper 6.0+ (Hopper 4/5 ship none). The bridge is installed but "
            "tool calls will fail until a compatible Hopper is present. Override with "
            "--hopper-path once installed."
        )

    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.launch_agent_path.parent.mkdir(parents=True, exist_ok=True)

    with _config_lock(settings.claude_config_path):
        data = _load_claude_config(settings.claude_config_path)
        _set_server_entry(
            data, settings.server_name, settings.url, settings.tool_timeout_ms
        )
        _atomic_write_json(settings.claude_config_path, data)

    settings.launch_agent_path.write_text(
        _launch_agent_plist(settings),
        encoding="utf-8",
    )

    if load_agent:
        uid = str(_user_uid())
        _run_launchctl("bootout", f"gui/{uid}", str(settings.launch_agent_path))
        bootstrap = _run_launchctl(
            "bootstrap",
            f"gui/{uid}",
            str(settings.launch_agent_path),
        )
        if bootstrap.returncode != 0:
            raise RuntimeError(bootstrap.stderr.strip() or bootstrap.stdout.strip())
        kickstart = _run_launchctl(
            "kickstart",
            "-k",
            f"gui/{uid}/{settings.label}",
        )
        if kickstart.returncode != 0:
            raise RuntimeError(kickstart.stderr.strip() or kickstart.stdout.strip())

    return warnings


def uninstall(
    settings: InstallSettings,
    *,
    remove_config: bool = True,
    unload_agent: bool = True,
) -> None:
    if unload_agent and settings.launch_agent_path.exists():
        uid = str(_user_uid())
        _run_launchctl("bootout", f"gui/{uid}", str(settings.launch_agent_path))

    if settings.launch_agent_path.exists():
        settings.launch_agent_path.unlink()

    if remove_config and settings.claude_config_path.exists():
        with _config_lock(settings.claude_config_path):
            data = _load_claude_config(settings.claude_config_path)
            if _remove_server_entry(data, settings.server_name):
                _atomic_write_json(settings.claude_config_path, data)


def status(settings: InstallSettings) -> dict[str, str]:
    base = f"http://{settings.host}:{settings.port}"

    bridge_health = "down"
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as response:
            if response.read().decode("utf-8").strip() == "ok":
                bridge_health = "up"
    except (OSError, urllib.error.URLError):
        pass

    backend = "unknown"
    hopper_server = settings.hopper_server_path or "?"
    try:
        with urllib.request.urlopen(f"{base}/readyz", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            hopper_server = payload.get("server_path", hopper_server)
            if not payload.get("server_path_exists", True):
                backend = "missing-hopper-binary (needs Hopper 6+)"
            else:
                backend = payload.get("backend", "unknown")
    except (OSError, urllib.error.URLError, ValueError):
        pass

    registered = "no"
    try:
        data = _load_claude_config(settings.claude_config_path)
        entry = data.get("mcpServers", {}).get(settings.server_name)
        if isinstance(entry, dict) and entry.get("url"):
            registered = (
                "yes" if entry["url"] == settings.url else f"other:{entry['url']}"
            )
    except (OSError, ValueError):
        registered = "unknown"

    return {
        "claude_config": str(settings.claude_config_path),
        "registered": registered,
        "launch_agent": str(settings.launch_agent_path),
        "mcp_url": settings.url,
        "tool_timeout_sec": str(settings.tool_timeout_sec),
        "hopper_server": hopper_server,
        "bridge_health": bridge_health,
        "backend": backend,
    }
