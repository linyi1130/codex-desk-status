#!/usr/bin/env python3
"""LAN sidecar that serves Codex desk-display state.

Read-only on ``~/.codex/sessions``. Credentials in ``~/.codex/auth.json``
are never opened. Bind this to the LAN and let the T-Display-S3 poll
``GET /status``. Hook POSTs are accepted only from localhost.

Run: ``python3 codex_status.py``
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import parse_session
from parse_session import (
    STALE_RUNNING_SECONDS,
    HookHint,
    SessionFollower,
    SessionView,
    build_payload,
    child_belongs_to,
    empty_payload,
    hint_from_hook,
    is_child_session,
    local_tz_offset,
    merge_quota,
    parse_file,
    read_session_head,
    thread_title_ids,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MDNS = "codex-status"
DONOR_LIMIT = 8
MAX_FRESH_PARSES = 24


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def hook_for_session(hook: HookHint | None, view: SessionView, is_focus: bool) -> HookHint | None:
    """Apply a hook only to the session it names. Anonymous hooks stay on the focus window."""
    if hook is None or hook.phase is None:
        return None
    if hook.session_id:
        if view.session_id and hook.session_id == view.session_id:
            return hook
        return None
    return hook if is_focus else None


def load_thread_titles(path: Path) -> dict[str, str]:
    """Latest sidebar name for each thread id in ``session_index.jsonl``.

    Codex appends one ``{id, thread_name, updated_at}`` line per rename.
    The newest ``updated_at`` wins. A missing timestamp, or a tie, keeps
    the later line.
    """
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


def clip_sidebar_title(title: str, limit: int = 32) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def apply_sidebar_title(payload: dict, view: SessionView, titles: dict[str, str]) -> dict:
    for thread_id in thread_title_ids(view):
        title = titles.get(thread_id)
        if title:
            payload["window_name"] = clip_sidebar_title(title)
            return payload
    return payload


class ThreadTitleIndex:
    """Reload ``session_index.jsonl`` only when the append-only file changes."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.signature: tuple[int, int] | None = None
        self.titles: dict[str, str] = {}

    def get(self, codex_home: Path) -> dict[str, str]:
        path = Path(codex_home).expanduser() / "session_index.jsonl"
        try:
            stat = path.stat()
        except OSError:
            self.path = path
            self.signature = None
            self.titles = {}
            return self.titles
        signature = (stat.st_mtime_ns, stat.st_size)
        if self.path == path and self.signature == signature:
            return self.titles
        self.path = path
        self.signature = signature
        self.titles = load_thread_titles(path)
        return self.titles


def stamp_page(payload: dict, index: int, count: int) -> dict:
    payload["page_index"] = index
    payload["page_count"] = count
    if count > 1:
        project = payload.get("project_name") or "Codex"
        elapsed = payload.get("turn_elapsed_text") or ""
        payload["footer"] = f"{project} · {index}/{count} · 本轮已运行 {elapsed}"
    return payload


