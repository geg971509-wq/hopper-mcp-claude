"""HTTP <-> stdio bridge that exposes Hopper's MCP server over streamable HTTP.

Data flow:

    Claude Code  --streamable HTTP-->  this bridge  --stdio/NDJSON-->  HopperMCPServer

The bridge itself is client-agnostic: it speaks the streamable-HTTP MCP transport on
one side and Hopper's native newline-delimited JSON-RPC on the other. Only the install
layer knows about Claude Code.

Requires Hopper 6.0 or newer. The bundled ``HopperMCPServer`` binary only ships with
Hopper 6+; Hopper 4/5 include no MCP server and cannot be bridged.
"""

from __future__ import annotations

import glob
import json
import os
import queue
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import uvicorn
from jsonschema import ValidationError, validate
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route


DEFAULT_HOPPER_SERVER_PATH = (
    "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer"
)
# Hopper bundles the MCP server under a few different app names / locations across
# versions. These globs are probed in order when no explicit path is given, so both
# the canonical bundle and versioned/relocated bundles resolve automatically. Only
# paths where the binary actually exists match, so an MCP-less Hopper (4/5) is skipped.
HOPPER_SERVER_GLOBS = (
    "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer",
    "/Applications/Hopper Disassembler*/Contents/MacOS/HopperMCPServer",
    "/Applications/Hopper*.app/Contents/MacOS/HopperMCPServer",
    str(Path("~/Applications").expanduser())
    + "/Hopper*.app/Contents/MacOS/HopperMCPServer",
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MOUNT_PATH = "/mcp"
# Reverse-engineering calls (decompiling a large function, analyzing a big Mach-O)
# routinely exceed the MCP client's ~60s default. Give them room, on both the client
# config (Claude Code's per-server ``timeout``) and this bridge's backend read timeout.
DEFAULT_TOOL_TIMEOUT_SEC = 300
DEFAULT_LOG_PATH = Path("~/.claude/logs/hopper-mcp-http-bridge.log").expanduser()

_VERSION = "0.2.0"
# Sentinel pushed onto the response queue when the backend's stdout closes.
_EOF = object()


def find_hopper_server(explicit: str | None = None) -> str:
    """Resolve the HopperMCPServer path.

    Order: explicit argument -> ``$HOPPER_MCP_SERVER_PATH`` -> known bundle globs ->
    default path. Falls back to the default (which may not exist) so callers can raise
    a clear "install Hopper 6+" error against a concrete path.
    """
    if explicit:
        return explicit
    env = os.environ.get("HOPPER_MCP_SERVER_PATH")
    if env:
        return env
    for pattern in HOPPER_SERVER_GLOBS:
        for match in sorted(glob.glob(pattern)):
            if os.path.exists(match):
                return match
    return DEFAULT_HOPPER_SERVER_PATH


def _normalize_mount_path(value: str) -> str:
    value = value.strip() or DEFAULT_MOUNT_PATH
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/"


@dataclass(frozen=True)
class BridgeSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mount_path: str = DEFAULT_MOUNT_PATH
    hopper_server_path: str = DEFAULT_HOPPER_SERVER_PATH
    tool_timeout_sec: int = DEFAULT_TOOL_TIMEOUT_SEC
    log_path: Path = DEFAULT_LOG_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "mount_path", _normalize_mount_path(self.mount_path))
        object.__setattr__(self, "log_path", self.log_path.expanduser())

    @property
    def mcp_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.mount_path}/"

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        return cls(
            host=os.environ.get("HOPPER_HTTP_BRIDGE_HOST", DEFAULT_HOST),
            port=int(os.environ.get("HOPPER_HTTP_BRIDGE_PORT", str(DEFAULT_PORT))),
            mount_path=_normalize_mount_path(
                os.environ.get("HOPPER_HTTP_BRIDGE_PATH", DEFAULT_MOUNT_PATH)
            ),
            hopper_server_path=find_hopper_server(),
            tool_timeout_sec=int(
                os.environ.get(
                    "HOPPER_HTTP_BRIDGE_TOOL_TIMEOUT", str(DEFAULT_TOOL_TIMEOUT_SEC)
                )
            ),
            log_path=Path(
                os.environ.get(
                    "HOPPER_HTTP_BRIDGE_LOG",
                    str(DEFAULT_LOG_PATH),
                )
            ).expanduser(),
        )


class BridgeLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def log(self, message: str) -> None:
        line = f"[hopper-http-bridge] {message}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class HopperBackend:
    """Owns the HopperMCPServer subprocess and talks to it over newline-delimited JSON.

    A dedicated reader thread parses every line Hopper emits and enqueues responses,
    which lets a request wait for *its own* id and skip past interleaved notifications
    (Hopper can emit those mid-call) instead of blindly treating the next line as the
    answer. The same queue makes a bounded read timeout possible, so a wedged Hopper
    call cannot pin the shared lock forever.
    """

    def __init__(
        self,
        server_path: str,
        logger: BridgeLogger,
        *,
        read_timeout_sec: int = DEFAULT_TOOL_TIMEOUT_SEC,
    ) -> None:
        self.server_path = server_path
        self.logger = logger
        self.read_timeout_sec = read_timeout_sec
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.next_id = count(1)
        self.tools_cache: list[types.Tool] | None = None
        self._responses: queue.Queue[Any] = queue.Queue()

    def close(self) -> None:
        # Best-effort: don't block shutdown behind an in-flight long call. If the lock
        # is held, terminate anyway — the in-flight request will unblock on EOF.
        acquired = self.lock.acquire(timeout=2)
        try:
            self._terminate_locked()
        finally:
            if acquired:
                self.lock.release()

    def probe(self) -> dict[str, Any]:
        """Best-effort, lock-free liveness snapshot (safe to call during a long call)."""
        proc = self.proc
        running = proc is not None and proc.poll() is None
        tools = self.tools_cache
        return {
            "server_path": self.server_path,
            "server_path_exists": os.path.exists(self.server_path),
            "backend": "running" if running else "stopped",
            "tools_cached": len(tools) if tools else 0,
        }

    def list_tools(self) -> list[types.Tool]:
        with self.lock:
            self._ensure_proc_locked()
            return self._list_tools_locked()

    def _list_tools_locked(self) -> list[types.Tool]:
        if self.tools_cache is None:
            response = self._request_locked("tools/list", {})
            if "error" in response:
                error = response["error"]
                raise RuntimeError(
                    f"Hopper tools/list error {error.get('code')}: "
                    f"{error.get('message')}"
                )
            self.tools_cache = [
                types.Tool.model_validate(tool)
                for tool in response["result"]["tools"]
            ]
            self.logger.log(f"cached {len(self.tools_cache)} Hopper tools")
        tools = deepcopy(self.tools_cache)
        for tool in tools:
            schema = tool.inputSchema
            properties = schema.get("properties", {})
            notes = []
            if "document" in properties:
                properties["document"].update(type="string", pattern=r"\S")
                schema["required"] = [
                    key for key in schema.get("required", []) if key != "document"
                ]
                notes.append(
                    "Omit document to resolve current_document at call time; "
                    "an explicit document must be a nonblank string and is never replaced."
                )
            if "procedure" in properties and "address" not in properties:
                properties["address"] = deepcopy(properties["procedure"])
                properties["address"]["description"] = "Alias for procedure."
                if "procedure" in schema.get("required", []):
                    schema["required"].remove("procedure")
                    schema.setdefault("allOf", []).append(
                        {"anyOf": [{"required": ["procedure"]}, {"required": ["address"]}]}
                    )
                notes.append("address is an alias for procedure; if both are supplied they must match.")
            schema["additionalProperties"] = False
            notes.append(
                "Accepted keys: " + (", ".join(sorted(properties)) or "(none)")
                + ". Unknown keys are rejected; use search_procedures to narrow procedure listings "
                "instead of an unsupported limit."
            )
            tool.description = " ".join([tool.description or "", *notes]).strip()
        return tools

    def list_prompts(self) -> list[types.Prompt]:
        return self._list_optional("prompts/list", "prompts", types.Prompt)

    def list_resources(self) -> list[types.Resource]:
        return self._list_optional("resources/list", "resources", types.Resource)

    def list_resource_templates(self) -> list[types.ResourceTemplate]:
        return self._list_optional(
            "resources/templates/list", "resourceTemplates", types.ResourceTemplate
        )

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        with self.lock:
            self._ensure_proc_locked()
            arguments = dict(arguments) if arguments is not None else {}
            tool = next((tool for tool in self._list_tools_locked() if tool.name == name), None)
            try:
                if tool is None:
                    raise ValueError(f"Unknown Hopper tool: {name}. Refresh tools/list.")
                properties = tool.inputSchema.get("properties", {})
                unknown = arguments.keys() - properties.keys()
                if unknown:
                    raise ValueError(
                        f"Unknown arguments: {', '.join(sorted(unknown))}. "
                        f"Accepted keys: {', '.join(sorted(properties)) or '(none)'}. "
                        "Use search_procedures to narrow procedure listings instead of an unsupported limit."
                    )
                if "document" in properties and "document" in arguments:
                    document = arguments["document"]
                    if not isinstance(document, str) or not document.strip():
                        raise ValueError("Supply a nonblank document name, or omit document to use current_document.")
                validate(arguments, tool.inputSchema)
                raw_tool = next((tool for tool in self.tools_cache or [] if tool.name == name), None)
                if raw_tool is None:
                    raise ValueError("Hopper tools changed during validation; refresh tools/list and retry.")
                raw_properties = raw_tool.inputSchema.get("properties", {})
                if "procedure" in raw_properties and "address" not in raw_properties and "address" in arguments:
                    address = arguments.pop("address")
                    if "procedure" in arguments and arguments["procedure"] != address:
                        raise ValueError("address and procedure must match; supply only one to select a procedure.")
                    arguments["procedure"] = address
                if "document" in properties and "document" not in arguments:
                    hint = "Cannot resolve current_document; supply an explicit document name (see list_documents)."
                    try:
                        current = self._request_locked(
                            "tools/call", {"name": "current_document", "arguments": {}}
                        )
                        if "error" in current:
                            raise ValueError(hint)
                        result = types.CallToolResult.model_validate(current.get("result", {}))
                    except Exception as exc:
                        raise ValueError(hint) from exc
                    if result.isError or len(result.content) != 1:
                        raise ValueError(hint)
                    content = result.content[0]
                    if not isinstance(content, types.TextContent) or not content.text.strip():
                        raise ValueError(hint)
                    arguments["document"] = content.text
            except (ValueError, ValidationError) as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))], isError=True
                )
            response = self._request_locked(
                "tools/call", {"name": name, "arguments": arguments}
            )

            if "error" in response:
                error = response["error"]
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=f"Hopper error {error.get('code')}: {error.get('message')}",
                        )
                    ],
                    isError=True,
                )

            result = dict(response.get("result", {}))
            result.setdefault("isError", False)
            return types.CallToolResult.model_validate(result)

    def _list_optional(
        self, method: str, key: str, model: Any
    ) -> list[Any]:
        """Forward an optional capability (prompts/resources) transparently.

        Only probed when Hopper is *already* running, so a tool-only session is never
        forced to launch Hopper just to answer a startup ``prompts/list``. If Hopper
        does not implement the method it replies with an error, which we treat as empty.
        """
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return []
            response = self._request_locked(method, {})
            if "error" in response:
                return []
            items = response.get("result", {}).get(key, []) or []
            return [model.model_validate(item) for item in items]

    def _ensure_proc_locked(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return

        self._terminate_locked()
        self.tools_cache = None

        if not os.path.exists(self.server_path):
            raise RuntimeError(
                f"HopperMCPServer not found at {self.server_path}. The MCP server ships "
                "only with Hopper 6.0+ (Hopper 4/5 do not include it). Install Hopper 6+, "
                "or point --hopper-path / $HOPPER_MCP_SERVER_PATH at the binary."
            )

        self.logger.log(f"starting Hopper backend path={self.server_path}")
        self._responses = queue.Queue()
        try:
            self.proc = subprocess.Popen(
                [self.server_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(
                f"failed to launch HopperMCPServer at {self.server_path}: {exc}"
            ) from exc

        threading.Thread(
            target=self._reader_loop,
            args=(self.proc, self._responses),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._stderr_loop,
            args=(self.proc,),
            daemon=True,
        ).start()

        response = self._request_locked(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "hopper-claude-mcp-http-bridge",
                    "version": _VERSION,
                },
            },
        )
        negotiated = response.get("result", {}).get("protocolVersion")
        self.logger.log(f"Hopper backend initialized protocol={negotiated}")
        # MCP spec: the client acknowledges initialization before issuing requests.
        self._notify_locked("notifications/initialized", {})

    def _reader_loop(self, proc: subprocess.Popen[str], responses: queue.Queue) -> None:
        stdout = proc.stdout
        assert stdout is not None
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.logger.log(f"backend -> dropped non-JSON line: {line[:200]}")
                    continue
                method = message.get("method")
                if method == "notifications/tools/list_changed":
                    self.tools_cache = None
                    self.logger.log("backend -> tools/list_changed; tool cache cleared")
                # A response carries an id and no method; anything else is a
                # notification (or server-initiated request) we don't forward.
                if "id" in message and method is None:
                    responses.put(message)
                else:
                    self.logger.log(f"backend -> notification {method}")
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.log(f"reader thread error: {exc}")
        finally:
            responses.put(_EOF)

    def _stderr_loop(self, proc: subprocess.Popen[str]) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                line = line.rstrip()
                if line:
                    self.logger.log(f"backend stderr: {line}")
        except Exception:  # pragma: no cover - defensive
            pass

    def _request_locked(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = next(self.next_id)
        self._write_locked(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )

        deadline = time.monotonic() + self.read_timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.logger.log(
                    f"backend timeout after {self.read_timeout_sec}s "
                    f"waiting for id={request_id} method={method}"
                )
                self._terminate_locked()
                raise RuntimeError(
                    f"Hopper backend timed out after {self.read_timeout_sec}s on {method}"
                )
            try:
                message = self._responses.get(timeout=remaining)
            except queue.Empty:
                continue
            if message is _EOF:
                raise RuntimeError("Hopper backend closed stdout")
            if message.get("id") == request_id:
                self.logger.log(f"backend -> response id={request_id}")
                return message
            self.logger.log(
                f"backend -> discarding stale response id={message.get('id')}"
            )

    def _notify_locked(self, method: str, params: dict[str, Any]) -> None:
        self._write_locked({"jsonrpc": "2.0", "method": method, "params": params})

    def _write_locked(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Hopper backend is not running")

        if self.proc.poll() is not None:
            raise RuntimeError(
                f"Hopper backend exited with code {self.proc.returncode}"
            )

        message = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self.proc.stdin.write(message + "\n")
        self.proc.stdin.flush()
        self.logger.log(
            f"backend <- {payload.get('method', 'response')} id={payload.get('id')}"
        )

    def _terminate_locked(self) -> None:
        if self.proc is None:
            return

        proc = self.proc
        self.proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def create_app(settings: BridgeSettings) -> Starlette:
    logger = BridgeLogger(settings.log_path)
    backend = HopperBackend(
        settings.hopper_server_path,
        logger,
        read_timeout_sec=settings.tool_timeout_sec,
    )
    server = Server("hopper", version=_VERSION)
    # A single stateless transport: the bridge serves exactly one local client
    # (Claude Code) issuing sequential request/response calls. Multi-client fan-out
    # would want StreamableHTTPSessionManager instead.
    transport = StreamableHTTPServerTransport(mcp_session_id=None)

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return await anyio.to_thread.run_sync(backend.list_prompts)

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return await anyio.to_thread.run_sync(backend.list_resources)

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return await anyio.to_thread.run_sync(backend.list_resource_templates)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await anyio.to_thread.run_sync(backend.list_tools)

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        return await anyio.to_thread.run_sync(backend.call_tool, name, arguments)

    async def healthz(_request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def readyz(_request) -> JSONResponse:
        data = await anyio.to_thread.run_sync(backend.probe)
        return JSONResponse(data)

    async def mcp_asgi(scope, receive, send) -> None:
        await transport.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        logger.log(f"HTTP bridge starting on {settings.mcp_url}")
        if not os.path.exists(settings.hopper_server_path):
            logger.log(
                f"WARNING: HopperMCPServer not found at {settings.hopper_server_path}; "
                "the MCP server requires Hopper 6.0+ (Hopper 4/5 have none). "
                "Tool calls will fail until a compatible Hopper is installed."
            )
        async with transport.connect() as (read_stream, write_stream):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    server.run,
                    read_stream,
                    write_stream,
                    server.create_initialization_options(NotificationOptions()),
                )
                try:
                    yield
                finally:
                    task_group.cancel_scope.cancel()
                    await anyio.to_thread.run_sync(backend.close)
                    logger.log("HTTP bridge stopped")

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Mount(settings.mount_path, app=mcp_asgi),
        ],
        lifespan=lifespan,
    )
    app.state.bridge_logger = logger
    app.state.bridge_settings = settings
    app.state.bridge_backend = backend
    return app


def serve(settings: BridgeSettings) -> None:
    app = create_app(settings)
    logger: BridgeLogger = app.state.bridge_logger

    def handle_signal(signum, _frame) -> None:
        logger.log(f"received signal {signum}")
        backend: HopperBackend = app.state.bridge_backend
        backend.close()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


def main() -> None:
    serve(BridgeSettings.from_env())


if __name__ == "__main__":
    main()
