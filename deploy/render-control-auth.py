"""Change only the HP 6033A Nginx authentication directives."""
import argparse
from pathlib import Path
import re


def render(source, mode):
    protected = mode == 'protected'
    header = {'protected': 0, 'password-free': 1, 'dashboard': 2}[mode]
    auth = 'auth_basic "HP 6033A Control";' if protected else 'auth_basic off;'
    if protected:
        auth += '\n        auth_basic_user_file /etc/nginx/hp6033a.htpasswd;'

    source, count = re.subn(
        r'^        auth_basic (?:"HP 6033A Control"|off);'
        r'(?:\n        auth_basic_user_file /etc/nginx/hp6033a\.htpasswd;)?$',
        lambda match: '        ' + auth,
        source, flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError('Expected one HP 6033A control authentication block')

    source, count = re.subn(
        r'^(        proxy_set_header X-Forwarded-Proto \$scheme;)'
        r'(?:\n        proxy_set_header X-HP6033A-Password-Free [012];)?$',
        lambda match: match[1] + f'\n        proxy_set_header X-HP6033A-Password-Free {header};',
        source, flags=re.MULTILINE,
    )
    if count != 2:
        raise ValueError('Expected two HP 6033A HTTPS proxy locations')
    return source


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('protected', 'password-free', 'dashboard'))
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    try:
        print(render(args.source.read_text(), args.mode), end='')
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
