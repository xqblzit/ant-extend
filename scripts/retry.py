"""Native Antigravity Stop hook. No chat injection, API calls, or tool replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from platform_utils import lock_fd as acquire_lock, replace_file
import session_store


ROOT = Path(__file__).resolve().parent.parent


def package_version() -> str:
    return json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]


MAX_INPUT_BYTES = 1024 * 1024
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
MAX_RETRY_DELAY_SECONDS = 60
STOP_HOOK_TIMEOUT_SECONDS = MAX_RETRY_DELAY_SECONDS + 15
SCHEDULE_LIMITS = {
    "retryDelaysSeconds": (20, MAX_RETRY_DELAY_SECONDS),
    "idleTimeoutsSeconds": (10, 120),
}
DEFAULTS = {
    "enabled": True,
    "maxRetries": 8,
    "maxWindowSeconds": 1800,
    "retryDelaysSeconds": [10, 20, 30, 60],
    "maxRetryAfterSeconds": 30,
    "earlyRecovery": True,
    "idleTimeoutsSeconds": [120],
    "pollIntervalSeconds": 2,
    "nativeApiTimeoutSeconds": 5,
    "panelEnabled": True,
    "progressView": True,
}


class InvalidSchedule(ValueError):
    def __init__(self, setting, max_items, max_seconds):
        self.setting = setting
        self.max_items = max_items
        self.max_seconds = max_seconds
        super().__init__(f"{setting} must contain 1 to {max_items} increasing values up to {max_seconds} seconds")


# Never retry policy, credential, credit, daily-quota, or input errors even if
# the same text also contains a transient status code.
PERMANENT = re.compile(
    r"unauthenticated|unauthorized|forbidden|permission[ _-]?denied|"
    r"invalid[ _-]?(?:api[ _-]?)?key|invalid[ _-]?argument|"
    r"\b(?:401|403)\b|insufficient[ _-]?(?:quota|credits|balance)|"
    r"(?:daily|monthly|weekly|per[ -]?day|per[ -]?month).{0,60}(?:quota|limit)|"
    r"(?:quota|limit).{0,60}(?:daily|monthly|weekly|per[ -]?day|per[ -]?month)|"
    r"(?:quota|credits|balance).{0,60}(?:exhausted|depleted)|"
    r"(?:billing|payment).{0,40}(?:required|disabled|failed)|"
    r"content[ _-]?filter|safety[ _-]?(?:block|filter)|"
    r"policy[ _-]?(?:violation|denied)|context.{0,20}(?:too long|length exceeded)|"
    r"maximum context|token budget exceeded",
    re.IGNORECASE,
)
TRANSIENT = (
    ("stream_timeout", re.compile(r"stream idle timeout", re.I)),
    ("timeout", re.compile(r"context deadline exceeded|request.{0,50}timed?\s*out|"
                           r"request.{0,50}timeout|(?:connect|read|tls handshake)[ _-]?timeout|"
                           r"\bETIMEDOUT\b", re.I)),
    ("connection", re.compile(r"unexpected EOF|\bECONNRESET\b|\bECONNREFUSED\b|"
                              r"\bEAI_AGAIN\b|connection (?:reset|aborted|closed)|"
                              r"socket hang up|network is unreachable|fetch failed|failed to fetch|"
                              r"http2.{0,30}(?:stream closed|internal_error)", re.I)),
    ("service_unavailable", re.compile(r"\b50[0234]\b|\bUNAVAILABLE\b|"
                                       r"no capacity available|temporarily unavailable|"
                                       r"server (?:is )?(?:busy|overloaded)", re.I)),
    ("rate_limit", re.compile(r"\b429\b|too many requests|per[ -]?minute.{0,40}(?:limit|quota)|"
                              r"rate[ _-]?limit", re.I)),
)


def error_category(error: str) -> str | None:
    if not error or PERMANENT.search(error):
        return None
    return next((name for name, pattern in TRANSIENT if pattern.search(error)), None)


def load_settings(root: Path = ROOT) -> dict:
    settings = dict(DEFAULTS)
    candidate = root / "config.json"
    if candidate.exists():
        overrides = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            raise ValueError("Invalid configuration")
        if set(overrides) - set(DEFAULTS):
            raise ValueError("Unknown configuration keys")
        settings.update({key: overrides[key] for key in DEFAULTS if key in overrides})
    return validate_settings(settings)


def validate_settings(settings: dict) -> dict:
    for key in ("enabled", "earlyRecovery", "panelEnabled", "progressView"):
        if not isinstance(settings[key], bool):
            raise ValueError("Invalid boolean setting")
    bounds = {
        "maxRetries": (0, 20), "maxWindowSeconds": (1, 7200),
        "maxRetryAfterSeconds": (1, 30),
        "pollIntervalSeconds": (1, 5), "nativeApiTimeoutSeconds": (1, 10),
    }
    for key, (low, high) in bounds.items():
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Invalid numeric setting")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Setting out of range")
    if int(settings["maxRetries"]) != settings["maxRetries"]:
        raise ValueError("Retry count must be integral")
    for key, (max_items, max_seconds) in SCHEDULE_LIMITS.items():
        values = settings[key]
        if not isinstance(values, list) or not 1 <= len(values) <= max_items:
            raise InvalidSchedule(key, max_items, max_seconds)
        previous = 0
        for value in values:
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not previous < value <= max_seconds):
                raise InvalidSchedule(key, max_items, max_seconds)
            previous = value
    return settings


def server_retry_after(error: str) -> float | None:
    """Accept explicit delays, not arbitrary dates or numbers from an error."""
    matches = re.findall(
        r'(?:retry[ _-]?after|retryDelay|retry (?:in|after))["\']?\s*[=:]?\s*["\']?'
        r'(\d+(?:\.\d+)?)\s*(milliseconds?|ms|seconds?|s|minutes?|m)?\b',
        error, re.I
    )
    delays = []
    for value, unit in matches:
        multiplier = 0.001 if unit.lower().startswith("milli") or unit.lower() == "ms" else 1
        if unit.lower() in ("m", "minute", "minutes"):
            multiplier = 60
        delays.append(float(value) * multiplier)
    return max(delays) if delays else None


def transcript_identity(payload: dict) -> tuple[str, str]:
    """Only read runtime metadata, never interpret transcript instructions."""
    user_key = ""
    last_step = ""
    transcript = payload.get("transcriptPath")
    if isinstance(transcript, str) and transcript:
        try:
            with Path(transcript).expanduser().open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                offset = max(0, stream.tell() - MAX_TRANSCRIPT_BYTES)
                stream.seek(offset)
                if offset:
                    stream.readline()  # Discard a partial first line.
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    index = row.get("step_index", row.get("stepIndex", ""))
                    created = row.get("created_at", row.get("createdAt", ""))
                    last_step = f"{index}:{created}"
                    if row.get("type") == "USER_INPUT" and row.get("source") == "USER_EXPLICIT":
                        user_key = f"{index}:{created}"
        except OSError:
            pass
    if not user_key:
        # Older products may not expose the transcript. A changed prompt still
        # starts a new retry budget, without persisting the prompt itself.
        user_key = str(payload.get("lastUserInput", ""))
    return hashlib.sha256(user_key.encode()).hexdigest(), last_step


def atomic_write(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".write-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, separators=(",", ":"))
        replace_file(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def audit(data_dir: Path, event: dict) -> None:
    # No prompt, raw error or secret is persisted.
    atomic_write(data_dir / "last-event.json", event)


def default_data_dir() -> Path:
    if os.environ.get("PLUGIN_DATA"):
        return Path(os.environ["PLUGIN_DATA"])
    # Make manual pause/status commands address the same App data as the hook.
    if ROOT.parent.name == "plugins" and ROOT.parent.parent.name == "config":
        return ROOT.parents[2] / "antigravity" / "plugin_data" / "ant-extend"
    return ROOT / ".runtime"


def handle(payload: dict, settings: dict, data_dir: Path,
           *, now=None, sleep=time.sleep) -> dict:
    if payload.get("hookEventName") not in (None, "", "Stop"):
        return {}
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation:
        return {}
    reason = str(payload.get("terminationReason", "")).strip().upper()
    reason = reason.removeprefix("EXECUTOR_TERMINATION_REASON_")
    error = payload.get("error", "")
    if not isinstance(error, str):
        return {}
    category = error_category(error) if reason == "ERROR" else None

    # One independent lock per conversation avoids cross-conversation blocking.
    key = hashlib.sha256(conversation.encode()).hexdigest()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    states = data_dir / "states"
    states.mkdir(exist_ok=True, mode=0o700)
    state_path = states / f"{key}.json"
    lock_fd = os.open(states / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            acquire_lock(lock_fd, blocking=False)
        except BlockingIOError:
            return {}
        if not category or not settings["enabled"] or session_store.is_paused(data_dir, conversation):
            state_path.unlink(missing_ok=True)
            if not category:
                phase = "cancelled" if "CANCEL" in reason else "error" if reason == "ERROR" else "idle"
                # An internally canceled request is still being recovered.
                import native_watchdog
                control = native_watchdog.read_control(native_watchdog.control_paths(data_dir, conversation)[0])
                if not control.get("recovering"):
                    session_store.record(data_dir, conversation, phase, nextRetryAt=None)
            return {}
        now = time.time() if now is None else now
        turn_key, last_step = transcript_identity(payload)
        session_store.begin(data_dir, conversation, turn_key, now)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("turn") != turn_key:
            state = {"turn": turn_key, "attempts": 0, "firstFailureAt": now,
                     "lastDelaySeconds": 0}
        fingerprint = hashlib.sha256(json.dumps(
            [payload.get("executionNum"), last_step, error],
            ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        if state.get("lastFailure") == fingerprint:
            return {}
        retry_after = server_retry_after(error)
        if retry_after is not None and retry_after > settings["maxRetryAfterSeconds"]:
            audit(data_dir, {"at": now, "conversationId": conversation,
                             "action": "server_cooldown", "category": category,
                             "retryAfterSeconds": retry_after})
            session_store.record(data_dir, conversation, "cooldown", now=now,
                                 category=category, retryAfterSeconds=retry_after)
            return {}
        reservation = session_store.reserve(data_dir, conversation, turn_key, settings,
            "stop", category, now=now, fingerprint=fingerprint, minimum_delay=retry_after or 0)
        if reservation is None:
            row = session_store.read(data_dir, conversation)
            if row.get("turn") == turn_key and row.get("phase") == "limited":
                audit(data_dir, {"at": now, "conversationId": conversation,
                                 "action": "limit_reached", "attempt": row["attempts"],
                                 "category": category, "reason": row["limitReason"]})
            return {}
        attempt, delay = reservation["attempt"], reservation["delay"]
        state.update(attempts=attempt, lastFailure=fingerprint,
                     lastDelaySeconds=delay)
        atomic_write(state_path, state)
        audit(data_dir, {"at": now, "conversationId": conversation,
                         "action": "continue", "attempt": attempt,
                         "category": category, "delaySeconds": delay})
        remaining = delay
        while remaining > 0:
            interval = min(.25, remaining)
            sleep(interval)
            remaining -= interval
            if session_store.is_paused(data_dir, conversation):
                return {}
            if transcript_identity(payload)[0] != turn_key:
                return {}
            if not plugin_enabled(load_settings()):
                return {}
        session_store.record(data_dir, conversation, "resuming", history=True, nextRetryAt=None)
        # Deliberately omit reason/injectSteps: they can create visible rows.
        return {"decision": "continue"}
    finally:
        os.close(lock_fd)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        import install
        install.main()
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("--sidecar", "--serve", "--open", "--panel-url"):
        import dashboard
        if sys.argv[1] == "--sidecar":
            dashboard.serve(default_data_dir())
        else:
            print(dashboard.OPEN_IN_APP)
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("--arm", "--disarm", "--watch"):
        try:
            import native_watchdog
            if sys.argv[1] == "--watch" and len(sys.argv) == 3:
                native_watchdog.watch(sys.argv[2], default_data_dir())
                return
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ValueError("Input too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Invalid payload")
            if sys.argv[1] == "--arm":
                native_watchdog.arm(payload, default_data_dir())
            else:
                native_watchdog.disarm(payload, default_data_dir())
        except (OSError, ValueError, TypeError, KeyError):
            pass
        sys.stdout.write("{}\n")
        return
    if len(sys.argv) > 1:
        manage(sys.argv[1:])
        return
    result = {}
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("Input too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid payload")
        settings = load_settings()
        # Installed plugins receive PLUGIN_DATA from Antigravity itself.
        result = handle(payload, settings, default_data_dir()) if plugin_enabled(settings) else {}
    except (OSError, ValueError, TypeError, KeyError):
        # A broken hook must never strand the agent or print a chat notice.
        result = {}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")


def plugin_enabled(settings: dict) -> bool:
    if not settings["enabled"] or not (ROOT / "plugin.json").is_file():
        return False
    if ROOT.parent.name == "plugins" and ROOT.parent.parent.name == "config":
        path = ROOT.parent.parent / "config.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return config.get("plugins", {}).get("ant-extend", {}).get("enabled") is not False
    return True


def manage(arguments: list[str]) -> None:
    """Explicit diagnostic commands print to the terminal, never to a chat."""
    if arguments not in (["--status"], ["--check-config"], ["--pause"], ["--resume"]):
        sys.stderr.write("Usage: ant-extend [--open|--status|--check-config|--pause|--resume]\n")
        raise SystemExit(2)
    try:
        settings = load_settings()
        data_dir = default_data_dir()
        if arguments[0] in ("--pause", "--resume"):
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            pause = data_dir / "paused"
            if arguments[0] == "--pause":
                fd = os.open(pause, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            else:
                pause.unlink(missing_ok=True)
                import dashboard
                dashboard.Application(data_dir).resume_monitors()
        result = {"enabled": settings["enabled"],
                  "paused": (data_dir / "paused").exists(), "settings": settings}
        if arguments[0] == "--status":
            event_path = data_dir / "last-event.json"
            result["lastEvent"] = json.loads(event_path.read_text()) if event_path.exists() else None
            native_path = data_dir / "last-native-event.json"
            result["lastNativeEvent"] = json.loads(native_path.read_text()) if native_path.exists() else None
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, TypeError):
        sys.stderr.write("Invalid ant-extend configuration or state.\n")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