class Engine:
    def __init__(self) -> None:
        self.follower = SessionFollower()
        self.cache: dict[str, tuple[int, int, SessionView]] = {}
        # session_meta is the first record and does not change when the file grows.
        self.heads: dict[str, tuple[int, SessionView]] = {}
        self.thread_titles = ThreadTitleIndex()

    def payload(
        self,
        codex_home: Path,
        now: float,
        tz_offset_seconds: int | None,
        hook: HookHint | None,
    ) -> dict:
        sessions = Path(codex_home).expanduser() / "sessions"
        files = list_session_files(sessions)
        if not files:
            message = "还没有 Codex 会话" if sessions.is_dir() else "找不到 ~/.codex/sessions"
            return stamp_page(empty_payload(now, tz_offset_seconds, message), 1, 1)
        focus_path = self.select_main(files)
        titles = self.thread_titles.get(codex_home)
        try:
            focus_view = self.follower.sync(focus_path)
        except OSError as exc:
            log(f"无法读取 {focus_path.name}: {exc}")
            return stamp_page(empty_payload(now, tz_offset_seconds, "会话文件暂时读不了"), 1, 1)
        fresh = self.fresh_views(files, focus_path, now)
        pool = self.quota_pool(files, focus_path, focus_view, fresh)
        focus_payload = self.page_payload(
            focus_view,
            focus_path,
            pool,
            now,
            tz_offset_seconds,
            hook,
            is_focus=True,
            file_mtime=self.focus_mtime(focus_path, focus_view, fresh),
            titles=titles,
        )
        # One page: the user's chatting window. Running helpers are not shown.
        # 已完成 / 已中断 stays until that same window starts another turn.
        return stamp_page(focus_payload, 1, 1)

    def select_main(self, files: list[Path]) -> Path:
        """Newest rollout that is the user's chatting window.

        Sub-agent files are often written a moment after the parent, and
        several siblings of one turn get writes within the same second.
        Following mtime alone swaps context percent between those files.
        The parent has no ``parent_thread_id``; children do. A fork
        (``forked_from_id``) is still a chatting window.
        """
        for path in files:
            try:
                view = self.head(path)
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
                continue
            if not view.session_id:
                continue
            if not is_child_session(view):
                return path
        return files[0]

    def fresh_views(
        self,
        files: list[Path],
        focus_path: Path,
        now: float,
    ) -> list[tuple[Path, SessionView, float]]:
        """Sessions touched recently enough that they might still be running."""
        found: list[tuple[Path, SessionView, float]] = []
        for path in files:
            if path == focus_path or len(found) >= MAX_FRESH_PARSES:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
                continue
            if now - mtime > STALE_RUNNING_SECONDS:
                break
            try:
                found.append((path, self.cached(path), mtime))
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
        return found

    def quota_pool(
        self,
        files: list[Path],
        focus_path: Path,
        focus_view: SessionView,
        fresh: list[tuple[Path, SessionView, float]],
    ) -> list[SessionView]:
        """Account quota from chatting windows, never from a sub-agent file."""
        pool: list[SessionView] = []
        seen: set[str] = set()

        def add(view: SessionView) -> None:
            if is_child_session(view) or not (view.weekly or view.five_hour):
                return
            key = view.session_id or str(id(view))
            if key in seen:
                return
            seen.add(key)
            pool.append(view)

        add(focus_view)
        for _, view, _ in fresh:
            add(view)
            if len(pool) >= DONOR_LIMIT:
                return pool
        if focus_view.weekly or focus_view.five_hour:
            return pool
        roots = 0
        for path in files:
            if len(pool) >= DONOR_LIMIT or roots >= DONOR_LIMIT:
                break
            if path == focus_path:
                continue
            try:
                if is_child_session(self.head(path)):
                    continue
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
                continue
            roots += 1
            try:
                add(self.cached(path))
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
        return pool

    def focus_mtime(
        self,
        path: Path,
        view: SessionView,
        fresh: list[tuple[Path, SessionView, float]],
    ) -> float | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if view.phase != "running":
            return mtime
        for _, child, child_mtime in fresh:
            if child.phase == "running" and child_belongs_to(child, view):
                mtime = max(mtime, child_mtime)
        return mtime

    def page_payload(
        self,
        view: SessionView,
        path: Path,
        pool: list[SessionView],
        now: float,
        tz_offset_seconds: int | None,
        hook: HookHint | None,
        is_focus: bool,
        file_mtime: float | None,
        titles: dict[str, str],
    ) -> dict:
        shown = view if (view.weekly or view.five_hour) else merge_quota(view, pool)
        payload = build_payload(
            shown,
            now,
            tz_offset_seconds=tz_offset_seconds,
            session_file=path.name,
            file_mtime=file_mtime,
            hook=hook_for_session(hook, view, is_focus),
        )
        if payload["source"] == "session" and not payload["has_session"]:
            payload["has_session"] = True
        return apply_sidebar_title(payload, view, titles)

    def head(self, path: Path) -> SessionView:
        stat = path.stat()
        key = str(path)
        signature = (stat.st_mtime_ns, stat.st_size)
        found = self.heads.get(key)
        # Re-read when the file grows. A first look can land mid-write, before
        # session_meta is a full line; keeping that guess hid new user chats
        # until the process restarted.
        if found and found[0] == signature:
            return found[1]
        view = read_session_head(path)
        self.heads[key] = (signature, view)
        if len(self.heads) > 512:
            self.heads.clear()
            self.heads[key] = (signature, view)
        return view

    def cached(self, path: Path) -> SessionView:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        found = self.cache.get(key)
        if found and (found[0], found[1]) == signature:
            return found[2]
        view = parse_file(path)
        self.cache[key] = (signature[0], signature[1], view)
        if len(self.cache) > 64:
            self.cache.clear()
            self.cache[key] = (signature[0], signature[1], view)
        return view


def list_session_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = [path for path in root.glob("*/*/*/*.jsonl") if path.is_file()]
    if not found:
        found = [
            path
            for path in root.rglob("*.jsonl")
            if path.is_file() and not path.name.endswith(".jsonl.zst")
        ]
    found.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return found


def payload_from_fixture(path: Path, now: float, tz_offset_seconds: int | None) -> dict:
    view = parse_file(path)
    return build_payload(
        view,
        now,
        tz_offset_seconds=tz_offset_seconds,
        session_file=path.name,
        file_mtime=now,
        hook=None,
    )


def lan_ip() -> str:
    override = os.environ.get("CODEX_STATUS_ADVERTISE_IP", "").strip()
    if override:
        return override
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def client_is_local(ip: str) -> bool:
    return ip in {"127.0.0.1", "::1"}


