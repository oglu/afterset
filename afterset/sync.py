"""Background Garmin sync jobs, persisted for polling over MCP."""
from __future__ import annotations

from typing import Any, Literal

import json
import errno
import fcntl
import threading
from datetime import timedelta
from uuid import uuid4

from afterset import connection, db, fetch, store

_lock = threading.Lock()


def _client():
    from afterset.personal_api import configured, PersonalApiGarmin
    if configured():
        return PersonalApiGarmin()
    from afterset.auth import TOKEN_STORE, save_session_tokens
    from garminconnect import Garmin
    # MCP must never prompt for credentials or MFA on its protocol stream.
    if not (TOKEN_STORE / 'garmin_tokens.json').is_file():
        raise RuntimeError('A current Garmin session is required; run afterset login once. Old OAuth1/OAuth2 caches cannot be converted.')
    client = Garmin()
    client.login(str(TOKEN_STORE))
    save_session_tokens(client)
    return client


def job_status(job_id: str) -> dict[str, Any]:
    with store.connect() as conn:
        row = conn.execute('SELECT * FROM sync_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown job ID')
    result = dict(row)
    result['request'] = json.loads(result.pop('request_json'))
    result['progress'] = json.loads(result.pop('progress_json'))
    return result


def _update(job_id: str, state: str, progress: dict) -> None:
    with store.connect() as conn:
        conn.execute('UPDATE sync_jobs SET state=?,progress_json=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?', (state, json.dumps(progress), job_id))


def _process_lock():
    connection.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(connection.DB_PATH) + '.sync.lock', 'a+b')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise ValueError('A sync is already running in another process') from None
        raise
    return handle


def mark_interrupted() -> None:
    """Called once when the single server process starts."""
    try:
        handle = _process_lock()
    except ValueError:
        return
    try:
        with store.connect() as conn:
            conn.execute("UPDATE sync_jobs SET state='interrupted',updated_at=CURRENT_TIMESTAMP WHERE state IN ('queued','running')")
    finally:
        handle.close()


def _queue_job(start: str, end: str, scope: str, enrich: bool):
    store.date_range(start, end, 31)
    if scope not in ('activities', 'wellness'):
        raise ValueError('scope must be activities or wellness')
    if not _lock.acquire(blocking=False):
        raise ValueError('A sync is already running; poll its job before starting another')
    job_id = str(uuid4())
    handle = None
    try:
        handle = _process_lock()
        request = {'start': start, 'end': end, 'scope': scope, 'enrich': enrich}
        with store.connect() as conn:
            conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)', (job_id, 'queued', json.dumps(request), '{}'))
        return job_id, request, handle
    except BaseException:
        if handle:
            handle.close()
        _lock.release()
        raise


def start_sync(start: str, end: str, scope: Literal['activities', 'wellness'] = 'activities', enrich: bool = False) -> dict[str, Any]:
    job_id, request, handle = _queue_job(start, end, scope, enrich)
    try:
        threading.Thread(target=_run, args=(job_id, request, handle), daemon=True).start()
    except BaseException:
        handle.close()
        _lock.release()
        _update(job_id, 'failed', {'hint': 'Could not start sync worker; retry.'})
        raise
    return {'job_id': job_id, 'state': 'queued', 'hint': 'Poll sync_status until completed, partial, failed, or interrupted.'}


def run_sync(start: str, end: str, scope: Literal['activities', 'wellness'] = 'wellness', enrich: bool = False, *, client=None) -> dict[str, Any]:
    """Run one bounded job in the foreground, using an authenticated CLI client."""
    job_id, request, handle = _queue_job(start, end, scope, enrich)
    _run(job_id, request, handle, client=client)
    return job_status(job_id)


