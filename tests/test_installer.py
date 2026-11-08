import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class InstallerSafetyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.script = INSTALLER.read_text(encoding="utf-8")

    def test_main_function_exists(self):
        self.assertRegex(
            self.script,
            r"(?m)^main\(\)\s*\{"
        )

    def test_main_runs_only_when_executed_directly(self):
        script = INSTALLER.read_text(encoding="utf-8")

        expected = (
            'if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then\n'
            '    main "$@"\n'
            'fi'
        )

        self.assertTrue(
            script.rstrip().endswith(expected),
            "Installer must invoke main only when executed directly."
        )

        self.assertEqual(
            script.count('    main "$@"'),
            1,
            "Installer must have exactly one guarded main invocation."
        )


    def test_recovery_functions_exist(self):
        for name in (
            "installation_failed",
            "safe_installation_recovery",
            "rollback_nginx_activation",
        ):
            with self.subTest(function=name):
                self.assertIn(f"{name}() {{", self.script)


if __name__ == "__main__":
    unittest.main()


class RecoveryStructureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.script = INSTALLER.read_text(encoding="utf-8")

    def get_function(self, name):
        start = self.script.index(f"{name}() {{")
        end = self.script.index("\n}\n", start)
        return self.script[start:end]

    def test_nginx_rollback_runs_first(self):
        handler = self.get_function("installation_failed")

        nginx_pos = handler.index("rollback_nginx_activation")
        app_pos = handler.index("safe_installation_recovery")

        self.assertLess(nginx_pos, app_pos)

    def test_application_recovery_is_independent(self):
        handler = self.get_function("installation_failed")

        self.assertIn(
            "if ! rollback_nginx_activation; then",
            handler
        )
        self.assertIn(
            "if ! safe_installation_recovery; then",
            handler
        )

    def test_original_error_code_preserved(self):
        handler = self.get_function("installation_failed")

        self.assertIn('local exit_code="$1"', handler)
        self.assertIn('exit "$exit_code"', handler)


