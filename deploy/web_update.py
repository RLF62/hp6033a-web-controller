"""Fixed-purpose systemd broker and unprivileged release worker."""

import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import update

STATE = Path("/var/lib/hp6033a-update")
HELPER_FILES = (
    STATE / "config.json",
    Path("/usr/local/lib/hp6033a-update"),
    Path("/etc/systemd/system/hp6033a-update.service"),
    Path("/etc/systemd/system/hp6033a-update.socket"),
)
API = "https://api.github.com/repos/RLF62/hp6033a-web-controller"
NOTES = "https://github.com/RLF62/hp6033a-web-controller/releases/tag/"
CHECK_INTERVAL = 6 * 60 * 60
APPROVAL_TTL = 120
MAX_MESSAGE = 4096
ROUNDS = 600_000
RUNNING = {"queued", "downloading", "staging", "backup", "activation", "verification"}
PHASES = {
    "queued": "Update approved. Checking the release again.",
    "downloading": "Downloading and verifying the pinned release.",
    "staging": "Checking compatibility and preparing the application and Python environment.",
    "backup": "Saving the current application and system configuration.",
    "activation": "Switching the application. The dashboard may briefly disconnect.",
    "verification": "Checking HTTPS and read-only controller status.",
    "complete": "Update completed and verified.",
    "rolled_back": "Verification failed. The previous application was restored and checked.",
    "failed": "The update stopped before activation. See the recovery guide and helper journal.",
    "recovery": "Automatic recovery could not be verified. See the recovery guide and job log.",
}


def write_state(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_message(stream):
    line = stream.readline(MAX_MESSAGE + 1)
    if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
        raise ValueError("Incomplete or oversized request")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Expected a JSON object")
    return message


def send_message(connection, message):
    connection.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")


def valid_pin(tag, commit):
    return (isinstance(tag, str) and update.RELEASE.fullmatch(tag) is not None
            and isinstance(commit, str) and update.COMMIT.fullmatch(commit) is not None)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def github_json(path):
    request = Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "HP6033A-Release-Checker",
    })
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        data = response.read(262145)
    if len(data) > 262144:
        raise ValueError("GitHub response too large")
    return json.loads(data)


def latest_release():
    try:
        release = github_json("/releases/latest")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise
    tag = release["tag_name"]
    if (not isinstance(tag, str) or not update.RELEASE.fullmatch(tag)
            or release.get("draft") is not False or release.get("prerelease") is not False):
        raise ValueError("Expected a published stable vMAJOR.MINOR.PATCH release")

    target = github_json("/git/ref/tags/" + tag)["object"]
    for _ in range(5):
        commit = target["sha"]
        if not valid_pin(tag, commit):
            raise ValueError("Invalid GitHub tag target")
        if target["type"] == "commit":
            return {"tag": tag, "commit": commit, "notes": NOTES + tag}
        if target["type"] != "tag":
            break
        target = github_json("/git/tags/" + commit)["object"]
    raise ValueError("Could not resolve the release tag to a commit")


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ROUNDS)


def verify_password(password, credential):
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        return False
    salt = bytes.fromhex(credential["salt"])
    expected = bytes.fromhex(credential["digest"])
    try:
        actual = password_digest(password, salt)
    except UnicodeError:
        return False
    return hmac.compare_digest(actual, expected)


def trusted_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise update.UpdateError("Updater requires root-owned systemd and Nginx configuration")


