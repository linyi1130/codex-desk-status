"""Parser, quota merge, and HTTP tests. No network and no ~/.codex access."""

from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path

import codex_status
import parse_session
from codex_status import Engine, Handler, Service, client_is_local
from parse_session import HookHint, SessionFollower, hint_from_hook, parse_jsonl


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
NOW = datetime(2026, 11, 5, 0, 27, 12, tzinfo=timezone.utc).timestamp()
TZ = 8 * 3600


def load(name: str) -> dict:
    return codex_status.payload_from_fixture(FIXTURES / name, NOW, TZ)


class RunningFixtureTest(unittest.TestCase):
    def test_mockup_numbers(self) -> None:
        payload = load("running.jsonl")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["detail"], "跑命令")
        self.assertEqual(payload["label"], "执行中 · 跑命令")
        self.assertEqual(payload["color"], "orange")
        self.assertEqual(payload["project_name"], "NiceShot")
        self.assertEqual(payload["plan_name"], "Pro")
        self.assertEqual(payload["client"], "cli")
        self.assertEqual(payload["weekly_used_percent"], 16.0)
        self.assertEqual(payload["weekly_reset_text"], "约 6 天后重置")
        self.assertEqual(payload["weekly_window_minutes"], 10080)
        self.assertEqual(payload["five_hour_used_percent"], 41.0)
        self.assertEqual(payload["five_hour_reset_text"], "约 2 小时后重置")
        self.assertEqual(payload["five_hour_window_minutes"], 300)
        self.assertEqual(payload["context_used_tokens"], 349000)
        self.assertEqual(payload["context_limit_tokens"], 475000)
        self.assertEqual(payload["context_text"], "349k / 475k tokens")
        self.assertAlmostEqual(payload["context_used_percent"], 73.47, places=2)
        self.assertEqual(payload["turn_elapsed_seconds"], 252)
        self.assertEqual(payload["turn_elapsed_text"], "4分12秒")
        self.assertEqual(payload["clock"], "08:27")
        self.assertEqual(payload["updated_at"], "2026-11-05T08:27:12+08:00")
        self.assertEqual(payload["footer"], "NiceShot · 本轮已运行 4分12秒")
        self.assertFalse(payload["rate_limit_reached"])
        self.assertFalse(payload["stale"])
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-test-secret-do-not-leak", blob)
        self.assertNotIn("npm", blob)
        sample = json.loads((FIXTURES / "sample_status.json").read_text(encoding="utf-8"))
        self.assertEqual(payload, sample)

    def test_null_rate_limits_do_not_wipe_quota_or_use_session_total(self) -> None:
        view = parse_jsonl((FIXTURES / "running.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(view.context_used, 349000)
        self.assertEqual(view.weekly.used_percent, 16.0)
        self.assertEqual(view.plan_raw, "pro")


class OtherStateTest(unittest.TestCase):
    def test_complete(self) -> None:
        payload = load("complete.jsonl")
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["label"], "已完成")
        self.assertEqual(payload["color"], "green")
        self.assertEqual(payload["plan_name"], "Plus")
        self.assertEqual(payload["weekly_used_percent"], 12)
        self.assertEqual(payload["context_used_tokens"], 12000)
        self.assertEqual(payload["context_text"], "12k / 475k tokens")
        self.assertEqual(payload["turn_elapsed_text"], "1分30秒")
        self.assertNotIn("must not leak", json.dumps(payload))

    def test_interrupted_elapsed_is_frozen(self) -> None:
        payload = load("interrupted.jsonl")
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["label"], "已中断")
        self.assertEqual(payload["color"], "red")
        self.assertEqual(payload["turn_elapsed_seconds"], 70)
        self.assertEqual(payload["turn_elapsed_text"], "1分10秒")
        self.assertEqual(payload["weekly_window_minutes"], 10079)

    def test_high_quota_overrides_running_color(self) -> None:
        payload = load("high_quota.jsonl")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["detail"], "思考中")
        self.assertEqual(payload["label"], "额度将尽 · 思考中")
        self.assertEqual(payload["color"], "red")
        self.assertEqual(payload["weekly_used_percent"], 96)
        self.assertTrue(payload["rate_limit_reached"])
        self.assertGreater(payload["context_used_percent"], 90)

    def test_weekly_only_pro_window_in_primary_slot(self) -> None:
        payload = load("weekly_only.jsonl")
        self.assertEqual(payload["plan_name"], "Pro")
        self.assertEqual(payload["weekly_used_percent"], 16)
        self.assertEqual(payload["weekly_window_minutes"], 10080)
        self.assertIsNone(payload["five_hour_used_percent"])
        self.assertIsNone(payload["five_hour_window_minutes"])
        self.assertEqual(payload["context_used_tokens"], 18193)
        self.assertEqual(payload["context_limit_tokens"], 258400)
        self.assertEqual(payload["context_text"], "18.2k / 258k tokens")
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["client"], "chatgpt")

    def test_camel_case_and_relative_reset(self) -> None:
        camel = load("camel_case.jsonl")
        self.assertEqual(camel["plan_name"], "Business")
        self.assertEqual(camel["project_name"], "Desk")
        self.assertEqual(camel["five_hour_used_percent"], 12.5)
        self.assertEqual(camel["weekly_used_percent"], 40)
        self.assertEqual(camel["context_used_tokens"], 1050)
        self.assertEqual(camel["label"], "执行中 · 思考中")

        relative = load("resets_in_seconds.jsonl")
        self.assertEqual(relative["five_hour_reset_text"], "约 2 小时后重置")
        self.assertEqual(relative["weekly_reset_text"], "约 6 天后重置")
        self.assertEqual(relative["plan_name"], "Plus")
        self.assertEqual(relative["status"], "complete")


