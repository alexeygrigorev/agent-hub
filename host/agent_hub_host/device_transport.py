"""Expiry-bounded transport retries for the shared device-login protocol."""
import json
import math
import time
import urllib.error
import urllib.request

from . import api


def poster(exchange=None, *, clock=time.monotonic, sleeper=time.sleep):
    deadline = None

    def post(url, body):
        nonlocal deadline
        polling = body.get('grant_type') == 'urn:ietf:params:oauth:grant-type:device_code'
        for attempt in range(6):
            remaining = deadline - clock() if polling and deadline is not None else None
            if remaining is not None and remaining <= 0:
                return 400, {'error': 'expired_token'}
            request = urllib.request.Request(
                url, data=json.dumps(body).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            )
            kwargs = {'timeout': min(10.0, remaining)} if remaining is not None else {}
            if exchange is not None and url == exchange.url:
                kwargs['_token_exchange'] = exchange
            try:
                result = api.safe_http_json(request, **kwargs)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    result = json.loads(exc.read(1024 * 1024).decode('utf-8'))
                except (ValueError, UnicodeError):
                    result = None
                finally:
                    exc.close()
                if status != 429 and not 500 <= status <= 599 and isinstance(result, dict):
                    return status, result
            except (json.JSONDecodeError, UnicodeError, urllib.error.URLError, TimeoutError):
                pass
            except RuntimeError as exc:
                # Do not hide token-exchange, origin, size or other security failures.
                if str(exc) != 'Expected JSON response':
                    raise
            else:
                if isinstance(result, dict):
                    if not polling and result.get('device_code'):
                        try:
                            lifetime = float(result.get('expires_in', 0))
                        except (ValueError, TypeError):
                            lifetime = 0
                        if math.isfinite(lifetime) and lifetime > 0:
                            deadline = clock() + lifetime
                    return 200, result
            if attempt == 5:
                raise urllib.error.URLError('Device login transport temporarily unavailable')
            delay = min(2 ** attempt, 16)
            if polling and deadline is not None:
                delay = min(delay, max(0, deadline - clock()))
            sleeper(delay)

    return post
