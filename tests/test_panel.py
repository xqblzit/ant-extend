import http.client
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import dashboard
import install
import native_watchdog as native
import platform_utils
import retry
import session_store as store


class PanelTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.data=Path(temporary.name);self.cid="test-session";self.settings=retry.load_settings()
        self.app=dashboard.Application(self.data)
        store.begin(self.data,self.cid,"turn",now=1000)

    def test_both_recovery_paths_share_one_budget_and_backoff(self):
        settings=dict(self.settings,maxRetries=3)
        results=[store.reserve(self.data,self.cid,"turn",settings,source,"timeout",now=1000+i)
                 for i,source in enumerate(["watchdog","stop","watchdog","stop"])]
        self.assertEqual([r["delay"] for r in results[:3]],[10,20,30])
        self.assertIsNone(results[-1]);self.assertEqual(store.read(self.data,self.cid)["attempts"],3)

    def test_legacy_session_keeps_recovery_budget_after_a_long_turn(self):
        first = 1000 + self.settings["maxWindowSeconds"] - 60
        self.assertEqual(store.reserve(self.data, self.cid, "turn", self.settings,
                                       "watchdog", "no_progress", now=first)["attempt"], 1)

        def legacy(row):
            row.pop("firstRecoveryAt")
            row.update(phase="limited", limitReason="window")

        store.change(self.data, self.cid, legacy)
        result = store.reserve(self.data, self.cid, "turn", self.settings,
                               "stop", "timeout", now=first + 180)
        self.assertEqual(result, {"attempt": 2, "delay": 20})
        row = store.read(self.data, self.cid)
        self.assertEqual(row["firstRecoveryAt"], first)
        self.assertNotIn("limitReason", row)

        remaining = first + self.settings["maxWindowSeconds"] - 5
        self.assertIsNone(store.reserve(self.data, self.cid, "turn", self.settings,
                                        "watchdog", "no_progress", now=remaining))
        row = store.read(self.data, self.cid)
        self.assertEqual((row["attempts"], row["limitReason"]), (2, "window"))

    def test_new_turn_resets_budget_but_retains_total_and_pause(self):
        store.reserve(self.data,self.cid,"turn",self.settings,"stop","timeout",now=1000)
        self.app.action({"action":"pause","conversationId":self.cid})
        row=store.begin(self.data,self.cid,"new-turn",now=1050)
        self.assertEqual(row["attempts"],0);self.assertEqual(row["totalAttempts"],1);self.assertTrue(row["paused"])

    def test_paused_or_stale_turn_does_not_reserve(self):
        self.assertIsNone(store.reserve(self.data,self.cid,"old-turn",self.settings,"stop","timeout",now=1000))
        self.app.action({"action":"pause","conversationId":self.cid})
        self.assertIsNone(store.reserve(self.data,self.cid,"turn",self.settings,"stop","timeout",now=1000))

    def test_resume_only_restarts_monitor_never_submits_messages(self):
        self.app.action({"action":"pause","conversationId":self.cid})
        with patch.object(dashboard,"spawn_background") as spawn:
            self.app.action({"action":"resume","conversationId":self.cid})
        spawn.assert_not_called()  # No armed live invocation to watch.
        self.assertFalse(store.read(self.data,self.cid)["paused"])

    def test_history_is_bounded(self):
        for n in range(60):store.record(self.data,self.cid,"watching",history=True,now=n)
        self.assertEqual(len(store.read(self.data,self.cid)["events"]),40)

    def test_invalid_settings_do_not_overwrite(self):
        with patch.object(retry,"atomic_write") as write:
            with self.assertRaises(ValueError):self.app.action({"action":"settings","settings":{"retryDelaysSeconds":[2,1]}})
            with self.assertRaises(retry.InvalidSchedule):
                self.app.action({"action":"settings","settings":{"idleTimeoutsSeconds":[40,20]}})
            with self.assertRaises(ValueError):self.app.action({"action":"settings","settings":{"arbitrary":True}})
            write.assert_not_called()

    def test_custom_settings_persist_including_a_fixed_detection_interval(self):
        load_settings = retry.load_settings
        changes = {"idleTimeoutsSeconds": [20, 35, 50], "maxRetries": 5,
                   "retryDelaysSeconds": [5, 10, 20, 30, 60]}
        with patch.object(retry, "ROOT", self.data), \
                patch.object(retry, "load_settings", side_effect=lambda: load_settings(self.data)):
            self.assertEqual(self.app.action({"action": "settings", "settings": changes}), {"ok": True})
            saved = load_settings(self.data)
            self.assertEqual({key: saved[key] for key in changes}, changes)
            self.app.action({"action": "settings", "settings": {"idleTimeoutsSeconds": [25]}})
        self.assertEqual(load_settings(self.data), dict(saved, idleTimeoutsSeconds=[25]))

    def test_timeline_groups_thoughts_and_leaves_final_answer_visible(self):
        hidden_text = "fixture text omitted from the timeline"
        steps=[{"type":"CORTEX_STEP_TYPE_USER_INPUT","userInput":{"items":[{"text":"test task"}]}},
               {"type":"CORTEX_STEP_TYPE_PLANNER_RESPONSE","plannerResponse":{"thinking":"public thought","response":"checking","toolCalls":[{}]}},
               {"type":"CORTEX_STEP_TYPE_GENERIC","metadata":{"toolCall":{"name":"read_file","args":{"text":hidden_text}}}},
               {"type":"CORTEX_STEP_TYPE_SYSTEM_MESSAGE","systemMessage":{"message":hidden_text}},
               {"type":"CORTEX_STEP_TYPE_PLANNER_RESPONSE","plannerResponse":{"response":"finished"}}]
        update={"status":"CASCADE_RUN_STATUS_IDLE","mainTrajectoryUpdate":{"stepsUpdate":{"indices":list(range(len(steps))),"steps":steps,"totalLength":len(steps)}}}
        result=dashboard.timeline(update);group=result["groups"][0]
        self.assertFalse(group["active"]);self.assertEqual(group["answer"],"finished")
        self.assertEqual([i["kind"] for i in group["items"]],["thought","progress","tool"])
        self.assertNotIn(hidden_text,json.dumps(result))
        update["status"]=native.RUNNING
        running=dashboard.timeline(update)["groups"][0]
        self.assertTrue(running["active"]);self.assertEqual(running["answer"],"")

    def test_internal_retry_idle_gap_keeps_process_open(self):
        update={"status":"CASCADE_RUN_STATUS_IDLE","mainTrajectoryUpdate":{"stepsUpdate":{
            "indices":[0,1],"steps":[{"type":"CORTEX_STEP_TYPE_USER_INPUT","userInput":{"items":[{"text":"task"}]}},
            {"type":"CORTEX_STEP_TYPE_PLANNER_RESPONSE","plannerResponse":{"response":"working"}}]}}}
        group=dashboard.timeline(update,recovering=True)["groups"][0]
        self.assertTrue(group["active"]);self.assertEqual(group["answer"],"")

    def test_empty_native_continuation_does_not_split_turn(self):
        update={"mainTrajectoryUpdate":{"stepsUpdate":{"indices":[0,1],"steps":[
            {"type":"CORTEX_STEP_TYPE_USER_INPUT","userInput":{"items":[{"text":"task"}]}},
            {"type":"CORTEX_STEP_TYPE_USER_INPUT","userInput":{"items":[]}}]}}}
        self.assertEqual(len(dashboard.timeline(update)["groups"]),1)

    def test_sidecar_requires_host_token_for_private_reads_and_actions(self):
        token = secrets.token_urlsafe(24)
        server=dashboard.ThreadingHTTPServer(("127.0.0.1",0),dashboard.Handler)
        port=server.server_address[1]
        server.app=dashboard.Application(self.data, token)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path,headers=None,body=None):
            client=http.client.HTTPConnection("127.0.0.1",port,timeout=2)
            client.request("POST" if body else "GET",path,body=body,headers=headers or {})
            response=client.getresponse();value=response.read();client.close();return response.status,value
        self.assertEqual(request('/api/state')[0],403)
        self.assertEqual(request('/api/state?token='+token)[0],403)
        self.assertEqual(request('/api/state',{'X-Sidecar-Token':'invalid-'+token})[0],403)
        headers={'X-Sidecar-Token':token}
        status, raw = request('/api/state?conversationId='+self.cid, headers)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)['session']['conversationId'], self.cid)
        self.assertIsNone(json.loads(request('/api/state?conversationId=another', headers)[1])['session'])
        self.assertEqual(request('/api/bootstrap', headers)[0],404)
        self.assertEqual(request('/../../config.json')[0],404)
        self.assertEqual(request('/api/action',{'Content-Type':'application/json'},'{}')[0],403)
        status,_=request('/api/action',dict(headers,**{'Content-Type':'application/json'}),json.dumps({'action':'pause'}))
        self.assertEqual(status,200);self.assertTrue((self.data/'paused').exists())

    def test_install_enables_only_the_pane_and_preserves_explicit_disabling(self):
        path = self.data / 'app-config.json'
        existing = {'sidecars': {'another/pane': {'projectId': 'project', 'enabled': True}},
                    'plugins': {'ant-extend': {'enabled': True}}}
        retry.atomic_write(path, existing)
        install.configure_app_sidecar(path, True)
        updated = json.loads(path.read_text())
        self.assertTrue(updated['sidecars'][dashboard.SIDECAR_ID]['enabled'])
        self.assertEqual(updated['sidecars']['another/pane'], existing['sidecars']['another/pane'])
        self.assertEqual(updated['plugins'], existing['plugins'])
        install.configure_app_sidecar(path, False)
        install.configure_app_sidecar(path, True)
        self.assertFalse(json.loads(path.read_text())['sidecars'][dashboard.SIDECAR_ID]['enabled'])

    def test_process_probe_is_read_only_and_negative_pids_rejected(self):
        self.assertTrue(platform_utils.process_alive(os.getpid()))
        self.assertFalse(platform_utils.process_alive(-1))
        self.assertFalse(platform_utils.process_alive(True))

    def test_install_commands_quote_space_and_unicode_paths(self):
        import shlex

        windows_path = r"fixtures\测试 目录\python.exe"
        self.assertTrue(install.hook_command(windows_path, "--arm", True).startswith(f'"{windows_path}" '))
        mac_path = "fixtures/测试 目录/bin/python3"
        self.assertEqual(shlex.split(install.hook_command(mac_path, "--arm"))[0], mac_path)

    def test_stop_timeout_covers_maximum_backoff_in_all_manifests(self):
        bundled = json.loads((retry.ROOT / "hooks.json").read_text(encoding="utf-8"))
        mac = install.hooks("python3")
        windows = install.hooks("python.exe", windows=True)
        for hooks in (bundled, mac, windows):
            timeout = hooks["ant-extend"]["Stop"][0]["timeout"]
            self.assertEqual(timeout, retry.STOP_HOOK_TIMEOUT_SECONDS)
            self.assertGreater(timeout, retry.MAX_RETRY_DELAY_SECONDS)

    def test_background_options_are_detached_and_do_not_retain_stdio(self):
        with patch.object(platform_utils.subprocess,'Popen') as popen:
            platform_utils.spawn_background(['python','worker.py'])
        kwargs=popen.call_args.kwargs
        self.assertTrue(kwargs['close_fds'])
        self.assertEqual(kwargs['stdout'],platform_utils.subprocess.DEVNULL)
        if os.name!='nt':self.assertTrue(kwargs['start_new_session'])

    def test_windows_process_check_does_not_call_kill(self):
        import ctypes
        kernel=Mock();kernel.OpenProcess.return_value=42
        def code(handle,result):result._obj.value=259;return True
        kernel.GetExitCodeProcess.side_effect=code
        with patch.object(platform_utils.os,'name','nt'), patch.object(ctypes,'WinDLL',return_value=kernel,create=True), patch.object(platform_utils.os,'kill') as kill:
            self.assertTrue(platform_utils.process_alive(42))
            kill.assert_not_called();kernel.CloseHandle.assert_called_once_with(42)

    def test_windows_background_creation_has_no_visible_console(self):
        with patch.object(platform_utils.os,'name','nt'), \
             patch.object(platform_utils.subprocess,'CREATE_NO_WINDOW',0x08000000,create=True), \
             patch.object(platform_utils.subprocess,'CREATE_NEW_PROCESS_GROUP',0x200,create=True), \
             patch.object(platform_utils.subprocess,'Popen') as popen:
            platform_utils.spawn_background(['python','worker.py'])
        self.assertEqual(popen.call_args.kwargs['creationflags'],0x08000200)
        self.assertNotIn('start_new_session',popen.call_args.kwargs)

    def test_windows_lock_contention_becomes_nonblocking_failure(self):
        import errno
        fd=os.open(self.data/'test.lock',os.O_CREAT|os.O_RDWR,0o600)
        self.addCleanup(os.close,fd)
        fake=Mock();fake.LK_NBLCK=1;fake.locking.side_effect=OSError(errno.EACCES,'busy')
        with patch.object(platform_utils.os,'name','nt'), patch.dict(sys.modules,{'msvcrt':fake}):
            with self.assertRaises(BlockingIOError):platform_utils.lock_fd(fd,blocking=False)

    def test_windows_special_path_is_quoted_and_expansions_rejected(self):
        interpreter = r"fixtures\Tools&More\python.exe"
        command = install.hook_command(interpreter, "--arm", True)
        self.assertTrue(command.startswith(f'"{interpreter}" '))
        with self.assertRaises(ValueError):
            install.hook_command(r"fixtures\%OTHER%\python.exe", "--arm", True)


if __name__=='__main__':unittest.main()
