"""Install with native CLI; generate shell commands for the current OS/runtime."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, unquote

import retry
import session_store
import native_watchdog as native
from dashboard import OPEN_IN_APP, SIDECAR_ID
from platform_utils import spawn_background


def hook_command(interpreter, mode, windows=False):
    args = [str(interpreter), "-B", "./scripts/retry.py"] + ([mode] if mode else [])
    if windows:
        if any(c in str(interpreter) for c in ('"', '%', '\r', '\n')):
            raise ValueError("Interpreter path contains unsupported cmd.exe characters")
        return '"' + str(interpreter) + '" -B "./scripts/retry.py"' + (' '+mode if mode else '')
    return shlex.join(args)


def hooks(interpreter, windows=False):
    handlers = [("PreInvocation", "--arm", 5), ("PostInvocation", "--disarm", 5),
                ("Stop", "", retry.STOP_HOOK_TIMEOUT_SECONDS)]
    return {"ant-extend": {event: [{"type": "command", "command": hook_command(interpreter, mode, windows), "timeout": timeout}]
            for event, mode, timeout in handlers}}


def find_cli():
    candidates = [Path.home()/".gemini/bin/agy", Path.home()/".gemini/bin/agy.exe",
                  Path.home()/".local/bin/agy", Path.home()/".local/bin/agy.exe"]
    for name in ("agy", "agy.exe", "agy.cmd"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ValueError("Install the Antigravity CLI (agy) first, then run this installer again.")


def call_cli(cli, *args, capture=False):
    result = subprocess.run([cli, *map(str,args)], check=True, text=True, encoding="utf-8",
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout


def configure_app_sidecar(config_path, panel_enabled):
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    sidecars = config.setdefault("sidecars", {})
    entry = sidecars.setdefault(SIDECAR_ID, {})
    if not panel_enabled:
        entry["enabled"] = False
    elif "enabled" not in entry:
        entry["enabled"] = True
    else:
        return
    retry.atomic_write(config_path, config)


def main():
    if sys.version_info < (3,10):
        raise SystemExit("Python 3.10 or later is required.")
    cli = find_cli()
    gemini = Path.home()/".gemini"
    installed = gemini/"config/plugins/ant-extend"
    data = gemini/"antigravity/plugin_data/ant-extend"
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = gemini/"config/plugins/quiet-retry"
    legacy_data = gemini/"antigravity/plugin_data/quiet-retry"
    upgrading = installed.is_dir()
    legacy_paused = (legacy_data/"paused").exists()
    # Read CLI status before changing anything, so rollback preserves old toggles.
    before = json.loads(call_cli(cli, "plugin", "list", "--json", capture=True))
    enabled = {row["name"]: row.get("enabled", False) for row in before}
    config_path = installed/"config.json" if (installed/"config.json").exists() else old/"config.json"
    settings = dict(retry.DEFAULTS)
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        settings.update({k:v for k,v in existing.items() if k in retry.DEFAULTS})
    retry.validate_settings(settings)
    backup = data/"backups"/time.strftime("%Y%m%d-%H%M%S")
    for path in (installed, old):
        if path.is_dir():
            shutil.copytree(path, backup/path.name, ignore=shutil.ignore_patterns("__pycache__", ".runtime"))
    version = retry.package_version()
    migration = {"at":time.time(), "previousPluginStates":enabled, "backup":str(backup), "version":version}
    with tempfile.TemporaryDirectory(prefix="ant-extend-install-") as temporary:
        staged = Path(temporary)/"ant-extend"
        shutil.copytree(retry.ROOT, staged, ignore=shutil.ignore_patterns(".runtime", "__pycache__", "*.pyc", ".git"))
        retry.atomic_write(staged/"hooks.json", hooks(sys.executable, os.name=="nt"))
        retry.atomic_write(staged/"config.json", settings)
        sidecar_path = staged / "sidecars" / "pane" / "sidecar.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar.update(command=sys.executable, args=["-B", "../../scripts/retry.py", "--sidecar"])
        retry.atomic_write(sidecar_path, sidecar)
        call_cli(cli, "plugin", "validate", "--strict", staged)
        call_cli(cli, "plugin", "install", staged)
        # Complete the native validation before retiring either predecessor.
        call_cli(cli, "plugin", "validate", "--strict", installed)
        call_cli(cli, "plugin", "enable", "ant-extend")
        for name in ("quiet-retry", "progress-updates"):
            if enabled.get(name):
                call_cli(cli, "plugin", "disable", name)
    configure_app_sidecar(gemini / "config" / "config.json", settings["panelEnabled"])
    # Old in-flight frozen hooks must also become inert.
    if old.exists():
        legacy_data.mkdir(parents=True, exist_ok=True)
        (legacy_data/"paused").touch(mode=0o600)
    # A disabled predecessor has a pause marker from migration, not user intent.
    if not upgrading and enabled.get("quiet-retry") and legacy_paused:
        (data/"paused").touch(mode=0o600)
    migrated = 0
    if enabled.get("quiet-retry"):
        for path in (legacy_data/"watch").glob("*.json"):
            try:
                control = native.read_control(path)
            except (OSError, ValueError, TypeError):
                continue
            cid, turn = control.get("conversationId"), control.get("turn")
            if not cid or not turn or session_store.read(data, cid).get("conversationId"):
                continue
            session_store.begin(data, cid, turn, control.get("startedAt"))
            attempts = int(control.get("attempts", 0))
            workspace = ""
            try:
                client = native.discover(cid, 2)
                meta = client.request("GetConversationMetadata", {"conversationId":cid}).get("metadata", {})
                uris = meta.get("workspaceUris", [])
                if uris:
                    workspace = unquote(urlsplit(uris[0]).path).rstrip("/").rsplit("/",1)[-1]
            except (OSError, ValueError, TypeError, KeyError):
                pass
            session_store.record(data, cid, "watching", attempts=attempts,
                                 totalAttempts=attempts, source="migrated", history=True, workspace=workspace)
            if control.get("armed") and not control.get("recovering") and not legacy_paused:
                control.update(nonce=os.urandom(12).hex(), recovering=False)
                retry.atomic_write(native.control_paths(data, cid)[0], control)
                spawn_background([sys.executable,"-B",str(installed/"scripts/retry.py"),"--watch",cid],
                                 env=dict(os.environ, PLUGIN_DATA=str(data)))
            migrated += 1
    migration["migratedSessions"] = migrated
    retry.atomic_write(data/"migration.json", migration)
    print(f"ant-extend {version} installed. {OPEN_IN_APP}")


if __name__ == "__main__":
    main()
