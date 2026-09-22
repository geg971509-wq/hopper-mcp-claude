<h1 align="center">hopper-mcp-claude</h1>

<p align="center">
  Drive <b>Hopper Disassembler</b> from <b>Claude Code</b> over a managed HTTP MCP
  endpoint — plus the iOS reverse-engineering helpers that go with it.
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-only-000000?logo=apple&logoColor=white">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Hopper 6.0+" src="https://img.shields.io/badge/Hopper-6.0%2B-E8681B">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-3DA639">
</p>

---

Hopper's built-in MCP server only speaks **stdio**, which is fiddly to drive
directly. This repo wraps it in a small **always-on HTTP bridge**, wired into Claude
Code and kept alive by launchd.

```mermaid
flowchart LR
    C["Claude Code"] ==>|"streamable HTTP"| B["hopper-mcp-bridge<br/>launchd · always-on"]
    B -->|"stdio / NDJSON"| H["HopperMCPServer<br/>Hopper 6+"]
```

## 📦 What's inside

| path | what it is |
|---|---|
| [`hopper-mcp-bridge/`](hopper-mcp-bridge/) | **the main tool** — Hopper MCP over HTTP, wired into Claude Code |
| [`references/open_ipa.sh`](references/open_ipa.sh) | unpack any iOS `.ipa`, triage every Mach-O, build IDA databases headlessly |
| [`references/methodology/`](references/methodology/) | evidence-gated de-obfuscation workflow + prompts |

## 🚀 Quick start

```bash
cd hopper-mcp-bridge
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
hopper-claude-mcp-http-bridge install     # then restart Claude Code
```

Full docs → **[hopper-mcp-bridge/README.md](hopper-mcp-bridge/README.md)**.

Already happy with raw stdio? Skip the bridge entirely. Hopper 6.0+ is required:

```bash
claude mcp add hopper -- "/Applications/Hopper Disassembler.app/Contents/MacOS/HopperMCPServer"
```

The local Claude plugin provides the same direct stdio server without a wrapper:

```bash
claude plugin marketplace add /absolute/path/to/hopper-mcp-claude
claude plugin install hopper-mcp@hopper-local
claude plugin uninstall hopper-mcp@hopper-local
```

The direct stdio plugin is separate from the existing HTTP bridge setup above; remote
marketplace publication is not done.

## ✅ Requirements

- **macOS** · **Claude Code** · **Python 3.10+**
- **Hopper 6.0+** — ships the `HopperMCPServer` binary (for the bridge and direct plugin)
- **IDA Pro** with `idat` — only for `references/open_ipa.sh`

## 💡 Why the bridge?

- **Reliable** — isolates Hopper's stdio quirks (interleaved notifications, GUI cold
  starts, long calls) in one place.
- **Always-on** — launchd keeps it running across logins.
- **Shared** — one endpoint for every project, not one Hopper per client.
- **Same tools** — pure proxy; every Hopper tool passes through unchanged.

## 📄 License

MIT — see [`LICENSE`](LICENSE).