class Supervisor:
    def __init__(self, root, config, release_provider=latest_release):
        self.root = root
        self.config = config
        self.release_provider = release_provider
        self.lock = threading.Lock()
        self.latest = None
        self.checked_at = None
        self.check_error = False
        self.checking = False
        self.next_manual = 0
        self.next_password = 0
        self.approval = None
        self.job = None
        status_file = root / "job.json"
        if status_file.exists():
            self.job = update.read_json(status_file)
            if self.job.get("phase") in RUNNING:
                self.phase("recovery")

    def phase(self, name):
        with self.lock:
            self.job = {**self.job, "phase": name, "message": PHASES[name]}
            write_state(self.root / "job.json", self.job)

    def check(self):
        try:
            latest = self.release_provider()
        except (OSError, ValueError, TypeError, KeyError) as error:
            print(f"Release check failed: {type(error).__name__}", file=sys.stderr, flush=True)
            with self.lock:
                self.check_error = True
        else:
            with self.lock:
                self.latest = latest
                self.checked_at = int(time.time())
                self.check_error = False
        finally:
            with self.lock:
                self.checking = False

    def schedule_check(self):
        with self.lock:
            if self.checking or time.monotonic() < self.next_manual:
                return
            self.checking = True
            self.next_manual = time.monotonic() + 300
        threading.Thread(target=self.check, daemon=True).start()

    def periodic_checks(self):
        while True:
            self.schedule_check()
            time.sleep(CHECK_INTERVAL)

    def dispatch(self, payload):
        action = payload.get("action")
        fields = {
            "status": {"action"}, "check": {"action"},
            "authorize": {"action", "tag", "commit", "password"},
            "install": {"action", "tag", "commit", "approval"},
        }
        if not isinstance(action, str) or action not in fields or set(payload) != fields[action]:
            return 400, {"error": "Invalid update request."}
        if action == "check":
            self.schedule_check()
            return 202, {"message": "Release check requested."}
        if action == "status":
            with self.lock:
                return 200, {"latest": self.latest, "checked_at": self.checked_at,
                             "checking": self.checking, "check_error": self.check_error,
                             "job": self.job}
        tag, commit = payload["tag"], payload["commit"]
        if not valid_pin(tag, commit):
            return 400, {"error": "A stable tag and its full commit are required."}
        with self.lock:
            if self.job and self.job.get("phase") in RUNNING:
                return 409, {"error": "An update is already running."}
            if (self.check_error or not self.checked_at
                    or time.time() - self.checked_at > CHECK_INTERVAL + 300
                    or not self.latest or (tag, commit) != (self.latest["tag"], self.latest["commit"])):
                return 409, {"error": "Check for the current stable release and approve that version."}
            now = time.monotonic()
            if action == "authorize":
                if now < self.next_password:
                    return 429, {"error": "Wait a few seconds before retrying authorization."}
                self.next_password = now + 4
                if not verify_password(payload["password"], self.config["credential"]):
                    return 401, {"error": "Incorrect update administrator password."}
                token = secrets.token_urlsafe(32)
                self.approval = (token, tag, commit, now + APPROVAL_TTL)
                return 200, {"approval": token}

            supplied = payload["approval"]
            if (self.approval is None or not isinstance(supplied, str) or not supplied.isascii()
                    or not hmac.compare_digest(supplied, self.approval[0])
                    or (tag, commit) != self.approval[1:3] or now >= self.approval[3]):
                return 403, {"error": "Update approval is missing, invalid or expired."}
            self.approval = None
            job = {"id": secrets.token_hex(12), "tag": tag, "commit": commit,
                   "phase": "queued", "message": PHASES["queued"]}
            write_state(self.root / "job.json", job)
            self.job = job
        threading.Thread(target=self.perform, args=(job,), daemon=True).start()
        return 202, {"job": job}


    def perform(self, job):
        outcome = "failed"
        broker = None
        try:
            latest = self.release_provider()
            if not latest or (latest["tag"], latest["commit"]) != (job["tag"], job["commit"]):
                raise update.UpdateError("The latest release changed after approval")
            account = pwd.getpwnam(self.config["user"])
            app = Path(account.pw_dir) / "hp6033a"
            trusted_file(update.UNIT)
            trusted_file(update.SITE)
            properties = update.service_properties()
            update.validate_service(properties, app, account.pw_name)
            mode, security = update.nginx_details(update.SITE)
            if mode != "2" or update.ENABLED_SITE.resolve() != update.SITE:
                raise update.UpdateError("Web updates require the dashboard-managed Nginx site")
            self.phase("downloading")
            directory = self.root / job["id"]
            directory.mkdir(mode=0o700)
            broker = JobBroker(self, directory, properties, mode, security)
            outcome = broker.run(account, job)
        except Exception as error:
            print(f"Job {job['id']} stopped: {error}", file=sys.stderr, flush=True)
            if broker and broker.stops:
                outcome = "recovery"
        finally:
            self.phase(outcome)