class ActivityTest(unittest.TestCase):
    def test_tool_transitions(self) -> None:
        text = "\n".join(
            [
                '{"timestamp":"2026-11-05T00:23:00Z","type":"session_meta","payload":{"id":"s","cwd":"/work/App"}}',
                '{"timestamp":"2026-11-05T00:23:01Z","type":"event_msg","payload":{"type":"task_started"}}',
                '{"timestamp":"2026-11-05T00:23:02Z","type":"response_item","payload":{"type":"reasoning"}}',
                '{"timestamp":"2026-11-05T00:23:03Z","type":"response_item","payload":{"type":"custom_tool_call","name":"apply_patch","call_id":"p1","input":"secret patch body"}}',
                '{"timestamp":"2026-11-05T00:23:04Z","type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"p1","output":"ok"}}',
                '{"timestamp":"2026-11-05T00:23:05Z","type":"event_msg","payload":{"type":"web_search_begin","call_id":"w1"}}',
                '{"timestamp":"2026-11-05T00:23:06Z","type":"event_msg","payload":{"type":"mcp_tool_call_begin","call_id":"m1"}}',
            ]
        )
        view = parse_jsonl(text)
        self.assertEqual(view.detail, "调用工具")
        self.assertEqual(view.phase, "running")
        self.assertNotIn("secret patch body", json.dumps(view.open_calls))
        view = parse_jsonl(text + '\n{"timestamp":"2026-11-05T00:23:07Z","type":"event_msg","payload":{"type":"mcp_tool_call_end","call_id":"m1"}}')
        self.assertEqual(view.detail, "搜索中")
        view = parse_jsonl(
            '{"timestamp":"2026-11-05T00:23:01Z","type":"event_msg","payload":{"type":"task_started"}}\n'
            '{"timestamp":"2026-11-05T00:23:02Z","type":"response_item","payload":{"type":"function_call","name":"update_plan","call_id":"plan1"}}'
        )
        self.assertEqual(view.detail, "规划中")

    def test_hot_threshold_and_interrupt_priority(self) -> None:
        base = (
            '{"timestamp":"2026-11-05T00:23:00Z","type":"session_meta","payload":{"id":"s","cwd":"/work/App"}}\n'
            '{"timestamp":"2026-11-05T00:23:01Z","type":"event_msg","payload":{"type":"task_started"}}\n'
        )
        almost = base + (
            '{"timestamp":"2026-11-05T00:23:02Z","type":"event_msg","payload":{"type":"token_count","info":null,'
            '"rate_limits":{"plan_type":"pro","primary":{"used_percent":89,"window_minutes":10080,"resets_at":1794356832}}}}'
        )
        hot = base + (
            '{"timestamp":"2026-11-05T00:23:02Z","type":"event_msg","payload":{"type":"token_count","info":null,'
            '"rate_limits":{"plan_type":"pro","primary":{"used_percent":90,"window_minutes":10080,"resets_at":1794356832}}}}'
        )
        stopped = hot + '\n{"timestamp":"2026-11-05T00:23:03Z","type":"event_msg","payload":{"type":"turn_aborted"}}'
        almost_payload = parse_session.build_payload(parse_jsonl(almost), NOW, tz_offset_seconds=TZ, session_file="a.jsonl", file_mtime=NOW)
        hot_payload = parse_session.build_payload(parse_jsonl(hot), NOW, tz_offset_seconds=TZ, session_file="a.jsonl", file_mtime=NOW)
        stop_payload = parse_session.build_payload(parse_jsonl(stopped), NOW, tz_offset_seconds=TZ, session_file="a.jsonl", file_mtime=NOW)
        self.assertEqual(almost_payload["color"], "orange")
        self.assertEqual(hot_payload["label"], "额度将尽 · 思考中")
        self.assertEqual(hot_payload["color"], "red")
        self.assertEqual(stop_payload["label"], "已中断")
        self.assertEqual(stop_payload["status"], "interrupted")

    def test_hook_overlay_until_log_catches_up(self) -> None:
        view = parse_jsonl(
            '{"timestamp":"2026-11-05T00:20:00Z","type":"session_meta","payload":{"id":"s","cwd":"/work/Old"}}\n'
            '{"timestamp":"2026-11-05T00:20:05Z","type":"event_msg","payload":{"type":"task_complete","duration_ms":1000}}'
        )
        hook = HookHint(received_at=NOW, phase="running", detail="跑命令", cwd=None, session_id=None)
        payload = parse_session.build_payload(view, NOW, tz_offset_seconds=TZ, session_file="a.jsonl", file_mtime=NOW, hook=hook)
        self.assertTrue(payload["hook_fresh"])
        self.assertEqual(payload["label"], "执行中 · 跑命令")
        caught_up = parse_jsonl(
            '{"timestamp":"2026-11-05T00:27:12Z","type":"event_msg","payload":{"type":"task_started"}}',
            view,
        )
        payload = parse_session.build_payload(caught_up, NOW, tz_offset_seconds=TZ, session_file="a.jsonl", file_mtime=NOW, hook=hook)
        self.assertFalse(payload["hook_fresh"])
        self.assertEqual(payload["detail"], "思考中")

    def test_stale_running_drops_to_idle(self) -> None:
        view = parse_jsonl(
            '{"timestamp":"2026-11-05T00:00:00Z","type":"session_meta","payload":{"id":"s","cwd":"/work/App"}}\n'
            '{"timestamp":"2026-11-05T00:00:01Z","type":"event_msg","payload":{"type":"task_started"}}'
        )
        payload = parse_session.build_payload(
            view,
            NOW,
            tz_offset_seconds=TZ,
            session_file="a.jsonl",
            file_mtime=NOW - 31 * 60,
        )
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["label"], "等待中")


