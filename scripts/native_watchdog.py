"""Early recovery via the local Antigravity App API; never submits chat text.

Protocol cross-checked against App 2.32.1, 2.32.2 and the MIT-licensed
Antigravity-Mobility-CLI client. This implementation has no UI automation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import urllib.request

import retry
import session_store
from platform_utils import lock_fd, process_alive, spawn_background


SERVICE = "/exa.language_server_pb.LanguageServerService/"
SUPPORTED_VERSIONS = frozenset({"2.32.1", "2.32.2"})
MAX_RESPONSE = 16 * 1024 * 1024
RUNNING = "CASCADE_RUN_STATUS_RUNNING"
ACTIVE_STEP = {"CORTEX_STEP_STATUS_PENDING", "CORTEX_STEP_STATUS_RUNNING",
               "CORTEX_STEP_STATUS_GENERATING", "CORTEX_STEP_STATUS_WAITING"}
TERMINAL_STEP = {"CORTEX_STEP_STATUS_DONE", "CORTEX_STEP_STATUS_ERROR",
                 "CORTEX_STEP_STATUS_CANCELED", "CORTEX_STEP_STATUS_CANCELLED",
                 "CORTEX_STEP_STATUS_SKIPPED"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class UnsupportedHostVersion(ValueError):
    def __init__(self, version: str):
        super().__init__("Unsupported native API version")
        self.version = version


class NativeClient:
    def __init__(self, discovery: dict, timeout: float = 5):
        self.pid = discovery.get("pid")
        if not process_alive(self.pid):
            raise ValueError("Native process is no longer running")
        self.version = discovery.get("lsVersion")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Missing native API version")
        if self.version not in SUPPORTED_VERSIONS:
            raise UnsupportedHostVersion(self.version[:32])
        port = discovery.get("httpPort")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Invalid loopback port")
        token = discovery.get("csrfToken")
        if not isinstance(token, str) or not token:
            raise ValueError("Missing local authentication")
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, payload: dict, *, streaming=False):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Codeium-Csrf-Token": self.token}
        if streaming:
            raw = b"\0" + struct.pack(">I", len(raw)) + raw
            headers.update({"Content-Type": "application/connect+json",
                            "Connect-Protocol-Version": "1"})
        request = urllib.request.Request(self.base + SERVICE + method, data=raw, headers=headers)
        # No redirect following: never send a local CSRF token to another host.
        with self.opener.open(request, timeout=self.timeout) as response:
            if response.geturl() != self.base + SERVICE + method:
                raise ValueError("Unexpected native API destination")
            if streaming:
                header = read_exact(response, 5)
                flags, length = struct.unpack(">BI", header)
                if flags or length > MAX_RESPONSE:
                    raise ValueError("Unexpected streaming envelope")
                return json.loads(read_exact(response, length))
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("Native response too large")
            return json.loads(body)

    def state_update(self, conversation: str, window: int = 12) -> dict:
        meta = self.request("GetConversationMetadata", {"conversationId": conversation})
        count = int(meta.get("numSteps", 0))
        result = self.request("StreamAgentStateUpdates", {
            "conversationId": conversation,
            "initialStepsPageBounds": {"startIndex": max(0, count - window)},
            "initialGeneratorMetadatasPageBounds": {"startIndex": 2147483647},
            "initialExecutorMetadatasPageBounds": {"startIndex": 2147483647},
            "disableRehydration": True,
        }, streaming=True)
        update = result.get("update", {})
        if update.get("conversationId") != conversation:
            raise ValueError("Native conversation mismatch")
        return update

    def snapshot(self, conversation: str) -> dict:
        return summarize(self.state_update(conversation))

    def cancel(self, conversation: str) -> None:
        self.request("CancelCascadeInvocation", {
            "cascadeId": conversation, "killBackgroundTasks": False, "notifyParent": False,
        })

    def executor_count(self, conversation: str) -> int:
        result = self.request("GetCascadeTrajectoryExecutorMetadatas", {
            "cascadeId": conversation, "executorMetadataOffset": 0,
        })
        records = result.get("executorMetadata", [])
        if not isinstance(records, list):
            raise ValueError("Invalid executor history")
        return len(records)

    def frozen_config(self, conversation: str, previous_count: int) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.request("GetCascadeTrajectoryExecutorMetadatas", {
                "cascadeId": conversation, "executorMetadataOffset": previous_count,
            })
            records = result.get("executorMetadata", [])
            if not isinstance(records, list):
                raise ValueError("Invalid executor configuration")
            if records:
                last = records[-1]
                if last.get("terminationReason") != "EXECUTOR_TERMINATION_REASON_USER_CANCELED":
                    raise ValueError("Executor did not stop as requested")
                config = last.get("cascadeConfig")
                if not isinstance(config, dict) or not config:
                    raise ValueError("Missing frozen executor configuration")
                return copy.deepcopy(config)
            time.sleep(.1)
        raise ValueError("No durable canceled-executor configuration")

    def continue_silently(self, conversation: str, config: dict) -> None:
        # Same empty-input protocol as the App's native Continue action.
        # Preserve model, workspace, permissions and the completed trajectory.
        self.request("SendUserCascadeMessage", {
            "cascadeId": conversation, "items": [], "media": [], "cascadeConfig": config,
        })


def read_exact(stream, count: int) -> bytes:
    chunks = []
    while count:
        part = stream.read(count)
        if not part:
            raise ValueError("Truncated native response")
        chunks.append(part)
        count -= len(part)
    return b"".join(chunks)


def summarize(update: dict) -> dict:
    trajectory = update.get("mainTrajectoryUpdate", {})
    patch = trajectory.get("stepsUpdate", {})
    steps = patch.get("steps", [])
    indices = patch.get("indices", [])
    blocked = bool(update.get("activeHook") or update.get("backgroundTasks") or
                   update.get("pendingAgentMessages"))
    users = []
    for index, step in zip(indices, steps):
        kind = step.get("type", "")
        if kind == "CORTEX_STEP_TYPE_USER_INPUT":
            users.append(index)
        if step.get("status") not in TERMINAL_STEP and kind != "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            blocked = True
        task = step.get("taskDetails", {})
        if task.get("status") in ("TASK_STATUS_RUNNING", "TASK_STATUS_PENDING"):
            blocked = True
    # Hash actual model/tool changes, not wall clocks or network heartbeats.
    progress = hashlib.sha256(json.dumps({
        "indices": indices, "steps": steps, "total": patch.get("totalLength"),
        "lastType": trajectory.get("lastStepType"),
        "context": update.get("contextWindowUsage", {}),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"status": update.get("status"), "blocked": blocked, "progress": progress,
            "lastUserIndex": max(users, default=-1), "steps": int(patch.get("totalLength", 0)),
            "executorCount": int(trajectory.get("executorMetadatasUpdate", {}).get("totalLength", 0)),
            "trajectoryId": update.get("trajectoryId")}


def discover(conversation: str, timeout: float = 5) -> NativeClient:
    if retry.ROOT.parent.name == "plugins" and retry.ROOT.parent.parent.name == "config":
        gemini_dir = retry.ROOT.parents[2]
    else:
        gemini_dir = Path.home() / ".gemini"
    directory = gemini_dir / "antigravity" / "daemon"
    paths = sorted(directory.glob("hub_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    unsupported_host = None
    for path in paths:
        try:
            client = NativeClient(json.loads(path.read_text()), timeout)
            client.request("GetConversationMetadata", {"conversationId": conversation})
            return client
        except UnsupportedHostVersion as exc:
            if unsupported_host is None:
                unsupported_host = exc
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if unsupported_host is not None:
        raise unsupported_host
    raise ValueError("No compatible local App server")


def control_paths(data: Path, conversation: str) -> tuple[Path, Path]:
    directory = data / "watch"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(conversation.encode()).hexdigest()
    return directory / f"{key}.json", directory / f"{key}.control.lock"


def read_control(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def update_control(data: Path, conversation: str, mutate) -> dict:
    path, lock = control_paths(data, conversation)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock_fd(fd)
        value = mutate(read_control(path))
        retry.atomic_write(path, value)
        return value
    finally:
        os.close(fd)


def arm(payload: dict, data: Path) -> None:
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation:
        return
    settings = retry.load_settings()
    if not settings["enabled"] or not settings["earlyRecovery"]:
        return
    turn, _ = retry.transcript_identity(payload)
    now = time.time()
    session = session_store.begin(data, conversation, turn)
    workspaces = payload.get("workspacePaths", [])
    session_store.record(data, conversation, "watching", model=payload.get("modelName", ""),
                         workspace=Path(workspaces[0]).name if workspaces else "")
    def change(old):
        fresh = old if old.get("turn") == turn else {"attempts": session["attempts"], "startedAt": now}
        fresh.update(conversationId=conversation, turn=turn, armed=True,
                     armedAt=now, nonce=os.urandom(12).hex(), recovering=False)
        return fresh
    update_control(data, conversation, change)
    env = dict(os.environ, PLUGIN_DATA=str(data))
    # Detached worker cannot retain the synchronous hook's stdout or stdin.
    spawn_background([sys.executable, "-B", str(retry.ROOT / "scripts" / "retry.py"),
                      "--watch", conversation], env=env)


def disarm(payload: dict, data: Path) -> None:
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation:
        return
    def change(old):
        if old.get("recovering") and time.time() - old.get("recoveryStartedAt", 0) < 15:
            return old  # The watchdog itself canceled only this model wait.
        return dict(old, armed=False)
    update_control(data, conversation, change)


def enabled(settings: dict, data: Path) -> bool:
    if not settings["enabled"] or not settings["earlyRecovery"] or (data / "paused").exists():
        return False
    return retry.plugin_enabled(settings)


def event(data: Path, conversation: str, action: str, **details) -> None:
    retry.atomic_write(data / "last-native-event.json", {
        "at": time.time(), "conversationId": conversation, "action": action, **details,
    })


def recover(client, conversation: str, before: dict, data: Path, control: dict,
            settings: dict, *, sleep=time.sleep) -> bool:
    path, _ = control_paths(data, conversation)
    nonce = control["nonce"]
    current = read_control(path)
    if (current.get("nonce") != nonce or not current.get("armed") or not enabled(settings, data)
            or session_store.is_paused(data, conversation)):
        return False
    checked = client.snapshot(conversation)
    if checked["status"] != RUNNING or checked["blocked"] or checked["progress"] != before["progress"]:
        return False
    if int(control.get("attempts", 0)) >= settings["maxRetries"]:
        return False
    session_store.begin(data, conversation, control["turn"])
    reservation = session_store.reserve(data, conversation, control["turn"], settings,
                                        "watchdog", "no_progress")
    if reservation is None:
        return False
    attempts, delay = reservation["attempt"] - 1, reservation["delay"]
    def mark(old):
        if old.get("nonce") == nonce:
            old.update(recovering=True, recoveryStartedAt=time.time(), attempts=attempts + 1)
        return old
    update_control(data, conversation, mark)
    previous_executors = client.executor_count(conversation)
    # One final generation check after fetching the cancellation receipt cursor.
    if read_control(path).get("nonce") != nonce:
        return False
    client.cancel(conversation)
    event(data, conversation, "model_wait_cancelled", attempt=attempts + 1, delaySeconds=delay)
    config = client.frozen_config(conversation, previous_executors)
    session_store.record(data, conversation, "waiting", nextRetryAt=time.time()+delay)
    # Inspect changes and manual pause throughout the short backoff.
    remaining = delay
    while remaining > 0:
        if (not enabled(retry.load_settings(), data) or read_control(path).get("nonce") != nonce
                or session_store.is_paused(data, conversation)):
            return False
        interval = min(.25, remaining)
        sleep(interval)
        remaining -= interval
    after = client.snapshot(conversation)
    if after["status"] == RUNNING or after["lastUserIndex"] > before["lastUserIndex"]:
        return False
    if (read_control(path).get("nonce") != nonce or not enabled(retry.load_settings(), data)
            or session_store.is_paused(data, conversation)):
        return False
    client.continue_silently(conversation, config)
    def unmark(old):
        if old.get("nonce") == nonce:
            old.update(recovering=False, armedAt=time.time())
        return old
    update_control(data, conversation, unmark)
    event(data, conversation, "continued_without_message", attempt=attempts + 1,
          delaySeconds=delay)
    session_store.record(data, conversation, "resuming", history=True, nextRetryAt=None)
    return True


def watch(conversation: str, data: Path) -> None:
    path, _ = control_paths(data, conversation)
    lock = path.with_suffix(".worker.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    client = None
    try:
        try:
            lock_fd(fd, blocking=False)
        except BlockingIOError:
            return
        settings = retry.load_settings()
        client = discover(conversation, settings["nativeApiTimeoutSeconds"])
        nonce = ""
        signature = ""
        progressed = time.monotonic()
        idle_seen = 0
        event(data, conversation, "watching", nativeVersion=client.version)
        while True:
            settings = retry.load_settings()
            control = read_control(path)
            if not enabled(settings, data) or not control.get("armed"):
                return
            if not process_alive(client.pid):
                return
            if session_store.is_paused(data, conversation):
                session_store.record(data, conversation, "paused", nextRetryAt=None)
                return
            if control.get("nonce") != nonce:
                nonce = control["nonce"]
                signature = ""
                progressed = time.monotonic()
            snapshot = client.snapshot(conversation)
            if snapshot["status"] != RUNNING:
                # Arm runs just before the model call; allow startup to settle.
                idle_seen += 1
                if idle_seen >= 3:
                    session_store.record(data, conversation, "idle", nextRetryAt=None)
                    return
                time.sleep(settings["pollIntervalSeconds"])
                continue
            idle_seen = 0
            if snapshot["progress"] != signature or snapshot["blocked"]:
                signature = snapshot["progress"]
                progressed = time.monotonic()
            attempts = int(control.get("attempts", 0))
            shared = session_store.read(data, conversation)
            attempts = max(attempts, int(shared.get("attempts", 0)))
            if attempts >= settings["maxRetries"]:
                event(data, conversation, "early_recovery_limit_reached", attempt=attempts)
                session_store.record(data, conversation, "limited", nextRetryAt=None, limitReason="attempts")
                return
            limits = settings["idleTimeoutsSeconds"]
            idle_limit = limits[min(attempts, len(limits) - 1)]
            session_store.record(data, conversation, "tool_running" if snapshot["blocked"] else "watching",
                lastCheckAt=time.time(), idleSeconds=round(time.monotonic()-progressed, 1),
                idleLimit=idle_limit, nextRetryAt=None, nativeVersion=client.version, nativeFailure=None)
            if not snapshot["blocked"] and time.monotonic() - progressed >= idle_limit:
                if recover(client, conversation, snapshot, data, control, settings):
                    progressed = time.monotonic()
                    signature = ""
                elif read_control(path).get("nonce") == nonce:
                    # A changed stream or a user action superseded recovery.
                    return
            time.sleep(settings["pollIntervalSeconds"])
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        unsupported = isinstance(exc, UnsupportedHostVersion)
        version = exc.version if unsupported else client.version if client else None
        reason = "unsupported_version" if unsupported else "connection_or_protocol"
        event(data, conversation, "native_watch_unavailable", reason=reason, nativeVersion=version)
        session_store.record(data, conversation, "unavailable", nextRetryAt=None,
                             nativeFailure=reason, nativeVersion=version)
    finally:
        os.close(fd)
