"""Read Codex rollout JSONL and reduce it to desk-display state.

Codex writes one JSON object per line under ``~/.codex/sessions/``. The
shape has moved more than once (snake_case and camelCase, ``resets_at``
and ``resets_in_seconds``, weekly data in either the primary or the
secondary slot, ``rate_limits: null``). This parser accepts the variants
seen in CLI and ChatGPT-app rollouts and ignores lines it does not
understand.

It never reads ``auth.json`` and it does not keep prompt text, command
lines, or tool arguments.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


WEEKLY_MINUTES = 24 * 60
STALE_RUNNING_SECONDS = 30 * 60
QUOTA_HOT_PERCENT = 90.0

START_EVENTS = {"task_started", "turn_started", "task_start"}
DONE_EVENTS = {"task_complete", "turn_complete", "task_completed", "turn_completed"}
ABORT_EVENTS = {
    "turn_aborted",
    "task_aborted",
    "task_cancelled",
    "task_canceled",
    "turn_cancelled",
    "turn_canceled",
    "interrupted",
}
TOOL_BEGIN = {
    "exec_command_begin": "跑命令",
    "patch_apply_begin": "编辑文件",
    "web_search_begin": "搜索中",
    "mcp_tool_call_begin": "调用工具",
    "image_generation_begin": "生成图片",
}
TOOL_END = {
    "exec_command_end",
    "patch_apply_end",
    "web_search_end",
    "mcp_tool_call_end",
    "image_generation_end",
}
OUTPUT_ITEMS = {
    "function_call_output",
    "custom_tool_call_output",
    "tool_call_output",
    "local_shell_call_output",
    "web_search_call_output",
}
THINKING_EVENTS = {
    "agent_reasoning",
    "reasoning",
    "agent_reasoning_raw_content",
    "reasoning_content_delta",
    "agent_reasoning_section_break",
}

_PLAN_NAMES = {
    "pro": "Pro",
    "plus": "Plus",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
    "education": "Edu",
    "free": "Free",
    "go": "Go",
    "prolite": "Pro Lite",
    "proliteplan": "Pro Lite",
}


@dataclass
class Window:
    used_percent: float
    window_minutes: int | None
    resets_at: float | None


# Sub-agent rollouts are not the window the user is looking at.
# `forked_from_id` is a copied history, not a child, so it is intentionally absent.
_CHILD_THREAD_SOURCES = {"subagent", "guardian_review", "memory_consolidation"}
# The parent's own live action wins. These are the labels a spawn leaves behind
# while the real shell/edit work is only in the child rollout.
_GENERIC_ACTIVITY = {"", "思考中", "回复中"}


@dataclass
class SessionView:
    session_id: str | None = None
    parent_thread_id: str | None = None
    # Root thread id (`session_meta.session_id`). Equal to `session_id` on the
    # chatting window; on a child it names the root of that agent tree.
    root_session_id: str | None = None
    thread_source: str | None = None
    # True when source/thread_source mark a sub-agent even if the parent id
    # was not copied onto the top-level field. Also set when the first line
    # of a rollout could not be parsed, so that file cannot steal the display.
    child_rollout: bool = False
    cwd: str | None = None
    project_name: str | None = None
    client: str | None = None
    phase: str = "idle"
    detail: str = ""
    plan_raw: str | None = None
    weekly: Window | None = None
    five_hour: Window | None = None
    context_used: int | None = None
    context_limit: int | None = None
    turn_started_at: float | None = None
    turn_ended_at: float | None = None
    last_event_at: float | None = None
    rate_limit_reached: bool = False
    quota_event_at: float | None = None
    open_calls: dict = field(default_factory=dict)
    anon_seq: int = 0


@dataclass
class HookHint:
    received_at: float
    phase: str | None = None
    detail: str | None = None
    cwd: str | None = None
    session_id: str | None = None


class SessionFollower:
    """Incrementally parse one rollout file as Codex appends to it."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.buf = b""
        self.view = SessionView()

    def sync(self, path: Path) -> SessionView:
        path = Path(path)
        if self.path != path:
            self.path = path
            self.offset = 0
            self.buf = b""
            self.view = SessionView()
        size = path.stat().st_size
        if size < self.offset:
            self.path = path
            self.offset = 0
            self.buf = b""
            self.view = SessionView()
        with path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        if chunk:
            self.buf += chunk
        if len(self.buf) > 8_000_000 and b"\n" not in self.buf:
            self.buf = b""
            return self.view
        while True:
            newline = self.buf.find(b"\n")
            if newline < 0:
                break
            raw = self.buf[:newline]
            self.buf = self.buf[newline + 1 :]
            apply_line(self.view, raw.decode("utf-8", "replace"))
        pending = self.buf.strip()
        if pending.startswith(b"{") and pending.endswith(b"}"):
            try:
                json.loads(pending.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                pass
            else:
                apply_line(self.view, pending.decode("utf-8", "replace"))
                self.buf = b""
        return self.view


def parse_file(path: Path) -> SessionView:
    return SessionFollower().sync(path)


def parse_jsonl(text: str, view: SessionView | None = None) -> SessionView:
    view = view or SessionView()
    for line in text.splitlines():
        apply_line(view, line)
    return view


def apply_line(view: SessionView, line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(record, dict):
        return
    timestamp = parse_timestamp(record.get("timestamp"))
    if timestamp is not None:
        view.last_event_at = timestamp
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    record_type = record.get("type")
    if record_type == "session_meta":
        apply_session_meta(view, payload)
        return
    if record_type == "turn_context":
        apply_cwd(view, payload.get("cwd"))
        return
    if record_type == "token_usage_record":
        apply_usage_blob(view, payload, timestamp)
        return
    if record_type == "event_msg":
        apply_event(view, payload, timestamp)
        return
    if record_type == "response_item":
        apply_response_item(view, payload, timestamp)
        return
    if record_type in {"token_count", "rate_limits"}:
        apply_token_count(view, payload or record, timestamp)


def _text(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and cleaned.lower() not in {"null", "none"}:
            return cleaned
    return None


def parent_from_source(source: object) -> str | None:
    """Parent thread id stored under ``source.subagent.thread_spawn``.

    Older Codex builds put lineage only there. ``forked_from_id`` is not a
    parent: a forked chat is still a window the user can be looking at.
    """
    if not isinstance(source, dict):
        return None
    sub = source.get("subagent")
    if sub is None:
        sub = source.get("sub_agent")
    if not isinstance(sub, dict):
        return None
    spawn = sub.get("thread_spawn")
    if spawn is None:
        spawn = sub.get("threadSpawn")
    if not isinstance(spawn, dict):
        return None
    return _text(spawn.get("parent_thread_id")) or _text(spawn.get("parentThreadId"))


def source_marks_child(source: object) -> bool:
    if isinstance(source, dict):
        return any(key in source for key in ("subagent", "sub_agent", "internal"))
    if isinstance(source, str):
        lowered = source.strip().lower()
        return lowered.startswith("subagent_") or lowered.startswith("internal_")
    return False


def apply_session_meta(view: SessionView, payload: dict) -> None:
    thread_id = _text(payload.get("id"))
    root_id = _text(payload.get("session_id")) or _text(payload.get("sessionId"))
    if thread_id:
        view.session_id = thread_id
    elif root_id:
        view.session_id = root_id
    view.root_session_id = root_id or view.session_id
    source = payload.get("source")
    parent = _text(payload.get("parent_thread_id")) or _text(payload.get("parentThreadId"))
    if not parent:
        parent = parent_from_source(source)
    # A child thread's `session_id` is the root chat. The root's own file
    # has `session_id == id` (or no separate session_id at all).
    if not parent and thread_id and root_id and thread_id != root_id:
        parent = root_id
    if parent:
        view.parent_thread_id = parent
    thread_source = _text(payload.get("thread_source")) or _text(payload.get("threadSource"))
    if thread_source:
        view.thread_source = thread_source
        if thread_source.lower() in _CHILD_THREAD_SOURCES:
            view.child_rollout = True
    if source_marks_child(source):
        view.child_rollout = True
    if isinstance(source, str) and source.strip():
        view.client = source.strip()
    else:
        originator = _text(payload.get("originator"))
        if originator:
            view.client = originator
    apply_cwd(view, payload.get("cwd") or payload.get("workspace"))


def is_child_session(view: SessionView) -> bool:
    """True for sub-agent / guardian / internal rollouts, not the chatting window.

    ``inter_agent_communication_metadata`` is not a signal. Both the parent
    and the child record those deliveries, so a main chat that receives a
    sub-agent reply would be misread as a child.
    """
    if view.parent_thread_id:
        return True
    return view.child_rollout


def child_belongs_to(child: SessionView, main: SessionView) -> bool:
    """Whether ``child`` was spawned under the chatting window ``main``."""
    main_id = main.session_id
    if not main_id or not is_child_session(child):
        return False
    if child.session_id == main_id:
        return False
    if child.parent_thread_id == main_id:
        return True
    if child.root_session_id == main_id:
        return True
    return False


def overlay_child_progress(main: SessionView, children: list[SessionView]) -> SessionView:
    """Show a sub-agent's current action without taking its context or quota.

    The chatting window's own phase stays in charge. Completion and
    interruption are never replaced by a child that is still writing.
    A concrete child action (跑命令, 编辑文件) fills in only while the
    main turn is running and has not itself opened a specific tool.
    """
    if main.phase != "running" or main.detail not in _GENERIC_ACTIVITY:
        return main
    best: SessionView | None = None
    best_at = -1.0
    for child in children:
        if child.phase != "running" or not child_belongs_to(child, main):
            continue
        if not child.detail or child.detail in _GENERIC_ACTIVITY:
            continue
        stamp = child.last_event_at if child.last_event_at is not None else -1.0
        if best is None or stamp >= best_at:
            best = child
            best_at = stamp
    if best is None:
        return main
    shown = copy.copy(main)
    shown.detail = best.detail
    return shown


_HEAD_LINE_CAP = 8_000_000
_HEAD_LINE_COUNT = 8


def read_session_head(path: Path) -> SessionView:
    """Read only the leading ``session_meta`` record.

    The meta line is stable for a rollout path and can be large because it
    embeds base instructions. Callers use it to decide which file is the
    chatting window without parsing the whole sub-agent transcript.
    """
    view = SessionView()
    with path.open("rb") as handle:
        for _ in range(_HEAD_LINE_COUNT):
            raw = handle.readline(_HEAD_LINE_CAP)
            if not raw:
                break
            if len(raw) >= _HEAD_LINE_CAP and not raw.endswith((b"\n", b"\r")):
                view.child_rollout = True
                return view
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                apply_session_meta(view, payload)
            return view
    return view


def apply_cwd(view: SessionView, cwd: object) -> None:
    if not isinstance(cwd, str):
        return
    cleaned = cwd.strip()
    if not cleaned:
        return
    view.cwd = cleaned
    view.project_name = project_name(cleaned)


def project_name(cwd: str) -> str:
    name = Path(cwd.rstrip("/\\")).name
    if not name or name in {".", ".."}:
        return "Codex"
    return name


def apply_event(view: SessionView, payload: dict, timestamp: float | None) -> None:
    kind = str(payload.get("type") or "")
    if kind in START_EVENTS:
        begin_turn(view, timestamp)
    elif kind in DONE_EVENTS:
        finish_turn(view, timestamp, payload, interrupted=False)
    elif kind in ABORT_EVENTS:
        finish_turn(view, timestamp, payload, interrupted=True)
    elif kind == "token_count":
        apply_token_count(view, payload, timestamp)
    elif kind in TOOL_BEGIN:
        open_tool(view, call_id(payload) or fresh_id(view), TOOL_BEGIN[kind], timestamp)
    elif kind in TOOL_END:
        close_tool(view, call_id(payload))
    elif kind in THINKING_EVENTS:
        note_thinking(view)
    elif kind == "agent_message":
        note_reply(view)
    elif kind == "user_message":
        note_user(view, timestamp)


def apply_response_item(view: SessionView, payload: dict, timestamp: float | None) -> None:
    kind = str(payload.get("type") or "")
    name = payload.get("name") if isinstance(payload.get("name"), str) else ""
    status = str(payload.get("status") or "").lower()
    identifier = call_id(payload)
    if kind in {"function_call", "custom_tool_call", "tool_call"}:
        if status in {"completed", "complete", "done"}:
            close_tool(view, identifier)
        else:
            open_tool(view, identifier or fresh_id(view), classify_tool(name), timestamp)
    elif kind in {"local_shell_call", "shell_call"}:
        open_tool(view, identifier or fresh_id(view), "跑命令", timestamp)
    elif kind == "web_search_call":
        open_tool(view, identifier or fresh_id(view), "搜索中", timestamp)
    elif kind in OUTPUT_ITEMS:
        close_tool(view, identifier)
    elif kind == "reasoning":
        note_thinking(view)
    elif kind == "message":
        role = payload.get("role")
        if role == "assistant":
            note_reply(view)
        elif role == "user":
            note_user(view, timestamp)


def begin_turn(view: SessionView, timestamp: float | None) -> None:
    view.phase = "running"
    view.detail = "思考中"
    view.turn_started_at = timestamp if timestamp is not None else view.turn_started_at
    view.turn_ended_at = None
    view.open_calls.clear()


def finish_turn(view: SessionView, timestamp: float | None, payload: dict, interrupted: bool) -> None:
    duration_ms = first_number(payload, "duration_ms", "durationMs", "elapsed_ms")
    if view.turn_started_at is not None and duration_ms is not None and duration_ms >= 0:
        view.turn_ended_at = view.turn_started_at + (duration_ms / 1000.0)
    else:
        view.turn_ended_at = timestamp if timestamp is not None else view.last_event_at
    view.phase = "interrupted" if interrupted else "complete"
    view.detail = ""
    view.open_calls.clear()


def note_user(view: SessionView, timestamp: float | None) -> None:
    if view.phase != "running":
        begin_turn(view, timestamp)


def note_thinking(view: SessionView) -> None:
    if view.phase == "running" and not view.open_calls:
        view.detail = "思考中"


def note_reply(view: SessionView) -> None:
    if view.phase == "running" and not view.open_calls:
        view.detail = "回复中"


def open_tool(view: SessionView, identifier: str, detail: str, timestamp: float | None) -> None:
    if view.phase != "running":
        begin_turn(view, timestamp)
    view.open_calls[identifier] = detail
    view.detail = detail


def close_tool(view: SessionView, identifier: str) -> None:
    if identifier and identifier in view.open_calls:
        del view.open_calls[identifier]
    elif view.open_calls and not identifier:
        view.open_calls.popitem()
    if view.phase == "running":
        if view.open_calls:
            view.detail = next(reversed(view.open_calls.values()))
        else:
            view.detail = "思考中"


def fresh_id(view: SessionView) -> str:
    view.anon_seq += 1
    return f"~{view.anon_seq}"


def call_id(payload: dict) -> str:
    for key in ("call_id", "callId", "tool_call_id", "toolCallId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def classify_tool(name: str) -> str:
    leaf = (name or "").strip().lower().split("__")[-1].replace("-", "_")
    shell = {
        "bash",
        "shell",
        "exec",
        "exec_command",
        "local_shell",
        "shell_command",
        "unified_exec",
        "command",
    }
    edit = {
        "apply_patch",
        "patch",
        "edit",
        "str_replace",
        "str_replace_editor",
        "write",
        "write_file",
    }
    search = {"web_search", "web_search_preview", "web_search_call", "search"}
    plan = {"update_plan", "plan"}
    image = {"image_generation", "generate_image", "image"}
    if leaf in shell:
        return "跑命令"
    if leaf in edit:
        return "编辑文件"
    if leaf in search or "web_search" in leaf:
        return "搜索中"
    if leaf in plan:
        return "规划中"
    if leaf in image:
        return "生成图片"
    return "调用工具"


def apply_usage_blob(view: SessionView, payload: dict, timestamp: float | None) -> None:
    apply_context(view, payload)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if usage:
        apply_context(view, {"last_token_usage": usage})
    limits = payload.get("rate_limits") or payload.get("rateLimits")
    if isinstance(limits, dict):
        apply_rate_limits(view, limits, timestamp)


def apply_token_count(view: SessionView, payload: dict, timestamp: float | None) -> None:
    if not isinstance(payload, dict):
        return
    info = payload.get("info") if isinstance(payload.get("info"), dict) else None
    if info:
        apply_context(view, info)
    elif "info" not in payload:
        apply_context(view, payload)
    limits = None
    for source in (payload, info or {}):
        if isinstance(source, dict):
            candidate = source.get("rate_limits")
            if candidate is None:
                candidate = source.get("rateLimits")
            if isinstance(candidate, dict):
                limits = candidate
    if limits:
        apply_rate_limits(view, limits, timestamp)


def apply_context(view: SessionView, container: dict) -> None:
    if not isinstance(container, dict):
        return
    limit = first_number(
        container,
        "model_context_window",
        "modelContextWindow",
        "context_window",
        "contextWindow",
        "model_context_tokens",
        "max_context_tokens",
        "context_limit",
        "context_window_size",
    )
    used = first_number(
        container,
        "tokens_in_context",
        "context_tokens_used",
        "context_used",
        "contextTokensUsed",
    )
    usage = None
    for key in ("last_token_usage", "lastTokenUsage", "usage"):
        if isinstance(container.get(key), dict):
            usage = container[key]
            break
    if used is None and usage is not None:
        used = usage_total(usage)
    if limit is not None and limit > 0:
        view.context_limit = int(limit)
    if used is not None and used >= 0:
        view.context_used = int(used)


def usage_total(usage: dict) -> float | None:
    total = first_number(usage, "total_tokens", "totalTokens")
    if total is not None:
        return total
    incoming = first_number(usage, "input_tokens", "inputTokens")
    outgoing = first_number(usage, "output_tokens", "outputTokens")
    if incoming is None and outgoing is None:
        return None
    return (incoming or 0) + (outgoing or 0)


def apply_rate_limits(view: SessionView, limits: dict, timestamp: float | None) -> None:
    primary = coerce_window(limits.get("primary"), timestamp)
    secondary = coerce_window(limits.get("secondary"), timestamp)
    if primary is None and secondary is None:
        return
    weekly, five_hour = classify_windows(primary, secondary)
    view.weekly = weekly
    view.five_hour = five_hour
    plan = first_text(limits, "plan_type", "planType", "plan")
    if plan:
        view.plan_raw = plan
    reached = limit_reached(limits)
    if (weekly and weekly.used_percent >= 100) or (five_hour and five_hour.used_percent >= 100):
        reached = True
    view.rate_limit_reached = reached
    if timestamp is not None:
        view.quota_event_at = timestamp
    elif view.quota_event_at is None:
        view.quota_event_at = view.last_event_at


def coerce_window(value: object, timestamp: float | None) -> Window | None:
    if not isinstance(value, dict):
        return None
    percent = first_number(value, "used_percent", "usedPercent", "percent_used", "percent")
    if percent is None:
        return None
    minutes = first_number(
        value,
        "window_minutes",
        "windowDurationMins",
        "window_duration_mins",
        "windowMinutes",
    )
    resets = first_number(value, "resets_at", "resetsAt", "reset_at", "resetAt")
    if resets is None:
        resets_in = first_number(value, "resets_in_seconds", "resetsInSeconds", "reset_in_seconds")
        if resets_in is not None and timestamp is not None:
            resets = timestamp + resets_in
    minutes_i = int(round(minutes)) if minutes is not None else None
    resets_f = float(resets) if resets is not None else None
    return Window(float(percent), minutes_i, resets_f)


def classify_windows(primary: Window | None, secondary: Window | None) -> tuple[Window | None, Window | None]:
    weekly = None
    five_hour = None
    unknown: list[Window] = []
    for window in (primary, secondary):
        if window is None:
            continue
        minutes = window.window_minutes
        if minutes is None:
            unknown.append(window)
        elif minutes >= WEEKLY_MINUTES:
            weekly = window
        elif minutes >= 12 * 60:
            weekly = window
        else:
            five_hour = window
    for window in unknown:
        if weekly is None:
            weekly = window
        elif five_hour is None:
            five_hour = window
    return weekly, five_hour


def limit_reached(limits: dict) -> bool:
    flag = limits.get("rate_limit_reached_type", limits.get("rateLimitReachedType"))
    if isinstance(flag, str):
        return flag.strip().lower() not in {"", "null", "none"}
    return bool(flag)


def first_number(obj: object, *keys: str) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        if key not in obj or obj[key] is None:
            continue
        try:
            return float(obj[key])
        except (TypeError, ValueError):
            continue
    return None


def first_text(obj: dict, *keys: str) -> str | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned.lower() not in {"null", "none"}:
                return cleaned
    return None


def parse_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return number
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def display_plan(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower().replace("-", "").replace(" ", "").replace("_", "")
    if key in _PLAN_NAMES:
        return _PLAN_NAMES[key]
    return raw.strip()[:16]


def format_reset(resets_at: float | None, now: float) -> str | None:
    if resets_at is None:
        return None
    seconds = resets_at - now
    if seconds < 30:
        return "即将重置"
    if seconds < 3600:
        minutes = max(1, int(round(seconds / 60)))
        return f"约 {minutes} 分钟后重置"
    if seconds < 48 * 3600:
        hours = max(1, int(round(seconds / 3600)))
        if hours >= 36:
            return "约 2 天后重置"
        return f"约 {hours} 小时后重置"
    days = max(1, int(round(seconds / 86400)))
    return f"约 {days} 天后重置"


def format_compact(value: int) -> str:
    number = int(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000:
        scaled = number / 1_000_000
        return sign + _trim(scaled, 1 if scaled < 10 else 0) + "M"
    if number >= 1000:
        scaled = number / 1000.0
        digits = 0 if scaled >= 100 else 1
        return sign + _trim(scaled, digits) + "k"
    return sign + str(number)


def _trim(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_elapsed(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def percent_ratio(used: int | None, limit: int | None) -> float | None:
    if used is None or limit is None or limit <= 0:
        return None
    return max(0.0, used / limit * 100.0)


def round_percent(value: float | None) -> int | None:
    if value is None:
        return None
    return int(math.floor(value + 0.5))


def quota_is_hot(view: SessionView) -> bool:
    if view.rate_limit_reached:
        return True
    if view.weekly is not None and view.weekly.used_percent >= QUOTA_HOT_PERCENT:
        return True
    return False


def status_fields(phase: str, detail: str, hot: bool) -> tuple[str, str, str]:
    """Return status, Chinese label, and color name."""
    if phase == "interrupted":
        return "interrupted", "已中断", "red"
    if phase == "running":
        activity = detail or "思考中"
        if hot:
            return "running", f"额度将尽 · {activity}", "red"
        return "running", f"执行中 · {activity}", "orange"
    if phase == "complete":
        if hot:
            return "complete", "额度将尽", "red"
        return "complete", "已完成", "green"
    if hot:
        return "quota", "额度将尽", "red"
    return "idle", "等待中", "green"


def turn_elapsed(view: SessionView, now: float, phase: str) -> float:
    if phase == "running" and view.turn_started_at is not None:
        return max(0.0, now - view.turn_started_at)
    if view.turn_started_at is not None and view.turn_ended_at is not None:
        return max(0.0, view.turn_ended_at - view.turn_started_at)
    if view.turn_started_at is not None and phase != "idle":
        return max(0.0, now - view.turn_started_at)
    return 0.0


def overlay_hook(view: SessionView, hook: HookHint | None) -> tuple[SessionView, bool]:
    if hook is None or hook.phase is None:
        return view, False
    if view.last_event_at is not None and hook.received_at <= view.last_event_at + 0.25:
        return view, False
    merged = copy.copy(view)
    merged.phase = hook.phase
    if hook.detail:
        merged.detail = hook.detail
    elif hook.phase != "running":
        merged.detail = ""
    if hook.phase == "running" and view.phase != "running":
        merged.turn_started_at = hook.received_at
        merged.turn_ended_at = None
        merged.open_calls = {}
    elif hook.phase == "complete" and view.phase == "running":
        merged.turn_ended_at = hook.received_at
    elif hook.phase == "interrupted" and view.phase == "running":
        merged.turn_ended_at = hook.received_at
    if hook.cwd and not merged.project_name:
        apply_cwd(merged, hook.cwd)
    if hook.session_id and not merged.session_id:
        merged.session_id = hook.session_id
    return merged, True


def merge_quota(active: SessionView, donors: list[SessionView]) -> SessionView:
    best = active
    best_at = active.quota_event_at if active.weekly or active.five_hour else None
    best_score = best_at if best_at is not None else -1
    for donor in donors:
        if donor.quota_event_at is None:
            continue
        if donor.weekly is None and donor.five_hour is None:
            continue
        if donor.quota_event_at >= best_score:
            best = donor
            best_score = donor.quota_event_at
    if best is active:
        return active
    merged = copy.copy(active)
    merged.weekly = best.weekly
    merged.five_hour = best.five_hour
    merged.plan_raw = best.plan_raw or active.plan_raw
    merged.rate_limit_reached = best.rate_limit_reached
    merged.quota_event_at = best.quota_event_at
    return merged


def hint_from_hook(body: dict, now: float) -> HookHint:
    event = str(body.get("hook_event_name") or body.get("event") or "")
    tool = body.get("tool_name") if isinstance(body.get("tool_name"), str) else ""
    cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else None
    session_id = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    phase = None
    detail = None
    if event in {"UserPromptSubmit", "SessionStart", "SubagentStart"}:
        phase, detail = "running", "思考中"
    elif event in {"PreToolUse", "PermissionRequest"}:
        phase = "running"
        detail = "等待确认" if event == "PermissionRequest" else classify_tool(tool)
    elif event == "PostToolUse":
        phase, detail = "running", "思考中"
    elif event in {"Stop", "SessionEnd"}:
        phase, detail = "complete", ""
    elif event == "SubagentStop":
        # A child finishing is not the chatting window finishing.
        phase, detail = None, None
    return HookHint(now, phase, detail, cwd, session_id)


def scope_hook(hook: HookHint | None, main: SessionView, children: list[SessionView]) -> HookHint | None:
    """Keep hook updates on the chatting window.

    A matching session id applies as before. A belonging child's running
    tool can refine the label while the main turn is in progress. Child
    stop events, and any other session, are dropped so they cannot flash
    已完成 or move the display onto another rollout.
    """
    if hook is None or hook.phase is None:
        return hook
    if not hook.session_id or not main.session_id or hook.session_id == main.session_id:
        return hook
    if hook.phase != "running" or main.phase != "running" or main.detail not in _GENERIC_ACTIVITY:
        return None
    if not hook.detail or hook.detail in _GENERIC_ACTIVITY:
        return None
    known = {
        child.session_id
        for child in children
        if child.session_id and child_belongs_to(child, main)
    }
    if hook.session_id not in known:
        return None
    return HookHint(hook.received_at, "running", hook.detail, None, main.session_id)


def local_tz_offset(now: float) -> int:
    aware = datetime.fromtimestamp(now).astimezone()
    offset = aware.utcoffset()
    if offset is None:
        return 0
    return int(offset.total_seconds())


def build_payload(
    view: SessionView,
    now: float,
    *,
    tz_offset_seconds: int | None = None,
    session_file: str | None = None,
    file_mtime: float | None = None,
    hook: HookHint | None = None,
) -> dict:
    merged, hook_fresh = overlay_hook(view, hook)
    phase = merged.phase
    stale = False
    if (
        phase == "running"
        and not hook_fresh
        and file_mtime is not None
        and (now - file_mtime) > STALE_RUNNING_SECONDS
    ):
        phase = "idle"
        stale = True
    hot = quota_is_hot(merged)
    status, label, color = status_fields(phase, merged.detail, hot)
    if tz_offset_seconds is None:
        tz_offset_seconds = local_tz_offset(now)
    clock_dt = datetime.fromtimestamp(now, timezone(timedelta(seconds=tz_offset_seconds)))
    elapsed = 0.0 if stale and phase == "idle" and merged.phase == "running" else turn_elapsed(merged, now, phase)
    if stale and merged.phase == "running":
        frozen_at = file_mtime if file_mtime is not None else now
        elapsed = turn_elapsed(merged, frozen_at, "running")
    plan = display_plan(merged.plan_raw)
    weekly_text = format_reset(merged.weekly.resets_at, now) if merged.weekly else None
    five_text = format_reset(merged.five_hour.resets_at, now) if merged.five_hour else None
    context_percent = percent_ratio(merged.context_used, merged.context_limit)
    if merged.context_used is not None and merged.context_limit:
        context_text = f"{format_compact(merged.context_used)} / {format_compact(merged.context_limit)} tokens"
    else:
        context_text = None
    project = merged.project_name
    elapsed_text = format_elapsed(elapsed)
    if project:
        footer = f"{project} · 本轮已运行 {elapsed_text}"
    else:
        footer = "还没有会话"
    return {
        "ok": True,
        "has_session": bool(merged.session_id or merged.project_name or merged.last_event_at),
        "source": "session" if session_file else "none",
        "session_id": merged.session_id,
        "session_file": session_file,
        "client": merged.client,
        "updated_at": clock_dt.isoformat(timespec="seconds"),
        "unix": int(now),
        "tz_offset_seconds": int(tz_offset_seconds),
        "clock": clock_dt.strftime("%H:%M"),
        "status": status,
        "detail": merged.detail if phase == "running" else "",
        "label": label,
        "color": color,
        "project_name": project,
        "plan_name": plan,
        "weekly_used_percent": round(merged.weekly.used_percent, 2) if merged.weekly else None,
        "weekly_resets_at": int(merged.weekly.resets_at) if merged.weekly and merged.weekly.resets_at else None,
        "weekly_reset_text": weekly_text,
        "weekly_window_minutes": merged.weekly.window_minutes if merged.weekly else None,
        "five_hour_used_percent": round(merged.five_hour.used_percent, 2) if merged.five_hour else None,
        "five_hour_resets_at": int(merged.five_hour.resets_at)
        if merged.five_hour and merged.five_hour.resets_at
        else None,
        "five_hour_reset_text": five_text,
        "five_hour_window_minutes": merged.five_hour.window_minutes if merged.five_hour else None,
        "context_used_tokens": merged.context_used,
        "context_limit_tokens": merged.context_limit,
        "context_used_percent": None if context_percent is None else round(context_percent, 2),
        "context_text": context_text,
        "turn_elapsed_seconds": int(elapsed),
        "turn_started_at": int(merged.turn_started_at) if merged.turn_started_at else None,
        "turn_elapsed_text": elapsed_text,
        "rate_limit_reached": merged.rate_limit_reached,
        "stale": stale,
        "hook_fresh": hook_fresh,
        "footer": footer,
        "message": None,
    }


def empty_payload(now: float, tz_offset_seconds: int | None = None, message: str = "还没有 Codex 会话") -> dict:
    if tz_offset_seconds is None:
        tz_offset_seconds = local_tz_offset(now)
    clock_dt = datetime.fromtimestamp(now, timezone(timedelta(seconds=tz_offset_seconds)))
    return {
        "ok": True,
        "has_session": False,
        "source": "none",
        "session_id": None,
        "session_file": None,
        "client": None,
        "updated_at": clock_dt.isoformat(timespec="seconds"),
        "unix": int(now),
        "tz_offset_seconds": int(tz_offset_seconds),
        "clock": clock_dt.strftime("%H:%M"),
        "status": "idle",
        "detail": "",
        "label": "等待中",
        "color": "green",
        "project_name": None,
        "plan_name": None,
        "weekly_used_percent": None,
        "weekly_resets_at": None,
        "weekly_reset_text": None,
        "weekly_window_minutes": None,
        "five_hour_used_percent": None,
        "five_hour_resets_at": None,
        "five_hour_reset_text": None,
        "five_hour_window_minutes": None,
        "context_used_tokens": None,
        "context_limit_tokens": None,
        "context_used_percent": None,
        "context_text": None,
        "turn_elapsed_seconds": 0,
        "turn_started_at": None,
        "turn_elapsed_text": "0秒",
        "rate_limit_reached": False,
        "stale": False,
        "hook_fresh": False,
        "footer": "还没有会话",
        "message": message,
    }
