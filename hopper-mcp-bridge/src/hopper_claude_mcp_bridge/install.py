"""Wire the running HTTP bridge into Claude Code and macOS launchd.

Claude Code keeps its MCP servers in ``~/.claude.json`` under the top-level
``mcpServers`` object, as ``{"type": "http", "url": "..."}``. We edit that file
atomically so the rest of Claude Code's state is preserved untouched. A launchd
agent keeps the bridge itself running across logins.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .bridge import (
    DEFAULT_HOST,
    DEFAULT_LOG_PATH,
    DEFAULT_MOUNT_PATH,
    DEFAULT_PORT,
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

    @property
    def url(self) -> str:
        return _mcp_url(self.host, self.port, self.mount_path)


# --------------------------------------------------------------------------- #
# ~/.claude.json editing (atomic, non-destructive)
# --------------------------------------------------------------------------- #
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


def _set_server_entry(data: dict, name: str, url: str) -> None:
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers[name] = {"type": "http", "url": url}


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
        <string>--log-path</string>
        <string>{settings.log_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
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
def install(settings: InstallSettings, *, load_agent: bool = True) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    settings.launch_agent_path.parent.mkdir(parents=True, exist_ok=True)

    data = _load_claude_config(settings.claude_config_path)
    _set_server_entry(data, settings.server_name, settings.url)
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
        data = _load_claude_config(settings.claude_config_path)
        if _remove_server_entry(data, settings.server_name):
            _atomic_write_json(settings.claude_config_path, data)


def status(settings: InstallSettings) -> dict[str, str]:
    health_url = f"http://{settings.host}:{settings.port}/healthz"
    health = "down"
    try:
        with urllib.request.urlopen(health_url, timeout=2) as response:
            if response.read().decode("utf-8").strip() == "ok":
                health = "up"
    except (OSError, urllib.error.URLError):
        pass

    registered = "no"
    try:
        data = _load_claude_config(settings.claude_config_path)
        entry = data.get("mcpServers", {}).get(settings.server_name)
        if isinstance(entry, dict) and entry.get("url"):
            registered = "yes" if entry["url"] == settings.url else f"other:{entry['url']}"
    except (OSError, ValueError):
        registered = "unknown"

    return {
        "claude_config": str(settings.claude_config_path),
        "registered": registered,
        "launch_agent": str(settings.launch_agent_path),
        "mcp_url": settings.url,
        "health": health,
    }
