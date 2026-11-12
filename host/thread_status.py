"""Read-only snapshot of Codex user threads for the desk companion page.

Sources (all opened briefly, sqlite in mode=ro):
  ~/.codex/state_5.sqlite          threads, read receipts, spawn edges
  ~/.codex/thread_history_1.sqlite latest turn status per thread
  ~/.codex/session_index.jsonl     sidebar titles (never first_user_message)

Derivation (v1, no app-server):
  - Only Codex originators; subagents are folded into the parent (running only).
  - interrupted -> idle; stale inProgress (>30 min) -> idle.
  - blocked older than 24h -> idle; ready (unread) older than 3 days -> idle.
  - Flash-failed codex_exec runs (failed within 30s) -> idle, not blocked.
  - needs_input only from a live PermissionRequest hook (detail 等待确认).
  - Priority: needs_input > running > blocked > ready > idle.
  - Output never includes first_user_message or other raw prompt text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

STALE_RUNNING_SECONDS = 30 * 60
STALE_BLOCKED_SECONDS = 24 * 60 * 60
STALE_READY_SECONDS = 3 * 24 * 60 * 60
FLASH_CLI_FAILURE_SECONDS = 30
MAX_THREADS = 8
TITLE_LIMIT = 32

CHILD_THREAD_SOURCES = {"subagent", "guardian_review", "memory_consolidation"}
CODEX_ORIGINATORS = {"Codex Desktop", "codex_exec", "codex_work_desktop"}

PRIORITY = {
    "needs_input": 0,
    "running": 1,
    "blocked": 2,
    "ready": 3,
    "idle": 4,
}

PET_LABELS = {
    "running": "运行中",
    "needs_input": "需要你回复",
    "ready": "已完成",
    "blocked": "出错了",
    "idle": "空闲",
}

HOOK_NEEDS_INPUT_DETAIL = "等待确认"


def empty_pet() -> dict:
    return {
        "pet_state": "idle",
        "pet_label": PET_LABELS["idle"],
        "pet_title": "",
        "pet_thread_id": None,
        "pet_age_s": 0,
        "counts": {
            "needs_input": 0,
            "blocked": 0,
            "ready": 0,
            "running": 0,
            "idle": 0,
        },
        "threads": [],
    }


def clip_title(title: str, limit: int = TITLE_LIMIT) -> str:
    title = (title or "").strip()
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def to_unix(value: object) -> float | None:
    """Accept seconds or milliseconds (values >= 1e11 are treated as ms)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            raw = float(value)
        except ValueError:
            return None
    else:
        return None
    if raw <= 0:
        return None
    if raw >= 1e11:
        return raw / 1000.0
    return raw


def is_child_source(thread_source: str | None) -> bool:
    if not thread_source:
        return False
    return thread_source.strip().lower() in CHILD_THREAD_SOURCES


def is_codex_originator(originator: str | None) -> bool:
    return (originator or "") in CODEX_ORIGINATORS


def hook_needs_input_id(hook: object | None) -> str | None:
    """A PermissionRequest hook is stored as phase=running, detail=等待确认."""
    if hook is None:
        return None
    detail = getattr(hook, "detail", None)
    session_id = getattr(hook, "session_id", None)
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if detail != HOOK_NEEDS_INPUT_DETAIL:
        return None
    return session_id.strip()


