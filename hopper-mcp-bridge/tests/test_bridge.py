"""Regression tests using fake Hopper responses, never the installed service."""

import asyncio
from copy import deepcopy
import io
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from hopper_claude_mcp_bridge.bridge import BridgeSettings, HopperBackend, create_app


def tool(name, *keys):
    return {"name": name, "inputSchema": {
        "type": "object", "properties": {key: {"type": "string"} for key in keys}
    }}


TOOLS = [
    tool("current_document"),
    tool("list_segments", "document"),
    tool("current_address", "document"),
    tool("list_procedures", "document"),
    tool("procedure_assembly", "document", "procedure"),
    tool("procedure_pseudo_code", "document", "procedure"),
    tool("goto_address", "document", "address"),
    tool("native_both", "document", "procedure", "address"),
]


def text_result(text):
    return {"result": {"content": [{"type": "text", "text": text}]}}


class FakeBackend(HopperBackend):
    def __init__(self):
        super().__init__("unused", Mock())
        self.calls = []
        self.raw_tools = deepcopy(TOOLS)
        self.current = text_result("libwebuiopsercore")
        self.target = text_result("ok")

    def _ensure_proc_locked(self):
        pass

    def _request_locked(self, method, params):
        assert self.lock.locked(), "backend requests must hold the existing lock"
        self.calls.append((method, deepcopy(params)))
        if method == "tools/list":
            return {"result": {"tools": deepcopy(self.raw_tools)}}
        if params["name"] == "current_document":
            return deepcopy(self.current)
        return deepcopy(self.target)

    def tool_calls(self):
        return [params for method, params in self.calls if method == "tools/call"]


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()

    def test_omitted_document_is_fresh_and_explicit_is_preserved(self):
        for name in ("list_segments", "current_address", "list_procedures", "procedure_pseudo_code"):
            self.assertFalse(self.backend.call_tool(name, None).isError)
            self.assertEqual(self.backend.tool_calls()[-1]["arguments"], {"document": "libwebuiopsercore"})
        self.backend.current = text_result("second")
        self.backend.call_tool("list_segments", {})
        self.assertEqual(self.backend.tool_calls()[-1]["arguments"]["document"], "second")
        self.backend.calls.clear()
        args = {"document": " explicit "}
        self.backend.call_tool("list_segments", args)
        self.assertEqual(self.backend.tool_calls(), [{"name": "list_segments", "arguments": args}])
        self.assertEqual(args, {"document": " explicit "})

    def test_invalid_explicit_document_never_falls_back(self):
        for value in (None, "", "  ", 12):
            with self.subTest(value=value):
                self.backend.calls.clear()
                result = self.backend.call_tool("list_segments", {"document": value})
                self.assertTrue(result.isError)
                self.assertIn("nonblank document", result.content[0].text)
                self.assertEqual(self.backend.tool_calls(), [])

    def test_bad_current_document_stops_target(self):
        for response in (
            {"error": {"code": -1, "message": "failed"}}, {},
            {"result": {"content": []}}, text_result(""), text_result(" \n"),
            {"result": {"isError": True, "content": [{"type": "text", "text": "No document"}]}},
            {"result": {"content": [{"type": "image", "data": "", "mimeType": "image/png"}]}},
            {"result": {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}},
        ):
            with self.subTest(response=response):
                self.backend.current = response
                self.backend.calls.clear()
                result = self.backend.call_tool("list_segments", {})
                self.assertTrue(result.isError)
                self.assertIn("explicit document", result.content[0].text)
                self.assertEqual([call["name"] for call in self.backend.tool_calls()], ["current_document"])
        with patch.object(self.backend, "_request_locked", side_effect=RuntimeError("timeout")):
            self.assertTrue(self.backend.call_tool("list_segments", {}).isError)

    def test_alias_matching_conflicting_and_optional_procedure(self):
        for name in ("procedure_assembly", "procedure_pseudo_code"):
            for args in ({"address": "0x100008"}, {"address": "0x100008", "procedure": "0x100008"}):
                original = deepcopy(args)
                self.assertFalse(self.backend.call_tool(name, args).isError)
                self.assertEqual(self.backend.tool_calls()[-1]["arguments"], {
                    "document": "libwebuiopsercore", "procedure": "0x100008"})
                self.assertEqual(args, original)
            self.backend.calls.clear()
            self.assertTrue(self.backend.call_tool(name, {"address": "a", "procedure": "b"}).isError)
            self.assertEqual(self.backend.tool_calls(), [])
            self.assertFalse(self.backend.call_tool(name, {}).isError)
            self.assertNotIn("procedure", self.backend.tool_calls()[-1]["arguments"])

    def test_native_address_and_unsupported_arguments(self):
        for name, args in (("goto_address", {"address": "0x100008"}),
                           ("native_both", {"address": "a", "procedure": "b"})):
            self.assertFalse(self.backend.call_tool(name, args).isError)
            self.assertEqual(self.backend.tool_calls()[-1]["arguments"], dict(args, document="libwebuiopsercore"))
        for name, args in (("list_procedures", {"limit": 5}), ("list_segments", {"address": "a"}),
                           ("current_document", {"document": "x"}), ("goto_address", {"procedure": "a"})):
            self.backend.calls.clear()
            result = self.backend.call_tool(name, args)
            self.assertTrue(result.isError)
            self.assertIn("Accepted keys", result.content[0].text)
            self.assertIn("search_procedures", result.content[0].text)
            self.assertEqual(self.backend.tool_calls(), [])
        self.assertTrue(self.backend.call_tool("missing", {}).isError)
        self.assertTrue(self.backend.call_tool("goto_address", {"address": 123}).isError)

    def test_schema_copies_cache_and_notification_invalidation(self):
        public = self.backend.list_tools()
        self.assertEqual([entry.model_dump(exclude_unset=True) for entry in self.backend.tools_cache], TOOLS)
        assembly = next(entry for entry in public if entry.name == "procedure_assembly")
        self.assertIn("address", assembly.inputSchema["properties"])
        self.assertFalse(assembly.inputSchema["additionalProperties"])
        self.assertIn("current_document", assembly.description)
        self.assertIn("search_procedures", assembly.description)
        assembly.inputSchema["properties"].clear()
        self.assertIn("procedure", self.backend.list_tools()[4].inputSchema["properties"])
        self.assertEqual(sum(method == "tools/list" for method, _ in self.backend.calls), 1)
        proc = Mock(stdout=io.StringIO('{"method":"notifications/tools/list_changed"}\n'))
        self.backend._reader_loop(proc, queue.Queue())
        self.assertIsNone(self.backend.tools_cache)
        self.backend.raw_tools[4]["inputSchema"]["properties"]["new_key"] = {"type": "string"}
        self.assertIn("new_key", self.backend.list_tools()[4].inputSchema["properties"])
        self.assertEqual(sum(method == "tools/list" for method, _ in self.backend.calls), 2)

    def test_restart_invalidates_cache(self):
        backend = HopperBackend("unused", Mock())
        backend.tools_cache = []
        with patch("hopper_claude_mcp_bridge.bridge.os.path.exists", return_value=False):
            with self.assertRaises(RuntimeError):
                backend.list_tools()
        self.assertIsNone(backend.tools_cache)

    def test_no_recursive_lock(self):
        result = queue.Queue()
        thread = threading.Thread(target=lambda: result.put(self.backend.call_tool("procedure_assembly", {"address": "0x100008"})), daemon=True)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "document resolution recursively acquired the backend lock")
        self.assertFalse(result.get_nowait().isError)

    def test_target_errors_preserved(self):
        for response in ({"error": {"code": 42, "message": "bad target"}},
                         {"result": {"isError": True, "content": [{"type": "text", "text": "bad target"}]}}):
            self.backend.target = response
            result = self.backend.call_tool("list_segments", {"document": "explicit"})
            self.assertTrue(result.isError)
            self.assertIn("bad target", result.content[0].text)


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_app_sdk_validation_and_forwarding(self):
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen()
            settings = BridgeSettings(port=port, log_path=Path(directory) / "bridge.log")
            with patch("hopper_claude_mcp_bridge.bridge.HopperBackend", return_value=backend):
                app = create_app(settings)
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if task.done():
                            await task
                            self.fail("HTTP server stopped before startup")
                        await asyncio.sleep(0.01)
                    async with streamable_http_client(settings.mcp_url) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            schema = next(tool.inputSchema for tool in tools.tools if tool.name == "procedure_assembly")
                            self.assertIn("address", schema["properties"])
                            self.assertFalse(schema["additionalProperties"])
                            for name, args, expected in (
                                ("procedure_assembly", {"address": "0x100008"}, {"procedure": "0x100008", "document": "libwebuiopsercore"}),
                                ("procedure_pseudo_code", {}, {"document": "libwebuiopsercore"}),
                                ("goto_address", {"address": "0x100008", "document": "explicit"}, {"address": "0x100008", "document": "explicit"}),
                            ):
                                result = await session.call_tool(name, args)
                                self.assertFalse(result.isError, result)
                                self.assertEqual(backend.tool_calls()[-1]["arguments"], expected)
                            for name, args in (
                                ("list_procedures", {"limit": 5}),
                                ("list_segments", {"document": None}),
                                ("list_segments", {"document": "  "}),
                                ("goto_address", {"address": 123}),
                            ):
                                backend.calls.clear()
                                with patch.object(backend, "call_tool", wraps=backend.call_tool) as handler:
                                    result = await session.call_tool(name, args)
                                    self.assertTrue(result.isError)
                                    self.assertIn("Input validation error", result.content[0].text)
                                    handler.assert_not_called()
                                self.assertEqual(backend.tool_calls(), [])
                            backend.calls.clear()
                            result = await session.call_tool("procedure_assembly", {"address": "a", "procedure": "b"})
                            self.assertTrue(result.isError)
                            self.assertEqual(backend.tool_calls(), [])
                            backend.current = text_result("")
                            result = await session.call_tool("list_segments", {})
                            self.assertTrue(result.isError)
                            self.assertIn("explicit document", result.content[0].text)
                            self.assertEqual([call["name"] for call in backend.tool_calls()], ["current_document"])
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)


if __name__ == "__main__":
    unittest.main()
