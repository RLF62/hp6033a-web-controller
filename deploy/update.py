"""Upgrade one existing HP 6033A installation from a reviewed release tag."""

import argparse
import ast
from contextlib import contextmanager
import fcntl
import filecmp
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

REPOSITORY = "https://github.com/RLF62/hp6033a-web-controller.git"
SERVICE = "hp6033a.service"
UNIT = Path("/etc/systemd/system") / SERVICE
SITE = Path("/etc/nginx/sites-available/hp6033a")
ENABLED_SITE = Path("/etc/nginx/sites-enabled/hp6033a")
CERTIFICATE = Path("/etc/nginx/ssl/hp6033a.crt")
SOCKET = "/run/hp6033a/gunicorn.sock"
RELEASE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
COMMIT = re.compile(r"[a-f0-9]{40}")
MANAGED_FILES = ("app.py", "requirements.txt", "config.example.json")
MANAGED_DIRS = ("templates", "static", "docs", "deploy")
LIVE_FILES = ("config.json", ".control_htpasswd", ".control_auth_mode")
VERSION_FILE = ".hp6033a-version.json"


class UpdateError(RuntimeError):
    pass


class RollbackComplete(UpdateError):
    pass


class RollbackFailed(UpdateError):
    pass


def run(*args, timeout=30, **kwargs):
    try:
        return subprocess.run(args, check=True, timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"{args[0]} failed ({type(error).__name__})") from error


def output(*args, **kwargs):
    return run(*args, stdout=subprocess.PIPE, text=True, **kwargs).stdout


def read_json(path):
    try:
        value = json.loads(path.read_text())
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    raise UpdateError(f"Expected a JSON object in {path}")


