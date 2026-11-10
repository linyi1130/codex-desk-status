"""Read-only Grok Bot usage and app state for the desk display.

Reads exactly two things from the Grok Bot desktop app's data directory:

* ``desktop-status.json``: pid and signed-in flag, used to tell whether the
  app is running.
* the ``*weekly-usage.cache`` blob inside ``sand-client-persistence``: the
  weekly usage percentage and the next reset time.

Nothing else is ever opened. In particular the roster, the per-bot transcript
replicas, drafts, the send journal, cookies and every ``*secrets*`` file are
never read. Persistence file names embed an account id, so names are only
decoded in memory to find the usage blob; they are never logged or returned.

The persistence format is private to the app. Any surprise (missing file,
unknown ``schemaVersion``, unexpected shape) yields ``state: "unavailable"``
instead of an exception.
"""

from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from pathlib import Path

DEFAULT_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Grok Bot"
STATUS_FILE = "desktop-status.json"
PERSIST_DIR = "sand-client-persistence"
USAGE_SUFFIX = ".weekly-usage.cache"
# Observed on Grok Bot 0.79.3. Anything else is treated as unavailable.
KNOWN_SCHEMA_VERSIONS = {2}
MAX_BLOB_BYTES = 64 * 1024
CACHE_SECONDS = 10.0


def _empty(state: str, now: float) -> dict:
    return {
        "state": state,
        "app_running": False,
        "signed_in": False,
        "weekly_percent": None,
        "next_reset_unix": None,
        "reset_text": "",
        "read_at_unix": None,
        "usage_stale": False,
        "checked_at_unix": int(now),
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _decode_name(filename: str) -> str:
    stem = filename[: -len(".blob")].upper()
    stem += "=" * (-len(stem) % 8)
    return base64.b32decode(stem).decode("utf-8", "replace")


def _find_usage_blob(persist: Path) -> Path | None:
    best: Path | None = None
    best_mtime = -1.0
    for entry in os.scandir(persist):
        if not entry.name.endswith(".blob") or not entry.is_file():
            continue
        try:
            name = _decode_name(entry.name)
        except Exception:
            continue
        if not name.endswith(USAGE_SUFFIX):
            continue
        mtime = entry.stat().st_mtime
        if mtime > best_mtime:
            best, best_mtime = Path(entry.path), mtime
    return best


def reset_text(next_reset_unix: float | None, now: float) -> str:
    if next_reset_unix is None:
        return ""
    remaining = next_reset_unix - now
    if remaining <= 0:
        return ""
    hours = remaining / 3600.0
    if hours < 48:
        return f"约 {max(1, math.ceil(hours))} 小时后重置"
    return f"约 {int(round(hours / 24.0))} 天后重置"


def _read_usage(persist: Path, now: float) -> dict | None:
    blob = _find_usage_blob(persist)
    if blob is None:
        return None
    if blob.stat().st_size > MAX_BLOB_BYTES:
        return None
    data = json.loads(blob.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") not in KNOWN_SCHEMA_VERSIONS:
        return None
    value = data.get("value")
    if isinstance(value, dict) and value.get("kind", "present") != "present":
        return None
    reading = value.get("reading") if isinstance(value, dict) else None
    usage = reading.get("usage") if isinstance(reading, dict) else None
    if not isinstance(usage, dict):
        return None
    percent = usage.get("percentUsed")
    reset_ms = usage.get("nextResetMs")
    read_ms = reading.get("readAtMs")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    percent = max(0.0, float(percent))
    next_reset = float(reset_ms) / 1000.0 if isinstance(reset_ms, (int, float)) and not isinstance(reset_ms, bool) else None
    read_at = float(read_ms) / 1000.0 if isinstance(read_ms, (int, float)) and not isinstance(read_ms, bool) else None
    stale = next_reset is not None and next_reset <= now
    return {
        "weekly_percent": round(percent, 1),
        "next_reset_unix": int(next_reset) if next_reset is not None else None,
        "reset_text": reset_text(next_reset, now),
        "read_at_unix": int(read_at) if read_at is not None else None,
        "usage_stale": stale,
    }


def read_grok(now: float | None = None, support_dir: Path | None = None) -> dict:
    """Return the ``grok`` section. Never raises."""
    now = time.time() if now is None else now
    out = _empty("unavailable", now)
    try:
        support = Path(support_dir) if support_dir is not None else DEFAULT_SUPPORT_DIR
        if not support.is_dir():
            return out
        running = False
        signed_in = False
        status_path = support / STATUS_FILE
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(status, dict):
                pid = status.get("pid")
                running = isinstance(pid, int) and not isinstance(pid, bool) and _pid_alive(pid)
                signed_in = bool(status.get("signedIn")) if running else False
        out["app_running"] = running
        out["signed_in"] = signed_in
        persist = support / PERSIST_DIR
        usage = _read_usage(persist, now) if persist.is_dir() else None
        if usage:
            out.update(usage)
        if running and signed_in and usage:
            out["state"] = "ok"
        elif not running:
            out["state"] = "closed"
        else:
            out["state"] = "unavailable"
        return out
    except Exception:
        return _empty("unavailable", now)


class GrokReader:
    """Thread-safe reader with a short cache so /status polls stay cheap."""

    def __init__(self, support_dir: Path | None = None, ttl: float = CACHE_SECONDS) -> None:
        self.support_dir = support_dir
        self.ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._value: dict | None = None

    def get(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            if self._value is None or now - self._at >= self.ttl or now < self._at:
                self._value = read_grok(now, self.support_dir)
                self._at = now
            return dict(self._value)
