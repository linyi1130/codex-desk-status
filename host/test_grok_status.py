"""Grok usage reader and shared-token tests. Synthetic data only; the real
Grok Bot data directory is never touched."""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import unittest
from email.message import Message
from pathlib import Path

import grok_status
from codex_status import Handler, Service
from grok_status import GrokReader, read_grok, reset_text

NOW = 1_800_000_000.0
FAKE_ACCOUNT = "test-account-0000"


def blob_name(logical: str) -> str:
    return base64.b32encode(logical.encode()).decode().lower().rstrip("=") + ".blob"


def make_dir(root: Path, *, pid: int | None, signed_in: bool = True, percent: float | None = 7.2,
             reset_in: float = 36 * 3600, schema: int = 2) -> Path:
    support = root / "Grok Bot"
    persist = support / "sand-client-persistence"
    persist.mkdir(parents=True)
    if pid is not None:
        (support / "desktop-status.json").write_text(
            json.dumps({"version": 1, "pid": pid, "appVersion": "0", "startedAtMs": 1, "signedIn": signed_in}))
    if percent is not None:
        usage = {"percentUsed": percent, "nextResetMs": int((NOW + reset_in) * 1000), "grokPlanLabel": "X"}
        body = {"schemaVersion": schema, "value": {"kind": "present", "reading": {"usage": usage, "readAtMs": int(NOW * 1000)}}}
        (persist / blob_name(f"sand.client.slice.account.{FAKE_ACCOUNT}.weekly-usage.cache")).write_text(json.dumps(body))
    return support


def make_decoys(support: Path) -> list[Path]:
    """Files the reader must never open: unreadable, so an open() would fail."""
    persist = support / "sand-client-persistence"
    decoys = [
        persist / blob_name(f"sand.client.slice.account.{FAKE_ACCOUNT}.roster.last-roster"),
        persist / blob_name(f"sand.client.slice.account.{FAKE_ACCOUNT}.transcript.replicas.abc"),
        persist / blob_name(f"sand.client.slice.account.{FAKE_ACCOUNT}.send-journal"),
        support / "Cookies",
        support / "sand-secrets.json",
    ]
    for path in decoys:
        path.write_text("SENTINEL")
        path.chmod(0)
    return decoys


def restore(paths: list[Path]) -> None:
    for path in paths:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class GrokStatusTest(unittest.TestCase):
    def test_ok_and_never_opens_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid())
            decoys = make_decoys(support)
            try:
                out = read_grok(NOW, support)
            finally:
                restore(decoys)
        self.assertEqual(out["state"], "ok")
        self.assertEqual(out["weekly_percent"], 7.2)
        self.assertEqual(out["next_reset_unix"], int(NOW + 36 * 3600))
        self.assertIn("36", out["reset_text"])
        self.assertTrue(out["app_running"] and out["signed_in"])
        dumped = json.dumps(out)
        self.assertNotIn(FAKE_ACCOUNT, dumped)
        self.assertNotIn("blob", dumped)

    def test_closed_keeps_cached_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=2**22 + 12345)
            out = read_grok(NOW, support)
        self.assertEqual(out["state"], "closed")
        self.assertEqual(out["weekly_percent"], 7.2)
        self.assertFalse(out["app_running"])

    def test_missing_status_file_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=None)
            out = read_grok(NOW, support)
        self.assertEqual(out["state"], "closed")

    def test_unknown_schema_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid(), schema=99)
            out = read_grok(NOW, support)
        self.assertEqual(out["state"], "unavailable")
        self.assertIsNone(out["weekly_percent"])

    def test_missing_dir_and_garbage_are_unavailable(self) -> None:
        self.assertEqual(read_grok(NOW, Path("/nonexistent-grok-dir"))["state"], "unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid())
            (support / "desktop-status.json").write_text("{not json")
            self.assertEqual(read_grok(NOW, support)["state"], "unavailable")

    def test_not_signed_in_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid(), signed_in=False)
            self.assertEqual(read_grok(NOW, support)["state"], "unavailable")

    def test_reset_text_units_and_expiry(self) -> None:
        self.assertIn("20", reset_text(NOW + 19.5 * 3600, NOW))
        self.assertIn("3", reset_text(NOW + 72 * 3600, NOW))
        self.assertEqual(reset_text(NOW - 5, NOW), "")
        self.assertEqual(reset_text(None, NOW), "")
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid(), reset_in=-60)
            out = read_grok(NOW, support)
        self.assertTrue(out["usage_stale"])
        self.assertEqual(out["reset_text"], "")

    def test_reader_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            support = make_dir(Path(tmp), pid=os.getpid())
            reader = GrokReader(support, ttl=10.0)
            first = reader.get(NOW)
            usage_file = next((support / "sand-client-persistence").iterdir())
            usage_file.unlink()
            self.assertEqual(reader.get(NOW + 5)["weekly_percent"], first["weekly_percent"])
            self.assertIsNone(reader.get(NOW + 11)["weekly_percent"])


class TokenAuthTest(unittest.TestCase):
    def make_handler(self, token: str, client_ip: str, header: str | None) -> Handler:
        handler = object.__new__(Handler)
        handler.service = Service(Path("/nonexistent"), 0, token, GrokReader(Path("/nonexistent-grok-dir")))
        handler.client_address = (client_ip, 50000)
        headers = Message()
        if header is not None:
            headers["X-Status-Token"] = header
        handler.headers = headers
        return handler

    def test_open_when_unset(self) -> None:
        self.assertTrue(self.make_handler("", "192.168.50.20", None)._authorized())

    def test_lan_client_needs_token(self) -> None:
        self.assertFalse(self.make_handler("s3cret", "192.168.50.20", None)._authorized())
        self.assertFalse(self.make_handler("s3cret", "192.168.50.20", "wrong")._authorized())
        self.assertTrue(self.make_handler("s3cret", "192.168.50.20", "s3cret")._authorized())

    def test_loopback_allowed(self) -> None:
        self.assertTrue(self.make_handler("s3cret", "127.0.0.1", None)._authorized())

    def test_snapshot_has_grok_section(self) -> None:
        snap = self.make_handler("", "127.0.0.1", None).service.snapshot()
        self.assertEqual(snap["grok"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
