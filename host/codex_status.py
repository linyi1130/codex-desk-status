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
    overlay_child_progress,
    parse_file,
    read_session_head,
    scope_hook,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MDNS = "codex-status"
DONOR_LIMIT = 8


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


class Engine:
    def __init__(self) -> None:
        self.follower = SessionFollower()
        self.cache: dict[str, tuple[int, int, SessionView]] = {}
        # session_meta is the first record and does not change when the file grows.
        self.heads: dict[str, tuple[int, SessionView]] = {}

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
            return empty_payload(now, tz_offset_seconds, message)
        active = self.select_main(files)
        try:
            view = self.follower.sync(active)
        except OSError as exc:
            log(f"无法读取 {active.name}: {exc}")
            return empty_payload(now, tz_offset_seconds, "会话文件暂时读不了")
        donors, children = self.companions(files, active, view)
        merged = merge_quota(view, donors)
        child_views = [child for _, child in children]
        shown = overlay_child_progress(merged, child_views)
        payload = build_payload(
            shown,
            now,
            tz_offset_seconds=tz_offset_seconds,
            session_file=active.name,
            file_mtime=self.display_mtime(active, shown, children),
            hook=scope_hook(hook, view, child_views),
        )
        if payload["source"] == "session" and not payload["has_session"]:
            payload["has_session"] = True
        return payload

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
                if not is_child_session(self.head(path)):
                    return path
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
        return files[0]

    def companions(
        self,
        files: list[Path],
        main_path: Path,
        main_view: SessionView,
    ) -> tuple[list[SessionView], list[tuple[Path, SessionView]]]:
        """Quota donors are other chatting windows. Children only hint progress.

        Donors stay capped at ``DONOR_LIMIT`` non-child files, skipping
        sub-agent rollouts so a burst of child writes cannot crowd out the
        session that actually has rate-limit numbers.
        """
        donors: list[SessionView] = []
        if main_view.weekly or main_view.five_hour:
            donors.append(main_view)
        children: list[tuple[Path, SessionView]] = []
        main_has_quota = bool(main_view.weekly or main_view.five_hour)
        roots_seen = 0
        for path in files:
            if path == main_path:
                continue
            try:
                head = self.head(path)
            except OSError as exc:
                log(f"跳过 {path.name}: {exc}")
                continue
            if is_child_session(head):
                if len(children) >= DONOR_LIMIT or not child_belongs_to(head, main_view):
                    continue
                try:
                    children.append((path, self.cached(path)))
                except OSError as exc:
                    log(f"跳过 {path.name}: {exc}")
                continue
            roots_seen += 1
            if not main_has_quota and len(donors) < DONOR_LIMIT:
                try:
                    donors.append(self.cached(path))
                except OSError as exc:
                    log(f"跳过 {path.name}: {exc}")
            if roots_seen >= DONOR_LIMIT and (main_has_quota or len(donors) >= DONOR_LIMIT):
                break
        return donors, children

    def display_mtime(
        self,
        main_path: Path,
        shown: SessionView,
        children: list[tuple[Path, SessionView]],
    ) -> float | None:
        try:
            mtime = main_path.stat().st_mtime
        except OSError:
            return None
        if shown.phase != "running":
            return mtime
        for path, child in children:
            if child.phase != "running":
                continue
            try:
                mtime = max(mtime, path.stat().st_mtime)
            except OSError:
                continue
        return mtime

    def head(self, path: Path) -> SessionView:
        stat = path.stat()
        key = str(path)
        found = self.heads.get(key)
        # The leading session_meta record is stable while the file grows.
        if found and stat.st_size >= found[0]:
            return found[1]
        view = read_session_head(path)
        self.heads[key] = (stat.st_size, view)
        if len(self.heads) > 512:
            self.heads.clear()
            self.heads[key] = (stat.st_size, view)
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
  .label { color: #e6eaf2; margin-top: 8px; }
  .pct { color: white; font-weight: 700; }
  .track { height: 12px; background: #2c3548; border-radius: 999px; margin-top: 4px; overflow: hidden; }
  .fill { height: 100%; border-radius: 999px; }
  .sub { color: #8b95a8; font-size: 12px; margin-top: 3px; }
  .foot { color: #4c9aff; font-size: 13px; margin-top: 8px; }
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
  const w = weekly == null ? 0 : Math.max(0, Math.min(100, weekly));
  const c = context == null ? 0 : Math.max(0, Math.min(100, context));
  document.getElementById("screen").innerHTML = `
    <div class="row"><div class="status ${esc(data.color)}">● ${esc(data.label)}</div><div class="clock">${esc(data.clock)}</div></div>
    <div class="row label"><div>本周额度</div><div class="pct">${weekly == null ? "--" : Math.round(weekly) + "%"}</div></div>
    <div class="track"><div class="fill" style="width:${w}%;background:${barColor(weekly)}"></div></div>
    <div class="sub">${esc(reset)}</div>
    <div class="row label"><div>上下文</div><div class="pct">${context == null ? "--" : Math.round(context) + "%"}</div></div>
    <div class="track"><div class="fill" style="width:${c}%;background:${barColor(context)}"></div></div>
    <div class="sub">${esc(ctx)}</div>
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
