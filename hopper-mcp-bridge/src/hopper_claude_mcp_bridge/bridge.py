"""HTTP <-> stdio bridge that exposes Hopper's MCP server over streamable HTTP.

Data flow:

    Claude Code  --streamable HTTP-->  this bridge  --stdio/NDJSON-->  HopperMCPServer

The bridge itself is client-agnostic: it speaks the streamable-HTTP MCP transport on
one side and Hopper's native newline-delimited JSON-RPC on the other. Only the install
layer knows about Claude Code.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route


DEFAULT_HOPPER_SERVER_PATH = (
    "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MOUNT_PATH = "/mcp"
DEFAULT_LOG_PATH = Path("~/.claude/logs/hopper-mcp-http-bridge.log").expanduser()


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
            hopper_server_path=os.environ.get(
                "HOPPER_MCP_SERVER_PATH",
                DEFAULT_HOPPER_SERVER_PATH,
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
    """Owns the HopperMCPServer subprocess and talks to it over newline-delimited JSON."""

    def __init__(self, server_path: str, logger: BridgeLogger) -> None:
        self.server_path = server_path
        self.logger = logger
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.next_id = count(1)
        self.tools_cache: list[types.Tool] | None = None

    def close(self) -> None:
        with self.lock:
            self._terminate_locked()

    def list_tools(self) -> list[types.Tool]:
        with self.lock:
            self._ensure_proc_locked()
            if self.tools_cache is None:
                response = self._send_locked(
                    {
                        "jsonrpc": "2.0",
                        "id": next(self.next_id),
                        "method": "tools/list",
                        "params": {},
                    }
                )
                self.tools_cache = [
                    types.Tool.model_validate(tool)
                    for tool in response["result"]["tools"]
                ]
                self.logger.log(f"cached {len(self.tools_cache)} Hopper tools")
            return list(self.tools_cache)

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        with self.lock:
            self._ensure_proc_locked()
            response = self._send_locked(
                {
                    "jsonrpc": "2.0",
                    "id": next(self.next_id),
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": arguments or {},
                    },
                }
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

    def _ensure_proc_locked(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return

        self._terminate_locked()
        self.tools_cache = None
        self.logger.log(f"starting Hopper backend path={self.server_path}")
        self.proc = subprocess.Popen(
            [self.server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        response = self._send_locked(
            {
                "jsonrpc": "2.0",
                "id": next(self.next_id),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "hopper-claude-mcp-http-bridge",
                        "version": "0.1.0",
                    },
                },
            }
        )
        negotiated = response.get("result", {}).get("protocolVersion")
        self.logger.log(f"Hopper backend initialized protocol={negotiated}")

    def _send_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
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

        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("Hopper backend closed stdout")

        response = json.loads(line)
        if "id" in response:
            self.logger.log(f"backend -> response id={response.get('id')}")
        elif "method" in response:
            self.logger.log(f"backend -> notification method={response.get('method')}")
        return response

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
    backend = HopperBackend(settings.hopper_server_path, logger)
    server = Server("hopper", version="0.1.0")
    transport = StreamableHTTPServerTransport(mcp_session_id=None)

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return []

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        return []

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return []

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

    async def mcp_asgi(scope, receive, send) -> None:
        await transport.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app):
        logger.log(f"HTTP bridge starting on {settings.mcp_url}")
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
