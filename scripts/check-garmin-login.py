"""Read-only Garmin Connect login probe for maintainers; never prints credentials or data."""
import argparse
from getpass import getpass
from importlib.metadata import version
import logging
from pathlib import Path
from urllib.parse import urlsplit

logging.disable(logging.CRITICAL)
from garminconnect import Garmin

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tokens', required=True, type=Path)
parser.add_argument('--renew', action='store_true', help='Test a DI token refresh after login')
args = parser.parse_args()
print('garminconnect:', version('garminconnect'), flush=True)
args.tokens.mkdir(mode=0o700, parents=True, exist_ok=True)
args.tokens.chmod(0o700)
try:
    if (args.tokens / 'garmin_tokens.json').is_file():
        client = Garmin()
        client.login(str(args.tokens))
        print('Current-format cached login: OK', flush=True)
    else:
        client = Garmin(getpass('Garmin email: '), getpass('Garmin password: '),
                        prompt_mfa=lambda: getpass('Garmin MFA code: '))
        client.login()
        client.client.dump(str(args.tokens))
        print('Fresh current-format login: OK', flush=True)
    assert isinstance(client.get_activities(0, 1), list)
    print('Read-only activity request: OK', flush=True)
    if args.renew:
        if not client.client.di_refresh_token:
            print('DI renewal: not tested (this session uses a different login strategy)', flush=True)
        else:
            # Diagnostic only, pinned to 0.3.20. Unlike _refresh_session, this
            # method propagates renewal failures rather than suppressing them.
            client.client._refresh_di_token()
            client.client.dump(str(args.tokens))
            assert isinstance(client.get_activities(0, 1), list)
            print('DI renewal and subsequent activity request: OK', flush=True)
    client.client.dump(str(args.tokens))
    resumed = Garmin()
    resumed.login(str(args.tokens))
    assert isinstance(resumed.get_activities(0, 1), list)
    print('Saved-session reload and activity request: OK', flush=True)
except Exception as exc:
    print('Probe failed:', type(exc).__name__, flush=True)
    pending = [exc]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        response = getattr(error, 'response', None)
        if response is not None:
            print('HTTP status:', response.status_code, flush=True)
            print('Endpoint path:', urlsplit(str(response.url)).path, flush=True)
        pending.extend(e for e in (getattr(error, '__cause__', None), getattr(error, '__context__', None)) if isinstance(e, BaseException))
    raise SystemExit(1)