def write_json(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def installed_version(app):
    metadata = app / VERSION_FILE
    if metadata.is_file():
        version = read_json(metadata)
        release, commit = version.get("release", ""), version.get("commit", "")
        if (isinstance(release, str) and RELEASE.fullmatch(release)
                and isinstance(commit, str) and COMMIT.fullmatch(commit)):
            return f"{release} ({commit[:12]})"
    digest = hashlib.sha256((app / "app.py").read_bytes()).hexdigest()[:12]
    return f"unversioned installation (app.py SHA-256 prefix {digest})"


def stable_releases(repository=REPOSITORY):
    refs = output("git", "ls-remote", "--refs", "--tags", repository, timeout=45)
    versions = []
    for line in refs.splitlines():
        object_id, separator, ref = line.partition("\t")
        tag = ref.removeprefix("refs/tags/")
        match = RELEASE.fullmatch(tag)
        if separator and match and ref.startswith("refs/tags/") and COMMIT.fullmatch(object_id):
            versions.append((tuple(map(int, match.groups())), tag))
    return [tag for _, tag in sorted(versions, reverse=True)]


def fetch_release(tag, destination, expected=None, repository=REPOSITORY):
    if not RELEASE.fullmatch(tag):
        raise UpdateError("Choose an explicit release tag such as v1.2.3")
    ref = f"refs/tags/{tag}"
    run("git", "init", "--quiet", str(destination))
    run("git", "-C", str(destination), "fetch", "--quiet", "--depth=1",
        "--no-tags", repository, f"{ref}:{ref}", timeout=120)
    commit = output("git", "-C", str(destination), "rev-parse", f"{ref}^{{commit}}").strip()
    if expected is not None and expected != commit:
        raise UpdateError("The release commit does not match --expect-commit")
    run("git", "-C", str(destination), "fsck", "--strict", "--no-dangling", timeout=120)
    run("git", "-C", str(destination), "checkout", "--quiet", "--detach", commit)
    return commit


def requirements(source):
    pins = {}
    requirements_file = source / "requirements.txt"
    safe_node(requirements_file)
    for line in requirements_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", line)
        if not match or match[1].lower() in pins:
            raise UpdateError("The release requirements must contain unique exact package pins")
        pins[match[1].lower()] = match[2]
    if not {"flask", "gunicorn", "pyvisa", "bcrypt"} <= pins.keys():
        raise UpdateError("Release requirements are missing controller dependencies")
    return pins


def reusable_environment(python, pins):
    script = (
        "import importlib.metadata as m, json, sys;"
        "sys.exit(not all(m.version(name)==version "
        "for name,version in json.loads(sys.argv[1]).items()))"
    )
    try:
        run(str(python), "-c", script, json.dumps(pins), stderr=subprocess.DEVNULL)
        run(str(python), "-m", "pip", "check", stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return True
    except UpdateError:
        return False


def prepare_environment(parent, source):
    # Keep this absolute path after activation so generated console scripts
    # retain valid shebangs. The app's venv entry will point to this directory.
    environment = Path(tempfile.mkdtemp(prefix=".hp6033a-venv-", dir=parent))
    try:
        run(sys.executable, "-m", "venv", str(environment), timeout=120)
        python = environment / "bin/python"
        run(str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "-r", str(source / "requirements.txt"), timeout=900)
        run(str(python), "-m", "pip", "check")
        return environment
    except BaseException:
        shutil.rmtree(environment)
        raise


def safe_node(path, directory=False):
    if path.is_symlink() or not (path.is_dir() if directory else path.is_file()):
        raise UpdateError(f"Expected a regular {'directory' if directory else 'file'}: {path}")


def release_nodes(source):
    for name in MANAGED_FILES:
        origin = source / name
        safe_node(origin)
        yield origin
    for name in MANAGED_DIRS:
        root = source / name
        safe_node(root, directory=True)
        yield root
        for origin in sorted(root.rglob("*")):
            safe_node(origin, directory=origin.is_dir())
            yield origin


def overlay_release(source, candidate):
    for origin in release_nodes(source):
        destination = candidate / origin.relative_to(source)
        if origin.is_dir():
            make_directory(destination)
        else:
            install_file(origin, destination)


def changes_to_apply(source, app):
    for origin in release_nodes(source):
        if origin.is_file():
            relative = origin.relative_to(source)
            target = app / relative
            if target.is_symlink() or not target.is_file() or not filecmp.cmp(origin, target, shallow=False):
                yield str(relative)


def make_directory(path):
    if path.is_symlink():
        raise UpdateError(f"Refusing to overlay a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)


def install_file(origin, destination):
    # Directory ancestors are checked by the walk before files are copied.
    make_directory(destination.parent)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise UpdateError(f"Refusing to overwrite a special path: {destination}")
    shutil.copy2(origin, destination)


def copy_application(app, candidate):
    def ignore(directory, names):
        excluded = {"__pycache__"}
        if Path(directory) == app:
            excluded |= {"venv", ".git"}
        return excluded.intersection(names)
    shutil.copytree(app, candidate, symlinks=True, ignore=ignore)


def legacy_visa_resource(app):
    tree = ast.parse((app / "app.py").read_text())
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "GPIB_RESOURCE"
            for target in statement.targets
        ):
            continue
        value = statement.value
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get" and len(value.args) == 2
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == "HP6033A_GPIB_RESOURCE"):
            value = value.args[1]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if re.fullmatch(r"(?:USB|GPIB)[0-9]*::[^\r\n\0]+::INSTR", value.value, re.I):
                return value.value
    return None


def migrate_configuration(previous, candidate):
    config = read_json(candidate / "config.json")
    if config.get("gpib_resource"):
        return
    resource = legacy_visa_resource(previous)
    if resource:
        config["gpib_resource"] = resource
        temporary = candidate / ".hp6033a-config.new"
        write_json(temporary, config)
        temporary.chmod((candidate / "config.json").stat().st_mode & 0o777)
        temporary.replace(candidate / "config.json")
        print("Preserved the legacy VISA resource in config.json.")


def validate_candidate(candidate, python):
    script = """
import importlib
import sys
sys.path.insert(0, sys.argv[1])
for module in ('gunicorn', 'bcrypt', 'pyvisa', 'pyvisa_py', 'usb', 'serial'):
    importlib.import_module(module)
from app import app
for name in app.jinja_env.list_templates():
    app.jinja_env.get_template(name)
with app.test_client() as client:
    for route in ('/', '/classic-dual', '/docs/'):
        if client.get(route).status_code != 200:
            raise SystemExit(f'Candidate page failed: {route}')
"""
    run(str(python), "-B", "-c", script, str(candidate), cwd=candidate)


def present(path):
    return path.exists() or path.is_symlink()


def service_properties():
    names = ("User", "WorkingDirectory", "FragmentPath", "DropInPaths", "EnvironmentFiles",
             "ExecStart", "ActiveState", "UnitFileState")
    value = output("systemctl", "--no-pager", "--full", "show", SERVICE,
                   *(f"--property={name}" for name in names))
    return dict(line.split("=", 1) for line in value.splitlines() if "=" in line)


def validate_service(properties, app, user):
    if (properties.get("User") != user
            or properties.get("WorkingDirectory") != str(app)
            or properties.get("FragmentPath") != str(UNIT)
            or properties.get("ActiveState") != "active"
            or properties.get("UnitFileState") != "enabled"):
        raise UpdateError(f"Expected the active, enabled {SERVICE} unit for {user} at {app}")
    command = re.fullmatch(r"\{ path=([^;]+) ; argv\[\]=(.+?) ; [^{}]+\s*\}",
                           properties.get("ExecStart", ""))
    executable = str(app / "venv/bin/gunicorn")
    if not command or command[1] != executable:
        raise UpdateError("Unsupported Gunicorn service command")
    argv = shlex.split(command[2])
    if not argv:
        raise UpdateError("Missing Gunicorn service arguments")
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("-w", "--workers")
    parser.add_argument("-b", "--bind")
    try:
        options, remainder = parser.parse_known_args(argv[1:])
    except argparse.ArgumentError:
        raise UpdateError("Could not parse the Gunicorn service arguments") from None
    if (argv[0] != executable or options.workers not in ("1", None)
            or options.bind != f"unix:{SOCKET}" or remainder != ["app:app"]):
        raise UpdateError("Expected one Gunicorn worker bound to the controller Unix socket")


def nginx_details(site):
    content = site.read_text()
    if (not re.search(rf"^\s*proxy_pass\s+http://unix:{re.escape(SOCKET)}:?;", content, re.M)
            or not re.search(r"^\s*deny all;", content, re.M)):
        raise UpdateError("Unsupported HP 6033A Nginx site")
    directives = re.findall(r"^\s*proxy_set_header X-HP6033A-Password-Free ([0-3]);",
                            content, re.M)
    if directives and (len(directives) != 2 or len(set(directives)) != 1):
        raise UpdateError("Inconsistent Nginx authentication policy")
    mode = directives[0] if directives else "0"
    if mode == "3":
        if (re.findall(r"^\s*listen\s+([^;]+);", content, re.M) != ["80"]
                or 'if ($http_origin != "http://$host")' not in content):
            raise UpdateError("Expected the LAN-only HTTP controller site")
    elif not re.search(r"^\s*listen\s+[^;]*\b443\s+ssl\b", content, re.M):
        raise UpdateError("Unsupported HP 6033A Nginx HTTPS site")
    auth = re.findall(r"^\s*auth_basic\s+([^;]+);", content, re.M)
    if len(auth) != 1 or (auth[0].strip() == "off") != (mode in ("1", "2", "3")):
        raise UpdateError("Unrecognized Nginx control authentication policy")
    security_files = {"ssl_certificate": [], "ssl_certificate_key": [], "auth_basic_user_file": []}
    for key in security_files:
        for value in re.findall(rf"^\s*{key}\s+([^;]+);", content, re.M):
            parts = shlex.split(value)
            if len(parts) != 1 or not parts[0].startswith("/") or "$" in parts[0]:
                raise UpdateError(f"Unsupported Nginx {key} path")
            security_files[key].append(Path(parts[0]))
    if mode == "3":
        if any(security_files.values()):
            raise UpdateError("Unexpected credentials or TLS files in the HTTP-only site")
    elif not security_files["ssl_certificate"] or not security_files["ssl_certificate_key"]:
        raise UpdateError("Nginx TLS certificate or key path was not found")
    if mode == "0" and len(security_files["auth_basic_user_file"]) != 1:
        raise UpdateError("Expected the legacy Nginx controller credential file")
    return mode, security_files


def validate_live_files(app):
    for name in ("app.py", "config.json"):
        safe_node(app / name)
    for name in (".control_htpasswd", ".control_auth_mode", VERSION_FILE):
        if present(app / name):
            safe_node(app / name)
    read_json(app / "config.json")
    mode_file = app / ".control_auth_mode"
    if mode_file.exists() and mode_file.read_text().strip() not in ("protected", "password-free"):
        raise UpdateError("Unrecognized saved authentication mode")
    if not os.access(app / "venv/bin/python", os.X_OK) or not os.access(
        app / "venv/bin/gunicorn", os.X_OK
    ):
        raise UpdateError("The existing virtual environment is incomplete")


def check_credentials(data):
    if not re.fullmatch(r"[^:\r\n]+:\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}\r?\n?", data):
        raise UpdateError("Expected one compatible bcrypt controller credential")


def prepare_credentials(candidate, mode, nginx_auth):
    auth = candidate / ".control_htpasswd"
    if auth.exists():
        data = auth.read_text()
        check_credentials(data)
        if mode == "0":
            original = output("sudo", "cat", "--", str(nginx_auth[0]))
            if original.strip() != data.strip():
                raise UpdateError("The app and Nginx controller credentials differ")
        if auth.stat().st_mode & 0o077:
            raise UpdateError("Controller credential file must be private (mode 0600)")
    elif mode == "0":
        data = output("sudo", "cat", "--", str(nginx_auth[0]))
        check_credentials(data)
        fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
        print("Copied the existing Nginx login to the upgraded app.")
    elif mode == "2":
        raise UpdateError("Dashboard-managed authentication requires the app credential file")


class Controller:
    def __init__(self, url, ca, properties, mode, security):
        self.url = url
        self.scheme = "http" if mode == "3" else "https"
        if urlsplit(url).scheme != self.scheme:
            raise UpdateError(f"Use the installed controller's {self.scheme.upper()} URL")
        self.ca = ca
        self.properties = properties
        self.mode = mode
        self.security = security

    def get(self, route):
        host = urlsplit(self.url).hostname
        port = 443 if self.scheme == "https" else 80
        tls = ("--cacert", str(self.ca)) if self.scheme == "https" else ()
        return output("curl", "--disable", "--noproxy", "*", "--proto", f"={self.scheme}",
                      "--fail", "--silent", "--show-error", "--max-time", "8",
                      "--resolve", f"{host}:{port}:127.0.0.1", *tls,
                      self.url + route, timeout=10)

    def status(self):
        try:
            status = json.loads(self.get("/api/status"))
        except ValueError:
            raise UpdateError("Controller status was not valid JSON") from None
        if (not isinstance(status, dict) or type(status.get("gpib_connected")) is not bool
                or status.get("remote_control_enabled") is not False):
            raise UpdateError("Expected a known read-only controller state")
        if status.get("output") is True or (status["gpib_connected"] and status.get("output") is not False):
            raise UpdateError("The supply output must be confirmed OFF before an update")
        return status["gpib_connected"]

    def check_health(self, previously_connected, pages):
        for attempt in range(6):
            try:
                connected = self.status()
                if previously_connected and not connected:
                    raise UpdateError("GPIB stopped responding after the update")
                if pages:
                    for path, marker in (("/", "remote_enable"), ("/classic-dual", "adjust-knob"),
                                         ("/docs/", "Documentation library")):
                        if marker not in self.get(path):
                            raise UpdateError(f"Unexpected application page: {path}")
                return
            except UpdateError:
                if attempt == 5:
                    raise
                time.sleep(1)

    def validate_system(self):
        run("sudo", "nginx", "-t")

    def backup_system(self, destination, privilege=("sudo",), extra=()):
        paths = [UNIT, SITE, ENABLED_SITE, Path("/etc/nginx/nginx.conf"), *extra]
        for path in (Path("/etc/nginx/hp6033a.htpasswd"), Path("/etc/udev/rules.d/99-usbgpib.rules")):
            if present(path):
                paths.append(path)
        paths += [Path(path) for path in shlex.split(self.properties.get("DropInPaths", ""))]
        envfiles = re.findall(r"(?:^|\s)(/[^\s]+)\s+\(ignore_errors=(?:yes|no)\)",
                              self.properties.get("EnvironmentFiles", ""))
        paths += [Path(path) for path in envfiles if present(Path(path))]
        paths += [path for files in self.security.values() for path in files]
        for path in list(paths):
            resolved = output(*privilege, "readlink", "-e", "--", str(path)).strip()
            if resolved and Path(resolved) != path:
                paths.append(Path(resolved))
        with destination.open("xb") as archive:
            run(*privilege, "tar", "-C", "/", "-czf", "-", "--",
                *(str(path).lstrip("/") for path in sorted(set(paths))),
                stdout=archive, timeout=120)
        destination.chmod(0o600)
        run("tar", "-tzf", str(destination), stdout=subprocess.DEVNULL)

    def stop(self):
        run("sudo", "systemctl", "stop", SERVICE, timeout=60)

    def start(self):
        run("sudo", "systemctl", "start", SERVICE, timeout=60)
        run("systemctl", "is-active", "--quiet", SERVICE)

    def active(self):
        return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE],
                              check=False, timeout=10).returncode == 0