def load_thread_titles(path: Path) -> dict[str, str]:
    """Latest sidebar name for each thread id in session_index.jsonl."""
    best: dict[str, tuple[str, str, int]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for seq, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        thread_id = record.get("id")
        title = record.get("thread_name")
        if not isinstance(thread_id, str) or not isinstance(title, str):
            continue
        thread_id = thread_id.strip()
        title = title.strip()
        if not thread_id or not title:
            continue
        updated = record.get("updated_at")
        updated_at = updated.strip() if isinstance(updated, str) else ""
        previous = best.get(thread_id)
        if previous is None:
            best[thread_id] = (updated_at, title, seq)
            continue
        old_at, _, old_seq = previous
        if not updated_at or not old_at:
            if seq >= old_seq:
                best[thread_id] = (updated_at or old_at, title, seq)
            continue
        if updated_at > old_at or (updated_at == old_at and seq >= old_seq):
            best[thread_id] = (updated_at, title, seq)
    return {thread_id: title for thread_id, (_, title, _) in best.items()}


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


@dataclass
class ThreadRow:
    id: str
    title: str
    originator: str
    thread_source: str
    archived: int
    updated_unix: float | None


@dataclass
class TurnRow:
    thread_id: str
    status: str
    started_unix: float | None
    completed_unix: float | None
    duration_s: float | None = None


def _load_threads(conn: sqlite3.Connection) -> list[ThreadRow]:
    try:
        rows = conn.execute(
            "SELECT id, title, originator, thread_source, archived, "
            "updated_at, updated_at_ms, recency_at, recency_at_ms "
            "FROM threads"
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[ThreadRow] = []
    for row in rows:
        tid = row["id"]
        if not tid:
            continue
        updated = (
            to_unix(row["updated_at_ms"])
            or to_unix(row["recency_at_ms"])
            or to_unix(row["updated_at"])
            or to_unix(row["recency_at"])
        )
        out.append(
            ThreadRow(
                id=str(tid),
                title=row["title"] or "",
                originator=row["originator"] or "",
                thread_source=row["thread_source"] or "",
                archived=int(row["archived"] or 0),
                updated_unix=updated,
            )
        )
    return out


def _load_receipts(conn: sqlite3.Connection) -> set[str]:
    unread: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT thread_id, first_unread_turn FROM thread_read_receipts"
        ).fetchall()
    except sqlite3.Error:
        return unread
    for row in rows:
        tid = row["thread_id"]
        mark = row["first_unread_turn"]
        if tid and mark is not None and str(mark).strip():
            unread.add(str(tid))
    return unread


def _load_edges(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """child_id -> parent_id is inverted to parent -> children."""
    children: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
        ).fetchall()
    except sqlite3.Error:
        return children
    for row in rows:
        parent, child = row["parent_thread_id"], row["child_thread_id"]
        if not parent or not child:
            continue
        children.setdefault(str(parent), []).append(str(child))
    return children


def _load_latest_turns(conn: sqlite3.Connection) -> dict[str, TurnRow]:
    latest: dict[str, TurnRow] = {}
    try:
        rows = conn.execute(
            "SELECT thread_id, status, started_at, completed_at, duration_ms, "
            "rollout_ordinal "
            "FROM thread_turns ORDER BY thread_id, rollout_ordinal DESC"
        ).fetchall()
    except sqlite3.Error:
        return latest
    for row in rows:
        tid = row["thread_id"]
        if not tid or tid in latest:
            continue
        duration_s = None
        raw_ms = row["duration_ms"] if "duration_ms" in row.keys() else None
        if raw_ms is not None:
            try:
                duration_s = max(0.0, float(raw_ms) / 1000.0)
            except (TypeError, ValueError):
                duration_s = None
        latest[str(tid)] = TurnRow(
            thread_id=str(tid),
            status=row["status"] or "",
            started_unix=to_unix(row["started_at"]),
            completed_unix=to_unix(row["completed_at"]),
            duration_s=duration_s,
        )
    return latest


def turn_is_stale(turn: TurnRow | None, now: float) -> bool:
    if turn is None or turn.status != "inProgress":
        return False
    anchor = turn.started_unix or turn.completed_unix
    if anchor is None:
        return True
    return (now - anchor) > STALE_RUNNING_SECONDS



def demote_stale(state: str, age_s: int) -> str:
    """Drop old blocked/ready threads so they do not bury current work."""
    if state == "blocked" and age_s > STALE_BLOCKED_SECONDS:
        return "idle"
    if state == "ready" and age_s > STALE_READY_SECONDS:
        return "idle"
    return state


def turn_is_running(turn: TurnRow | None, now: float) -> bool:
    return bool(turn and turn.status == "inProgress" and not turn_is_stale(turn, now))


def turn_duration_seconds(turn: TurnRow | None) -> float | None:
    if turn is None:
        return None
    if turn.duration_s is not None:
        return turn.duration_s
    if turn.started_unix is not None and turn.completed_unix is not None:
        return max(0.0, turn.completed_unix - turn.started_unix)
    return None


def is_flash_cli_failure(originator: str, turn: TurnRow | None) -> bool:
    """codex exec jobs that die within 30s are leftover, not a stuck chat."""
    if originator != "codex_exec":
        return False
    if turn is None or turn.status != "failed":
        return False
    duration = turn_duration_seconds(turn)
    if duration is None:
        return False
    return duration <= FLASH_CLI_FAILURE_SECONDS


def classify(
    *,
    latest: TurnRow | None,
    unread: bool,
    child_running: bool,
    needs_input: bool,
    now: float,
    originator: str = "",
) -> str:
    if needs_input:
        return "needs_input"
    if latest is not None and latest.status == "failed":
        if is_flash_cli_failure(originator, latest):
            return "idle"
        return "blocked"
    if latest is not None and latest.status == "completed" and unread:
        return "ready"
    if turn_is_running(latest, now) or child_running:
        return "running"
    return "idle"


def _activity_unix(thread: ThreadRow, turn: TurnRow | None, child_turns: Iterable[TurnRow]) -> float | None:
    times: list[float] = []
    if thread.updated_unix is not None:
        times.append(thread.updated_unix)
    if turn is not None:
        if turn.completed_unix is not None:
            times.append(turn.completed_unix)
        if turn.started_unix is not None:
            times.append(turn.started_unix)
    for child in child_turns:
        if child.completed_unix is not None:
            times.append(child.completed_unix)
        if child.started_unix is not None:
            times.append(child.started_unix)
    return max(times) if times else None


def _pick_title(thread_id: str, db_title: str, titles: dict[str, str]) -> str:
    title = titles.get(thread_id) or db_title or ""
    title = clip_title(title)
    return title or "未命名"


def snapshot(
    codex_home: Path,
    now: float,
    hook: object | None = None,
    *,
    titles: dict[str, str] | None = None,
    needs_input_ids: set[str] | None = None,
) -> dict:
    """Return pet_* fields and a short thread list. Never raises on IO errors."""
    home = Path(codex_home).expanduser()
    result = empty_pet()
    hook_id = hook_needs_input_id(hook)
    pending = set(needs_input_ids or ())
    if hook_id:
        pending.add(hook_id)

    if titles is None:
        titles = load_thread_titles(home / "session_index.jsonl")

    state_conn = _connect(home / "state_5.sqlite")
    hist_conn = _connect(home / "thread_history_1.sqlite")
    try:
        threads = _load_threads(state_conn) if state_conn else []
        unread = _load_receipts(state_conn) if state_conn else set()
        children_of = _load_edges(state_conn) if state_conn else {}
        latest = _load_latest_turns(hist_conn) if hist_conn else {}
    finally:
        if state_conn is not None:
            state_conn.close()
        if hist_conn is not None:
            hist_conn.close()

    by_id = {t.id: t for t in threads}

    def child_is_running(parent_id: str) -> bool:
        stack = list(children_of.get(parent_id, ()))
        seen: set[str] = set()
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            if turn_is_running(latest.get(cid), now):
                return True
            stack.extend(children_of.get(cid, ()))
        return False

    def child_turns(parent_id: str) -> list[TurnRow]:
        found: list[TurnRow] = []
        stack = list(children_of.get(parent_id, ()))
        seen: set[str] = set()
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            turn = latest.get(cid)
            if turn is not None:
                found.append(turn)
            stack.extend(children_of.get(cid, ()))
        return found

    entries: list[dict] = []
    for thread in threads:
        if thread.archived:
            continue
        if not is_codex_originator(thread.originator):
            continue
        if is_child_source(thread.thread_source):
            continue
        turn = latest.get(thread.id)
        activity = _activity_unix(thread, turn, child_turns(thread.id))
        age_s = max(0, int(now - activity)) if activity is not None else 0
        state = demote_stale(
            classify(
                latest=turn,
                unread=thread.id in unread,
                child_running=child_is_running(thread.id),
                needs_input=thread.id in pending,
                now=now,
                originator=thread.originator,
            ),
            age_s,
        )
        entries.append(
            {
                "id": thread.id,
                "state": state,
                "title": _pick_title(thread.id, thread.title, titles),
                "age_s": age_s,
            }
        )

    counts = {name: 0 for name in PRIORITY}
    for item in entries:
        counts[item["state"]] += 1
    result["counts"] = counts

    entries.sort(key=lambda item: (PRIORITY[item["state"]], item["age_s"], item["id"]))
    result["threads"] = [
        {"id": e["id"], "state": e["state"], "title": e["title"], "age_s": e["age_s"]}
        for e in entries[:MAX_THREADS]
    ]

    if entries:
        focus = entries[0]
        result["pet_state"] = focus["state"]
        result["pet_label"] = PET_LABELS[focus["state"]]
        result["pet_title"] = focus["title"]
        result["pet_thread_id"] = focus["id"]
        result["pet_age_s"] = focus["age_s"]
    return result


def attach_pet(
    payload: dict,
    codex_home: Path,
    now: float,
    hook: object | None = None,
    **kwargs: object,
) -> dict:
    """Copy pet fields onto an existing /status payload."""
    payload.update(snapshot(codex_home, now, hook, **kwargs))  # type: ignore[arg-type]
    return payload
