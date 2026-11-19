import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
import native_watchdog as native
import retry


def state(kind="CORTEX_STEP_TYPE_PLANNER_RESPONSE", status="CORTEX_STEP_STATUS_RUNNING", text="hello"):
    return {
        "conversationId": "test-conversation", "status": native.RUNNING,
        "mainTrajectoryUpdate": {
            "stepsUpdate": {"indices": [20], "steps": [{"type": kind, "status": status,
                                                          "plannerResponse": {"response": text}}],
                            "totalLength": 21},
            "executorMetadatasUpdate": {"totalLength": 4},
        },
    }


class FakeClient:
    version = "2.33.0"

    def __init__(self, snapshot):
        self.before = copy.deepcopy(snapshot)
        self.after = dict(snapshot, status="CASCADE_RUN_STATUS_IDLE")
        self.cancelled = False
        self.continued = []
        self.config = {"plannerConfig": {"requestedModel": {"alias": "flash"}},
                       "executorConfig": {"maxGeneratorInvocations": 100},
                       "marker": "permissions-and-model-unchanged"}
        self.offset = None

    def snapshot(self, _):
        return self.after if self.cancelled else self.before

    def cancel(self, _):
        self.cancelled = True

    def executor_count(self, _):
        return 4

    def frozen_config(self, _, offset):
        self.offset = offset
        return copy.deepcopy(self.config)

    def continue_silently(self, conversation, config):
        self.continued.append((conversation, config))


class EarlyRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.conversation = "test-conversation"
        self.settings = retry.load_settings(PLUGIN)
        self.control = {"nonce": "invocation-1", "armed": True, "turn": "turn-1",
                        "startedAt": time.time(), "attempts": 0}
        self.path, _ = native.control_paths(self.data, self.conversation)
        retry.atomic_write(self.path, self.control)
        self.snapshot = native.summarize(state())
        self.client = FakeClient(self.snapshot)

    def recover(self, sleep=lambda _: None):
        return native.recover(self.client, self.conversation, self.snapshot, self.data,
                              self.control, self.settings, sleep=sleep)

    def test_cancel_and_empty_continue_preserve_frozen_configuration(self):
        self.assertTrue(self.recover())
        self.assertTrue(self.client.cancelled)
        self.assertEqual(self.client.offset, 4)
        self.assertEqual(self.client.continued, [(self.conversation, self.client.config)])
        self.assertEqual(native.read_control(self.path)["attempts"], 1)

    def test_running_tool_is_never_cancelled(self):
        self.client.before = native.summarize(state(kind="CORTEX_STEP_TYPE_RUN_COMMAND"))
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_waiting_for_user_confirmation_is_never_cancelled(self):
        self.client.before = native.summarize(state(kind="CORTEX_STEP_TYPE_GENERIC", status="CORTEX_STEP_STATUS_PENDING"))
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_unknown_tool_wait_states_are_not_interrupted(self):
        self.client.before = native.summarize(state(kind="CORTEX_STEP_TYPE_RUN_COMMAND",
                                                   status="CORTEX_STEP_STATUS_WAITING_FOR_USER"))
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_new_model_output_invalidates_stall_detection(self):
        self.client.before = native.summarize(state(text="new token"))
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_manual_stop_is_not_resumed(self):
        self.client.before["status"] = "CASCADE_RUN_STATUS_IDLE"
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_new_invocation_supersedes_old_watchdog(self):
        retry.atomic_write(self.path, dict(self.control, nonce="new-invocation"))
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_new_user_input_during_backoff_is_not_overridden(self):
        self.client.after["lastUserIndex"] = 22
        self.assertFalse(self.recover())
        self.assertTrue(self.client.cancelled)
        self.assertFalse(self.client.continued)

    def test_pause_during_backoff_prevents_continue(self):
        def pause(_):
            (self.data / "paused").touch()
        self.assertFalse(self.recover(pause))
        self.assertTrue(self.client.cancelled)
        self.assertFalse(self.client.continued)

    def test_disabled_native_recovery_is_noop(self):
        self.settings["earlyRecovery"] = False
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_active_hook_and_background_tasks_block_recovery(self):
        for key in ["activeHook", "backgroundTasks", "pendingAgentMessages"]:
            with self.subTest(key=key):
                self.client.before = native.summarize(dict(state(), **{key: {"active": True}}))
                self.assertFalse(self.recover())
                self.assertFalse(self.client.cancelled)

    def test_retry_limit_is_checked_before_cancel(self):
        self.control["attempts"] = self.settings["maxRetries"]
        retry.atomic_write(self.path, self.control)
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)

    def test_time_limit_is_checked_before_cancel(self):
        first = time.time() - self.settings["maxWindowSeconds"] - 1
        native.session_store.begin(self.data, self.conversation, self.control["turn"], now=first)
        native.session_store.reserve(self.data, self.conversation, self.control["turn"],
                                     self.settings, "stop", "timeout", now=first)
        self.assertFalse(self.recover())
        self.assertFalse(self.client.cancelled)
        self.assertEqual(native.session_store.read(self.data, self.conversation)["limitReason"], "window")

    def test_first_stall_on_a_long_running_turn_can_recover(self):
        self.control["startedAt"] -= self.settings["maxWindowSeconds"] + 30
        retry.atomic_write(self.path, self.control)
        native.session_store.begin(self.data, self.conversation, self.control["turn"],
                                   now=self.control["startedAt"])

        self.assertTrue(self.recover())
        row = native.session_store.read(self.data, self.conversation)
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["firstRecoveryAt"] - row["startedAt"], self.settings["maxWindowSeconds"])

    def test_incompatible_executor_history_does_not_cancel_or_spend_a_retry(self):
        error = native.NativeProtocolError("recovery_protocol", "Invalid executor history")
        with patch.object(self.client, "executor_count", side_effect=error), self.assertRaises(type(error)):
            self.recover()
        self.assertFalse(self.client.cancelled)
        self.assertFalse(native.read_control(self.path).get("recovering"))
        self.assertFalse(native.session_store.read(self.data, self.conversation).get("attempts"))

    def test_failed_continuation_configuration_clears_the_recovery_marker(self):
        error = native.NativeProtocolError("recovery_protocol", "Missing frozen configuration")
        with patch.object(self.client, "frozen_config", side_effect=error), self.assertRaises(type(error)):
            self.recover()
        self.assertTrue(self.client.cancelled)
        self.assertFalse(self.client.continued)
        self.assertFalse(native.read_control(self.path).get("recovering"))

    def test_native_api_request_never_contains_chat_text(self):
        client = object.__new__(native.NativeClient)
        calls = []
        client.request = lambda method, body: calls.append((method, body))
        client.continue_silently(self.conversation, self.client.config)
        method, body = calls[0]
        self.assertEqual(method, "SendUserCascadeMessage")
        self.assertEqual(body["items"], [])
        self.assertEqual(body["media"], [])
        self.assertNotIn("messageText", body)
        self.assertNotIn("additionalSteps", body)

    def test_stream_fragmentation_is_supported(self):
        class Fragmented(io.BytesIO):
            def read(self, count):
                return super().read(min(2, count))
        self.assertEqual(native.read_exact(Fragmented(b"abcde"), 5), b"abcde")
        with self.assertRaises(ValueError):
            native.read_exact(Fragmented(b"a"), 5)

    def test_version_is_diagnostic_and_future_hosts_can_read_live_state(self):
        discovery = {"pid": os.getpid(), "httpPort": 32000, "csrfToken": secrets.token_urlsafe(24)}
        for version in ("2.32.1", "2.33.0", "99.0.0", None):
            with self.subTest(version=version):
                client = native.NativeClient(dict(discovery, lsVersion=version))
                self.assertEqual(client.version, version)
                update = dict(state(), extraFutureField=True)
                with patch.object(client, "request", side_effect=[{"numSteps": 21}, {"update": update}]):
                    self.assertEqual(client.snapshot(self.conversation)["status"], native.RUNNING)

    def test_native_state_rejects_missing_status_or_mismatched_step_arrays(self):
        client = object.__new__(native.NativeClient)
        for update in (dict(state(), status=None), dict(state(), conversationId="another-conversation"),
                       dict(state(), mainTrajectoryUpdate={}),
                       dict(state(), mainTrajectoryUpdate={"stepsUpdate": {"indices": [20], "steps": []}})):
            with self.subTest(update=update):
                with patch.object(client, "request", side_effect=[{"numSteps": 21}, {"update": update}]):
                    with self.assertRaises(native.NativeProtocolError) as error:
                        client.snapshot(self.conversation)
                self.assertEqual(error.exception.reason, "state_protocol")

    def test_state_stream_remains_available_when_pagination_metadata_changes(self):
        client = object.__new__(native.NativeClient)
        error = native.NativeProtocolError("state_protocol", "Metadata endpoint unavailable")
        with patch.object(client, "request", side_effect=[error, {"update": state()}]) as request:
            self.assertEqual(client.snapshot(self.conversation)["status"], native.RUNNING)
        self.assertEqual(request.call_args.args[1]["initialStepsPageBounds"], {"startIndex": 0})

    def test_invalid_ports_fail_regardless_of_version(self):
        for port in (True, 0, 70000):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, "Invalid loopback port"):
                native.NativeClient({"pid": os.getpid(), "lsVersion": "2.32.2", "httpPort": port})

    def test_watch_reconnects_after_a_temporary_read_error(self):
        self.client.pid = os.getpid()
        idle = dict(self.snapshot, status=native.IDLE)
        snapshots = [OSError("temporary reconnect"), idle, idle, idle]
        with patch.object(native, "discover", return_value=self.client) as discovery, \
             patch.object(self.client, "snapshot", side_effect=snapshots), \
             patch.object(native.time, "sleep"):
            native.watch(self.conversation, self.data)
        self.assertEqual(discovery.call_count, 2)
        row = native.session_store.read(self.data, self.conversation)
        self.assertEqual(row["phase"], "idle")
        self.assertIsNone(row["nativeFailure"])

    def test_watch_reports_unusable_state_interface_after_reconnecting(self):
        self.client.pid = os.getpid()
        error = native.NativeProtocolError("state_protocol", "Incomplete state stream")
        with patch.object(native, "discover", return_value=self.client) as discovery, \
             patch.object(self.client, "snapshot", side_effect=error), patch.object(native.time, "sleep"):
            native.watch(self.conversation, self.data)
        self.assertEqual(discovery.call_count, 3)
        row = native.session_store.read(self.data, self.conversation)
        self.assertEqual(row["phase"], "unavailable")
        self.assertEqual(row["nativeFailure"], "state_protocol")
        self.assertEqual(row["nativeVersion"], "2.33.0")
        self.assertNotIn("attempts", row)

    def test_http_redirects_are_rejected_before_following(self):
        self.assertIsNone(native.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_invalid_idle_schedule_is_rejected(self):
        for limits in ([], [30, 20], [30, 30], [30, 121], [False]):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "config.json").write_text(json.dumps({"idleTimeoutsSeconds": limits}))
                with self.assertRaises(ValueError):
                    retry.load_settings(root)

    def test_default_stall_recovery_uses_120_seconds_each_time(self):
        class Clock:
            value = 1000
            def now(self): return self.value
            def sleep(self, duration): self.value += duration
        clock = Clock()
        times = []
        self.control["startedAt"] = clock.now() - self.settings["maxWindowSeconds"] - 30
        retry.atomic_write(self.path, self.control)
        fake = self.client
        fake.pid = os.getpid()
        def recovered(client, conversation, snapshot, data, control, settings):
            times.append(clock.now())
            control = dict(control, attempts=len(times), armed=len(times) < 3)
            retry.atomic_write(self.path, control)
            return True
        with patch.object(native, "discover", return_value=fake), \
             patch.object(native, "recover", side_effect=recovered), \
             patch.object(native.time, "time", side_effect=clock.now), \
             patch.object(native.time, "monotonic", side_effect=clock.now), \
             patch.object(native.time, "sleep", side_effect=clock.sleep):
            native.watch(self.conversation, self.data)
        self.assertEqual(len(times), 3)
        for previous, recovered_at in zip([1000] + times, times):
            self.assertGreaterEqual(recovered_at - previous, 120)
            self.assertLessEqual(recovered_at - previous, 124)

    def test_regular_model_progress_prevents_false_stall_recovery(self):
        class Clock:
            value = 1000
            def now(self): return self.value
            def sleep(self, duration): self.value += duration
        clock = Clock()
        self.control["startedAt"] = clock.now()
        retry.atomic_write(self.path, self.control)
        fake = self.client
        fake.pid = os.getpid()
        def snapshot(_):
            return dict(self.snapshot, progress=str(int((clock.value - 1000) // 20)),
                        status=native.RUNNING if clock.value < 1360 else "CASCADE_RUN_STATUS_IDLE")
        fake.snapshot = snapshot
        with patch.object(native, "discover", return_value=fake), \
             patch.object(native, "recover") as recovered, \
             patch.object(native.time, "time", side_effect=clock.now), \
             patch.object(native.time, "monotonic", side_effect=clock.now), \
             patch.object(native.time, "sleep", side_effect=clock.sleep):
            native.watch(self.conversation, self.data)
        recovered.assert_not_called()

    def test_cancellation_config_waits_for_a_new_executor_receipt(self):
        client = object.__new__(native.NativeClient)
        responses = iter([{"executorMetadata": []}, {"executorMetadata": [{
            "terminationReason": "EXECUTOR_TERMINATION_REASON_USER_CANCELED",
            "cascadeConfig": self.client.config}]}])
        calls = []
        def request(method, body):
            calls.append((method, body))
            return next(responses)
        client.request = request
        with patch.object(native.time, "sleep"):
            config = client.frozen_config(self.conversation, 7)
        self.assertEqual(config, self.client.config)
        self.assertTrue(all(body["executorMetadataOffset"] == 7 for _, body in calls))

    def test_cancellation_config_requires_a_model_selector_before_continuing(self):
        client = object.__new__(native.NativeClient)
        receipt = {"terminationReason": "EXECUTOR_TERMINATION_REASON_USER_CANCELED",
                   "cascadeConfig": {"plannerConfig": {"toolConfig": {}}, "executorConfig": {}}}
        with patch.object(client, "request", return_value={"executorMetadata": [receipt]}):
            with self.assertRaises(native.NativeProtocolError) as error:
                client.frozen_config(self.conversation, 7)
        self.assertEqual(error.exception.reason, "recovery_protocol")


if __name__ == "__main__":
    unittest.main(verbosity=2)
