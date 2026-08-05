#!/usr/bin/env python3
"""Prove `codex mcp-server` speaks MCP and offers the tools the bridge depends on.

`claude mcp list` runs a health check, which tells you the server started and responded.
This goes further: it performs the handshake itself (initialize -> notifications/initialized
-> tools/list), requires the two tools the bridge actually needs, and prints their real
parameter lists so you can check them against your own build rather than a README table.

Usage:
    python3 scripts/verify-codex-mcp.py
    python3 scripts/verify-codex-mcp.py --command "codex mcp-server" --timeout 60
    python3 scripts/verify-codex-mcp.py --expect codex            # relax the requirement

Exit codes:
    0  handshake succeeded and every expected tool was advertised
    1  the server ran but the handshake failed, or an expected tool was missing
    2  the server could not be started at all
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading

PROTOCOL_VERSION = "2025-03-26"
DEFAULT_EXPECTED = ("codex", "codex-reply")

E_OK, E_FAILED, E_NOT_STARTED = 0, 1, 2


class HandshakeError(RuntimeError):
    """The server started but did not complete a usable MCP handshake."""


class Server:
    """A stdio MCP server, killed as a process group so children cannot outlive it."""

    def __init__(self, command: list[str]) -> None:
        if not command:
            print("no command given: --command must not be empty", file=sys.stderr)
            raise SystemExit(E_NOT_STARTED)
        try:
            self.proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,  # so we can signal descendants too
            )
        except OSError as exc:
            print(
                f"could not start {command[0]!r}: {exc}\n"
                "is the Codex CLI installed and on PATH? try: npm install -g @openai/codex@latest",
                file=sys.stderr,
            )
            raise SystemExit(E_NOT_STARTED)

        # A server that logs heavily to stderr will block on a full pipe buffer and never
        # answer, so drain stderr continuously instead of reading it at the end.
        self._stderr: list[str] = []
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr[-15:]).strip() or "(empty)"

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise HandshakeError(f"server closed stdin ({exc}). stderr:\n{self.stderr_tail}")

    def read_response(self, want_id: int) -> dict:
        """Read frames until the response with `want_id` arrives."""
        assert self.proc.stdout is not None
        noise = 0
        while True:
            try:
                line = self.proc.stdout.readline()
            except OSError as exc:
                raise HandshakeError(f"could not read from server ({exc}). stderr:\n{self.stderr_tail}")
            if not line:
                raise HandshakeError(f"server closed the stream. stderr:\n{self.stderr_tail}")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Not fatal on its own — some builds print banners — but it is not MCP either,
                # so surface it rather than swallowing it silently.
                noise += 1
                if noise <= 3:
                    print(f"  warning: non-JSON line on stdout: {line[:120]}", file=sys.stderr)
                continue
            if message.get("id") == want_id:
                if "error" in message:
                    raise HandshakeError(f"server returned an error: {message['error']}")
                if "result" not in message:
                    raise HandshakeError(f"response {want_id} carried neither result nor error")
                return message

    def close(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                self.proc.kill()


def handshake(server: Server) -> list[dict]:
    server.send({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "verify-codex-mcp", "version": "2.0"},
        },
    })
    result = server.read_response(1)["result"]
    info = result.get("serverInfo", {})
    print(f"server:   {info.get('name', '?')} {info.get('version', '?')}")
    print(f"protocol: {result.get('protocolVersion', '?')} (requested {PROTOCOL_VERSION})")

    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    return server.read_response(2)["result"].get("tools", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="codex mcp-server",
                        help="command that starts the MCP server (default: %(default)s)")
    parser.add_argument("--expect", nargs="*", default=list(DEFAULT_EXPECTED),
                        metavar="TOOL", help="tool names that must be present "
                                             f"(default: {' '.join(DEFAULT_EXPECTED)})")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="give up after this many seconds (default: %(default)s)")
    args = parser.parse_args()

    server = Server(shlex.split(args.command))

    # A hung server is the common failure. The watchdog kills the whole process group,
    # which unblocks readline() even when a descendant is holding the pipe open.
    watchdog = threading.Timer(args.timeout, server.close)
    watchdog.daemon = True
    watchdog.start()

    try:
        tools = handshake(server)
    except HandshakeError as exc:
        print(f"\nhandshake failed: {exc}", file=sys.stderr)
        return E_FAILED
    finally:
        watchdog.cancel()
        server.close()

    advertised = {t.get("name", "") for t in tools}
    for tool in sorted(tools, key=lambda t: t.get("name", "")):
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required", []))
        params = sorted(schema.get("properties", {}))
        rendered = ", ".join(f"{p}*" if p in required else p for p in params)
        print(f"\ntool: {tool.get('name', '?')}")
        print(f"  params: {rendered or '(none)'}")

    missing = [name for name in args.expect if name not in advertised]
    if missing:
        print(
            f"\nMISSING: {', '.join(missing)}"
            f"\nthe server answered, but it is not offering the tools this bridge needs."
            f"\nadvertised instead: {', '.join(sorted(advertised)) or '(nothing)'}",
            file=sys.stderr,
        )
        return E_FAILED

    print(f"\n(* = required)  all expected tools present: {', '.join(args.expect)}")
    return E_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