class JobBroker:
    """Accept only fixed operations, over a private inherited channel for one job."""

    def __init__(self, supervisor, directory, properties, mode, security):
        self.supervisor = supervisor
        self.directory = directory
        self.controller = update.Controller(supervisor.config["url"], update.CERTIFICATE,
                                            properties, mode, security)
        self.validated = False
        self.saved = False
        self.stops = 0
        self.starts = 0

    def dispatch(self, request):
        if set(request) != {"action"}:
            raise ValueError("Unexpected privileged operation parameters")
        action = request["action"]
        if action == "staging" and not self.validated:
            self.supervisor.phase("staging")
        elif action == "validate" and not self.validated:
            update.run("/usr/sbin/nginx", "-t")
            self.validated = True
        elif action == "backup" and self.validated and not self.saved:
            self.supervisor.phase("backup")
            archive = self.directory / "system.tar.gz"
            self.controller.backup_system(archive, privilege=(), extra=HELPER_FILES)
            with (self.directory / "SHA256SUMS").open("xb") as sums:
                update.run("/usr/bin/sha256sum", archive.name, cwd=self.directory, stdout=sums)
            self.saved = True
        elif action == "stop" and self.saved and self.starts >= self.stops and self.stops < 2:
            self.supervisor.phase("activation")
            self.stops += 1
            update.run("/usr/bin/systemctl", "stop", update.SERVICE, timeout=60)
        elif (action == "start" and self.saved and self.stops and self.starts < 2
              and (not self.starts or not self.controller.active())):
            # Allow the worker to retry start during rollback after a failed activation.
            update.run("/usr/bin/systemctl", "start", update.SERVICE, timeout=60)
            self.starts += 1
            self.supervisor.phase("verification")
        else:
            raise ValueError("Operation is outside the update sequence")

    def run(self, account, job):
        env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": account.pw_dir,
            "USER": account.pw_name, "LOGNAME": account.pw_name, "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "PIP_CONFIG_FILE": "/dev/null",
        }
        parent, child = socket.socketpair()
        with parent, child, (self.directory / "worker.log").open("xb") as log:
            parent.settimeout(1800)
            command = [
                sys.executable, "-E", "-s", "-B", str(Path(__file__).resolve()),
                "worker", str(child.fileno()), self.supervisor.config["url"],
                job["tag"], job["commit"], job["id"],
            ]
            with subprocess.Popen(
                command, pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, cwd="/", env=env, user=account.pw_uid,
                group=account.pw_gid, extra_groups=[], umask=0o077,
            ) as process:
                child.close()
                try:
                    with parent.makefile("rb") as stream:
                        while True:
                            first = stream.peek(1)
                            if not first:
                                break
                            request = read_message(stream)
                            try:
                                self.dispatch(request)
                            except (OSError, ValueError, update.UpdateError) as error:
                                print(f"Job {job['id']} operation failed: {error}", file=sys.stderr,
                                      flush=True)
                                send_message(parent, {"ok": False})
                            else:
                                send_message(parent, {"ok": True})
                    code = process.wait(timeout=20)
                except BaseException:
                    parent.close()
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
        if code == 0 and self.saved and self.stops == self.starts == 1:
            return "complete"
        if code == 2 and self.stops:
            return "rolled_back"
        return "recovery" if code == 3 or self.stops else "failed"


class WorkerController(update.Controller):
    def __init__(self, connection, stream, job, tag, *args):
        super().__init__(*args)
        self.connection = connection
        self.stream = stream
        self.job = job
        self.tag = tag

    def broker(self, action):
        send_message(self.connection, {"action": action})
        if read_message(self.stream) != {"ok": True}:
            raise update.UpdateError(f"The privileged helper rejected {action}")

    def check_https(self, previously_connected, pages):
        super().check_https(previously_connected, pages)
        if pages:
            try:
                status = json.loads(self.get("/api/software-updates"))
                if status["job"]["id"] == self.job and status["installed"]["tag"] == self.tag:
                    return
            except (ValueError, KeyError, TypeError):
                pass
            raise update.UpdateError("The upgraded dashboard did not reconnect to the update helper")

    def validate_system(self):
        self.broker("validate")

    def backup_system(self, destination):
        self.broker("backup")
        reference = destination.with_name("system-backup.json")
        update.write_json(reference, {"job": self.job, "archive": str(STATE / self.job / destination.name)})
        return reference.name

    def stop(self):
        self.broker("stop")

    def start(self):
        self.broker("start")


