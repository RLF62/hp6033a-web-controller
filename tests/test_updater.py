import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("controller_update", ROOT / "deploy/update.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class FakeController:
    mode = "2"
    security = {"auth_basic_user_file": []}

    def __init__(self, fail_health=False, on_stop=None):
        self.running = True
        self.fail_health = fail_health
        self.on_stop = on_stop
        self.calls = []

    def status(self):
        self.calls.append("read status")
        return True

    def validate_system(self):
        self.calls.append("validate system")

    def backup_system(self, path):
        self.calls.append("backup system")
        path.write_text("disposable fixture")

    def stop(self):
        self.calls.append("stop app")
        self.running = False
        if self.on_stop:
            callback, self.on_stop = self.on_stop, None
            callback()

    def start(self):
        self.calls.append("start app")
        self.running = True

    def active(self):
        return self.running

    def check_https(self, connected, pages):
        self.calls.append(("check HTTPS", connected, pages))
        if self.fail_health and pages:
            raise updater.UpdateError("simulated HTTPS failure")


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.parent = Path(self.workspace.name)
        self.app = self.parent / "hp6033a"
        self.app.mkdir()
        (self.app / "venv/bin").mkdir(parents=True)
        for name in ("python", "gunicorn"):
            executable = self.app / "venv/bin" / name
            executable.write_text("original runtime")
            executable.chmod(0o700)
        self.source = self.parent / "release"
        self.source.mkdir()
        for name in updater.MANAGED_DIRS:
            (self.source / name).mkdir()
        (self.source / "templates/index.html").write_text("new template")
        (self.source / "requirements.txt").write_text("Flask==3.1.3\n")
        (self.source / "config.example.json").write_text('{"classic_labels":{"model":"default"}}')
        (self.source / "app.py").write_text("release = True\n")
        self.old_app = "GPIB_RESOURCE = 'USB0::1234::5678::ABC::INSTR'\n"
        (self.app / "app.py").write_text(self.old_app)
        self.settings = {"classic_labels": {"model": "custom"}, "presets": [1, 2], "unknown": 11}
        (self.app / "config.json").write_text(json.dumps(self.settings))
        (self.app / "config.json").chmod(0o600)
        self.auth = "fixture:$2b$12$" + "A" * 53 + "\n"
        (self.app / ".control_htpasswd").write_text(self.auth)
        (self.app / ".control_htpasswd").chmod(0o600)
        (self.app / ".control_auth_mode").write_text("password-free\n")
        (self.app / "user-file.txt").write_text("keep")
        self.version = {"release": "v1.2.3", "commit": "a" * 40}

    def apply(self, controller, reuse=True):
        with patch.object(updater, "validate_candidate"):
            updater.apply_update(self.app, self.source, self.version, controller, reuse, True)

    def backup(self):
        return next(self.parent.glob(".hp6033a-backup-*"))

    def test_reuses_runtime_and_keeps_last_saved_settings(self):
        def final_write():
            self.settings["unknown"] = 99
            (self.app / "config.json").write_text(json.dumps(self.settings))

        controller = FakeController(on_stop=final_write)
        inode = (self.app / "venv").stat().st_ino
        self.apply(controller)
        expected = {**self.settings, "gpib_resource": "USB0::1234::5678::ABC::INSTR"}
        self.assertEqual(updater.read_json(self.app / "config.json"), expected)
        self.assertEqual((self.app / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.app / "venv").stat().st_ino, inode)
        self.assertEqual((self.app / ".control_auth_mode").read_text(), "password-free\n")
        self.assertEqual((self.app / ".control_htpasswd").read_text(), self.auth)
        self.assertEqual((self.app / "user-file.txt").read_text(), "keep")
        self.assertEqual(updater.read_json(self.app / updater.VERSION_FILE), self.version)
        self.assertEqual((self.backup() / "previous/app.py").read_text(), self.old_app)
        self.assertTrue((self.backup() / "application.tar.gz").exists())
        self.assertTrue((self.backup() / "system.tar.gz").exists())
        subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=self.backup(),
                       check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(controller.calls, [
            "validate system", "backup system", "read status", "stop app", "start app",
            ("check HTTPS", True, True),
        ])

    def test_changed_dependencies_use_separate_runtime(self):
        environment = self.parent / "new-runtime"
        environment.mkdir()
        with patch.object(updater, "prepare_environment", return_value=environment):
            self.apply(FakeController(), reuse=False)
        self.assertEqual((self.app / "venv").resolve(), environment)
        self.assertEqual((self.backup() / "previous/venv/bin/python").read_text(), "original runtime")

    def test_failed_https_restores_original_application_and_runtime(self):
        controller = FakeController(fail_health=True)
        old_config = (self.app / "config.json").read_bytes()
        inode = (self.app / "venv").stat().st_ino
        with self.assertRaisesRegex(updater.UpdateError, "Previous application restored"):
            self.apply(controller)
        self.assertEqual((self.app / "app.py").read_text(), self.old_app)
        self.assertEqual((self.app / "config.json").read_bytes(), old_config)
        self.assertEqual((self.app / "venv").stat().st_ino, inode)
        self.assertFalse((self.app / updater.VERSION_FILE).exists())
        self.assertFalse((self.parent / ".hp6033a-update.pending").exists())
        self.assertTrue(controller.active())
        self.assertEqual(controller.calls[-3:], ["stop app", "start app", ("check HTTPS", True, False)])
        self.assertTrue((self.backup() / "application.tar.gz").exists())

    def test_failed_recovery_keeps_pending_marker_and_private_backup(self):
        controller = FakeController(fail_health=True)
        with patch.object(updater, "restore_application",
                          side_effect=updater.UpdateError("simulated restore failure")):
            with self.assertRaisesRegex(updater.UpdateError, "rollback could not be verified"):
                self.apply(controller)
        marker = self.parent / ".hp6033a-update.pending"
        self.assertEqual(Path(marker.read_text().strip()), self.backup())
        self.assertTrue((self.backup() / "previous/app.py").is_file())
        self.assertTrue((self.backup() / "application.tar.gz").is_file())

    def test_failed_candidate_validation_leaves_service_running(self):
        controller = FakeController()
        with patch.object(updater, "validate_candidate", side_effect=updater.UpdateError("bad template")):
            with self.assertRaisesRegex(updater.UpdateError, "bad template"):
                updater.apply_update(self.app, self.source, self.version, controller, True, True)
        self.assertEqual(controller.calls, [])
        self.assertEqual((self.app / "app.py").read_text(), self.old_app)

    def test_legacy_nginx_credential_can_be_migrated_without_reset(self):
        auth = self.app / ".control_htpasswd"
        auth.unlink()
        with patch.object(updater, "output", return_value=self.auth):
            updater.prepare_credentials(self.app, "0", [Path("/etc/nginx/hp6033a.htpasswd")])
        self.assertEqual(auth.read_text(), self.auth)
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)


