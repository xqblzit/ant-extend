"""Behavior tests using the recorded stream timeout and native hook payloads."""

import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
spec = importlib.util.spec_from_file_location("quiet_retry", PLUGIN / "scripts" / "retry.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)
TIMEOUT = "request failed: stream idle timeout (5m0s): context deadline exceeded"


class RetryBehavior(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.settings = retry.load_settings(PLUGIN)
        self.payload = {"conversationId": "fixture-conversation", "hookEventName": "Stop",
                        "terminationReason": "ERROR", "fullyIdle": True,
                        "executionNum": 0, "lastUserInput": "fixture prompt",
                        "error": TIMEOUT}

    def run_hook(self, payload=None, **kwargs):
        return retry.handle(payload or self.payload, self.settings, self.data,
                            now=kwargs.pop("now", 1000), sleep=kwargs.pop("sleep", lambda _: None),
                            **kwargs)

    def event(self):
        return json.loads((self.data / "last-event.json").read_text())

    def test_actual_stream_timeout_continues_without_chat_injection(self):
        self.assertEqual(self.run_hook(), {"decision": "continue"})
        self.assertEqual(self.event()["delaySeconds"], 10)
        persisted = "".join(p.read_text() for p in self.data.rglob("*.json"))
        self.assertNotIn(self.payload["lastUserInput"], persisted)
        self.assertNotIn(TIMEOUT, persisted)

    def test_short_increasing_delays_and_finite_budget(self):
        actual = []
        for i in range(8):
            payload = dict(self.payload, executionNum=i, continuationActive=i > 0)
            self.assertEqual(self.run_hook(payload, now=1000+i*35), {"decision": "continue"})
            actual.append(self.event()["delaySeconds"])
        self.assertEqual(actual, [10, 20, 30, 60, 60, 60, 60, 60])
        self.assertEqual(self.run_hook(dict(self.payload, executionNum=8), now=1300), {})
        self.assertEqual(self.event()["action"], "limit_reached")

    def test_stop_hook_completes_custom_sixty_second_backoff(self):
        delays = [5, 10, 20, 30, 60]
        self.settings = retry.validate_settings(dict(self.settings, maxRetries=5, retryDelaysSeconds=delays))
        for attempt, delay in enumerate(delays):
            with self.subTest(attempt=attempt + 1):
                sleeps = []
                payload = dict(self.payload, executionNum=attempt, continuationActive=attempt > 0)
                self.assertEqual(self.run_hook(payload, now=1000 + attempt * 75, sleep=sleeps.append),
                                 {"decision": "continue"})
                self.assertAlmostEqual(sum(sleeps), delay)
                self.assertEqual(self.event()["delaySeconds"], delay)

    def test_manual_stop_always_wins_even_with_stale_timeout(self):
        self.run_hook()
        for reason in ("USER_CANCELED", "USER_CANCELLED", "NO_TOOL_CALL", "MAX_INVOCATIONS",
                       "MAX_FORCED_INVOCATIONS", "MAX_TOKEN_BUDGET_EXCEEDED", "TERMINAL_CUSTOM_HOOK"):
            with self.subTest(reason=reason):
                self.assertEqual(self.run_hook(dict(self.payload, terminationReason=reason)), {})

    def test_permanent_failures_are_not_retried(self):
        errors = ["UNAUTHENTICATED: 401", "PERMISSION_DENIED: 403", "invalid API key",
                  "429: daily quota limit reached", "429: quota exhausted", "503: insufficient credits",
                  "429: limit reached for requests per day", "429: monthly quota reached",
                  "INVALID_ARGUMENT: request too large", "maximum context length exceeded",
                  "503: content filter blocked", "permission denied; request timeout"]
        for error in errors:
            with self.subTest(error=error):
                self.assertEqual(self.run_hook(dict(self.payload, error=error)), {})

    def test_transient_failures_are_recognized(self):
        for i, error in enumerate(["UNAVAILABLE (code 503): No capacity available for model",
                                    "502 Bad Gateway", "504 gateway timeout", "500 Internal Server Error",
                                    "429 too many requests per minute", "unexpected EOF", "ECONNRESET",
                                    "request timeout", "fetch failed", "TLS handshake timeout"]):
            with self.subTest(error=error):
                self.assertEqual(self.run_hook(dict(self.payload, conversationId=f"case-{i}", error=error)),
                                 {"decision": "continue"})

    def test_unrecognized_errors_fail_open(self):
        for error in ("", "unit tests failed", "file not found", "permission approval required"):
            self.assertEqual(self.run_hook(dict(self.payload, error=error)), {})

    def test_same_failure_is_not_scheduled_twice(self):
        self.assertEqual(self.run_hook(), {"decision": "continue"})
        self.assertEqual(self.run_hook(), {})
        self.assertEqual(self.event()["attempt"], 1)

    def test_identical_user_text_in_new_turn_resets_budget(self):
        transcript = self.root / "transcript.jsonl"
        def write(index):
            transcript.write_text(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT",
                                               "step_index": index, "created_at": f"time-{index}"}) + "\n")
        payload = dict(self.payload, transcriptPath=str(transcript))
        write(0)
        self.run_hook(payload)
        self.run_hook(dict(payload, executionNum=1))
        self.assertEqual(self.event()["attempt"], 2)
        write(8)
        self.run_hook(payload)
        self.assertEqual(self.event()["attempt"], 1)
        self.assertEqual(self.event()["delaySeconds"], 10)

    def test_retry_does_not_exceed_total_time_window(self):
        self.run_hook()
        self.assertEqual(self.run_hook(dict(self.payload, executionNum=1), now=2800), {})
        self.assertEqual(self.event()["action"], "limit_reached")
        self.assertEqual(self.event()["reason"], "window")

    def test_delay_is_included_in_window_budget(self):
        self.settings["maxWindowSeconds"] = 1
        self.assertEqual(self.run_hook(), {})

    def test_short_server_delay_is_honored_without_later_decrease(self):
        payload = dict(self.payload, error='503: {"retryDelay":"25s"}')
        self.assertEqual(self.run_hook(payload), {"decision": "continue"})
        self.assertEqual(self.event()["delaySeconds"], 25)
        self.run_hook(dict(self.payload, executionNum=1))
        self.assertEqual(self.event()["delaySeconds"], 25)

    def test_long_server_cooldown_is_not_bypassed(self):
        for error in ("429: Retry-After: 120", '503 {"retryDelay":"2m"}',
                      "429 Retry after 31 seconds"):
            with self.subTest(error=error):
                self.assertEqual(self.run_hook(dict(self.payload, error=error)), {})
                self.assertEqual(self.event()["action"], "server_cooldown")

    def test_server_delay_units(self):
        for value, expected in [('retryDelay: "1.5s"', 1.5), ("Retry-After: 12", 12),
                                ("Retry in 300ms", .3), ("Retry after 2 minutes", 120)]:
            with self.subTest(value=value):
                self.assertAlmostEqual(retry.server_retry_after(value), expected)

    def test_pause_during_backoff_prevents_continuation(self):
        def pause(_):
            (self.data / "paused").touch()
        self.assertEqual(self.run_hook(sleep=pause), {})

    def test_paused_plugin_does_not_retry(self):
        self.data.mkdir()
        (self.data / "paused").touch()
        self.assertEqual(self.run_hook(), {})

    def test_disabled_plugin_does_not_retry(self):
        self.settings["enabled"] = False
        self.assertEqual(self.run_hook(), {})

    def test_bad_configuration_is_rejected(self):
        for patch in ({"maxRetries": True}, {"maxRetries": -1}, {"maxRetries": 1.5},
                      {"maxWindowSeconds": float("nan")}, {"retryDelaysSeconds": []},
                      {"retryDelaysSeconds": [2, 1]}, {"retryDelaysSeconds": [1, 1]},
                      {"retryDelaysSeconds": [1, 61]}, {"retryDelaysSeconds": [False]},
                      {"idleTimeoutsSeconds": [0]}, {"idleTimeoutsSeconds": [20, 15]},
                      {"idleTimeoutsSeconds": [121]},
                      {"enabled": "true"}, {"misspelledSetting": 1}):
            with self.subTest(patch=patch):
                (self.root / "config.json").write_text(json.dumps(patch))
                with self.assertRaises(ValueError):
                    retry.load_settings(self.root)

    def test_separate_conversations_have_independent_budgets(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.run_hook(dict(self.payload, conversationId=f"parallel-{i}")),
                                    range(16)))
        self.assertEqual(results, [{"decision": "continue"}] * 16)
        self.assertEqual(len(list((self.data / "states").glob("*.json"))), 16)

    @unittest.skipIf(os.name == "nt", "Windows uses user profile ACLs, not POSIX mode bits")
    def test_runtime_state_is_private(self):
        self.run_hook()
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        for path in self.data.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_other_hook_events_are_ignored(self):
        self.assertEqual(self.run_hook(dict(self.payload, hookEventName="PreToolUse")), {})

    def test_case_and_enum_prefix_compatibility(self):
        for index, reason in enumerate(("error", "EXECUTOR_TERMINATION_REASON_ERROR")):
            self.assertEqual(self.run_hook(dict(self.payload, conversationId=f"enum-{index}",
                                                terminationReason=reason)), {"decision": "continue"})

    def invoke_script(self, raw, arguments=()):
        script = self.root / "scripts" / "retry.py"
        if not script.exists():
            shutil.copytree(PLUGIN / "scripts", script.parent, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copyfile(PLUGIN / "plugin.json", self.root / "plugin.json")
            retry.atomic_write(self.root / "config.json", dict(self.settings, retryDelaysSeconds=[0.05]))
        env = dict(os.environ, PLUGIN_DATA=str(self.data), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", str(script), *arguments],
                              input=raw, text=True, capture_output=True, env=env, timeout=5)

    def test_real_stdin_stdout_hook_contract(self):
        process = self.invoke_script(json.dumps(self.payload))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, '{"decision":"continue"}\n')
        self.assertEqual(process.stderr, "")

    def test_malformed_input_never_strands_the_agent(self):
        for raw in ("not JSON", "[]", "null", "{}", "x" * (1024*1024+1)):
            with self.subTest(size=len(raw)):
                process = self.invoke_script(raw)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(process.stdout, "{}\n")
                self.assertEqual(process.stderr, "")

    def test_pause_resume_and_status_use_same_state_directory(self):
        self.assertEqual(self.invoke_script("", ("--pause",)).returncode, 0)
        self.assertEqual(self.invoke_script(json.dumps(self.payload)).stdout, "{}\n")
        status = json.loads(self.invoke_script("", ("--status",)).stdout)
        self.assertTrue(status["paused"])
        self.assertEqual(self.invoke_script("", ("--resume",)).returncode, 0)
        self.assertEqual(self.invoke_script(json.dumps(self.payload)).stdout, '{"decision":"continue"}\n')

    def test_corrupt_state_fails_open_without_raw_error_output(self):
        self.run_hook()
        state = next((self.data / "states").glob("*.json"))
        state.write_text("corrupt JSON")
        process = self.invoke_script(json.dumps(dict(self.payload, executionNum=1)))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, "{}\n")
        self.assertEqual(process.stderr, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