def refresh_live_settings(app, candidate):
    for name in LIVE_FILES:
        source, target = app / name, candidate / name
        if present(source):
            safe_node(source)
            install_file(source, target)
        elif target.exists():
            target.unlink()


def backup_application(app, destination):
    run("tar", "-C", str(app), "--exclude=./venv", "--exclude=./.git",
        "--exclude=__pycache__", "-czf", str(destination), ".", timeout=120)
    destination.chmod(0o600)
    run("tar", "-tzf", str(destination), stdout=subprocess.DEVNULL)


@contextmanager
def rollback_signals():
    signals = (signal.SIGINT, signal.SIGTERM)
    handlers = {signum: signal.signal(signum, signal.SIG_IGN) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def restore_application(app, backup, controller, reuse, connected):
    previous, candidate, failed = (backup / name for name in ("previous", "candidate", "failed"))
    if previous.exists():
        if app.exists():
            if controller.active():
                controller.stop()
            if controller.active():
                raise UpdateError("Could not stop the upgraded service for rollback")
            app.rename(failed)
        if reuse and not present(previous / "venv"):
            runtime = (failed if failed.exists() else candidate) / "venv"
            if not present(runtime):
                raise UpdateError("Original virtual environment could not be located")
            runtime.rename(previous / "venv")
        previous.rename(app)
    if not controller.active():
        controller.start()
    controller.check_health(connected, pages=False)


def apply_update(app, source, version, controller, reuse, connected):
    backup = Path(tempfile.mkdtemp(prefix=".hp6033a-backup-", dir=app.parent))
    candidate, previous = backup / "candidate", backup / "previous"
    pending = app.parent / ".hp6033a-update.pending"
    print(f"Private backup and recovery directory: {backup}", flush=True)
    new_env = None
    activated = False
    try:
        copy_application(app, candidate)
        overlay_release(source, candidate)
        metadata = candidate / VERSION_FILE
        if present(metadata):
            safe_node(metadata)
            metadata.unlink()
        write_json(metadata, version)
        if reuse:
            python = app / "venv/bin/python"
        else:
            new_env = prepare_environment(app.parent, source)
            python = new_env / "bin/python"
            (candidate / "venv").symlink_to(new_env, target_is_directory=True)
        migrate_configuration(app, candidate)
        prepare_credentials(candidate, controller.mode, controller.security["auth_basic_user_file"])
        validate_candidate(candidate, python)
        controller.validate_system()
        system_backup = controller.backup_system(backup / "system.tar.gz") or "system.tar.gz"
        write_json(backup / "update.json", {
            "app": str(app), "target": version, "reused_venv": reuse,
            "new_environment": str(new_env) if new_env else None,
        })
        available = controller.status()
        if connected and not available:
            raise UpdateError("GPIB became unavailable before activation")
        with pending.open("x") as handle:
            handle.write(str(backup) + "\n")

        try:
            controller.stop()
            if controller.active():
                raise UpdateError("Controller did not stop")
            # Snapshot the latest settings after the old worker can no longer write.
            validate_live_files(app)
            refresh_live_settings(app, candidate)
            migrate_configuration(app, candidate)
            prepare_credentials(candidate, controller.mode, controller.security["auth_basic_user_file"])
            backup_application(app, backup / "application.tar.gz")
            with (backup / "SHA256SUMS").open("x") as sums:
                run("sha256sum", "application.tar.gz", system_backup, cwd=backup, stdout=sums)
            app.rename(previous)
            if reuse:
                (previous / "venv").rename(candidate / "venv")
            candidate.rename(app)
            controller.start()
            controller.check_health(connected, pages=True)
        except BaseException as error:
            print(f"Activation failed: {error}. Restoring the previous application.", file=sys.stderr)
            with rollback_signals():
                try:
                    restore_application(app, backup, controller, reuse, connected)
                    pending.unlink()
                except Exception as restore_error:
                    raise RollbackFailed(
                        f"Automatic rollback could not be verified: {restore_error}. "
                        f"Keep {backup} and {pending}. See the recovery guide."
                    ) from restore_error
            raise RollbackComplete(f"Previous application restored and checked. Backup: {backup}") from error

        activated = True
        pending.unlink()
        print(f"Updated to {version['release']}. Dashboard checks passed. Backup: {backup}")
    finally:
        if new_env and not activated and not pending.exists():
            shutil.rmtree(new_env)


def read_url(value, *, schemes=("https", "http")):
    try:
        parts = urlsplit(value)
        if (parts.scheme in schemes and parts.netloc.lower() == parts.hostname
                and re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname or "")
                and parts.path in ("", "/") and not parts.query and not parts.fragment):
            return value.rstrip("/")
    except ValueError:
        pass
    protocol = " or ".join(scheme.upper() for scheme in schemes)
    raise argparse.ArgumentTypeError(f"Use the existing controller {protocol} URL without a port")