class InstallationConflictBehaviorTests(unittest.TestCase):

    def run_conflict_check(self, existing_app=False, service_state="not-found"):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="hp6033a-conflict-") as tmp:
            root = Path(tmp)
            script = INSTALLER.read_text(encoding="utf-8")

            start = script.index("check_installation_conflicts() {")
            end = script.index("\n}\n", start) + 2
            function = script[start:end]

            paths = {
                "/etc/systemd/system/hp6033a.service": str(root / "service"),
                "/etc/nginx/sites-available/hp6033a": str(root / "nginx-available"),
                "/etc/nginx/sites-enabled/hp6033a": str(root / "nginx-enabled"),
            }

            for old, new in paths.items():
                function = function.replace(old, new)

            app = root / "application"

            if existing_app:
                app.mkdir()

            test_script = f"""
set -e
APP_DIR={str(app)!r}

systemctl() {{
    if [[ "$*" == "show -p LoadState --value hp6033a.service" ]]; then
        echo {service_state!r}
    else
        return 99
    fi
}}

{function}

check_installation_conflicts
"""

            return subprocess.run(
                ["bash", "-c", test_script],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_clean_installation_accepted(self):
        result = self.run_conflict_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_application_rejected(self):
        result = self.run_conflict_check(existing_app=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing HP 6033A installation", result.stderr)

    def test_existing_systemd_service_rejected(self):
        result = self.run_conflict_check(service_state="loaded")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already registered", result.stderr)


class NginxConflictBehaviorTests(unittest.TestCase):

    def run_nginx_check(self, nginx_test_ok=True, listeners="", nginx_installed=True, preexisting=True):
        import subprocess

        script = INSTALLER.read_text(encoding="utf-8")
        start = script.index("check_nginx_conflicts() {")
        end = script.index("\n}\n", start) + 2
        function = script[start:end]

        mock = f"""
set -euo pipefail

command() {{
    if [[ "$*" == "-v nginx" ]]; then
        [[ {nginx_installed!r} == True ]]
        return
    fi
    builtin command "$@"
}}

sudo() {{
    if [[ "$*" == "nginx -t" ]]; then
        return {0 if nginx_test_ok else 1}
    elif [[ "$*" == "nginx -T" ]]; then
        printf '%s\\\\n' {listeners!r}
        return 0
    fi
    return 99
}}

{function}
NGINX_PREEXISTING={'true' if preexisting else 'false'}
check_nginx_conflicts
"""

        return subprocess.run(
            ["bash", "-c", mock],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_nginx_not_installed_accepted(self):
        result = self.run_nginx_check(nginx_installed=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not installed", result.stdout)

    def test_existing_listener_rejected(self):
        result = self.run_nginx_check(
            listeners="listen 80 default_server;"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active HTTP/HTTPS listener", result.stderr)

    def test_https_listener_rejected(self):
        result = self.run_nginx_check(
            listeners="listen 443 ssl;"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active HTTP/HTTPS listener", result.stderr)

    def test_ipv6_https_listener_rejected(self):
        result = self.run_nginx_check(
            listeners="listen [::]:443 ssl;"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active HTTP/HTTPS listener", result.stderr)

    def test_unused_nginx_accepted(self):
        result = self.run_nginx_check(
            listeners="listen 8080;"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_nginx_configuration_rejected(self):
        result = self.run_nginx_check(nginx_test_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration is invalid", result.stderr)


    def test_fresh_install_with_unexpected_https_listener_rejected(self):
        result = self.run_nginx_check(
            listeners="listen 80 default_server;\\nlisten 443 ssl;",
            preexisting=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active HTTP/HTTPS listener", result.stderr)


class AuthenticationDependencyTests(unittest.TestCase):

    def test_bcrypt_is_pinned(self):
        requirements = INSTALLER.parent / "requirements.txt"
        content = requirements.read_text(encoding="utf-8")

        self.assertRegex(content, r"(?m)^bcrypt==5\.0\.0$")

    def test_authentication_uses_bcrypt(self):
        auth_script = INSTALLER.parent / "deploy" / "setup-auth.sh"
        content = auth_script.read_text(encoding="utf-8")

        self.assertIn("bcrypt.hashpw(", content)
        self.assertIn("bcrypt.gensalt()", content)

    def test_authentication_refuses_existing_credentials(self):
        auth_script = INSTALLER.parent / "deploy" / "setup-auth.sh"
        content = auth_script.read_text(encoding="utf-8")

        self.assertIn("Refusing to overwrite credentials.", content)
        self.assertIn("os.O_EXCL", content)

    def test_authentication_file_permissions(self):
        auth_script = INSTALLER.parent / "deploy" / "setup-auth.sh"
        content = auth_script.read_text(encoding="utf-8")

        self.assertIn("os.O_CREAT | os.O_EXCL, 0o600", content)
        self.assertIn("-m 0640", content)
        self.assertIn("chmod 600", content)


class HttpsRecoveryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.script = INSTALLER.read_text(encoding="utf-8")

    def test_openssl_dependency_present(self):
        block = self.script[
            self.script.index("install_dependencies() {"):
            self.script.index("\n}\n", self.script.index("install_dependencies() {"))
        ]
        self.assertIn("openssl", block)

    def test_certificate_attempt_tracking(self):
        self.assertIn("CERT_CREATION_ATTEMPTED=false", self.script)
        self.assertIn("CERT_CREATION_ATTEMPTED=true", self.script)

    def test_partial_certificate_recovery_reported(self):
        recovery = self.script[
            self.script.index("safe_installation_recovery() {"):
            self.script.index("\n}\n", self.script.index("safe_installation_recovery() {"))
        ]
        self.assertIn("HTTPS certificate creation was incomplete", recovery)

    def test_partial_authentication_recovery_reported(self):
        recovery = self.script[
            self.script.index("safe_installation_recovery() {"):
            self.script.index("\n}\n", self.script.index("safe_installation_recovery() {"))
        ]
        self.assertIn("Authentication setup was attempted", recovery)

    def test_installation_state_reported_on_failure(self):
        handler = self.script[
            self.script.index("installation_failed() {"):
            self.script.index("\n}\n", self.script.index("installation_failed() {"))
        ]
        self.assertIn("report_installation_state", handler)


class NetworkValidationTests(unittest.TestCase):

    def validate(self, host, subnet):
        import ipaddress
        import re

        try:
            network = ipaddress.ip_network(subnet, strict=False)
            if network.version != 4 or not network.is_private:
                return False

            try:
                address = ipaddress.ip_address(host)
                if address.version != 4 or not address.is_private:
                    return False
                if address not in network:
                    return False
                if address in (network.network_address, network.broadcast_address):
                    return False
            except ValueError:
                if re.fullmatch(r"[0-9.]+", host):
                    return False

                labels = host.split(".")
                if (
                    len(host) > 253
                    or any(
                        not re.fullmatch(
                            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                            label
                        )
                        for label in labels
                    )
                ):
                    return False

            return True
        except ValueError:
            return False

    def test_valid_network_settings(self):
        self.assertTrue(self.validate("192.168.1.230", "192.168.1.0/24"))

    def test_valid_hostname(self):
        self.assertTrue(self.validate("hp6033a.example.lan", "192.168.1.0/24"))

    def test_single_label_hostname(self):
        self.assertTrue(self.validate("hp-6033a", "192.168.1.0/24"))

    def test_wrong_subnet_rejected(self):
        self.assertFalse(self.validate("192.168.2.230", "192.168.1.0/24"))

    def test_public_ip_rejected(self):
        self.assertFalse(self.validate("8.8.8.8", "192.168.1.0/24"))

    def test_network_address_rejected(self):
        self.assertFalse(self.validate("192.168.1.0", "192.168.1.0/24"))

    def test_invalid_ipv4_rejected(self):
        self.assertFalse(self.validate("192.168.1.256", "192.168.1.0/24"))

    def test_invalid_hostname_rejected(self):
        self.assertFalse(self.validate("-hp6033a", "192.168.1.0/24"))

    def test_trailing_hyphen_rejected(self):
        self.assertFalse(self.validate("hp6033a-", "192.168.1.0/24"))

    def test_empty_hostname_label_rejected(self):
        self.assertFalse(self.validate("hp..example", "192.168.1.0/24"))

    def test_public_subnet_rejected(self):
        self.assertFalse(self.validate("hp6033a", "8.8.8.0/24"))


class InstallationCancellationTests(unittest.TestCase):

    def test_cancellation_has_distinct_return_code(self):
        script = INSTALLER.read_text(encoding="utf-8")

        start = script.index("confirm_installation() {")
        end = script.index("\n}\n", start) + 2
        function = script[start:end]

        self.assertIn(
            "Installation cancelled. No changes made.",
            function
        )
        self.assertIn("return 2", function)

    def test_main_handles_cancellation_without_recovery(self):
        script = INSTALLER.read_text(encoding="utf-8")

        start = script.index("main() {")
        end = script.index("\n}\n", start) + 2
        function = script[start:end]

        self.assertIn('confirmation_status=$?', function)
        self.assertIn('if [[ "$confirmation_status" -eq 2 ]]', function)
        self.assertIn("Installation cancelled cleanly.", function)
        self.assertIn("return 0", function)


class MainSequenceSafetyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        script = INSTALLER.read_text(encoding="utf-8")
        start = script.index("main() {")
        end = script.index("\n}\n", start) + 2
        cls.main = script[start:end]

    def test_installation_stages_remain_ordered(self):
        stages = [
            "collect_network_settings",
            "run_preflight_checks",
            "confirm_installation",
            "install_application",
            "install_dependencies",
            "initialize_configuration",
            "configure_usbgpib",
            "install_systemd_service",
            "configure_https",
            "configure_nginx",
            "configure_authentication",
            "validate_nginx_configuration",
            "validate_systemd_service",
            "verify_authentication_permissions",
            "verify_gpib_configuration",
            "verify_ready_for_activation",
        ]

        positions = [
            self.main.index(stage)
            for stage in stages
        ]

        self.assertEqual(positions, sorted(positions))

    def test_application_created_before_virtual_environment(self):
        """The application directory must exist before venv creation."""
        self.assertLess(
            self.main.index("install_application"),
            self.main.index("install_dependencies"),
        )

    def test_activation_follows_validation(self):
        self.assertLess(
            self.main.index("verify_ready_for_activation"),
            self.main.index("activate_controller || return 1"),
        )
        self.assertLess(
            self.main.index("activate_controller || return 1"),
            self.main.index("activate_nginx || return 1"),
        )

    def test_https_verified_before_enabling_startup(self):
        self.assertIn('url = f"https://{host}/api/status"', self.main)
        self.assertIn(
            'required = {"gpib_connected", "gpib_status", "mode"}',
            self.main,
        )
        self.assertLess(
            self.main.index('url = f"https://{host}/api/status"'),
            self.main.index("systemctl enable hp6033a.service"),
        )

    def test_success_message_includes_https_url(self):
        self.assertIn(
            'Controller URL: https://${CONTROLLER_HOST}/',
            self.main,
        )

    def test_activation_failure_is_reported(self):
        self.assertIn("activate_controller || return 1", self.main)
        self.assertIn("activate_nginx || return 1", self.main)


class NginxOriginSecurityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        template = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "nginx.conf.example"
        )
        cls.nginx = template.read_text(encoding="utf-8")

    def test_installer_keeps_rendered_dashboard_authentication(self):
        source = INSTALLER.read_text(encoding="utf-8")
        function = source.split("\nconfigure_nginx() {", 1)[1].split(
            "\nverify_authentication() {", 1
        )[0]
        function = "configure_nginx() {" + function
        function = function.replace(
            "/etc/nginx/sites-available/hp6033a", "$APP_DIR/test-available"
        ).replace("/etc/nginx/sites-enabled/hp6033a", "$APP_DIR/test-enabled")

        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            deploy = app / "deploy"
            deploy.mkdir()
            for name in ("nginx.conf.example", "render-control-auth.py"):
                shutil.copyfile(INSTALLER.parent / "deploy" / name, deploy / name)

            subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 function + '\nAPP_DIR="$1" CONTROLLER_HOST=192.168.1.108 '
                 'LAN_SUBNET=192.168.1.0/24 configure_nginx', "test", str(app)],
                check=True, capture_output=True, text=True,
            )
            config = (app / "hp6033a.nginx.generated").read_text(encoding="utf-8")
            self.assertEqual(config.count("X-HP6033A-Password-Free 2;"), 2)
            self.assertIn("auth_basic off;", config)
            self.assertIn("192.168.1.108", config)
            self.assertIn("192.168.1.0/24", config)
            http = config.split("listen 443 ssl;", 1)[0]
            self.assertIn("try_files \"\" @hp6033a_https_redirect;", http)
            self.assertIn("return 308 https://192.168.1.108$request_uri;", http)

    def test_control_origin_uses_request_host(self):
        self.assertIn(
            'if ($http_origin != "https://$host") {',
            self.nginx
        )

    def test_mismatched_origin_still_returns_forbidden(self):
        location = self.nginx.split(
            "location ^~ /api/control/", 1
        )[1].split("\n    }", 1)[0]

        self.assertIn("return 403;", location)

    def test_origin_is_not_hardcoded_to_controller_ip(self):
        self.assertNotIn(
            'if ($http_origin != "https://CONTROLLER_HOST") {',
            self.nginx
        )
        self.assertNotIn(
            'if ($http_origin != "https://192.168.1.177") {',
            self.nginx
        )



class InstallationStateReportingTests(unittest.TestCase):

    def test_systemd_creation_state_is_reported(self):
        self.assertIn(
            'echo "  systemd service created: $SERVICE_CREATED"',
            INSTALLER.read_text(encoding="utf-8")
        )