def _wellness_day(client, day: str, activities: list[dict]) -> None:
    # Fetch the entire day before writing. An endpoint failure must not replace
    # good historic metrics with nulls.
    hrv = client.get_hrv_data(day)
    sleep = client.get_sleep_data(day)
    stress = client.get_stress_data(day)
    battery = client.get_body_battery(day)
    heart = client.get_heart_rates(day)
    stats = client.get_stats(day)
    if not all(isinstance(value, dict) and value for value in (sleep, stress, heart, stats)):
        raise ValueError('Missing core wellness response; existing day retained')
    row = {'date': day}
    row.update(fetch._parse_sleep(sleep, hrv))
    row.update(fetch._parse_stress(stress))
    row.update(fetch._parse_body_battery(battery))
    row.update(fetch._parse_heart_rate(heart))
    row.update(fetch._parse_stats(stats))
    row.update(fetch._parse_workouts(activities))
    # Preserve existing optional metric values if Garmin omits them this time.
    with store.connect() as conn:
        existing = conn.execute('SELECT * FROM daily_metrics WHERE date=?', (day,)).fetchone()
        if existing:
            for key in db.METRIC_COLS:
                if row.get(key) is None:
                    row[key] = existing[key]
    db.upsert_rows([row])
    db.upsert_sleep_stages(day, fetch._parse_sleep_stages(sleep))
    db.upsert_timeseries('heart_rate_readings', day, fetch._parse_hr_readings(day, heart))
    db.upsert_timeseries('stress_readings', day, fetch._parse_stress_readings(day, stress))
    db.upsert_timeseries('hrv_readings', day, fetch._parse_hrv_readings(day, hrv))


def _run(job_id: str, request: dict, handle=None, *, client=None) -> None:
    from afterset.personal_api import PersonalApiGarmin
    owns_client = client is None
    progress = {'completed_items': 0, 'failures': [], 'activities': 0}
    try:
        _update(job_id, 'running', progress)
        if client is None:
            client = _client()
        if isinstance(client, PersonalApiGarmin):
            progress['backend'] = 'personal-api'
            progress['limitations'] = [
                'Recorded exercise sets, lap splits and chart samples are unavailable through this adapter; existing payloads are retained.',
                'Sleep stages and stress time series are unavailable through this adapter; existing readings are retained.',
            ]
        start, end = request['start'], request['end']
        raw = client.get_activities_by_date(start, end)
        if not isinstance(raw, list):
            raise ValueError('Unexpected activity list response')
        db.upsert_activities([fetch._parse_activity(a) for a in raw])
        progress['activities'] = len(raw)
        for activity in raw:
            store.store_payload(activity['activityId'], 'summary', activity)
        if request['scope'] == 'activities':
            progress['total_items'] = len(raw)
            for activity in raw:
                activity_id = activity['activityId']
                if isinstance(client, PersonalApiGarmin):
                    # Skip data the Personal API never has; `limitations` reports it.
                    calls = [('detail', client.get_activity)] if request['enrich'] else []
                else:
                    calls = [('sets', client.get_activity_exercise_sets)]
                    if request['enrich']:
                        calls += [('detail', client.get_activity), ('splits', client.get_activity_splits), ('samples', client.get_activity_details)]
                for kind, call in calls:
                    try:
                        payload = call(activity_id)
                        if not isinstance(payload, dict):
                            raise ValueError('Unexpected response')
                        if kind == 'sets':
                            db.upsert_exercise_sets(activity_id, payload)
                        else:
                            store.store_payload(activity_id, kind, payload)
                    except Exception as exc:
                        # Do not persist exception text that may include auth headers.
                        progress['failures'].append({'activity_id': activity_id, 'kind': kind, 'error': type(exc).__name__})
                progress['completed_items'] += 1
                _update(job_id, 'running', progress)
        else:
            a, b = store.date_range(start, end, 31)
            grouped = fetch._group_activities_by_date(raw)
            progress['total_items'] = (b-a).days+1
            for index in range(progress['total_items']):
                day = (a + timedelta(days=index)).isoformat()
                try:
                    _wellness_day(client, day, grouped.get(day, []))
                except Exception as exc:
                    progress['failures'].append({'date': day, 'error': type(exc).__name__})
                progress['completed_items'] += 1
                _update(job_id, 'running', progress)
        # A long sync may renew again while fetching records.
        from afterset.auth import save_session_tokens
        from afterset.personal_api import PersonalApiGarmin
        if not isinstance(client, PersonalApiGarmin):
            save_session_tokens(client)
        _update(job_id, 'partial' if progress['failures'] else 'completed', progress)
    except Exception as exc:
        progress['error'] = type(exc).__name__
        progress['hint'] = 'Check your configured Garmin login or Personal API key; retry the date range. Existing corrections are retained.'
        _update(job_id, 'failed', progress)
    finally:
        if owns_client and isinstance(client, PersonalApiGarmin):
            client.close()
        if handle:
            handle.close()
        _lock.release()
