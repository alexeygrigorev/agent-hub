#!/usr/bin/env python3
"""Issue a client credential without printing it or following credential-bearing redirects."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--admin-token-file', required=True)
    parser.add_argument('--client', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    destination = None
    try:
        url = urllib.parse.urlsplit(args.url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Use an HTTPS hub origin')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.client):
            raise ValueError('Invalid client label')
        source = Path(args.admin_token_file)
        if source.is_symlink() or (os.name != 'nt' and stat.S_IMODE(source.stat().st_mode) & 0o077):
            raise ValueError('Admin credential file must be private and not a symlink')
        token = source.read_text().strip()
        if not 16 <= len(token) <= 2048:
            raise ValueError('Invalid credential file')
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Reserve the path before issuing anything. No overwrite and no symlink traversal.
        destination = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        # Cloudflare error 1010 blocks Python's default User-Agent; identify explicitly.
        opener.addheaders = [('User-Agent', 'agent-hub-host/1')]
        base = args.url.rstrip('/')
        login = urllib.request.Request(base + '/auth/token', data=urllib.parse.urlencode({'token': token}).encode(), method='POST', headers={'Content-Type':'application/x-www-form-urlencoded', 'User-Agent': 'agent-hub-host/1'})
        try:
            response = opener.open(login, timeout=20)
            response.close()
            raise ValueError('Unexpected sign-in response')
        except urllib.error.HTTPError as error:
            if error.code != 303: raise
            error.close()
        with opener.open(base + '/auth/me', timeout=20) as response:
            me = json.loads(response.read(32768))
        csrf = me.get('csrf_token')
        if me.get('authenticated') is not True or not isinstance(csrf, str):
            raise ValueError('Admin session unavailable')
        request = urllib.request.Request(base + '/api/fleet/routing/clients/' + args.client, data=b'{}', method='POST', headers={'Content-Type':'application/json','X-CSRF-Token':csrf, 'User-Agent': 'agent-hub-host/1'})
        with opener.open(request, timeout=20) as response:
            credential = json.loads(response.read(32768))
        value = credential.get('token')
        if not isinstance(value, str) or not re.fullmatch(r'hubt_[A-Za-z0-9_-]+', value):
            raise ValueError('Credential response unavailable')
        with os.fdopen(destination, 'w') as stream:
            destination = None
            stream.write(value + '\n')
        print('Saved client credential to private output file.')
    except Exception:
        if destination is not None:
            os.close(destination)
            # Keep the empty reserved file: issuance may have succeeded remotely.
        print('Client credential setup failed. Check the private input, output path, admin login and deployed routing API.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
