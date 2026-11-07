import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "deploy"), *sys.path]):
    import web_update

TAG = "v1.2.3"
COMMIT = "a" * 40
PIN = {"tag": TAG, "commit": COMMIT}
PASSWORD = "".join(("local", "-fixture-", "passphrase"))
WEB_DEPENDENCIES = importlib.util.find_spec("flask") is not None


class SupervisorFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        rounds = patch.object(web_update, "ROUNDS", 100)
        rounds.start()
        self.addCleanup(rounds.stop)
        salt = b"fixture"
        credential = {"salt": salt.hex(), "digest": web_update.password_digest(PASSWORD, salt).hex()}
        self.config = {"credential": credential, "url": "https://localhost", "user": "pi"}
        self.supervisor = web_update.Supervisor(self.root, self.config, lambda: PIN)
        self.supervisor.check()

    def authorize(self, password=PASSWORD):
        return self.supervisor.dispatch({"action": "authorize", **PIN, "password": password})


class SupervisorTests(SupervisorFixture):
    def test_approval_is_bound_to_one_release_and_single_use(self):
        code, approval = self.authorize()
        self.assertEqual(code, 200)
        request = {"action": "install", **PIN, "approval": approval["approval"]}
        self.assertEqual(self.supervisor.dispatch({**request, "commit": "b" * 40})[0], 409)
        with patch.object(web_update.threading, "Thread") as thread:
            self.assertEqual(self.supervisor.dispatch(request)[0], 202)
            self.assertEqual(self.supervisor.dispatch(request)[0], 409)
            thread.return_value.start.assert_called_once()
        self.supervisor.phase("complete")
        self.assertEqual(self.supervisor.dispatch(request)[0], 403)
        self.assertEqual(json.loads((self.root / "job.json").read_text())["phase"], "complete")

    def test_expired_approval_and_guessed_password_are_rejected(self):
        self.assertEqual(self.authorize("wrong")[0], 401)
        self.assertEqual(self.authorize()[0], 429)
        self.supervisor.next_password = 0
        code, approval = self.authorize()
        self.assertEqual(code, 200)
        with patch.object(web_update.time, "monotonic", return_value=time.monotonic() + 150):
            request = {"action": "install", **PIN, "approval": approval["approval"]}
            self.assertEqual(self.supervisor.dispatch(request)[0], 403)
        self.assertIsNone(self.supervisor.job)

    def test_check_does_not_queue_an_install(self):
        self.supervisor.check()
        code, data = self.supervisor.dispatch({"action": "status"})
        self.assertEqual(code, 200)
        self.assertEqual(data["latest"], PIN)
        self.assertIsNone(data["job"])

    def test_restart_records_interrupted_job_as_recovery(self):
        web_update.write_state(self.root / "job.json", {"phase": "activation"})
        restored = web_update.Supervisor(self.root, self.config)
        self.assertEqual(restored.job["phase"], "recovery")

    def test_broker_accepts_fixed_sequence_only(self):
        broker = web_update.JobBroker(self.supervisor, self.root, {}, "2", {})
        with self.assertRaises(ValueError):
            broker.dispatch({"action": "stop"})
        with self.assertRaises(ValueError):
            broker.dispatch({"action": "stop", "service": "nginx"})
        with patch.object(web_update.update, "run") as command, patch.object(
                broker.controller, "backup_system") as backup, patch.object(
                broker.controller, "active", return_value=False):
            self.supervisor.job = {"phase": "staging"}
            for action in ("validate", "backup", "stop", "start", "stop", "start"):
                broker.dispatch({"action": action})
            with self.assertRaises(ValueError):
                broker.dispatch({"action": "stop"})
            backup.assert_called_once_with(self.root / "system.tar.gz", privilege=(),
                                           extra=web_update.HELPER_FILES)
            calls = [item.args for item in command.call_args_list if item.args[0].endswith("systemctl")]
            self.assertEqual([item[1:] for item in calls],
                             [("stop", "hp6033a.service"), ("start", "hp6033a.service")] * 2)

    def test_broker_can_restart_after_the_candidate_dies(self):
        broker = web_update.JobBroker(self.supervisor, self.root, {}, "2", {})
        broker.saved = True
        broker.stops = broker.starts = 1
        self.supervisor.job = {"phase": "verification"}
        with patch.object(web_update.update, "run") as command, patch.object(
                broker.controller, "active", return_value=False):
            broker.dispatch({"action": "start"})
            command.assert_called_once_with("/usr/bin/systemctl", "start", "hp6033a.service", timeout=60)

    def test_release_change_after_approval_never_starts_worker(self):
        self.supervisor.job = {"id": "fixture", "phase": "queued", **PIN}
        self.supervisor.release_provider = lambda: {**PIN, "commit": "b" * 40}
        with patch.object(web_update.JobBroker, "run") as worker:
            self.supervisor.perform(self.supervisor.job)
            worker.assert_not_called()
        self.assertEqual(self.supervisor.job["phase"], "failed")

    def test_tag_resolution_uses_published_release_and_peels_annotation(self):
        results = [
            {"tag_name": TAG, "draft": False, "prerelease": False},
            {"object": {"type": "tag", "sha": "b" * 40}},
            {"object": {"type": "commit", "sha": COMMIT}},
        ]
        with patch.object(web_update, "github_json", side_effect=results):
            self.assertEqual(web_update.latest_release(), {**PIN, "notes": web_update.NOTES + TAG})