def worker(fd, url, tag, commit, job):
    if os.geteuid() == 0 or not valid_pin(tag, commit):
        raise update.UpdateError("Release workers must be unprivileged and pinned")
    import fcntl
    app = Path.home() / "hp6033a"
    with (app.parent / ".hp6033a-update.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise update.UpdateError("Another software update is running") from None
        if update.present(app.parent / ".hp6033a-update.pending"):
            raise update.UpdateError("An interrupted update needs manual recovery")
        update.safe_node(app, directory=True)
        update.validate_live_files(app)
        properties = update.service_properties()
        update.validate_service(properties, app, getpass.getuser())
        mode, security = update.nginx_details(update.SITE)
        if mode != "2":
            raise update.UpdateError("Dashboard-managed authentication is required")
        metadata = app / update.VERSION_FILE
        if metadata.exists():
            installed = update.read_json(metadata)
            old_tag = installed.get("release", "")
            if isinstance(old_tag, str) and update.RELEASE.fullmatch(old_tag):
                version = lambda value: tuple(map(int, update.RELEASE.fullmatch(value).groups()))
                if version(tag) <= version(old_tag):
                    raise update.UpdateError("The selected release is not newer")
        with socket.socket(fileno=int(fd)) as connection, connection.makefile("rb") as stream:
            connection.settimeout(180)
            controller = WorkerController(connection, stream, job, tag, url, update.CERTIFICATE,
                                          properties, mode, security)
            if not controller.status():
                raise update.UpdateError("Connect the GPIB adapter and verify the output is OFF")
            with tempfile.TemporaryDirectory(prefix="hp6033a-web-release-") as temp:
                source = Path(temp)
                update.fetch_release(tag, source, commit)
                controller.broker("staging")
                pins = update.requirements(source)
                reuse = update.reusable_environment(app / "venv/bin/python", pins)
                update.apply_update(app, source, {"release": tag, "commit": commit},
                                    controller, reuse, True)


def handle_client(listener, supervisor, uid):
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(3)
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, peer_uid, _ = struct.unpack("3i", credentials)
        if peer_uid != uid:
            return
        try:
            with connection.makefile("rb") as stream:
                code, body = supervisor.dispatch(read_message(stream))
            send_message(connection, {"code": code, "body": body})
        except (OSError, ValueError, TypeError, RecursionError):
            return


def serve():
    if os.geteuid() != 0 or os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise update.UpdateError("Start the root-owned socket-activated updater with systemd")
    os.umask(0o077)
    trusted_file(STATE / "config.json")
    config = update.read_json(STATE / "config.json")
    uid = pwd.getpwnam(config["user"]).pw_uid
    if uid == 0:
        raise update.UpdateError("The controller account must be unprivileged")
    supervisor = Supervisor(STATE, config)
    threading.Thread(target=supervisor.periodic_checks, daemon=True).start()
    with socket.socket(fileno=3) as listener:
        while True:
            handle_client(listener, supervisor, uid)


def configure(user, url):
    if os.geteuid() != 0 or pwd.getpwnam(user).pw_uid == 0:
        raise update.UpdateError("Provision the helper as root for an unprivileged controller account")
    url = update.read_url(url)
    if (STATE / "config.json").exists():
        raise update.UpdateError("An updater configuration already exists")
    print("Software update administrator password requirements: 12 to 256 characters.")
    print("This password is separate from the controller password. Enter it twice.")
    password = getpass.getpass("New software update administrator password: ")
    if not 12 <= len(password) <= 256:
        raise update.UpdateError("Administrator password must contain 12 to 256 characters")
    if password != getpass.getpass("Repeat administrator password: "):
        raise update.UpdateError("Administrator passwords do not match")
    salt = secrets.token_bytes(32)
    os.umask(0o077)
    update.write_json(STATE / "config.json", {
        "user": user, "url": url,
        "credential": {"salt": salt.hex(), "digest": password_digest(password, salt).hex()},
    })


if __name__ == "__main__":
    try:
        command, *arguments = sys.argv[1:]
        if command == "serve" and not arguments:
            serve()
        elif command == "configure" and len(arguments) == 2:
            configure(*arguments)
        elif command == "worker" and len(arguments) == 5:
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, update.interrupted)
            worker(*arguments)
        else:
            raise update.UpdateError("Invalid updater invocation")
    except (update.UpdateError, OSError, ValueError, KeyError) as error:
        print(f"Software update stopped: {error}", file=sys.stderr, flush=True)
        sys.exit(2 if isinstance(error, update.RollbackComplete)
                 else 3 if isinstance(error, update.RollbackFailed) else 1)
