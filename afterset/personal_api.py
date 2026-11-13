"""Opt-in Garmin Personal API adapter; only documented endpoints are used."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator

from afterset.config import token_store


class PersonalApiRequestError(RuntimeError):
    """A sanitized upstream failure; response bodies and credentials are omitted."""


class UnsupportedPersonalApiData(RuntimeError):
    """The documented Personal API does not expose this legacy data type."""


def configured() -> bool:
    backend = os.environ.get('AFTERSET_GARMIN_BACKEND', 'connect')
    if backend not in ('connect', 'personal-api'):
        raise ValueError('AFTERSET_GARMIN_BACKEND must be connect or personal-api')
    return backend == 'personal-api'


def private_directory() -> Path:
    directory = token_store() / 'personal-api'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _key() -> str:
    if 'GARMIN_PERSONAL_API_KEY' in os.environ:
        key = os.environ['GARMIN_PERSONAL_API_KEY'].strip()
    else:
        try:
            key = (private_directory() / 'api-key').read_text().strip()
        except FileNotFoundError:
            key = ''
    if not key:
        raise ValueError('Personal API key missing; run afterset personal-api-setup in your private console')
    return key


def _request(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        suffix = f' (HTTP {status})' if isinstance(status, int) else ''
        raise PersonalApiRequestError('Garmin Personal API request failed' + suffix) from None


class PersonalApiGarmin:
    """Adapt documented responses to existing parsers without inventing data."""
    def __init__(self, key: str | None = None, *, cache: Path | None = None):
        self._key = _key() if key is None else key
        self._auth = PersonalApiAuthenticator(
            bearer_token_cache_file=cache or private_directory() / 'bearer-token.json')
        self._api = None
        self._summary = {}

    def _call(self, method: str, *args, **kwargs):
        # Recheck expiration before each endpoint so long jobs can renew too.
        token = _request(self._auth.obtain_token, self._key)
        if self._api is None:
            self._api = GarminPersonalApiClient(token.access_token)
        else:
            self._api.update_bearer_token(token.access_token)
        return _request(getattr(self._api, method), *args, **kwargs)

    def close(self):
        self._auth.close()
        if self._api is not None:
            self._api.close()

    def verify(self, *, renew: bool = False):
        if renew:
            _request(self._auth.obtain_token, self._key, force_refresh=True)
        profile = self._call('get_user_profile_settings')
        activities = self._call('search_activities', start=0, limit=1)
        if not isinstance(profile, dict) or not isinstance(activities, list):
            raise ValueError('Unexpected Personal API verification response')

    def get_activities_by_date(self, start, end):
        result = []
        seen = set()
        for offset in range(0, 10000, 100):
            page = self._call('search_activities', start=offset, limit=100,
                              start_date=start, end_date=end)
            if not isinstance(page, list):
                raise ValueError('Unexpected Personal API activity list')
            for item in page:
                if not isinstance(item, dict) or not item.get('activityId') or not item.get('startTimeLocal'):
                    raise ValueError('Incomplete Personal API activity summary')
                if item['activityId'] in seen:
                    raise ValueError('Repeated Personal API page; refusing an incomplete sync')
                seen.add(item['activityId'])
                result.append(item)
            if len(page) < 100:
                return result
        raise ValueError('Personal API activity limit reached; sync a smaller date range')

    def get_activity(self, activity_id):
        return self._call('get_activity', activity_id)

    def get_hrv_data(self, day):
        return self._call('get_daily_hrv', day)

    def get_sleep_data(self, day):
        dto = self._call('get_daily_sleep', day)
        if not isinstance(dto, dict) or not dto:
            return {}
        # Personal API returns the sleep DTO directly; Connect wraps it.
        return {'dailySleepDTO': dto}

    def get_stats(self, day):
        if day not in self._summary:
            self._summary[day] = self._call('get', '/usersummary-service/usersummary/daily',
                                          params={'calendarDate': day})
        return self._summary[day]

    def get_stress_data(self, day):
        data = self.get_stats(day)
        if not isinstance(data, dict) or not data:
            return {}
        return {'avgStressLevel': data.get('averageStressLevel'),
                'maxStressLevel': data.get('maxStressLevel')}

    def get_body_battery(self, day):
        reports = self._call('get_daily_body_battery', day, day)
        if not isinstance(reports, list):
            raise ValueError('Unexpected Personal API Body Battery response')
        readings = []
        for report in reports:
            columns = {d['bodyBatteryValueDescriptorKey']: d['bodyBatteryValueDescriptorIndex']
                       for d in report.get('bodyBatteryValueDescriptorDTOList', [])}
            if 'timestamp' not in columns or 'bodyBatteryLevel' not in columns:
                continue
            for row in report.get('bodyBatteryValuesArray', []):
                timestamp = row[columns['timestamp']]
                value = row[columns['bodyBatteryLevel']]
                if isinstance(timestamp, (int, float)) and isinstance(value, (int, float)) and 0 <= value <= 100:
                    readings.append((timestamp, value))
        readings.sort()
        return [{'startBodyBatteryLevel': readings[0][1],
                 'endBodyBatteryLevel': readings[-1][1]}] if readings else []

    def get_heart_rates(self, day):
        return self._call('get_daily_heart_rate', day)

    def get_activity_exercise_sets(self, activity_id):
        raise UnsupportedPersonalApiData('Recorded exercise sets are not in the documented Personal API')

    def get_activity_splits(self, activity_id):
        raise UnsupportedPersonalApiData('Lap splits are not in the documented Personal API')

    def get_activity_details(self, activity_id):
        raise UnsupportedPersonalApiData('Chart samples are not in the documented Personal API')


def setup(key: str) -> None:
    """Verify first, then privately install the key and its verified token cache."""
    key = key.strip()
    if not key:
        raise ValueError('Personal API key must not be empty')
    directory = private_directory()
    with tempfile.TemporaryDirectory(prefix='.setup-', dir=directory) as staging:
        root = Path(staging)
        client = PersonalApiGarmin(key, cache=root / 'bearer-token.json')
        try:
            client.verify()
        finally:
            client.close()
        (root / 'api-key').write_text(key)
        for name in ('api-key', 'bearer-token.json'):
            (root / name).chmod(0o600)
        for name in ('api-key', 'bearer-token.json'):
            os.replace(root / name, directory / name)
