#!/usr/bin/env python3
"""Tell the desk sidecar that a Codex turn moved, without blocking the turn.

Codex passes one JSON object on stdin. This script forwards only the event
name, tool name, cwd, and ids to 127.0.0.1. It drops prompts and tool
arguments. It always prints {"continue": true} and exits 0.

Point the command at this file from ~/.codex/hooks.json. Do not edit
~/Developer/TokenEconomizer; plugin hooks are loaded in addition to the
user hooks file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    body = {
        "hook_event_name": event.get("hook_event_name"),
        "tool_name": event.get("tool_name"),
        "cwd": event.get("cwd") if isinstance(event.get("cwd"), str) else None,
        "session_id": event.get("session_id") if isinstance(event.get("session_id"), str) else None,
        "turn_id": event.get("turn_id") if isinstance(event.get("turn_id"), str) else None,
    }
    port = os.environ.get("CODEX_STATUS_PORT", "8787")
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.4) as response:
            response.read()
    except Exception:
        pass
    sys.stdout.write('{"continue": true}\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