@unittest.skipUnless(WEB_DEPENDENCIES, "Run with the controller virtual environment for Flask tests")
class DashboardTests(SupervisorFixture):
    def setUp(self):
        super().setUp()
        import app as controller
        self.controller = controller
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.socket.close)
        self.path = str(self.root / "helper.sock")
        self.socket.bind(self.path)
        self.socket.listen()
        self.socket.settimeout(2)
        self.client = controller.app.test_client()
        self.context = patch.object(controller, "UPDATE_SOCKET", self.path)
        self.context.start()
        self.addCleanup(self.context.stop)

    def call(self, data=None, expected_peer=None):
        uid = os.getuid() if expected_peer is None else expected_peer
        helper = threading.Thread(target=web_update.handle_client, args=(self.socket, self.supervisor, uid))
        helper.start()
        try:
            if data is None:
                return self.client.get("/api/software-updates", base_url="https://localhost")
            return self.client.post("/api/software-updates", json=data,
                                    base_url="https://localhost", headers={"Origin": "https://localhost"})
        finally:
            helper.join(timeout=3)
            self.assertFalse(helper.is_alive())

    def test_desktop_and_mobile_markup_and_status(self):
        for route in ("/", "/classic-dual"):
            page = self.client.get(route, base_url="https://localhost")
            self.assertEqual(page.status_code, 200)
            for label in ("Software Updates", "Check for Updates", "Release Notes", "Install Update",
                          "Confirm and install", "min-height:44px"):
                self.assertIn(label, page.get_data(as_text=True))
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertIn("installed", response.json)
        self.assertEqual(response.json["latest"]["tag"], TAG)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_helper_rejects_other_uids(self):
        self.assertEqual(self.call(expected_peer=os.getuid() + 1).status_code, 503)

    def test_cross_origin_or_insecure_post_rejected(self):
        for url, origin in (("https://localhost", "https://other"), ("http://localhost", "http://localhost")):
            response = self.client.post("/api/software-updates", base_url=url,
                                        json={"action": "check"}, headers={"Origin": origin})
            self.assertEqual(response.status_code, 403)

    def test_update_admin_is_required_in_both_control_modes(self):
        mode_path = self.root / ".control_auth_mode"
        (self.root / ".control_htpasswd").touch()
        with patch.object(self.controller, "proxy_control_auth_mode", return_value="2"), patch.object(
                self.controller, "ROOT", self.root):
            for mode in ("protected", "password-free"):
                with self.subTest(mode=mode), self.controller.app.test_request_context():
                    mode_path.write_text(mode)
                    self.assertEqual(self.controller.control_password_required(), mode == "protected")
                    self.assertEqual(self.call({"action": "install", **PIN, "approval": "invalid"}).status_code, 403)
                    self.assertEqual(self.call({"action": "authorize", **PIN, "password": "wrong"}).status_code, 401)
                    self.supervisor.next_password = 0
                    self.assertEqual(self.call({"action": "authorize", **PIN, "password": PASSWORD}).status_code, 200)
                    self.supervisor.next_password = 0