def interrupted(signum, frame):
    raise UpdateError(f"Interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="list stable GitHub tags without installing")
    action.add_argument("--release", help="stable version tag, for example v1.2.3")
    parser.add_argument("--expect-commit", help="reviewed full commit from the release notes (required to apply)")
    parser.add_argument("--url", type=read_url, help="existing controller HTTP or HTTPS URL")
    parser.add_argument("--ca-cert", type=Path, default=CERTIFICATE,
                        help="trusted CA or the installed self-signed controller certificate")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    app = Path.home() / "hp6033a"
    if args.check:
        if args.dry_run or args.expect_commit or args.url:
            parser.error("--check is a separate read-only operation")
        if (app / "app.py").is_file():
            print(f"Installed: {installed_version(app)}")
        releases = stable_releases()
        if not releases:
            print("No stable GitHub release tags have been published.")
        else:
            print(f"Latest stable: {releases[0]}")
            for release in releases:
                print(f"{release} https://github.com/RLF62/hp6033a-web-controller/releases/tag/{release}")
        return
    if not args.url:
        parser.error("--release requires --url")
    if args.expect_commit is not None and not COMMIT.fullmatch(args.expect_commit):
        parser.error("--expect-commit must be the full 40-character lowercase commit")
    if not args.dry_run and args.expect_commit is None:
        parser.error("Applying an update requires --expect-commit from the reviewed release")
    if os.geteuid() == 0:
        raise UpdateError("Run as the existing controller user. The updater calls sudo as needed.")
    pending = app.parent / ".hp6033a-update.pending"
    if present(pending):
        raise UpdateError(f"An interrupted update needs recovery. See {pending}")
    safe_node(app, directory=True)
    validate_live_files(app)
    properties = service_properties()
    validate_service(properties, app, getpass.getuser())
    if not ENABLED_SITE.is_symlink() or ENABLED_SITE.resolve() != SITE.resolve():
        raise UpdateError("Expected the installed HP 6033A Nginx site symlink")
    mode, security = nginx_details(SITE)
    controller = Controller(args.url, args.ca_cert, properties, mode, security)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="hp6033a-release-") as directory:
        source = Path(directory)
        commit = fetch_release(args.release, source, args.expect_commit)
        pins = requirements(source)
        reuse = reusable_environment(app / "venv/bin/python", pins)
        print(f"Installed: {installed_version(app)}")
        print(f"Selected update: {args.release} ({commit})")
        print(f"Python environment: {'reuse' if reuse else 'build and validate a separate environment'}")
        changes = list(changes_to_apply(source, app))
        if changes:
            print("Release files added or replaced:")
            for change in changes:
                print(f"  {change}")
        else:
            print("Release files already match; an unversioned install can still record the release.")
        print("Preserve: settings, labels, credentials, auth mode, TLS, Nginx and systemd configuration.")
        print(f"Activation: stop/start hp6033a.service only, then run read-only {controller.scheme.upper()} checks.")
        if mode in ("0", "1"):
            print("Existing Nginx authentication policy will be retained. See the guide for optional migration.")
        if args.dry_run:
            print("Dry run complete. No installation, backup or service changes.")
            return
        lock = app.parent / ".hp6033a-update.lock"
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("Another updater is running") from None
            if present(pending):
                raise UpdateError(f"An interrupted update needs recovery. See {pending}")
            # Recheck under the lock in case another updater completed during the download.
            validate_live_files(app)
            version = {"release": args.release, "commit": commit}
            metadata = app / VERSION_FILE
            if metadata.exists():
                installed = read_json(metadata)
                if installed == version:
                    print("The selected release is already installed.")
                    return
                if installed.get("release") == args.release:
                    raise UpdateError("This tag already identifies a different installed commit")
            reuse = reusable_environment(app / "venv/bin/python", pins)
            connected = controller.status()
            if not connected:
                raise UpdateError("GPIB is disconnected. Connect the supply and verify output is OFF.")
            run("sudo", "-v", timeout=120)
            apply_update(app, source, version, controller, reuse, connected)


if __name__ == "__main__":
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        main()
    except (UpdateError, OSError, ValueError, SyntaxError) as error:
        print(f"Update stopped: {error}", file=sys.stderr)
        sys.exit(1)
