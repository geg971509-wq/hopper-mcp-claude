# hopper-claude-mcp-http-bridge

Expose Hopper Disassembler's MCP server to Claude Code over streamable HTTP.

```
Claude Code  --streamable HTTP-->  this bridge  --stdio/NDJSON-->  HopperMCPServer
```

Hopper ships an MCP server (`HopperMCPServer`) that speaks a newline-delimited
JSON protocol over stdio. This bridge runs a small local HTTP MCP server, forwards
`tools/list` and `tools/call` to Hopper, and registers itself with Claude Code as
an HTTP MCP server. A launchd agent keeps it running across logins.

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
- Hopper with the bundled `HopperMCPServer` at
  `/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer`
  (override with `--hopper-path` or `HOPPER_MCP_SERVER_PATH`)
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

1. Adds this block to `~/.claude.json` under `mcpServers` (atomically, leaving the
   rest of the file untouched):

   ```json
   "hopper": { "type": "http", "url": "http://127.0.0.1:8765/mcp/" }
   ```

2. Writes and loads a launchd agent at
   `~/Library/LaunchAgents/io.github.hopper-claude-mcp-http-bridge.plist`.

Because Claude Code also writes to `~/.claude.json`, run `install` while Claude
Code is closed, then start it again.

Note: the default server name is `hopper`. If you already have a `hopper` entry
(for example a different Hopper MCP server), `install` replaces it. Use
`--server-name hopper-bridge` to keep both.

## Verify

```bash
curl http://127.0.0.1:8765/healthz          # -> ok
hopper-claude-mcp-http-bridge status
claude mcp list                             # hopper should be listed as http
```

## Commands

```bash
hopper-claude-mcp-http-bridge serve       # run the bridge in the foreground
hopper-claude-mcp-http-bridge install     # register + launchd agent
hopper-claude-mcp-http-bridge uninstall   # remove agent, unregister
hopper-claude-mcp-http-bridge status      # url + agent + health
```

Useful options: `--port 9876`, `--server-name hopper-bridge`, `--no-load-agent`
(write files only), `uninstall --keep-config` (drop the agent, keep the entry).

## Logs

```
~/.claude/logs/hopper-mcp-http-bridge.log
~/.claude/logs/hopper-mcp-http-bridge.log.stdout.log
~/.claude/logs/hopper-mcp-http-bridge.log.stderr.log
```

## Notes

- The bridge forwards Hopper's tools and keeps prompts and resources empty.
- It reads one JSON line per request from Hopper. If a Hopper build interleaves
  unsolicited notifications during a call, the read would desync; this has not been
  a problem in practice but is worth knowing.

## License

MIT — see [`LICENSE`](../LICENSE).