class Service:
    def __init__(self, codex_home: Path, tz_offset: int | None) -> None:
        self.codex_home = codex_home
        self.tz_offset = tz_offset
        self.engine = Engine()
        self.lock = threading.Lock()
        self.hook: HookHint | None = None
        self.payload: dict = empty_payload(time.time(), tz_offset)
        self.wake = threading.Event()

    def publish(self) -> dict:
        with self.lock:
            hook = self.hook
            tz = self.tz_offset
        payload = self.engine.payload(self.codex_home, time.time(), tz, hook)
        with self.lock:
            self.payload = payload
        return payload

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.payload)

    def accept_hook(self, body: dict) -> None:
        hint = hint_from_hook(body, time.time())
        with self.lock:
            self.hook = hint
        try:
            self.publish()
        except Exception as exc:
            log(f"hook 刷新失败: {exc}")
        self.wake.set()


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex 桌面状态</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #1b1e27; color: #e8edf5; font: 15px/1.45 "PingFang SC", "Noto Sans SC", sans-serif; }
  main { max-width: 420px; margin: 28px auto; padding: 0 16px 32px; }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 12px; }
  .screen { width: 320px; height: 170px; background: #0b1220; border-radius: 8px; padding: 10px 14px; box-sizing: border-box; }
  .row { display: flex; justify-content: space-between; align-items: baseline; }
  .status { font-weight: 650; }
  .orange { color: #f5a524; } .green { color: #3ddc6a; } .red { color: #ff5d5d; } .gray { color: #9aa3b5; }
  .clock { color: #9aa3b5; }
  .page { color: #9aa3b5; margin-right: 8px; }
  .label { color: #e6eaf2; margin-top: 8px; }
  .pct { color: white; font-weight: 700; }
  .track { height: 12px; background: #2c3548; border-radius: 999px; margin-top: 4px; overflow: hidden; }
  .fill { height: 100%; border-radius: 999px; }
  .sub { color: #8b95a8; font-size: 12px; margin-top: 3px; }
  .foot { color: #4c9aff; font-size: 13px; margin-top: 4px; }
  .summary { color: #8b95a8; font-size: 12px; line-height: 14px; max-height: 28px; overflow: hidden; margin-top: 2px; }
  pre { background: #121722; padding: 12px; border-radius: 8px; overflow: auto; font-size: 12px; }
  p.note { color: #9aa3b5; font-size: 13px; }
</style>
</head>
<body>
<main>
  <h1>Codex 桌面状态</h1>
  <div class="screen" id="screen"></div>
  <p class="note">开发板轮询的是 <code>/status</code>。本页只是本机核对用。</p>
  <pre id="raw"></pre>
</main>
<script>
const colors = {green: "#3ddc6a", amber: "#e6b23c", red: "#ff5d5d"};
function barColor(pct) {
  if (pct == null) return "#2c3548";
  if (pct >= 85) return colors.red;
  if (pct >= 55) return colors.amber;
  return colors.green;
}
function esc(s) { return (s ?? "").replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function render(data) {
  const weekly = data.weekly_used_percent;
  const context = data.context_used_percent;
  const plan = data.plan_name ? " · " + data.plan_name : "";
  const reset = (data.weekly_reset_text || "暂无数据") + plan;
  const ctx = data.context_text || "暂无数据";
  const page = (data.page_count > 1) ? `<span class="page">${data.page_index}/${data.page_count}</span>` : "";
  const summary = data.summary ? `<div class="summary">${esc(data.summary)}</div>` : "";
  const w = weekly == null ? 0 : Math.max(0, Math.min(100, weekly));
  const c = context == null ? 0 : Math.max(0, Math.min(100, context));
  document.getElementById("screen").innerHTML = `
    <div class="row"><div class="status ${esc(data.color)}">● ${esc(data.label)}</div><div class="clock">${page}${esc(data.clock)}</div></div>
    <div class="row label"><div>本周额度</div><div class="pct">${weekly == null ? "--" : Math.round(weekly) + "%"}</div></div>
    <div class="track"><div class="fill" style="width:${w}%;background:${barColor(weekly)}"></div></div>
    <div class="sub">${esc(reset)}</div>
    <div class="row label"><div>上下文${data.window_name ? " <span class=\"sub\">" + esc(data.window_name) + "</span>" : ""}</div><div class="pct">${context == null ? "--" : Math.round(context) + "%"}</div></div>
    <div class="track"><div class="fill" style="width:${c}%;background:${barColor(context)}"></div></div>
    <div class="sub">${esc(ctx)}</div>
    ${summary}
    <div class="foot">${esc(data.footer || "")}</div>`;
  document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
}
async function tick() {
  try {
    const res = await fetch("/status", {cache: "no-store"});
    render(await res.json());
  } catch (err) {
    document.getElementById("raw").textContent = String(err);
  }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    service: Service

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/status", "/status.json", "/api/status"}:
            self._send(200, self.service.snapshot(), "application/json; charset=utf-8")
        elif path == "/health":
            self._send(200, {"ok": True}, "application/json; charset=utf-8")
        elif path in {"/", "/index.html"}:
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, {"ok": False, "message": "未知路径"}, "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/hook":
            self._send(404, {"ok": False, "message": "未知路径"}, "application/json; charset=utf-8")
            return
        if not client_is_local(self.client_address[0]):
            self._send(403, {"ok": False, "message": "hook 只接受本机"}, "application/json; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0 or length > 65536:
            self._send(413, {"ok": False, "message": "请求太大"}, "application/json; charset=utf-8")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "message": "不是 JSON"}, "application/json; charset=utf-8")
            return
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "message": "不是 JSON 对象"}, "application/json; charset=utf-8")
            return
        self.service.accept_hook(body)
        self._send(200, {"ok": True}, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        log("http " + (fmt % args))

    def _send(self, code: int, payload: dict, content_type: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def start_mdns(name: str, port: int, ip: str) -> subprocess.Popen | None:
    if not shutil.which("dns-sd"):
        log("未找到 dns-sd，跳过 mDNS。开发板请使用 config.h 里的 SIDECAR_URL。")
        return None
    command = [
        "dns-sd",
        "-P",
        name,
        "_codexstatus._tcp",
        "local",
        str(port),
        f"{name}.local",
        ip,
        "txtvers=1",
        "path=/status",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"mDNS 注册失败: {exc}")
        return None
    time.sleep(0.2)
    if process.poll() is not None:
        log("dns-sd 已退出，mDNS 名称可能不可用。")
        return None
    log(f"mDNS  {name}.local:{port}  →  {ip}")
    return process


def serve(codex_home: Path, host: str, port: int, mdns_name: str, poll_seconds: float, tz_offset: int | None) -> int:
    service = Service(codex_home, tz_offset)
    service.publish()
    handler = type("BoundHandler", (Handler,), {"service": service})
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        log(f"无法监听 {host}:{port}: {exc}")
        return 1
    server.daemon_threads = True
    ip = lan_ip()
    actual_port = server.server_address[1]
    log(f"读取  {codex_home / 'sessions'}")
    log(f"监听  http://{host}:{actual_port}/status")
    log(f"本机  http://127.0.0.1:{actual_port}/")
    if ip != "127.0.0.1":
        log(f"局域网 http://{ip}:{actual_port}/status")
    mdns = start_mdns(mdns_name, actual_port, ip)
    stop = threading.Event()

    def request_stop(signum, _frame) -> None:
        log(f"收到信号 {signum}，正在退出")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
    thread.start()

    def poll() -> None:
        while not stop.is_set():
            try:
                service.publish()
            except Exception as exc:  # keep the desk display up if one file is odd
                log(f"刷新失败: {exc}")
            service.wake.wait(poll_seconds)
            service.wake.clear()

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        server.shutdown()
        if mdns is not None and mdns.poll() is None:
            mdns.terminate()
        thread.join(timeout=2)
    return 0


def parse_now(text: str | None) -> float:
    if not text:
        return time.time()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把本机 Codex 会话状态提供给 T-Display-S3")
    parser.add_argument("--once", action="store_true", help="打印一次 JSON 后退出")
    parser.add_argument("--fixture", type=Path, help="解析单个 jsonl，不扫描 ~/.codex")
    parser.add_argument("--now", help="配合 --fixture 的当前时间，ISO-8601")
    parser.add_argument("--tz-offset", type=int, default=None, help="时区偏移秒数，例如东八区 28800")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(os.environ["CODEX_STATUS_ENV"]) if os.environ.get("CODEX_STATUS_ENV") else ROOT / ".env")
    args = build_parser().parse_args(argv)
    tz = args.tz_offset
    if args.fixture:
        now = parse_now(args.now)
        payload = payload_from_fixture(args.fixture, now, tz)
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    if args.once:
        payload = Engine().payload(codex_home, time.time(), tz, None)
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    host = args.host or os.environ.get("CODEX_STATUS_HOST", DEFAULT_HOST)
    port = args.port if args.port is not None else int(os.environ.get("CODEX_STATUS_PORT", DEFAULT_PORT))
    mdns_name = os.environ.get("CODEX_STATUS_MDNS_NAME", DEFAULT_MDNS).strip() or DEFAULT_MDNS
    poll = float(os.environ.get("CODEX_STATUS_POLL", "0.8"))
    return serve(codex_home, host, port, mdns_name, poll, tz)


if __name__ == "__main__":
    raise SystemExit(main())