class ReleaseTests(unittest.TestCase):
    def test_download_accepts_exact_tag_commit_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            repository = folder / "repository"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            git = ["git", "-C", str(repository)]
            (repository / "installer").write_text("fixture\n")
            subprocess.run([*git, "add", "installer"], check=True)
            subprocess.run([*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "Release"], check=True)
            commit = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
            for tag in ("v1.2.3", "v1.10.1", "v2.0.0-rc1"):
                subprocess.run([*git, "tag", tag], check=True)
            self.assertEqual(updater.stable_releases(str(repository)), ["v1.10.1", "v1.2.3"])
            checkout = folder / "verified"
            self.assertEqual(updater.fetch_release("v1.2.3", checkout, commit, str(repository)), commit)
            self.assertEqual((checkout / "installer").read_text(), "fixture\n")
            mismatch = folder / "mismatch"
            with self.assertRaisesRegex(updater.UpdateError, "does not match"):
                updater.fetch_release("v1.2.3", mismatch, "0" * 40, str(repository))
            self.assertFalse((mismatch / "installer").exists())

    def test_dry_run_does_not_probe_or_change_services(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "hp6033a"
            app.mkdir()
            site = root / "nginx"
            site.touch()
            enabled = root / "enabled"
            enabled.symlink_to(site)
            arguments = ["update.sh", "--release", "v1.2.3", "--url", "https://localhost", "--dry-run"]
            log = StringIO()
            with (
                patch.object(sys, "argv", arguments),
                patch.object(updater.Path, "home", return_value=root),
                patch.object(updater.os, "geteuid", return_value=1000),
                patch.object(updater, "SITE", site),
                patch.object(updater, "ENABLED_SITE", enabled),
                patch.object(updater, "validate_live_files"),
                patch.object(updater, "service_properties"),
                patch.object(updater, "validate_service"),
                patch.object(updater, "nginx_details", return_value=("0", {})),
                patch.object(updater, "fetch_release", return_value="a" * 40),
                patch.object(updater, "requirements"),
                patch.object(updater, "reusable_environment", return_value=True),
                patch.object(updater, "installed_version", return_value="unversioned"),
                patch.object(updater, "changes_to_apply", return_value=["app.py"]),
                patch.object(updater.Controller, "status") as probe,
                patch.object(updater, "apply_update") as apply,
                redirect_stdout(log),
            ):
                updater.main()
            probe.assert_not_called()
            apply.assert_not_called()
            self.assertIn("app.py", log.getvalue())
            self.assertFalse((root / ".hp6033a-update.lock").exists())

    def test_compatibility_accepts_installed_service_and_legacy_nginx(self):
        app = Path("/home/pi/hp6033a")
        exe = app / "venv/bin/gunicorn"
        properties = {
            "User": "pi", "WorkingDirectory": str(app), "FragmentPath": str(updater.UNIT),
            "ActiveState": "active", "UnitFileState": "enabled",
            "ExecStart": (
                f"{{ path={exe} ; argv[]={exe} -w 1 -b unix:{updater.SOCKET} app:app ; "
                "ignore_errors=no ; status=0/0 }"
            ),
        }
        updater.validate_service(properties, app, "pi")
        with self.assertRaisesRegex(updater.UpdateError, "one Gunicorn worker"):
            updater.validate_service({**properties, "ExecStart": properties["ExecStart"].replace("-w 1", "-w 2")}, app, "pi")
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            template = (ROOT / "deploy/nginx.conf.example").read_text()
            site.write_text("\n".join(line for line in template.splitlines() if "Password-Free" not in line))
            mode, files = updater.nginx_details(site)
            self.assertEqual(mode, "0")
            self.assertEqual(files["auth_basic_user_file"], [Path("/etc/nginx/hp6033a.htpasswd")])
