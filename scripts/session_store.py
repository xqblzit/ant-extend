"""One retry budget and a bounded local activity history per user turn."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time

from platform_utils import file_lock


def paths(data: Path, conversation: str):
    directory = data / "sessions"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = hashlib.sha256(conversation.encode()).hexdigest()
    return directory / (stem + ".json"), directory / (stem + ".lock")


def read(data, conversation):
    path, _ = paths(data, conversation)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def change(data, conversation, mutate):
    import retry
    path, lock = paths(data, conversation)
    with file_lock(lock):
        row = read(data, conversation)
        result = mutate(row)
        retry.atomic_write(path, row)
        return result


def begin(data, conversation, turn, now=None):
    now = time.time() if now is None else now
    def update(row):
        if row.get("turn") != turn:
            total, events = row.get("totalAttempts", 0), row.get("events", [])
            paused = row.get("paused", False)
            row.clear()
            row.update(conversationId=conversation, turn=turn, attempts=0,
                       totalAttempts=total, startedAt=now, phase="watching",
                       events=events[-40:], paused=paused)
        row["updatedAt"] = now
        return dict(row)
    return change(data, conversation, update)


def record(data, conversation, phase, *, history=False, now=None, **fields):
    now = time.time() if now is None else now
    def update(row):
        row.update(conversationId=conversation, phase=phase, updatedAt=now, **fields)
        if history:
            event = {"at": now, "phase": phase, "attempt": row.get("attempts", 0)}
            event.update({k: fields[k] for k in ("source", "category", "delaySeconds") if k in fields})
            row["events"] = (row.get("events", []) + [event])[-40:]
        return dict(row)
    return change(data, conversation, update)


def reserve(data, conversation, turn, settings, source, category, *, now=None,
            fingerprint=None, minimum_delay=0):
    now = time.time() if now is None else now
    def update(row):
        if row.get("turn") != turn or row.get("paused"):
            return None
        if fingerprint and fingerprint == row.get("lastFailure"):
            return None
        count = row.get("attempts", 0)
        schedule = settings["retryDelaysSeconds"]
        delay = max(schedule[min(count, len(schedule)-1)], minimum_delay,
                    row.get("lastDelaySeconds", 0))
        first_recovery = row.get("firstRecoveryAt")
        if first_recovery is None:
            # Older session files only recorded the attempt times in history.
            first_recovery = next((event["at"] for event in row.get("events", [])
                                   if count and event.get("phase") == "waiting"
                                   and row["startedAt"] <= event["at"] <= now), now)
        elapsed = max(0, now - first_recovery)
        if count >= settings["maxRetries"] or elapsed + delay >= settings["maxWindowSeconds"]:
            row.update(phase="limited", updatedAt=now, nextRetryAt=None,
                       limitReason="attempts" if count >= settings["maxRetries"] else "window")
            return None
        row.update(attempts=count+1, totalAttempts=row.get("totalAttempts", 0)+1,
                   phase="waiting", updatedAt=now, nextRetryAt=now+delay,
                   firstRecoveryAt=first_recovery, lastDelaySeconds=delay,
                   source=source, category=category)
        row.pop("limitReason", None)
        if fingerprint:
            row["lastFailure"] = fingerprint
        row["events"] = (row.get("events", []) + [{"at": now, "phase": "waiting",
            "attempt": count+1, "source": source, "category": category,
            "delaySeconds": delay}])[-40:]
        return {"attempt": count+1, "delay": delay}
    return change(data, conversation, update)


def is_paused(data, conversation):
    return (data / "paused").exists() or bool(read(data, conversation).get("paused"))