class TransactionTests(SupervisorFixture):
    def run_transaction(self, fail):
        controller_app = self.root / "hp6033a"
        controller_app.mkdir()
        source = self.root / "release"
        source.mkdir()
        (controller_app / "venv/bin").mkdir(parents=True)
        for name in ("python", "gunicorn"):
            program = controller_app / "venv/bin" / name
            program.touch(mode=0o700)
        (controller_app / "app.py").write_text("old = True\n")
        (controller_app / "config.json").write_text('{"classic_labels":{"model":"keep"}}')
        credentials = controller_app / ".control_htpasswd"
        credentials.write_text("fixture:$2b$12$" + "A" * 53)
        credentials.chmod(0o600)
        (controller_app / ".control_auth_mode").write_text("password-free\n")
        for name in web_update.update.MANAGED_DIRS:
            (source / name).mkdir()
        for name in web_update.update.MANAGED_FILES:
            (source / name).write_text("candidate = True\n")
        job_dir = self.root / "fixture-job"
        job_dir.mkdir()
        broker = web_update.JobBroker(self.supervisor, job_dir, {}, "2", {})
        active = True

        def privileged(*args, **kwargs):
            nonlocal active
            if args[0] == "/usr/sbin/nginx":
                return
            if args[:2] == ("/usr/bin/systemctl", "stop"):
                active = False
            elif args[:2] == ("/usr/bin/systemctl", "start"):
                active = True
            else:
                return real_run(*args, **kwargs)

        def backup(destination, privilege, extra):
            self.assertEqual(privilege, ())
            self.assertEqual(extra, web_update.HELPER_FILES)
            destination.write_bytes(b"isolated system archive")

        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)

        def serve_job():
            with server, server.makefile("rb") as stream:
                while stream.peek(1):
                    broker.dispatch(web_update.read_message(stream))
                    web_update.send_message(server, {"ok": True})

        def verify(connected, pages):
            self.assertTrue(connected)
            if fail and pages:
                raise web_update.update.UpdateError("Simulated candidate health failure")

        self.supervisor.job = {"id": "fixture-job", "phase": "staging", **PIN}
        thread = threading.Thread(target=serve_job)
        real_run = web_update.update.run
        with patch.object(broker.controller, "backup_system", side_effect=backup), patch.object(
                web_update.update, "run", side_effect=privileged), patch.object(
                web_update.update, "validate_candidate"), patch.object(
                broker.controller, "active", side_effect=lambda: active):
            # The real broker handles the socket protocol; only machine-level
            # privileged effects and hardware reads are isolated.
            thread.start()
            try:
                with client, client.makefile("rb") as stream:
                    worker = web_update.WorkerController(
                        client, stream, "fixture-job", TAG, self.config["url"], None,
                        {}, "2", {"auth_basic_user_file": []},
                    )
                    with patch.object(worker, "status", return_value=True), patch.object(
                            worker, "active", side_effect=lambda: active), patch.object(
                            worker, "check_https", side_effect=verify):
                        def operation():
                            web_update.update.apply_update(
                                controller_app, source, {"release": TAG, "commit": COMMIT},
                                worker, True, True,
                            )

                        if fail:
                            with self.assertRaises(web_update.update.RollbackComplete):
                                operation()
                        else:
                            operation()
            finally:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertTrue(active)
        self.assertEqual((controller_app / ".control_auth_mode").read_text(), "password-free\n")
        self.assertEqual((controller_app / "app.py").read_text(),
                         "old = True\n" if fail else "candidate = True\n")
        self.assertEqual(json.loads((controller_app / "config.json").read_text())["classic_labels"]["model"],
                         "keep")
        self.assertTrue((job_dir / "system.tar.gz").exists())
        self.assertEqual(broker.stops, 2 if fail else 1)
        self.assertEqual(broker.starts, 2 if fail else 1)

    def test_web_worker_installs_with_private_backup(self):
        self.run_transaction(False)

    def test_web_worker_rolls_back_on_failed_health(self):
        self.run_transaction(True)


class HelperUnitTests(unittest.TestCase):
    def test_nginx_validation_has_bind_service_capability(self):
        unit = (ROOT / "deploy/hp6033a-update.service").read_text()
        capabilities = next(line.split("=", 1)[1].split() for line in unit.splitlines()
                            if line.startswith("CapabilityBoundingSet="))
        # nginx -t can attempt to bind configured privileged listen ports.
        self.assertIn("CAP_NET_BIND_SERVICE", capabilities)
