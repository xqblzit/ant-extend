"""Read the active conversation's status and host-reported token accounting."""
from __future__ import annotations

import re
import time

import native_watchdog as native


UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
ENUM = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
NEEDS_USER = {"WAITING_FOR_USER", "WAITING_FOR_CONFIRMATION", "WAITING_FOR_PERMISSION"}
ACTIVE_TOOL_TYPES = {"RUN_COMMAND", "READ_FILE", "VIEW_FILE", "EDIT_FILE", "WRITE_FILE", "GREP_SEARCH",
                     "MCP_TOOL", "MCP_TOOL_CALL", "BROWSER_ACTION", "TOOL_CALL"}
NUMERIC_LIMIT = 2**53 - 1  # Keep integers comparable through the JSON/JavaScript boundary.


def fields(value):
    return value if isinstance(value, dict) else {}


def token_count(value):
    if isinstance(value, str) and len(value) <= 16 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value <= NUMERIC_LIMIT else None


def label(value):
    if not isinstance(value, str):
        return ""
    # The pane needs item labels only. Keep them on one line and omit controls.
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]", " ", value[:600]).split())[:240]


def enum_name(value, prefix):
    return value.removeprefix(prefix) if isinstance(value, str) and ENUM.fullmatch(value) else ""


def runtime(update):
    status = enum_name(update.get("status"), "CASCADE_RUN_STATUS_")
    if status == "IDLE":
        return {"phase": "idle"}
    if status in NEEDS_USER:
        return {"phase": "approval"}
    if status != "RUNNING":
        return {"phase": "unknown"}

    patch = fields(fields(update.get("mainTrajectoryUpdate")).get("stepsUpdate"))
    active = list(zip(patch.get("indices", []), patch.get("steps", [])))
    for _, step in sorted(active, key=lambda pair: pair[0], reverse=True):
        kind = enum_name(step.get("type"), "CORTEX_STEP_TYPE_")
        state = enum_name(step.get("status"), "CORTEX_STEP_STATUS_")
        if kind == "USER_INPUT":
            break
        if state in NEEDS_USER:
            return {"phase": "approval"}
        if state in ("RUNNING", "GENERATING") and kind == "PLANNER_RESPONSE":
            return {"phase": "generating"}
        if state in ("RUNNING", "GENERATING"):
            call = fields(fields(step.get("metadata")).get("toolCall"))
            tool = label(call.get("name"))
            if tool or kind in ACTIVE_TOOL_TYPES:
                return {"phase": "tool", "tool": tool or kind.lower()}
    return {"phase": "running"}


def accounting(raw, reported_groups, *, breakdown=False):
    """Counts from the same host sample, with availability for each level."""
    if not isinstance(raw, dict):
        return None
    used = token_count(raw.get("estimatedTokensUsed"))
    if breakdown and used is None:
        used = token_count(fields(raw.get("tokenBreakdown")).get("totalTokens"))
    capacity = token_count(raw.get("maxContextTokens")) or None
    groups = []
    partial = False
    for value in reported_groups if isinstance(reported_groups, list) else []:
        if not isinstance(value, dict):
            partial = True
            continue
        item = {"type": enum_name(value.get("type"), "TOKEN_TYPE_"),
                "name": label(value.get("name")), "tokens": token_count(value.get("numTokens"))}
        partial |= item["tokens"] is None
        if breakdown:
            children = value.get("children")
            if isinstance(children, list) and children:
                items = [{"name": label(child.get("name")), "tokens": token_count(child.get("numTokens"))}
                         for child in children if isinstance(child, dict)]
                item["children"] = items
                item["partial"] = (len(items) != len(children) or any(child["tokens"] is None for child in items)
                                   or item["tokens"] is None
                                   or sum(child["tokens"] or 0 for child in items) != item["tokens"])
        groups.append(item)
    if groups:
        observed_total = sum(group["tokens"] or 0 for group in groups)
        coverage = "complete" if not partial and observed_total == used else "partial"
    else:
        coverage = "unavailable"
    return {"used": used, "limit": capacity, "groups": groups, "coverage": coverage}


def generation_index(update):
    usage = fields(update.get("contextWindowUsage"))
    if "generatorMetadataIndex" in usage:
        return token_count(usage["generatorMetadataIndex"])
    # Protobuf JSON omits the zero index. The state stream can verify that row 0 exists.
    if "lastStepIndex" in usage and "tokenGroups" in usage:
        return 0
    return None


def request_accounting(update, index):
    patch = fields(fields(update.get("mainTrajectoryUpdate")).get("generatorMetadatasUpdate"))
    indices, records = patch.get("indices"), patch.get("generatorMetadatas")
    if not isinstance(indices, list) or not isinstance(records, list) or len(indices) != len(records):
        return None
    matches = [record for number, record in zip(indices, records) if token_count(number) == index]
    if len(matches) != 1:
        return None
    chat = fields(fields(matches[0]).get("chatModel"))
    stats = fields(fields(chat.get("chatStartMetadata")).get("contextWindowMetadata"))
    if not stats:
        return None
    groups = fields(stats.get("tokenBreakdown")).get("groups")
    return accounting(stats, groups, breakdown=True)


def project(update):
    window = update.get("contextWindowUsage")
    index = generation_index(update)
    trajectory = label(update.get("trajectoryId"))
    return {"connection": "ready", "runtime": runtime(update),
            "window": accounting(window, fields(window).get("tokenGroups")),
            "request": None, "requestRef": f"{trajectory}/{index}" if index is not None else None,
            "sampledAt": time.time()}


def read(conversation, include_request=False):
    if not isinstance(conversation, str) or not UUID.fullmatch(conversation):
        return {"connection": "no_conversation"}
    try:
        client = native.discover(conversation, 2)
        update = client.state_update(conversation, 12)
        report = project(update)
    except native.READ_ERRORS:
        return {"connection": "unavailable"}

    index = generation_index(update)
    if not include_request or index is None:
        return report
    try:
        detailed = client.state_update(conversation, 12, generator_start=index)
        report = project(detailed)
        actual = generation_index(detailed)
        if actual is not None:
            report["request"] = request_accounting(detailed, actual)
    except native.READ_ERRORS:
        pass  # Retain the valid runtime/window sample when a detail read fails.
    return report