class MergeAndServerTest(unittest.TestCase):
    def test_quota_comes_from_older_file_when_newest_has_none(self) -> None:
        home = Path(self.id().replace(".", "_"))
        # Use a real temp dir via unittest.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            older = day / "rollout-old.jsonl"
            newer = day / "rollout-new.jsonl"
            older.write_text((FIXTURES / "weekly_only.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            newer.write_text(
                "\n".join(
                    [
                        '{"timestamp":"2026-11-05T00:26:00Z","type":"session_meta","payload":{"id":"live","cwd":"/Users/me/Developer/NiceShot","source":"cli"}}',
                        '{"timestamp":"2026-11-05T00:26:10Z","type":"event_msg","payload":{"type":"task_started"}}',
                        '{"timestamp":"2026-11-05T00:26:12Z","type":"event_msg","payload":{"type":"exec_command_begin","call_id":"c"}}',
                        '{"timestamp":"2026-11-05T00:26:20Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"total_tokens":349000},"model_context_window":475000},"rate_limits":null}}',
                    ]
                ),
                encoding="utf-8",
            )
            os.utime(older, (NOW - 50, NOW - 50))
            os.utime(newer, (NOW, NOW))
            (root / ".codex" / "auth.json").write_text('{"token":"sk-live-should-stay"}', encoding="utf-8")
            payload = Engine().payload(root / ".codex", NOW, TZ, None)
            self.assertEqual(payload["session_file"], "rollout-new.jsonl")
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["detail"], "跑命令")
            self.assertEqual(payload["project_name"], "NiceShot")
            self.assertEqual(payload["context_used_tokens"], 349000)
            self.assertEqual(payload["weekly_used_percent"], 16)
            self.assertEqual(payload["plan_name"], "Pro")
            self.assertIsNone(payload["five_hour_used_percent"])
            self.assertNotIn("sk-live-should-stay", json.dumps(payload))
            self.assertEqual((root / ".codex" / "auth.json").read_text(encoding="utf-8"), '{"token":"sk-live-should-stay"}')

    def test_follower_reads_appended_lines(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(
                '{"timestamp":"2026-11-05T00:23:00Z","type":"event_msg","payload":{"type":"task_started"}}\n',
                encoding="utf-8",
            )
            follower = SessionFollower()
            self.assertEqual(follower.sync(path).phase, "running")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"timestamp":"2026-11-05T00:24:00Z","type":"event_msg","payload":{"type":"task_complete","duration_ms":60000}}\n'
                )
            self.assertEqual(follower.sync(path).phase, "complete")

    def test_http_status_and_local_hook(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex"
            day = root / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            target = day / "rollout-live.jsonl"
            target.write_text((FIXTURES / "complete.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            service = Service(root, TZ)
            service.publish()
            handler = type("BoundHandler", (Handler,), {"service": service})
            server = codex_status.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                conn = HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/status")
                body = json.loads(conn.getresponse().read().decode("utf-8"))
                self.assertEqual(body["label"], "已完成")
                conn.request("GET", "/health")
                self.assertEqual(json.loads(conn.getresponse().read().decode("utf-8"))["ok"], True)
                hook = json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "session_id": "sess-done",
                        "cwd": "/Users/me/Developer/NiceShot",
                        "tool_input": {"command": "echo sk-hook-secret"},
                        "prompt": "do not keep this prompt",
                    }
                ).encode("utf-8")
                conn.request("POST", "/hook", body=hook)
                posted = conn.getresponse()
                self.assertEqual(posted.status, 200)
                posted.read()
                conn.request("GET", "/status")
                fresh = json.loads(conn.getresponse().read().decode("utf-8"))
                self.assertEqual(fresh["label"], "执行中 · 跑命令")
                self.assertTrue(fresh["hook_fresh"])
                self.assertNotIn("sk-hook-secret", json.dumps(fresh))
                self.assertNotIn("do not keep", json.dumps(fresh))
                conn.request("GET", "/")
                page = conn.getresponse().read().decode("utf-8")
                self.assertIn("本周额度", page)
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_hook_classifier_and_local_check(self) -> None:
        self.assertTrue(client_is_local("127.0.0.1"))
        self.assertTrue(client_is_local("::1"))
        self.assertFalse(client_is_local("192.168.1.20"))
        hint = hint_from_hook({"hook_event_name": "PreToolUse", "tool_name": "apply_patch"}, NOW)
        self.assertEqual(hint.detail, "编辑文件")
        hint = hint_from_hook({"hook_event_name": "Stop"}, NOW)
        self.assertEqual(hint.phase, "complete")
        hint = hint_from_hook({"event": "UserPromptSubmit", "prompt": "secret"}, NOW)
        self.assertEqual(hint.detail, "思考中")
        hint = hint_from_hook({"hook_event_name": "SubagentStop", "session_id": "child"}, NOW)
        self.assertIsNone(hint.phase)

    def test_missing_sessions_dir(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            payload = Engine().payload(Path(tmp), NOW, TZ, None)
            self.assertFalse(payload["has_session"])
            self.assertEqual(payload["label"], "等待中")
            self.assertIn("sessions", payload["message"])


def _line(kind: str, payload: dict, timestamp: str = "2026-11-05T00:23:00Z") -> str:
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload}, ensure_ascii=False)


