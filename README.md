# hopper-mcp-claude

An improved Hopper Disassembler MCP setup for Claude Code, plus the reverse
engineering helpers that go with it.

The main tool is [`hopper-mcp-bridge/`](hopper-mcp-bridge/), a small local server
that puts Hopper's MCP interface behind a plain HTTP endpoint and wires it into
Claude Code. Hopper exposes an MCP server that speaks a newline-delimited JSON
protocol over stdio. Claude Code can drive that stdio server directly, but a
managed HTTP endpoint is easier to live with: it survives logins through a launchd
agent, it is shared across every project instead of per workspace, and it sidesteps
the stdio edge cases that show up in some setups.

```
Claude Code  --streamable HTTP-->  hopper-mcp-bridge  --stdio/NDJSON-->  HopperMCPServer
```

## Layout

```
hopper-mcp-bridge/     the main tool: Hopper MCP over HTTP, wired into Claude Code
references/            supporting material used during RE and de-obfuscation
  open_ipa.sh          unpack any iOS .ipa, triage every Mach-O, build IDA databases
  methodology/         an evidence-gated de-obfuscation workflow + prompts
```

### hopper-mcp-bridge (main tool)

Register Hopper's MCP server with Claude Code as an HTTP server and keep it running.
Full install and usage in [hopper-mcp-bridge/README.md](hopper-mcp-bridge/README.md).

```bash
cd hopper-mcp-bridge
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
hopper-claude-mcp-http-bridge install
# restart Claude Code
```

If Hopper's stdio server already works for you, you may not need the bridge at all:

```bash
claude mcp add hopper -- "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer"
```

The bridge is for when you want the HTTP endpoint, the launchd management, or a way
around stdio problems.

### references (helpers)

Not the product, just what supports the work:

- `references/open_ipa.sh` opens any iOS `.ipa` for analysis. It unpacks the
  archive, triages every Mach-O image (arch, encryption state, Swift/Obj-C
  metadata, base, size), and builds ready-to-open IDA databases headlessly. See
  the header of the script for options.
- `references/methodology/` documents an evidence-gated approach to naming the
  anonymous functions once a database is open, with a verification step that keeps
  the results honest. The `prompts/` implement it as inventory, one-unit loop, and
  an independent check.

## Requirements

- macOS
- Hopper **6.0+** with the bundled `HopperMCPServer` (for the bridge). The MCP
  server ships only with Hopper 6+; Hopper 4/5 have none. The bridge auto-detects
  the binary and errors clearly if it is missing.
- IDA Pro with `idat` (for `references/open_ipa.sh`)
- Claude Code, Python 3.10+

## License

MIT — see [`LICENSE`](LICENSE).
