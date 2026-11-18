"""Antigravity sidecar pane backed by the same state as the retry hooks."""
from __future__ import annotations
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import native_watchdog as native
from platform_utils import file_lock, spawn_background
import retry
import session_store

SIDECAR_ID = "ant-extend/pane"
OPEN_IN_APP = "在 Antigravity 打开会话，然后选择右侧 Overview > Extensions > ant-extend。"
CID_LIMIT = 160


def safe_text(value, limit=16000):
    return value[:limit] if isinstance(value, str) else ""


def timeline(update, recovering=False):
    """Expose only user-visible fields, never generator prompts or tool arguments."""
    patch = update.get("mainTrajectoryUpdate", {}).get("stepsUpdate", {})
    groups, current = [], None
    running = update.get("status") == native.RUNNING or recovering
    for index, step in zip(patch.get("indices", []), patch.get("steps", [])):
        kind = step.get("type", "").removeprefix("CORTEX_STEP_TYPE_")
        status = step.get("status", "").removeprefix("CORTEX_STEP_STATUS_")
        if kind == "USER_INPUT":
            user = step.get("userInput", {})
            text = " ".join(safe_text(item.get("text"), 180) for item in user.get("items", []) if isinstance(item, dict)).strip()
            text = text or safe_text(user.get("userResponse"), 180)
            # Native empty continuation is not a new user turn.
            if not text:
                continue
            current = {"id": str(index), "title": text[:100], "items": [], "active": False, "answer": ""}
            groups.append(current)
            continue
        if current is None:
            current = {"id": "earlier", "title": "本轮过程", "items": [], "active": False, "answer": ""}
            groups.append(current)
        if kind == "PLANNER_RESPONSE":
            response = step.get("plannerResponse", {})
            # These are precisely the fields rendered in the App's Thought UI.
            thought = safe_text(response.get("rawThinking") or response.get("thinking"))
            if thought:
                current["items"].append({"id": f"{index}-thought", "kind": "thought", "label": "思考摘要", "text": thought, "status": status})
            text = safe_text(response.get("response"))
            if text:
                current["items"].append({"id": f"{index}-progress", "kind": "progress", "label": "进展", "text": text, "status": status})
        elif kind not in ("SYSTEM_MESSAGE", "ERROR_MESSAGE", "CHECKPOINT", "EPHEMERAL_MESSAGE"):
            tool = step.get("metadata", {}).get("toolCall", {})
            label = safe_text(step.get("generic", {}).get("title"), 160) or safe_text(tool.get("name"), 100)
            label = label or kind.lower().replace("_", " ")
            current["items"].append({"id": str(index), "kind": "tool", "label": label, "text": "", "status": status})
    if groups:
        groups[-1]["active"] = running
    for group in groups:
        if not group["active"] and group["items"] and group["items"][-1]["kind"] == "progress":
            group["answer"] = group["items"].pop()["text"]
    return {"groups": groups, "running": running,
            "partial": bool(patch.get("indices") and min(patch["indices"]) > 0),
            "stepCount": patch.get("totalLength", 0)}