def _tokens(used: int, timestamp: str, weekly: float | None = None) -> str:
    limits = None
    if weekly is not None:
        limits = {
            "plan_type": "pro",
            "primary": {"used_percent": weekly, "window_minutes": 10080, "resets_at": 1794356832},
        }
    return _line(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": used},
                "model_context_window": 475000,
            },
            "rate_limits": limits,
        },
        timestamp,
    )


def _write(day: Path, name: str, text: str, mtime: float) -> Path:
    path = day / name
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


class MainWindowSelectionTest(unittest.TestCase):
    def test_child_markers_and_forks(self) -> None:
        top = parse_jsonl(_line("session_meta", {"id": "child", "parent_thread_id": "main"}))
        self.assertTrue(parse_session.is_child_session(top))
        self.assertEqual(top.parent_thread_id, "main")

        nested = parse_jsonl(
            _line(
                "session_meta",
                {
                    "id": "child",
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": "main", "depth": 1}}},
                },
            )
        )
        self.assertEqual(nested.parent_thread_id, "main")
        self.assertTrue(parse_session.is_child_session(nested))

        sourced = parse_jsonl(_line("session_meta", {"id": "child", "thread_source": "subagent"}))
        self.assertTrue(parse_session.is_child_session(sourced))

        mismatch = parse_jsonl(_line("session_meta", {"id": "child", "session_id": "main"}))
        self.assertEqual(mismatch.parent_thread_id, "main")
        self.assertTrue(parse_session.is_child_session(mismatch))

        forked = parse_jsonl(
            _line("session_meta", {"id": "fork", "session_id": "fork", "forked_from_id": "older"})
        )
        self.assertFalse(parse_session.is_child_session(forked))
        self.assertIsNone(forked.parent_thread_id)

        chatting = parse_jsonl(
            "\n".join(
                [
                    _line("session_meta", {"id": "main", "cwd": "/work/App", "source": "cli"}),
                    _line("inter_agent_communication_metadata", {"trigger_turn": False}),
                ]
            )
        )
        self.assertFalse(parse_session.is_child_session(chatting))

    def test_newer_child_does_not_steal_context_and_later_writes_do_not_flip_it(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            main = "\n".join(
                [
                    _line(
                        "session_meta",
                        {
                            "id": "main-chat",
                            "session_id": "main-chat",
                            "cwd": "/Users/me/Developer/NiceShot",
                            "source": "cli",
                        },
                    ),
                    _line("inter_agent_communication_metadata", {"trigger_turn": False}, "2026-11-05T00:23:01Z"),
                    _line("event_msg", {"type": "task_started"}, "2026-11-05T00:23:00Z"),
                    _tokens(218000, "2026-11-05T00:23:05Z", weekly=22),
                ]
            )
            own_child = "\n".join(
                [
                    _line(
                        "session_meta",
                        {
                            "id": "child-edit",
                            "session_id": "main-chat",
                            "parent_thread_id": "main-chat",
                            "cwd": "/Users/me/Developer/NiceShot",
                            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "main-chat", "depth": 1}}},
                            "thread_source": "subagent",
                        },
                    ),
                    _line("event_msg", {"type": "task_started"}, "2026-11-05T00:26:00Z"),
                    _line(
                        "event_msg",
                        {"type": "exec_command_begin", "call_id": "c1", "command": ["echo", "sk-child-secret"]},
                        "2026-11-05T00:26:20Z",
                    ),
                    _tokens(410000, "2026-11-05T00:26:30Z", weekly=86),
                ]
            )
            sibling = "\n".join(
                [
                    _line(
                        "session_meta",
                        {"id": "child-shell", "parent_thread_id": "main-chat", "cwd": "/Users/me/Developer/NiceShot"},
                    ),
                    _line(
                        "event_msg",
                        {"type": "exec_command_begin", "call_id": "c0", "command": ["echo", "do-not-leak-prompt"]},
                        "2026-11-05T00:25:00Z",
                    ),
                    _tokens(300000, "2026-11-05T00:25:10Z", weekly=40),
                ]
            )
            foreign = "\n".join(
                [
                    _line(
                        "session_meta",
                        {"id": "other-child", "parent_thread_id": "someone-else", "thread_source": "subagent"},
                    ),
                    _line("event_msg", {"type": "web_search_begin", "call_id": "w"}, "2026-11-05T00:27:00Z"),
                    _tokens(410000, "2026-11-05T00:27:05Z", weekly=99),
                ]
            )
            _write(day, "rollout-main.jsonl", main, NOW - 40)
            _write(day, "rollout-shell.jsonl", sibling, NOW - 20)
            _write(day, "rollout-edit.jsonl", own_child, NOW - 10)
            _write(day, "rollout-foreign.jsonl", foreign, NOW - 1)
            engine = Engine()
            home = Path(tmp) / ".codex"
            first = engine.payload(home, NOW, TZ, None)
            self.assertEqual(first["session_file"], "rollout-main.jsonl")
            self.assertEqual(first["session_id"], "main-chat")
            self.assertEqual(first["context_used_tokens"], 218000)
            self.assertEqual(first["context_limit_tokens"], 475000)
            self.assertEqual(first["weekly_used_percent"], 22)
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["label"], "执行中 · 跑命令")
            self.assertEqual(first["detail"], "跑命令")
            self.assertEqual(first["project_name"], "NiceShot")
            self.assertEqual(first["turn_elapsed_seconds"], 252)
            blob = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("sk-child-secret", blob)
            self.assertNotIn("do-not-leak-prompt", blob)

            edit_path = day / "rollout-edit.jsonl"
            with edit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _line(
                        "event_msg",
                        {"type": "patch_apply_begin", "call_id": "p1", "patch": "secret patch body"},
                        "2026-11-05T00:27:08Z",
                    )
                    + "\n"
                )
            os.utime(edit_path, (NOW + 5, NOW + 5))
            os.utime(day / "rollout-foreign.jsonl", (NOW + 6, NOW + 6))
            second = engine.payload(home, NOW, TZ, None)
            self.assertEqual(second["session_file"], "rollout-main.jsonl")
            self.assertEqual(second["session_id"], "main-chat")
            self.assertEqual(second["context_used_tokens"], 218000)
            self.assertEqual(second["context_used_percent"], first["context_used_percent"])
            self.assertEqual(second["weekly_used_percent"], 22)
            self.assertEqual(second["label"], "执行中 · 编辑文件")
            self.assertNotIn("secret patch body", json.dumps(second, ensure_ascii=False))

    def test_main_completion_and_interruption_stay_on_the_chatting_window(self) -> None:
        import tempfile

        def scenario(ending: str) -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
                day.mkdir(parents=True)
                _write(
                    day,
                    "rollout-main.jsonl",
                    "\n".join(
                        [
                            _line("session_meta", {"id": "main-chat", "cwd": "/Users/me/Developer/NiceShot", "source": "cli"}),
                            _line("event_msg", {"type": "task_started"}, "2026-11-05T00:23:00Z"),
                            _tokens(218000, "2026-11-05T00:26:00Z", weekly=22),
                            _line("event_msg", {"type": ending, "duration_ms": 180000}, "2026-11-05T00:26:40Z"),
                        ]
                    ),
                    NOW - 5,
                )
                _write(
                    day,
                    "rollout-child.jsonl",
                    "\n".join(
                        [
                            _line(
                                "session_meta",
                                {"id": "child", "parent_thread_id": "main-chat", "thread_source": "subagent"},
                            ),
                            _line("event_msg", {"type": "task_started"}, "2026-11-05T00:27:00Z"),
                            _line("event_msg", {"type": "exec_command_begin", "call_id": "c"}, "2026-11-05T00:27:05Z"),
                            _tokens(410000, "2026-11-05T00:27:06Z", weekly=86),
                        ]
                    ),
                    NOW,
                )
                return Engine().payload(Path(tmp) / ".codex", NOW, TZ, None)

        done = scenario("task_complete")
        self.assertEqual(done["session_file"], "rollout-main.jsonl")
        self.assertEqual(done["status"], "complete")
        self.assertEqual(done["label"], "已完成")
        self.assertEqual(done["color"], "green")
        self.assertEqual(done["context_used_tokens"], 218000)
        self.assertEqual(done["weekly_used_percent"], 22)
        self.assertEqual(done["turn_elapsed_text"], "3分0秒")

        stopped = scenario("turn_aborted")
        self.assertEqual(stopped["session_file"], "rollout-main.jsonl")
        self.assertEqual(stopped["status"], "interrupted")
        self.assertEqual(stopped["label"], "已中断")
        self.assertEqual(stopped["color"], "red")
        self.assertEqual(stopped["context_used_tokens"], 218000)
        self.assertNotEqual(stopped["context_used_tokens"], 410000)

    def test_nested_source_and_thread_source_cannot_become_the_window(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            _write(
                day,
                "rollout-main.jsonl",
                "\n".join(
                    [
                        _line(
                            "session_meta",
                            {"id": "main-chat", "cwd": "/Users/me/Developer/NiceShot", "source": "cli", "forked_from_id": "older-chat"},
                        ),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:23:00Z"),
                        _tokens(218000, "2026-11-05T00:24:00Z"),
                    ]
                ),
                NOW - 30,
            )
            _write(
                day,
                "rollout-nested.jsonl",
                "\n".join(
                    [
                        _line(
                            "session_meta",
                            {
                                "id": "nested",
                                "session_id": "main-chat",
                                "source": {
                                    "subagent": {
                                        "thread_spawn": {
                                            "parent_thread_id": "main-chat",
                                            "depth": 2,
                                            "agent_role": "explorer",
                                        }
                                    }
                                },
                            },
                        ),
                        _line("event_msg", {"type": "patch_apply_begin", "call_id": "p"}, "2026-11-05T00:27:00Z"),
                        _tokens(410000, "2026-11-05T00:27:02Z"),
                    ]
                ),
                NOW - 1,
            )
            _write(
                day,
                "rollout-guardian.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "guard", "thread_source": "guardian_review"}),
                        _tokens(50000, "2026-11-05T00:27:03Z"),
                    ]
                ),
                NOW,
            )
            payload = Engine().payload(Path(tmp) / ".codex", NOW, TZ, None)
            self.assertEqual(payload["session_file"], "rollout-main.jsonl")
            self.assertEqual(payload["session_id"], "main-chat")
            self.assertEqual(payload["context_used_tokens"], 218000)
            self.assertEqual(payload["label"], "执行中 · 编辑文件")
            self.assertEqual(payload["status"], "running")

    def test_child_keeps_a_quiet_main_turn_from_looking_idle(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            _write(
                day,
                "rollout-main.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "main-chat", "cwd": "/work/App"}),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:00:01Z"),
                        _tokens(218000, "2026-11-05T00:00:02Z"),
                    ]
                ),
                NOW - 31 * 60,
            )
            _write(
                day,
                "rollout-child.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "child", "parent_thread_id": "main-chat"}),
                        _line("event_msg", {"type": "exec_command_begin", "call_id": "c"}, "2026-11-05T00:27:00Z"),
                        _tokens(410000, "2026-11-05T00:27:01Z"),
                    ]
                ),
                NOW,
            )
            payload = Engine().payload(Path(tmp) / ".codex", NOW, TZ, None)
            self.assertFalse(payload["stale"])
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["label"], "执行中 · 跑命令")
            self.assertEqual(payload["context_used_tokens"], 218000)
            self.assertEqual(payload["session_file"], "rollout-main.jsonl")

    def test_quota_donor_still_works_and_child_quota_does_not_replace_it(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            older = day / "rollout-old.jsonl"
            older.write_text((FIXTURES / "weekly_only.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            os.utime(older, (NOW - 80, NOW - 80))
            _write(
                day,
                "rollout-main.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "live", "cwd": "/Users/me/Developer/NiceShot", "source": "cli"}),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:26:10Z"),
                        _line("event_msg", {"type": "exec_command_begin", "call_id": "c"}, "2026-11-05T00:26:12Z"),
                        _tokens(218000, "2026-11-05T00:26:20Z"),
                    ]
                ),
                NOW - 30,
            )
            _write(
                day,
                "rollout-child.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "child", "parent_thread_id": "live"}),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:27:00Z"),
                        _tokens(410000, "2026-11-05T00:27:05Z", weekly=99),
                    ]
                ),
                NOW,
            )
            payload = Engine().payload(Path(tmp) / ".codex", NOW, TZ, None)
            self.assertEqual(payload["session_file"], "rollout-main.jsonl")
            self.assertEqual(payload["context_used_tokens"], 218000)
            self.assertEqual(payload["weekly_used_percent"], 16)
            self.assertEqual(payload["plan_name"], "Pro")
            self.assertIsNone(payload["five_hour_used_percent"])
            self.assertNotEqual(payload["weekly_used_percent"], 99)

    def test_hooks_follow_the_main_window(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / ".codex" / "sessions" / "2026" / "11" / "05"
            day.mkdir(parents=True)
            _write(
                day,
                "rollout-main.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "main-chat", "cwd": "/Users/me/Developer/NiceShot", "source": "cli"}),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:23:00Z"),
                        _tokens(218000, "2026-11-05T00:24:00Z", weekly=22),
                    ]
                ),
                NOW - 5,
            )
            _write(
                day,
                "rollout-child.jsonl",
                "\n".join(
                    [
                        _line("session_meta", {"id": "child", "parent_thread_id": "main-chat"}),
                        _line("event_msg", {"type": "task_started"}, "2026-11-05T00:26:00Z"),
                    ]
                ),
                NOW,
            )
            home = Path(tmp) / ".codex"
            engine = Engine()
            child_stop = HookHint(NOW, "complete", "", None, "child")
            held = engine.payload(home, NOW, TZ, child_stop)
            self.assertEqual(held["status"], "running")
            self.assertEqual(held["context_used_tokens"], 218000)
            self.assertEqual(held["session_file"], "rollout-main.jsonl")

            child_tool = HookHint(NOW, "running", "编辑文件", None, "child")
            hinted = engine.payload(home, NOW, TZ, child_tool)
            self.assertEqual(hinted["label"], "执行中 · 编辑文件")
            self.assertEqual(hinted["context_used_tokens"], 218000)
            self.assertTrue(hinted["hook_fresh"])

            main_done = HookHint(NOW, "complete", "", None, "main-chat")
            finished = engine.payload(home, NOW, TZ, main_done)
            self.assertEqual(finished["status"], "complete")
            self.assertEqual(finished["label"], "已完成")
            self.assertEqual(finished["color"], "green")
            self.assertEqual(finished["context_used_tokens"], 218000)
            self.assertEqual(finished["session_id"], "main-chat")


class FontCoverageTest(unittest.TestCase):
    def test_generated_font_covers_interface_copy(self) -> None:
        header = ROOT.parent / "firmware" / "src" / "font_zh.cpp"
        if not header.exists():
            self.skipTest("font not generated yet")
        encoded = header.read_text(encoding="utf-8")
        covered = {int(item, 16) for item in __import__("re").findall(r"\{0x([0-9A-Fa-f]+),", encoded)}
        missing = []
        roots = [ROOT, ROOT.parent / "hooks", ROOT.parent / "firmware" / "src", ROOT.parent / "firmware" / "include"]
        for base in roots:
            for path in base.rglob("*"):
                if path.suffix not in {".py", ".h", ".cpp"} or path.name == "font_zh.cpp":
                    continue
                for char in path.read_text(encoding="utf-8", errors="ignore"):
                    if ord(char) > 127 and ord(char) not in covered:
                        missing.append((str(path.relative_to(ROOT.parent)), char))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
