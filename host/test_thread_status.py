"""Fixture-based tests for the read-only thread_status snapshot.

Builds temporary sqlite databases and a session_index.jsonl. Test titles are
synthetic and never use first_user_message as a fallback.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

import thread_status as ts


NOW = 1_800_000_000.0  # 2027-01-15-ish, seconds


@dataclass
class Hook:
    session_id: str | None
    detail: str = ""
    phase: str = "running"


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _create_state(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            originator TEXT NOT NULL DEFAULT '',
            thread_source TEXT,
            archived INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER,
            updated_at_ms INTEGER,
            recency_at INTEGER,
            recency_at_ms INTEGER,
            first_user_message TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE thread_read_receipts (
            thread_id TEXT PRIMARY KEY,
            first_unread_turn TEXT,
            revision TEXT NOT NULL DEFAULT '1',
            last_published_turn TEXT
        );
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    return conn


def _create_history(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_json TEXT,
            started_at INTEGER,
            completed_at INTEGER,
            duration_ms INTEGER,
            PRIMARY KEY (thread_id, turn_id)
        );
        """
    )
    return conn


class FixtureHome:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = _create_state(root / "state_5.sqlite")
        self.history = _create_history(root / "thread_history_1.sqlite")
        self.index_path = root / "session_index.jsonl"
        self.index_path.write_text("", encoding="utf-8")
        self._ordinal: dict[str, int] = {}

    def close(self) -> None:
        self.state.close()
        self.history.close()

    def add_thread(
        self,
        thread_id: str,
        *,
        title: str = "",
        originator: str = "Codex Desktop",
        thread_source: str | None = None,
        archived: int = 0,
        updated_s: float | None = None,
        first_user_message: str = "SECRET_USER_PROMPT_MUST_NOT_LEAK",
        sidebar: str | None = None,
        unread: bool = False,
    ) -> None:
        updated_ms = _ms(updated_s) if updated_s is not None else None
        self.state.execute(
            "INSERT INTO threads (id, title, originator, thread_source, archived, "
            "updated_at_ms, first_user_message) VALUES (?,?,?,?,?,?,?)",
            (
                thread_id,
                title,
                originator,
                thread_source,
                archived,
                updated_ms,
                first_user_message,
            ),
        )
        self.state.commit()
        if unread:
            self.state.execute(
                "INSERT INTO thread_read_receipts (thread_id, first_unread_turn) "
                "VALUES (?,?)",
                (thread_id, "turn-unread"),
            )
            self.state.commit()
        if sidebar is not None:
            with self.index_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "id": thread_id,
                            "thread_name": sidebar,
                            "updated_at": "2026-11-11T00:00:00.000Z",
                        }
                    )
                    + "\n"
                )

    def add_edge(self, parent: str, child: str) -> None:
        self.state.execute(
            "INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) "
            "VALUES (?,?,?)",
            (parent, child, "open"),
        )
        self.state.commit()

    def add_turn(
        self,
        thread_id: str,
        status: str,
        *,
        started_s: float | None = None,
        completed_s: float | None = None,
        use_seconds: bool = False,
    ) -> None:
        ordinal = self._ordinal.get(thread_id, 0) + 1
        self._ordinal[thread_id] = ordinal
        started = started_s if use_seconds else (_ms(started_s) if started_s is not None else None)
        completed = completed_s if use_seconds else (_ms(completed_s) if completed_s is not None else None)
        self.history.execute(
            "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, "
            "started_at, completed_at) VALUES (?,?,?,?,?,?)",
            (thread_id, f"turn-{ordinal}", ordinal, status, started, completed),
        )
        self.history.commit()

    def snap(self, hook: Hook | None = None, **kwargs: object) -> dict:
        return ts.snapshot(self.root, NOW, hook, **kwargs)


class ThreadStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = FixtureHome(Path(self._tmp.name))

    def tearDown(self) -> None:
        self.home.close()
        self._tmp.cleanup()

    def test_empty_home_is_idle(self) -> None:
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["pet_label"], "空闲")
        self.assertEqual(snap["pet_title"], "")
        self.assertIsNone(snap["pet_thread_id"])
        self.assertEqual(snap["pet_age_s"], 0)
        self.assertEqual(snap["counts"]["idle"], 0)
        self.assertEqual(snap["threads"], [])

    def test_missing_databases_do_not_raise(self) -> None:
        empty = Path(self._tmp.name) / "missing"
        empty.mkdir()
        snap = ts.snapshot(empty, NOW)
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["threads"], [])

    def test_completed_unread_is_ready(self) -> None:
        self.home.add_thread(
            "t-ready",
            title="DB title ignored",
            sidebar="整理本周任务列表",
            unread=True,
            updated_s=NOW - 120,
        )
        self.home.add_turn("t-ready", "completed", started_s=NOW - 200, completed_s=NOW - 120)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "ready")
        self.assertEqual(snap["pet_label"], "已完成")
        self.assertEqual(snap["pet_title"], "整理本周任务列表")
        self.assertEqual(snap["pet_thread_id"], "t-ready")
        self.assertEqual(snap["pet_age_s"], 120)
        self.assertEqual(snap["counts"]["ready"], 1)
        self.assertEqual(snap["threads"][0]["title"], "整理本周任务列表")

    def test_session_index_title_preferred_over_db(self) -> None:
        self.home.add_thread("t1", title="db-title", sidebar="sidebar-title", updated_s=NOW - 10)
        self.home.add_turn("t1", "completed", completed_s=NOW - 10)
        snap = self.home.snap()
        self.assertEqual(snap["pet_title"], "sidebar-title")

    def test_db_title_used_when_index_missing(self) -> None:
        self.home.add_thread("t1", title="仅数据库标题", updated_s=NOW - 10)
        self.home.add_turn("t1", "completed", completed_s=NOW - 10)
        snap = self.home.snap()
        self.assertEqual(snap["pet_title"], "仅数据库标题")

    def test_untitled_fallback_is_unnamed_not_first_message(self) -> None:
        self.home.add_thread("t1", title="", first_user_message="SECRET_USER_PROMPT_MUST_NOT_LEAK")
        self.home.add_turn("t1", "idle")
        snap = self.home.snap()
        dumped = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("SECRET_USER_PROMPT_MUST_NOT_LEAK", dumped)
        self.assertNotIn("first_user_message", dumped)
        self.assertEqual(snap["pet_title"], "未命名")

    def test_title_is_clipped(self) -> None:
        long_title = "这是一段非常非常非常非常非常非常非常长的对话标题需要被截断处理一下"
        self.home.add_thread("t1", title=long_title, updated_s=NOW - 5)
        snap = self.home.snap()
        self.assertLessEqual(len(snap["pet_title"]), ts.TITLE_LIMIT)
        self.assertTrue(snap["pet_title"].endswith("…"))

    def test_interrupted_becomes_idle(self) -> None:
        self.home.add_thread("t1", title="打断的会话", updated_s=NOW - 30)
        self.home.add_turn("t1", "interrupted", started_s=NOW - 80, completed_s=NOW - 30)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_stale_in_progress_becomes_idle(self) -> None:
        self.home.add_thread("t1", title="卡住的会话", updated_s=NOW - 4000)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 4000)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["counts"]["running"], 0)
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_fresh_in_progress_is_running(self) -> None:
        self.home.add_thread("t1", title="正在跑", updated_s=NOW - 20)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 20)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_label"], "运行中")
        self.assertEqual(snap["pet_age_s"], 20)

    def test_timestamp_in_seconds_is_accepted(self) -> None:
        self.home.add_thread("t1", title="秒级时间戳", updated_s=NOW - 15)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 15, use_seconds=True)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_age_s"], 15)

    def test_failed_latest_turn_is_blocked(self) -> None:
        self.home.add_thread("t1", title="失败了", updated_s=NOW - 40)
        self.home.add_turn("t1", "failed", started_s=NOW - 80, completed_s=NOW - 40)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "blocked")
        self.assertEqual(snap["pet_label"], "出错了")

    def test_older_failed_turn_is_ignored_if_later_completed(self) -> None:
        self.home.add_thread("t1", title="后来修好了", updated_s=NOW - 10, unread=True)
        self.home.add_turn("t1", "failed", started_s=NOW - 80, completed_s=NOW - 70)
        self.home.add_turn("t1", "completed", started_s=NOW - 20, completed_s=NOW - 10)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "ready")

    def test_subagent_in_progress_folds_into_parent_running(self) -> None:
        self.home.add_thread("parent", title="主会话", updated_s=NOW - 60)
        self.home.add_turn("parent", "completed", started_s=NOW - 80, completed_s=NOW - 60)
        self.home.add_thread(
            "child",
            title="子代理",
            originator="codex_exec",
            thread_source="subagent",
            updated_s=NOW - 5,
        )
        self.home.add_turn("child", "inProgress", started_s=NOW - 5)
        self.home.add_edge("parent", "child")
        snap = self.home.snap()
        ids = [t["id"] for t in snap["threads"]]
        self.assertEqual(ids, ["parent"])
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_thread_id"], "parent")
        self.assertEqual(snap["counts"]["running"], 1)

    def test_subagent_failed_does_not_block_parent(self) -> None:
        self.home.add_thread("parent", title="主会话", updated_s=NOW - 10, unread=True)
        self.home.add_turn("parent", "completed", started_s=NOW - 40, completed_s=NOW - 10)
        self.home.add_thread(
            "child",
            title="失败的子代理",
            originator="codex_exec",
            thread_source="subagent",
            updated_s=NOW - 5,
        )
        self.home.add_turn("child", "failed", started_s=NOW - 20, completed_s=NOW - 5)
        self.home.add_edge("parent", "child")
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "ready")
        self.assertEqual(snap["counts"]["blocked"], 0)
        self.assertEqual([t["id"] for t in snap["threads"]], ["parent"])

    def test_stale_subagent_does_not_keep_parent_running(self) -> None:
        self.home.add_thread("parent", title="主会话", updated_s=NOW - 4000)
        self.home.add_turn("parent", "completed", completed_s=NOW - 4000)
        self.home.add_thread(
            "child",
            title="卡住的子代理",
            originator="codex_exec",
            thread_source="subagent",
            updated_s=NOW - 4000,
        )
        self.home.add_turn("child", "inProgress", started_s=NOW - 4000)
        self.home.add_edge("parent", "child")
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")

    def test_archived_and_non_codex_originators_are_excluded(self) -> None:
        self.home.add_thread("archived", title="归档", archived=1, updated_s=NOW - 5)
        self.home.add_turn("archived", "inProgress", started_s=NOW - 5)
        self.home.add_thread(
            "chatgpt",
            title="来自 ChatGPT",
            originator="ChatGPT",
            updated_s=NOW - 5,
        )
        self.home.add_turn("chatgpt", "inProgress", started_s=NOW - 5)
        self.home.add_thread("ok", title="正常会话", originator="codex_work_desktop", updated_s=NOW - 8)
        self.home.add_turn("ok", "inProgress", started_s=NOW - 8)
        snap = self.home.snap()
        self.assertEqual([t["id"] for t in snap["threads"]], ["ok"])
        self.assertEqual(snap["counts"]["running"], 1)

    def test_priority_order_and_list_cap(self) -> None:
        # Nine user threads covering every state; list must cap at 8 and
        # pick needs_input first, then running, blocked, ready, idle.
        # Older threads of the same state sort later.
        specs = [
            ("need-new", "needs", NOW - 10, "inProgress", False),
            ("need-old", "needs", NOW - 50, "inProgress", False),
            ("block", "blocked", NOW - 20, "failed", False),
            ("ready", "ready", NOW - 30, "completed", True),
            ("run-new", "running", NOW - 15, "inProgress", False),
            ("run-old", "running", NOW - 80, "inProgress", False),
            ("idle-a", "idle", NOW - 100, "completed", False),
            ("idle-b", "idle", NOW - 200, "interrupted", False),
            ("idle-c", "idle", NOW - 400, "completed", False),
        ]
        for tid, _kind, age, status, unread in specs:
            self.home.add_thread(tid, title=tid, updated_s=age, unread=unread, sidebar=tid)
            self.home.add_turn(tid, status, started_s=age, completed_s=age)
        snap = self.home.snap(
            hook=Hook(session_id="need-new", detail="等待确认"),
            needs_input_ids={"need-old"},
        )
        ids = [t["id"] for t in snap["threads"]]
        self.assertEqual(
            ids,
            [
                "need-new",
                "need-old",
                "run-new",
                "run-old",
                "block",
                "ready",
                "idle-a",
                "idle-b",
            ],
        )
        self.assertNotIn("idle-c", ids)
        self.assertEqual(len(snap["threads"]), 8)
        self.assertEqual(snap["pet_state"], "needs_input")
        self.assertEqual(snap["pet_thread_id"], "need-new")
        self.assertEqual(snap["pet_label"], "需要你回复")
        self.assertEqual(snap["counts"]["needs_input"], 2)
        self.assertEqual(snap["counts"]["blocked"], 1)
        self.assertEqual(snap["counts"]["ready"], 1)
        self.assertEqual(snap["counts"]["running"], 2)
        self.assertEqual(snap["counts"]["idle"], 3)

    def test_hook_without_permission_detail_does_not_promote(self) -> None:
        self.home.add_thread("t1", title="普通运行", updated_s=NOW - 10)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 10)
        snap = self.home.snap(hook=Hook(session_id="t1", detail="正在调用工具"))
        self.assertEqual(snap["pet_state"], "running")

    def test_hook_unknown_session_is_ignored(self) -> None:
        self.home.add_thread("t1", title="普通运行", updated_s=NOW - 10)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 10)
        snap = self.home.snap(hook=Hook(session_id="missing", detail="等待确认"))
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["counts"]["needs_input"], 0)

    def test_codex_exec_originator_is_included(self) -> None:
        self.home.add_thread("cli", title="命令行会话", originator="codex_exec", updated_s=NOW - 12)
        self.home.add_turn("cli", "inProgress", started_s=NOW - 12)
        snap = self.home.snap()
        self.assertEqual(snap["pet_thread_id"], "cli")
        self.assertEqual(snap["pet_state"], "running")

    def test_flash_cli_failure_is_idle(self) -> None:
        self.home.add_thread(
            "cli",
            title="闪崩的命令行",
            originator="codex_exec",
            updated_s=NOW - 6,
        )
        self.home.add_turn("cli", "failed", started_s=NOW - 12, completed_s=NOW - 6)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["counts"]["blocked"], 0)
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_flash_cli_failure_at_30s_is_idle(self) -> None:
        age = ts.FLASH_CLI_FAILURE_SECONDS
        self.home.add_thread(
            "cli",
            title="刚好三十秒",
            originator="codex_exec",
            updated_s=NOW - 1,
        )
        self.home.add_turn("cli", "failed", started_s=NOW - 1 - age, completed_s=NOW - 1)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["counts"]["blocked"], 0)

    def test_longer_cli_failure_stays_blocked(self) -> None:
        self.home.add_thread(
            "cli",
            title="跑了一会儿才失败",
            originator="codex_exec",
            updated_s=NOW - 40,
        )
        self.home.add_turn("cli", "failed", started_s=NOW - 80, completed_s=NOW - 40)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "blocked")
        self.assertEqual(snap["counts"]["blocked"], 1)

    def test_desktop_flash_failure_stays_blocked(self) -> None:
        self.home.add_thread("t1", title="桌面里失败了", updated_s=NOW - 6)
        self.home.add_turn("t1", "failed", started_s=NOW - 12, completed_s=NOW - 6)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "blocked")

    def test_running_outranks_fresh_blocked(self) -> None:
        self.home.add_thread("fail", title="失败了", updated_s=NOW - 40)
        self.home.add_turn("fail", "failed", started_s=NOW - 80, completed_s=NOW - 40)
        self.home.add_thread("run", title="正在跑", updated_s=NOW - 20)
        self.home.add_turn("run", "inProgress", started_s=NOW - 20)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_thread_id"], "run")
        self.assertEqual(snap["counts"]["running"], 1)
        self.assertEqual(snap["counts"]["blocked"], 1)
        self.assertEqual([t["id"] for t in snap["threads"]], ["run", "fail"])

    def test_flash_cli_failure_does_not_mask_running(self) -> None:
        self.home.add_thread(
            "cli",
            title="闪崩的命令行",
            originator="codex_exec",
            updated_s=NOW - 6,
        )
        self.home.add_turn("cli", "failed", started_s=NOW - 12, completed_s=NOW - 6)
        self.home.add_thread("run", title="正在跑", updated_s=NOW - 5)
        self.home.add_turn("run", "inProgress", started_s=NOW - 5)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_thread_id"], "run")
        self.assertEqual(snap["counts"]["blocked"], 0)
        self.assertEqual(snap["counts"]["running"], 1)
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_to_unix_helpers(self) -> None:
        self.assertEqual(ts.to_unix(1_800_000_000), 1_800_000_000.0)
        self.assertEqual(ts.to_unix(1_800_000_000_000), 1_800_000_000.0)
        self.assertIsNone(ts.to_unix(0))
        self.assertIsNone(ts.to_unix(None))
        self.assertIsNone(ts.to_unix("nope"))


    def test_blocked_just_under_24h_stays_blocked(self) -> None:
        age = ts.STALE_BLOCKED_SECONDS - 1
        self.home.add_thread("t1", title="刚失败", updated_s=NOW - age)
        self.home.add_turn("t1", "failed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "blocked")
        self.assertEqual(snap["pet_age_s"], age)
        self.assertEqual(snap["counts"]["blocked"], 1)
        self.assertEqual(snap["counts"]["idle"], 0)

    def test_blocked_exactly_24h_stays_blocked(self) -> None:
        age = ts.STALE_BLOCKED_SECONDS
        self.home.add_thread("t1", title="刚好一天", updated_s=NOW - age)
        self.home.add_turn("t1", "failed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "blocked")
        self.assertEqual(snap["counts"]["blocked"], 1)

    def test_blocked_older_than_24h_becomes_idle(self) -> None:
        age = ts.STALE_BLOCKED_SECONDS + 1
        self.home.add_thread("t1", title="很久以前失败", updated_s=NOW - age)
        self.home.add_turn("t1", "failed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["pet_age_s"], age)
        self.assertEqual(snap["counts"]["blocked"], 0)
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_ready_just_under_3_days_stays_ready(self) -> None:
        age = ts.STALE_READY_SECONDS - 1
        self.home.add_thread("t1", title="未读但还新鲜", updated_s=NOW - age, unread=True)
        self.home.add_turn("t1", "completed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "ready")
        self.assertEqual(snap["counts"]["ready"], 1)
        self.assertEqual(snap["counts"]["idle"], 0)

    def test_ready_exactly_3_days_stays_ready(self) -> None:
        age = ts.STALE_READY_SECONDS
        self.home.add_thread("t1", title="刚好三天", updated_s=NOW - age, unread=True)
        self.home.add_turn("t1", "completed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "ready")
        self.assertEqual(snap["counts"]["ready"], 1)

    def test_ready_older_than_3_days_becomes_idle(self) -> None:
        age = ts.STALE_READY_SECONDS + 1
        self.home.add_thread("t1", title="很久未读", updated_s=NOW - age, unread=True)
        self.home.add_turn("t1", "completed", started_s=NOW - age - 10, completed_s=NOW - age)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "idle")
        self.assertEqual(snap["pet_age_s"], age)
        self.assertEqual(snap["counts"]["ready"], 0)
        self.assertEqual(snap["counts"]["idle"], 1)

    def test_stale_blocked_does_not_outrank_fresh_running(self) -> None:
        self.home.add_thread("old-fail", title="旧失败", updated_s=NOW - 2 * ts.STALE_BLOCKED_SECONDS)
        self.home.add_turn(
            "old-fail",
            "failed",
            started_s=NOW - 2 * ts.STALE_BLOCKED_SECONDS - 10,
            completed_s=NOW - 2 * ts.STALE_BLOCKED_SECONDS,
        )
        self.home.add_thread("run", title="正在跑", updated_s=NOW - 20)
        self.home.add_turn("run", "inProgress", started_s=NOW - 20)
        snap = self.home.snap()
        self.assertEqual(snap["pet_state"], "running")
        self.assertEqual(snap["pet_thread_id"], "run")
        self.assertEqual(snap["counts"]["running"], 1)
        self.assertEqual(snap["counts"]["blocked"], 0)
        self.assertEqual(snap["counts"]["idle"], 1)
        self.assertEqual([t["id"] for t in snap["threads"]], ["run", "old-fail"])

    def test_needs_input_is_not_demoted_by_age(self) -> None:
        age = ts.STALE_BLOCKED_SECONDS * 3
        self.home.add_thread("t1", title="仍在等确认", updated_s=NOW - age)
        self.home.add_turn("t1", "inProgress", started_s=NOW - age)
        snap = self.home.snap(hook=Hook(session_id="t1", detail="等待确认"))
        self.assertEqual(snap["pet_state"], "needs_input")
        self.assertEqual(snap["counts"]["needs_input"], 1)

    def test_demote_stale_helper_boundaries(self) -> None:
        self.assertEqual(ts.demote_stale("blocked", ts.STALE_BLOCKED_SECONDS), "blocked")
        self.assertEqual(ts.demote_stale("blocked", ts.STALE_BLOCKED_SECONDS + 1), "idle")
        self.assertEqual(ts.demote_stale("ready", ts.STALE_READY_SECONDS), "ready")
        self.assertEqual(ts.demote_stale("ready", ts.STALE_READY_SECONDS + 1), "idle")
        self.assertEqual(ts.demote_stale("running", 10**9), "running")
        self.assertEqual(ts.demote_stale("needs_input", 10**9), "needs_input")
        self.assertEqual(ts.demote_stale("idle", 10**9), "idle")

    def test_attach_pet_merges_into_existing_payload(self) -> None:
        self.home.add_thread("t1", title="附加字段", updated_s=NOW - 7)
        self.home.add_turn("t1", "inProgress", started_s=NOW - 7)
        payload = {"ok": True, "status": "running", "label": "运行中"}
        ts.attach_pet(payload, self.home.root, NOW)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["pet_state"], "running")
        self.assertEqual(payload["pet_title"], "附加字段")


if __name__ == "__main__":
    unittest.main()
