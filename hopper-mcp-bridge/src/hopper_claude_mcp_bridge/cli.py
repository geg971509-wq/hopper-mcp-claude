from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bridge import (
    DEFAULT_HOST,
    DEFAULT_LOG_PATH,
    DEFAULT_MOUNT_PATH,
    DEFAULT_PORT,
    DEFAULT_TOOL_TIMEOUT_SEC,
    BridgeSettings,
    find_hopper_server,
    serve,
)
from .install import (
    DEFAULT_LABEL,
    DEFAULT_SERVER_NAME,
    InstallSettings,
    install,
    status,
    uninstall,
)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hopper-claude-mcp-http-bridge",
        description="Bridge Hopper's MCP server to Claude Code over streamable HTTP.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the HTTP bridge.")
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--path", default=DEFAULT_MOUNT_PATH)
    serve_parser.add_argument(
        "--hopper-path",
        default=None,
        help="Path to HopperMCPServer. Default: auto-detect installed Hopper 6+.",
    )
    serve_parser.add_argument(
        "--tool-timeout-sec",
        type=int,
        default=DEFAULT_TOOL_TIMEOUT_SEC,
        help="Backend read timeout for a single tool call (seconds).",
    )
    serve_parser.add_argument("--log-path", type=_path, default=DEFAULT_LOG_PATH)

    install_parser = subparsers.add_parser(
        "install",
        help="Register the bridge with Claude Code and install the launchd agent.",
    )
    install_parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    install_parser.add_argument("--label", default=DEFAULT_LABEL)
    install_parser.add_argument("--host", default=DEFAULT_HOST)
    install_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    install_parser.add_argument("--path", default=DEFAULT_MOUNT_PATH)
    install_parser.add_argument(
        "--hopper-path",
        default=None,
        help="Path to HopperMCPServer. Default: auto-detect installed Hopper 6+.",
    )
    install_parser.add_argument(
        "--tool-timeout-sec",
        type=int,
        default=DEFAULT_TOOL_TIMEOUT_SEC,
        help="Per-tool timeout written to Claude Code config and used by the bridge.",
    )
    install_parser.add_argument(
        "--claude-config",
        type=_path,
        default=Path("~/.claude.json").expanduser(),
    )
    install_parser.add_argument(
        "--launch-agent",
        type=_path,
        default=Path(f"~/Library/LaunchAgents/{DEFAULT_LABEL}.plist").expanduser(),
    )
    install_parser.add_argument("--log-path", type=_path, default=DEFAULT_LOG_PATH)
    install_parser.add_argument(
        "--no-load-agent",
        action="store_true",
        help="Only write files; do not call launchctl.",
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove the launchd agent and optionally the Claude Code entry.",
    )
    uninstall_parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    uninstall_parser.add_argument("--label", default=DEFAULT_LABEL)
    uninstall_parser.add_argument(
        "--claude-config",
        type=_path,
        default=Path("~/.claude.json").expanduser(),
    )
    uninstall_parser.add_argument(
        "--launch-agent",
        type=_path,
        default=Path(f"~/Library/LaunchAgents/{DEFAULT_LABEL}.plist").expanduser(),
    )
    uninstall_parser.add_argument("--log-path", type=_path, default=DEFAULT_LOG_PATH)
    uninstall_parser.add_argument(
        "--keep-config",
        action="store_true",
        help="Leave the hopper entry in ~/.claude.json.",
    )
    uninstall_parser.add_argument(
        "--no-unload-agent",
        action="store_true",
        help="Only remove files; do not call launchctl.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show the configured URL, launch agent path, and health state.",
    )
    status_parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    status_parser.add_argument("--label", default=DEFAULT_LABEL)
    status_parser.add_argument("--host", default=DEFAULT_HOST)
    status_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    status_parser.add_argument("--path", default=DEFAULT_MOUNT_PATH)
    status_parser.add_argument("--hopper-path", default=None)
    status_parser.add_argument(
        "--tool-timeout-sec", type=int, default=DEFAULT_TOOL_TIMEOUT_SEC
    )
    status_parser.add_argument(
        "--claude-config",
        type=_path,
        default=Path("~/.claude.json").expanduser(),
    )
    status_parser.add_argument(
        "--launch-agent",
        type=_path,
        default=Path(f"~/Library/LaunchAgents/{DEFAULT_LABEL}.plist").expanduser(),
    )
    status_parser.add_argument("--log-path", type=_path, default=DEFAULT_LOG_PATH)

    return parser


def _install_settings(args: argparse.Namespace) -> InstallSettings:
    return InstallSettings(
        server_name=args.server_name,
        label=args.label,
        host=getattr(args, "host", DEFAULT_HOST),
        port=getattr(args, "port", DEFAULT_PORT),
        mount_path=getattr(args, "path", DEFAULT_MOUNT_PATH),
        tool_timeout_sec=getattr(args, "tool_timeout_sec", DEFAULT_TOOL_TIMEOUT_SEC),
        hopper_server_path=getattr(args, "hopper_path", None),
        claude_config_path=args.claude_config,
        launch_agent_path=args.launch_agent,
        log_path=args.log_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve(
            BridgeSettings(
                host=args.host,
                port=args.port,
                mount_path=args.path,
                hopper_server_path=find_hopper_server(args.hopper_path),
                tool_timeout_sec=args.tool_timeout_sec,
                log_path=args.log_path,
            )
        )
        return 0

    if args.command == "install":
        settings = _install_settings(args)
        warnings = install(settings, load_agent=not args.no_load_agent)
        print(f"Installed Hopper bridge at {settings.url}")
        print(f"Registered '{settings.server_name}' in {settings.claude_config_path}")
        print(f"Hopper server: {settings.hopper_server_path}")
        print(f"Per-tool timeout: {settings.tool_timeout_sec}s")
        print(f"LaunchAgent: {settings.launch_agent_path}")
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        print("Restart Claude Code to pick up the new MCP server.")
        return 0

    if args.command == "uninstall":
        settings = _install_settings(args)
        uninstall(
            settings,
            remove_config=not args.keep_config,
            unload_agent=not args.no_unload_agent,
        )
        print("Removed Hopper bridge launch agent.")
        if not args.keep_config:
            print(f"Removed '{settings.server_name}' from {settings.claude_config_path}")
        return 0

    if args.command == "status":
        settings = _install_settings(args)
        data = status(settings)
        for key, value in data.items():
            print(f"{key}: {value}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
