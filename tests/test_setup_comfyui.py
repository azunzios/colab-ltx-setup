import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import setup_comfyui as app


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.comfy = self.root / "ComfyUI"
        self.comfy.mkdir()
        self.runtime = self.comfy / ".provisioning"
        self.runtime.mkdir()

    def provisioner(self):
        return app.Provisioner(self.comfy, self.runtime, "hf", sys.executable)

    def finish(self, provisioner):
        deadline = time.monotonic() + 8
        while provisioner.pending.unfinished_tasks:
            if time.monotonic() > deadline:
                self.fail("Worker did not finish in time")
            time.sleep(0.02)


class DownloadTests(WorkspaceTest):
    def test_wget_keeps_partial_until_success_and_resumes_on_retry(self):
        worker = self.provisioner()
        commands = []
        def transfer(job, command, **kwargs):
            commands.append(command)
            partial = Path(command[command.index("--output-document") + 1])
            if len(commands) == 1:
                partial.write_bytes(b"first")
                raise RuntimeError("connection lost")
            self.assertEqual(partial.read_bytes(), b"first")
            partial.write_bytes(b"first-second")
        worker.command = transfer
        payload = {"kind": "wget", "url": "https://example.com/weights?x=1", "filename": "test.safetensors",
                   "category": "loras", "subfolder": "project"}
        first = worker.submit(payload)
        self.finish(worker)
        destination = self.comfy / "models/loras/project/test.safetensors"
        self.assertEqual(worker.jobs[first["id"]]["status"], "failed")
        self.assertFalse(destination.exists())
        self.assertTrue(destination.with_suffix(".safetensors.part").exists())
        second = worker.submit(worker.jobs[first["id"]]["payload"])
        self.finish(worker)
        self.assertEqual(worker.jobs[second["id"]]["status"], "completed")
        self.assertEqual(destination.read_bytes(), b"first-second")
        self.assertIn("--continue", commands[1])
        self.assertFalse(destination.with_suffix(".safetensors.part").exists())
        third = worker.submit(payload)
        self.finish(worker)
        self.assertEqual(worker.jobs[third["id"]]["status"], "failed")
        self.assertEqual(len(commands), 2)

    def test_hf_retry_reuses_staging_and_flattens_repo_path(self):
        worker = self.provisioner()
        staging_paths = []
        def transfer(job, command, **kwargs):
            staging = Path(command[command.index("--local-dir") + 1])
            staging_paths.append(staging)
            source = staging / "nested/model.safetensors"
            if len(staging_paths) == 1:
                source.parent.mkdir(parents=True)
                source.write_bytes(b"partial")
                raise RuntimeError("cancelled transfer")
            self.assertEqual(source.read_bytes(), b"partial")
            source.write_bytes(b"complete")
        worker.command = transfer
        first = worker.submit({"kind": "hf", "repo": "owner/model", "file": "nested/model.safetensors", "category": "loras"})
        self.finish(worker)
        self.assertEqual(worker.jobs[first["id"]]["status"], "failed")
        second = worker.submit(worker.jobs[first["id"]]["payload"])
        self.finish(worker)
        self.assertEqual(worker.jobs[second["id"]]["status"], "completed")
        self.assertEqual(staging_paths[0], staging_paths[1])
        self.assertFalse(staging_paths[1].exists())
        self.assertEqual((self.comfy / "models/loras/model.safetensors").read_bytes(), b"complete")

    def test_device_login_uses_cli_without_token_or_stdin_prompt(self):
        worker = self.provisioner()
        with mock.patch.object(worker, "command") as command:
            worker.submit({"kind": "login"})
            self.finish(worker)
        actual = command.call_args.args[1]
        self.assertEqual(actual, ["hf", "auth", "login", "--force", "--format", "agent"])
        self.assertNotIn("--token", actual)

    def test_requirements_use_comfy_interpreter_and_node_directory(self):
        directory = self.comfy / "custom_nodes/node"
        directory.mkdir(parents=True)
        (directory / "requirements.txt").write_text("pillow", encoding="utf-8")
        worker = self.provisioner()
        with mock.patch.object(worker, "command") as command:
            worker.submit({"kind": "requirements", "path": "custom_nodes/node/requirements.txt"})
            self.finish(worker)
        self.assertEqual(command.call_args.args[1][:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(command.call_args.kwargs["cwd"], directory)

    def test_cancel_stops_running_command_and_queue_continues(self):
        worker = self.provisioner()
        started = threading.Event()
        commands = []
        def execute(job):
            commands.append(job["kind"])
            if job["kind"] == "whoami":
                started.set()
                worker.command(job, [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(20)"])
        worker.execute = execute
        job = worker.submit({"kind": "whoami"})
        self.assertTrue(started.wait(3))
        time.sleep(0.1)
        worker.cancel(job["id"])
        next_job = worker.submit({"kind": "pip_check"})
        self.finish(worker)
        self.assertEqual(worker.jobs[job["id"]]["status"], "cancelled")
        self.assertEqual(worker.jobs[next_job["id"]]["status"], "completed")
        self.assertEqual(commands, ["whoami", "pip_check"])

    def test_recovery_marks_previous_jobs_interrupted(self):
        data = {"id": "previous", "kind": "login", "payload": {"kind": "login"}, "status": "running", "created": 1}
        app.save_json(self.runtime / "jobs/previous.json", data)
        worker = self.provisioner()
        self.assertEqual(worker.jobs["previous"]["status"], "interrupted")
        self.assertEqual(worker.pending.qsize(), 0)

    def test_failed_package_install_can_retry_normalized_payload(self):
        worker = self.provisioner()
        with mock.patch.object(worker, "command", side_effect=RuntimeError("network error")):
            job = worker.submit({"kind": "packages", "packages": "pillow safetensors>=0.5"})
            self.finish(worker)
        self.assertEqual(worker.jobs[job["id"]]["status"], "failed")
        with mock.patch.object(worker, "command") as command:
            retry = worker.submit(worker.jobs[job["id"]]["payload"])
            self.finish(worker)
        self.assertEqual(worker.jobs[retry["id"]]["status"], "completed")
        self.assertEqual(command.call_args.args[1][-2:], ["pillow", "safetensors>=0.5"])

    def test_node_retry_installs_requirements_without_cloning_again(self):
        directory = self.comfy / "custom_nodes/node"
        directory.mkdir(parents=True)
        (directory / "requirements.txt").write_text("pillow", encoding="utf-8")
        worker = self.provisioner()
        origin = subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/node.git\n")
        with mock.patch.object(app.subprocess, "run", return_value=origin), mock.patch.object(worker, "command") as command:
            job = worker.submit({"kind": "node", "url": "https://github.com/owner/node"})
            self.finish(worker)
        self.assertEqual(worker.jobs[job["id"]]["status"], "completed")
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[1][:4], [sys.executable, "-m", "pip", "install"])


class ValidationTests(WorkspaceTest):
    def test_download_traversal_and_command_options_are_rejected(self):
        worker = self.provisioner()
        bad_payloads = [
            {"kind": "wget", "url": "file:///etc/passwd"},
            {"kind": "wget", "url": "https://example.com/a", "filename": "../main.py"},
            {"kind": "wget", "url": "https://example.com/a", "subfolder": "../../../outside"},
            {"kind": "wget", "url": "https://example.com/a", "category": "../custom_nodes"},
            {"kind": "wget", "url": "https://user:secret@example.com/a"},
            {"kind": "hf", "repo": "owner/model", "file": "../config.json"},
            {"kind": "hf", "repo": "owner/model", "file": "--token"},
            {"kind": "hf", "repo": "owner/model", "file": "*.safetensors"},
            {"kind": "packages", "packages": "--target /tmp/a"},
            {"kind": "packages", "packages": "pillow; whoami"},
            {"kind": "node", "url": "https://example.com/repo.git"},
            {"kind": "requirements", "path": "../../etc/passwd"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                worker.submit(payload)
        self.assertEqual(worker.snapshot(), [])

    def test_symlink_escape_is_rejected(self):
        models = self.comfy / "models"
        models.mkdir()
        try:
            (models / "loras").symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable on this host")
        worker = self.provisioner()
        with self.assertRaises(ValueError):
            worker.submit({"kind": "wget", "url": "https://example.com/a", "category": "loras"})


class HttpTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.worker = self.provisioner()
        self.server = app.DashboardServer(("127.0.0.1", 0), app.dashboard_handler(self.worker, "test-password"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, data=None, authenticated=True):
        headers = {"Authorization": "Bearer test-password"} if authenticated else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as response:
            return response

    def test_html_public_but_all_apis_require_auth_and_no_cors(self):
        with self.request("/", authenticated=False) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"ComfyUI control desk", response.read())
        for path, data in [("/api/status", None), ("/api/jobs", None), ("/api/log?service=comfy", None),
                           ("/api/files", None), ("/api/jobs", {"kind": "login"})]:
            with self.subTest(path=path), self.request(path, data, authenticated=False) as response:
                self.assertEqual(response.code, 401)
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(self.worker.snapshot(), [])

    def test_authenticated_status_and_inventory(self):
        path = self.comfy / "models/loras/project/a.safetensors"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"1234")
        with self.request("/api/status") as response:
            state = json.load(response)
            self.assertEqual(state["comfy_dir"], str(self.comfy))
            self.assertGreater(state["disk"]["total"], 0)
            self.assertIn("loras", state["categories"])
            self.assertNotIn("password", state)
        with self.request("/api/files?category=loras") as response:
            self.assertEqual(json.load(response)["files"][0]["size"], 4)

    def test_api_submit_and_logs(self):
        def execute(job):
            (self.worker.jobs_dir / f"{job['id']}.log").write_text("Device code: ABCD-EFGH", encoding="utf-8")
        self.worker.execute = execute
        with self.request("/api/jobs", {"kind": "login"}) as response:
            self.assertEqual(response.status, 202)
            job = json.load(response)
        self.finish(self.worker)
        with self.request("/api/log?job=" + job["id"]) as response:
            self.assertIn("ABCD-EFGH", json.load(response)["text"])
        with self.request("/api/log?job=../../state") as response:
            self.assertEqual(response.code, 400)
        with self.request("/api/retry", {"id": job["id"]}) as response:
            self.assertEqual(response.code, 400)

    def test_non_object_json_rejected(self):
        with self.request("/api/jobs", ["login"]) as response:
            self.assertEqual(response.code, 400)


class LifecycleTests(WorkspaceTest):
    def test_stop_kills_worker_before_jobs_and_marks_queue_interrupted(self):
        job = {"id": "active", "status": "running", "process": {"pid": 22, "identity": "ticks"}}
        app.save_json(self.runtime / "jobs/active.json", job)
        app.save_json(self.runtime / "jobs/queued.json", {"id": "queued", "status": "queued"})
        dashboard, comfy = {"pid": 11}, {"pid": 33}
        with mock.patch.object(app, "stop_process") as stop:
            app.stop_stack(self.runtime, {"services": {"comfy": comfy, "dashboard": dashboard}})
        self.assertEqual(stop.call_args_list[0].args[0], dashboard)
        self.assertIn(mock.call(job["process"]), stop.call_args_list)
        self.assertEqual(app.load_json(self.runtime / "jobs/active.json")["status"], "interrupted")
        self.assertEqual(app.load_json(self.runtime / "jobs/queued.json")["status"], "interrupted")

    def test_startup_failure_rolls_back_servers_and_tunnel(self):
        args = mock.Mock(host="127.0.0.1", port=8188, dashboard_port=8189, extra_args="", tunnel="cloudflare")
        launched = []
        def launch(command, cwd, logfile):
            entry = {"pid": len(launched) + 1, "identity": "ticks", "log": str(logfile)}
            launched.append(command)
            return mock.Mock(), entry
        with mock.patch.object(app, "port_open", return_value=False), \
             mock.patch.object(app, "launch_detached", side_effect=launch), \
             mock.patch.object(app, "wait_for_service"), \
             mock.patch.object(app, "wait_for_tunnel", side_effect=RuntimeError("tunnel failed")), \
             mock.patch.object(app, "stop_stack") as stop:
            with self.assertRaisesRegex(RuntimeError, "tunnel failed"):
                app.launch_stack(args, self.comfy, self.runtime, "hf", Path("cloudflared"))
        self.assertEqual(len(launched), 3)
        self.assertIn("--serve-dashboard", launched[1])
        self.assertEqual(set(stop.call_args.args[1]["services"]), {"comfy", "dashboard", "dashboard_tunnel"})

    def test_detached_services_use_nohup_with_no_terminal_pipes(self):
        with mock.patch.object(app.subprocess, "Popen") as popen, mock.patch.object(app, "process_identity", return_value="ticks"):
            popen.return_value.pid = 123
            _, entry = app.launch_detached(["python", "main.py"], self.comfy, self.runtime / "service.log")
            self.assertEqual(popen.call_args.args[0], ["nohup", "python", "main.py"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(entry["identity"], "ticks")

    def test_stale_pid_is_never_killed(self):
        with mock.patch.object(app, "process_identity", return_value="new-start"), mock.patch.object(app.os, "killpg", create=True) as kill:
            app.stop_process({"pid": 123, "identity": "old-start"})
            kill.assert_not_called()

    def test_tunnel_does_not_use_wildcard_host_as_destination(self):
        command = app.tunnel_command("cloudflare", "0.0.0.0", 8188, "/bin/cloudflared")
        self.assertIn("http://127.0.0.1:8188", command)
        self.assertIn("127.0.0.1:8188", command)

    def test_log_tail_is_bounded(self):
        file = self.runtime / "test.log"
        file.write_bytes(b"x" * 100000 + b"END")
        result = app.tail_log(file, limit=100)
        self.assertEqual(len(result), 100)
        self.assertTrue(result.endswith("END"))


if __name__ == "__main__":
    unittest.main()