class Application:
    def __init__(self, data, token=""):
        self.data = data
        self.token = token.encode("utf-8")
        self.cache = {}
        self.cache_lock = threading.Lock()

    def state(self, conversation=""):
        settings = retry.load_settings()
        now = time.time()
        row = session_store.read(self.data, conversation) if conversation else {}
        if row:
            control = native.read_control(native.control_paths(self.data, conversation)[0])
            row["monitorActive"] = bool(control.get("armed") and now - row.get("lastCheckAt", 0) < 15)
            if row.get("phase") in ("watching", "tool_running") and not row["monitorActive"]:
                row["phase"] = "idle"
        return {
            "version": retry.package_version(),
            "now": now,
            "settings": settings,
            "enabled": retry.plugin_enabled(settings),
            "paused": (self.data / "paused").exists(),
            "session": row or None,
        }

    def detail(self, conversation):
        session = session_store.read(self.data, conversation)
        if not session.get("conversationId"):
            raise ValueError("Unknown conversation")
        if not retry.load_settings()["progressView"]:
            return {"items": [], "disabled": True}
        with self.cache_lock:
            cached = self.cache.get(conversation)
            if cached and time.monotonic()-cached[0] < 2:
                return cached[1]
            try:
                client = native.discover(conversation, 3)
                control = native.read_control(native.control_paths(self.data, conversation)[0])
                recovering = bool((control.get("recovering") or session.get("phase") in ("waiting", "resuming"))
                                  and not session_store.is_paused(self.data, conversation))
                grouped = timeline(client.state_update(conversation, 60), recovering)
                current = grouped["groups"][-1] if grouped["groups"] else None
                value = {
                    "available": True,
                    "active": grouped["running"],
                    "items": current["items"] if current else [],
                    "partial": grouped["partial"],
                }
            except (OSError, ValueError, TypeError, KeyError):
                value = {"available": False, "items": []}
            self.cache = {k: v for k, v in self.cache.items() if time.monotonic()-v[0] < 60}
            self.cache[conversation] = (time.monotonic(), value)
            return value

    def resume_monitors(self, conversation=None):
        # Re-arm monitoring only. Never submit a message or restart a stopped task.
        paths = list((self.data / "watch").glob("*.json"))
        for path in paths:
            try:
                control = native.read_control(path)
            except (OSError, ValueError, TypeError):
                continue
            cid = control.get("conversationId")
            if cid and control.get("armed") and (conversation is None or cid == conversation):
                spawn_background([sys.executable, "-B", str(retry.ROOT / "scripts/retry.py"), "--watch", cid],
                                 env=dict(os.environ, PLUGIN_DATA=str(self.data)))

    def action(self, payload):
        action = payload.get("action")
        cid = payload.get("conversationId")
        if cid is not None and (not isinstance(cid, str) or not 1 <= len(cid) <= CID_LIMIT
                                or not session_store.read(self.data, cid).get("conversationId")):
            raise ValueError("Unknown conversation")
        if action in ("pause", "resume"):
            paused = action == "pause"
            if cid:
                session_store.record(self.data, cid, "paused" if paused else "idle", paused=paused,
                                     nextRetryAt=None, history=True)
            elif paused:
                self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.data / "paused", os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            else:
                (self.data / "paused").unlink(missing_ok=True)
            if not paused:
                self.resume_monitors(cid)
            return {"ok": True}
        if action == "settings":
            changes = payload.get("settings")
            allowed = {"earlyRecovery", "idleTimeoutsSeconds", "maxRetries", "retryDelaysSeconds", "progressView"}
            if not isinstance(changes, dict) or set(changes) - allowed:
                raise ValueError("Unknown settings")
            with file_lock(self.data / "settings.lock"):
                candidate = dict(retry.load_settings(), **changes)
                retry.validate_settings(candidate)
                retry.atomic_write(retry.ROOT / "config.json", candidate)
            if candidate["enabled"] and candidate["earlyRecovery"]:
                self.resume_monitors()
            return {"ok": True}
        raise ValueError("Unknown action")


class Handler(BaseHTTPRequestHandler):
    server_version = "ant-extend"

    def log_message(self, *_):
        pass

    def send(self, status, value, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors https://127.0.0.1:* https://localhost:*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        token = self.server.app.token
        provided = self.headers.get("X-Sidecar-Token", "").encode("utf-8")
        return bool(token) and hmac.compare_digest(provided, token)

    def do_GET(self):
        parts = urlsplit(self.path)
        app = self.server.app
        if parts.path.startswith("/api/"):
            if not self.authorized():
                self.send(403, {"error": "请在 Antigravity 侧栏重新打开 ant-extend。"})
                return
            try:
                cid = parse_qs(parts.query).get("conversationId", [""])[0]
                if len(cid) > CID_LIMIT:
                    raise ValueError("Invalid conversation")
                if parts.path == "/api/state":
                    result = app.state(cid)
                elif parts.path == "/api/session":
                    if not cid:
                        raise ValueError("Invalid conversation")
                    result = app.detail(cid)
                else:
                    self.send(404, {"error": "Not found"})
                    return
                self.send(200, result)
            except (OSError, ValueError, KeyError, TypeError):
                self.send(400, {"error": "暂时无法读取，请稍后刷新。"})
            return
        files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        name = files.get(parts.path)
        if not name:
            self.send(404, {"error": "Not found"})
            return
        path = retry.ROOT / "web" / name
        content_type = "text/javascript" if name.endswith(".js") else mimetypes.guess_type(name)[0]
        self.send(200, path.read_bytes(), f"{content_type}; charset=utf-8")

    def do_POST(self):
        if urlsplit(self.path).path != "/api/action" or not self.authorized():
            self.send(403, {"error": "Request denied"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 16384 or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid request")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            self.send(200, self.server.app.action(payload))
        except retry.InvalidSchedule as error:
            labels = {"retryDelaysSeconds": "递增等待", "idleTimeoutsSeconds": "检测间隔"}
            self.send(400, {"error": f"{labels[error.setting]}需填写 1 至 {error.max_items} 个递增秒数，"
                                     f"每项大于 0 且不超过 {error.max_seconds} 秒。"})
        except (OSError, ValueError, KeyError, TypeError):
            self.send(400, {"error": "设置未保存，请检查间隔与次数。"})


def serve(data):
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "0"))
    token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN", "")
    if not 1 <= port <= 65535 or not token:
        raise SystemExit(OPEN_IN_APP)
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.daemon_threads = True
        server.app = Application(data, token)
        print("ant-extend pane ready", flush=True)
        server.serve_forever()
