# hopper-claude-mcp-http-bridge

Expose Hopper Disassembler's MCP server to Claude Code over streamable HTTP.

```
Claude Code  --streamable HTTP-->  this bridge  --stdio/NDJSON-->  HopperMCPServer
```

Hopper ships an MCP server (`HopperMCPServer`) that speaks a newline-delimited
JSON protocol over stdio. This bridge runs a small local HTTP MCP server, forwards
`tools/list`/`tools/call` (and, transparently, `prompts`/`resources` when Hopper
offers them), and registers itself with Claude Code as an HTTP MCP server. A launchd
agent keeps it running across logins.

## What this is (vs. Hopper's built-in MCP)

Hopper 6.0+ has a **built-in** MCP server, `HopperMCPServer`, that speaks MCP over
**stdio** (newline-delimited JSON) and does all the real work — every tool
(disassemble, decompile, xrefs, comments, …) is Hopper's own. A client can talk to
it directly, no bridge involved:

```
Claude Code  --stdio/NDJSON-->  HopperMCPServer      # standard, built-in
```

This project does **not** replace that server or add any tools. It is a
transport-translating proxy: one long-lived local process that is an MCP **server**
to Claude (over HTTP) and an MCP **client** to Hopper (over stdio), forwarding
`tools/list` / `tools/call` between the two (the HTTP diagram at the top).

So the only differences from the built-in Hopper MCP are **transport** and **process
ownership** — the tools you get are identical:

| | Built-in Hopper MCP (direct) | Through this bridge |
|---|------------------------------|---------------------|
| Tools | Hopper's own | the same, forwarded unchanged |
| Transport to the client | stdio / NDJSON | streamable HTTP |
| Who launches Hopper | the client, per instance | the bridge, once |
| Lifetime / scope | while the client runs | always-on (launchd), shared across projects |

Why bother: Hopper's bundled server does not behave like a normal framed stdio MCP
server in every client setup (it is a GUI app, and the stdio channel can misframe,
buffer, or interleave notifications). The bridge keeps that stdio messiness in one
place and hands the client a plain HTTP endpoint it handles cleanly. If direct stdio
already works for you, you do not need the bridge — see below.

## When you actually need this

Claude Code talks to stdio MCP servers natively, so if Hopper's stdio server works
directly for you, the simplest setup is no bridge at all:

```bash
claude mcp add hopper -- "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer"
```

Use this bridge when the stdio transport misbehaves in your setup and you want a
plain HTTP endpoint instead, or when you want the server managed by launchd and
shared across projects.

## Requirements

- macOS
- **Hopper 6.0 or newer.** The bundled `HopperMCPServer` binary only ships with
  Hopper 6+ — Hopper 4 and 5 have no MCP server and cannot be bridged. The bridge
  auto-detects the binary across common bundle names/locations; override with
  `--hopper-path` or `HOPPER_MCP_SERVER_PATH` if yours lives elsewhere.
- Claude Code
- Python 3.10+

## Install

```bash
cd hopper-mcp-bridge
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
hopper-claude-mcp-http-bridge install
```

Then restart Claude Code so it re-reads its config.

`install` does two things:

1. Adds this block to `~/.claude.json` under `mcpServers` (atomically, under an
   advisory lock, leaving the rest of the file untouched):

   ```json
   "hopper": {
     "type": "http",
     "url": "http://127.0.0.1:8765/mcp/",
     "timeout": 300000
   }
   ```

   The `timeout` (milliseconds) matters: Claude Code's default per-tool timeout is
   ~60s, and reverse-engineering calls (decompiling a large function, analyzing a big
   Mach-O) routinely exceed that. Tune it with `--tool-timeout-sec` (default 300).

2. Writes and loads a launchd agent at
   `~/Library/LaunchAgents/io.github.hopper-claude-mcp-http-bridge.plist`. The agent
   bakes in the resolved Hopper path and the tool timeout.

Because Claude Code also writes to `~/.claude.json`, run `install` while Claude
Code is closed, then start it again. (The advisory lock only guards against other
bridge processes — Claude Code itself does not take it.)

If Claude Code ignores the per-server `timeout` in your build (there are open bugs
around HTTP/SSE timeout handling), set a global fallback in `~/.claude/settings.json`:

```json
{ "env": { "MCP_TOOL_TIMEOUT": "300000" } }
```

Note: the default server name is `hopper`. If you already have a `hopper` entry,
`install` replaces it. Use `--server-name hopper-bridge` to keep both.

## Verify

```bash
curl http://127.0.0.1:8765/healthz          # -> ok           (bridge process alive)
curl http://127.0.0.1:8765/readyz           # -> JSON: backend state + Hopper path
hopper-claude-mcp-http-bridge status        # url, timeout, bridge_health, backend
claude mcp list                             # hopper should be listed as http
```

`status` reports `bridge_health` (is the HTTP bridge up) separately from `backend`
(is the Hopper subprocess running / is the binary present). A `backend:
missing-hopper-binary` line means you need Hopper 6+.

## Commands

```bash
hopper-claude-mcp-http-bridge serve       # run the bridge in the foreground
hopper-claude-mcp-http-bridge install     # register + launchd agent
hopper-claude-mcp-http-bridge uninstall   # remove agent, unregister
hopper-claude-mcp-http-bridge status      # url + agent + health
```

Useful options: `--port 9876`, `--server-name hopper-bridge`, `--tool-timeout-sec 600`,
`--hopper-path /path/to/HopperMCPServer`, `--no-load-agent` (write files only),
`uninstall --keep-config` (drop the agent, keep the entry).

## Logs

```
~/.claude/logs/hopper-mcp-http-bridge.log            # bridge + captured Hopper stderr
~/.claude/logs/hopper-mcp-http-bridge.log.stdout.log
~/.claude/logs/hopper-mcp-http-bridge.log.stderr.log
```

## Notes

- The bridge forwards Hopper's tools, and forwards prompts/resources transparently
  when Hopper is already running and implements them (it will not start Hopper just
  to answer a startup `prompts/list`).
- A dedicated reader thread parses Hopper's output, so a request waits for its own
  id and skips interleaved notifications instead of mistaking one for the response.
  Each call is bounded by the tool timeout; a wedged Hopper call is terminated and
  restarted rather than pinning the bridge.

### On the `mcp` dependency pin

`mcp` is pinned to `>=1.27,<2` on purpose. This bridge is built on the **low-level
`mcp.server.lowlevel.Server` API** and its decorators (`@server.list_tools()`,
`@server.call_tool()`, `@server.list_prompts()`, `@server.list_resources()`).

That API changed in **mcp 2.0**: those decorators were removed/reworked, so an
unpinned `mcp>=1.27.0` install now resolves to 2.x and the bridge fails at startup
with `AttributeError: 'Server' object has no attribute 'list_prompts'`. The upper
bound keeps installs on the 1.x line the code targets. Moving to mcp 2.x is a
separate migration (adapt the low-level handlers to the new API), not a version bump.

## License

MIT — see [`LICENSE`](../LICENSE).
