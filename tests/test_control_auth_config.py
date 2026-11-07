from pathlib import Path
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDER = runpy.run_path(str(ROOT / 'deploy/render-control-auth.py'))['render']
TEMPLATE = (ROOT / 'deploy/nginx.conf.example').read_text()


class ControlAuthConfigTests(unittest.TestCase):
    def test_password_free_keeps_https_origin_check_and_lan_allowlist(self):
        site = RENDER(TEMPLATE, 'password-free')
        self.assertIn('auth_basic off;', site)
        self.assertNotIn('auth_basic_user_file', site)
        self.assertEqual(site.count('X-HP6033A-Password-Free 1;'), 2)
        for directive in (
            'listen 443 ssl;',
            'if ($http_origin != "https://$host") {',
            'allow LAN_SUBNET;',
            'deny all;',
        ):
            self.assertEqual(site.count(directive), TEMPLATE.count(directive))

    def test_switching_back_restores_template_exactly(self):
        site = RENDER(TEMPLATE, 'password-free')
        self.assertEqual(RENDER(site, 'protected'), TEMPLATE)

    def test_existing_site_without_mode_headers_is_supported(self):
        old = TEMPLATE.replace('        proxy_set_header X-HP6033A-Password-Free 0;\n', '')
        self.assertEqual(RENDER(old, 'protected'), TEMPLATE)

    def test_dashboard_delegates_authentication_without_removing_origin_or_lan_checks(self):
        site = RENDER(TEMPLATE, 'dashboard')
        self.assertIn('auth_basic off;', site)
        self.assertNotIn('auth_basic_user_file', site)
        self.assertEqual(site.count('X-HP6033A-Password-Free 2;'), 2)
        self.assertEqual(site.count('allow LAN_SUBNET;'), TEMPLATE.count('allow LAN_SUBNET;'))
        self.assertEqual(site.count('return 403;'), TEMPLATE.count('return 403;'))
        self.assertEqual(RENDER(site, 'protected'), TEMPLATE)

    def test_unrecognized_site_is_rejected(self):
        with self.assertRaises(ValueError):
            RENDER(TEMPLATE.replace('X-Forwarded-Proto', 'Other-Header'), 'password-free')
