import copy
from contextlib import closing
import http.client
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dashboard
import native_watchdog as native
import runtime_monitor as monitor


def window(used=440, index=3):
    return {"status": native.IDLE, "trajectoryId": "synthetic-trajectory", "contextWindowUsage": {
        "estimatedTokensUsed": used, "maxContextTokens": 1000, "lastStepIndex": 9,
        "generatorMetadataIndex": index, "tokenGroups": [
            {"type": "TOKEN_TYPE_RULES", "numTokens": 40},
            {"type": "TOKEN_TYPE_CHAT_MESSAGES", "numTokens": used - 40},
        ],
    }}


def detail(base, index=3):
    value = copy.deepcopy(base)
    value["mainTrajectoryUpdate"] = {"generatorMetadatasUpdate": {
        "indices": [index], "generatorMetadatas": [{"chatModel": {
            "request": {"systemPrompt": "private fixture prompt"},
            "chatStartMetadata": {"contextWindowMetadata": {
                "estimatedTokensUsed": 300, "maxContextTokens": 1000,
                "tokenBreakdown": {"totalTokens": 300, "groups": [
                    {"type": "TOKEN_TYPE_RULES", "numTokens": 40,
                     "children": [{"name": "workspace rules", "numTokens": 40}]},
                    {"type": "TOKEN_TYPE_CHAT_MESSAGES", "numTokens": 260,
                     "children": [{"name": "6: USER_INPUT", "numTokens": 25},
                                  {"name": "8: GENERIC", "numTokens": 235}]},
                ]},
            }},
        }}],
    }}
    return value


class RuntimeMonitorTests(unittest.TestCase):
    def setUp(self):
        self.cid = str(uuid4())

    def test_window_and_request_have_distinct_totals(self):
        client = Mock()
        client.state_update.side_effect = [window(), detail(window())]
        with patch.object(native, "discover", return_value=client) as discover:
            result = monitor.read(self.cid, include_request=True)
        discover.assert_called_once_with(self.cid, 2)
        self.assertEqual(client.state_update.call_args.kwargs, {"generator_start": 3})
        self.assertEqual(result["runtime"], {"phase": "idle"})
        self.assertEqual(result["window"]["used"], 440)
        self.assertEqual(result["request"]["used"], 300)
        self.assertEqual(result["window"]["coverage"], "complete")
        self.assertEqual(result["request"]["coverage"], "complete")
        self.assertEqual(result["request"]["groups"][1]["children"][1], {"name": "8: GENERIC", "tokens": 235})
        self.assertNotIn("private fixture prompt", json.dumps(result))
        client.cancel.assert_not_called()
        client.continue_silently.assert_not_called()

    def test_summary_skips_request_read_until_expanded(self):
        client = Mock()
        client.state_update.return_value = window()
        with patch.object(native, "discover", return_value=client):
            result = monitor.read(self.cid)
        client.state_update.assert_called_once_with(self.cid, 12)
        self.assertIsNone(result["request"])
        self.assertEqual(result["requestRef"], "synthetic-trajectory/3")

    def test_mismatched_generation_leaves_detail_unavailable(self):
        client = Mock()
        client.state_update.side_effect = [window(), detail(window(550, index=4), index=3)]
        with patch.object(native, "discover", return_value=client):
            result = monitor.read(self.cid, include_request=True)
        self.assertEqual(result["window"]["used"], 550)
        self.assertEqual(result["requestRef"], "synthetic-trajectory/4")
        self.assertIsNone(result["request"])

    def test_zero_index_can_be_omitted_by_native_serializer(self):
        first = window(index=0)
        del first["contextWindowUsage"]["generatorMetadataIndex"]
        client = Mock()
        client.state_update.side_effect = [first, detail(first, index=0)]
        with patch.object(native, "discover", return_value=client):
            result = monitor.read(self.cid, include_request=True)
        self.assertEqual(client.state_update.call_args.kwargs, {"generator_start": 0})
        self.assertEqual(result["request"]["used"], 300)

    def test_detail_failure_preserves_current_host_window(self):
        client = Mock()
        client.state_update.side_effect = [window(), TimeoutError()]
        with patch.object(native, "discover", return_value=client):
            result = monitor.read(self.cid, include_request=True)
        self.assertEqual(result["connection"], "ready")
        self.assertEqual(result["window"]["used"], 440)
        self.assertIsNone(result["request"])

    def test_missing_and_invalid_counts_stay_unknown(self):
        update = window()
        update["contextWindowUsage"] = {"estimatedTokensUsed": "0", "maxContextTokens": False,
                                        "tokenGroups": [{"type": "TOKEN_TYPE_RULES"}, "invalid row"]}
        body = monitor.project(update)["window"]
        self.assertEqual(body["used"], 0)
        self.assertIsNone(body["limit"])
        self.assertIsNone(body["groups"][0]["tokens"])
        self.assertEqual(body["coverage"], "partial")
        self.assertIsNone(monitor.project({"status": native.IDLE})["window"])

    def test_only_host_signals_distinguish_running_phases(self):
        def status(kind, phase, metadata=None):
            step = {"type": "CORTEX_STEP_TYPE_" + kind, "status": "CORTEX_STEP_STATUS_" + phase,
                    "metadata": metadata}
            return monitor.runtime({"status": native.RUNNING, "mainTrajectoryUpdate": {"stepsUpdate": {
                "indices": [1], "steps": [step],
            }}})

        self.assertEqual(status("PLANNER_RESPONSE", "GENERATING"), {"phase": "generating"})
        self.assertEqual(status("GENERIC", "WAITING_FOR_USER"), {"phase": "approval"})
        self.assertEqual(status("GENERIC", "PENDING"), {"phase": "running"})
        self.assertEqual(status("GENERIC", "RUNNING", {"toolCall": {"name": "read_file", "args": "secret"}}),
                         {"phase": "tool", "tool": "read_file"})
        self.assertEqual(monitor.runtime({"status": "CASCADE_RUN_STATUS_NEW_VALUE"}), {"phase": "unknown"})

    def test_no_session_and_connection_failures_return_explicit_status(self):
        with patch.object(native, "discover", side_effect=ConnectionError()) as discover:
            self.assertEqual(monitor.read("../other"), {"connection": "no_conversation"})
            discover.assert_not_called()
            self.assertEqual(monitor.read(self.cid), {"connection": "unavailable"})

    def test_sidecar_route_requires_token_and_works_without_retry_history(self):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        token = secrets.token_urlsafe(24)
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        server.app = dashboard.Application(Path(data.name), token)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path, headers=None):
            with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                client.request("GET", path, headers=headers or {})
                with client.getresponse() as response:
                    return response.status, response.getheader("Content-Type"), response.read()

        path = "/api/monitor?conversationId=" + self.cid
        self.assertEqual(get(path)[0], 403)
        self.assertEqual(get("/monitor.css")[1], "text/css; charset=utf-8")
        client = Mock()
        client.state_update.side_effect = [window(), detail(window())]
        with patch.object(native, "discover", return_value=client):
            code, _, raw = get(path + "&request=1", {"X-Sidecar-Token": token})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["request"]["used"], 300)
        self.assertFalse((Path(data.name) / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
