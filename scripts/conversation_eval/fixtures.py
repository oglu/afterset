"""Create only new synthetic databases for the external evaluation observer."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from afterset import connection, db, store
from afterset.store.payloads import save_fetched


def initialize_fixture(root: Path, mode: str = 'fresh') -> None:
    if mode not in {'fresh', 'missing', 'partial', 'auth'}:
        raise ValueError('Unknown synthetic fixture mode')
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'training.db'
    if path.exists() or (root / 'fixture.json').exists():
        raise ValueError('Fixture directory must be new')
    with patch.object(connection, 'DB_PATH', path):
        store.initialize()
        if mode == 'fresh':
            for aid, date in ((4101, '2026-01-02'), (4102, '2026-01-09')):
                db.upsert_activities([{'activity_id': aid, 'date': date, 'name': 'Synthetic strength', 'duration_seconds': 600}])
                save_fetched(aid, 'sets', 'connect', {'exerciseSets': [{'duration': 600, 'setType': 'ACTIVE'}]})
                save_fetched(aid, 'detail', 'connect', {'activityId': aid, 'summaryDTO': {'elapsedDuration': 600}})
                save_fetched(aid, 'splits', 'connect', {'lapDTOs': []})
                save_fetched(aid, 'samples', 'connect', {'metricDescriptors': [
                    {'key': 'sumElapsedDuration', 'unit': 's', 'metricsIndex': 0},
                    {'key': 'directHeartRate', 'unit': 'bpm', 'metricsIndex': 1}],
                    'activityDetailMetrics': [{'metrics': [t, 100 + t // 60]} for t in range(0, 601, 5)]})
                store.store_payload(aid, 'summary', {'elapsedDuration': 600})
                store.save_note(aid, 'Synthetic feedback: felt steady.')
            with store.connect() as conn:
                for scope in ('activities', 'wellness'):
                    request = {'start': '2026-01-01', 'end': '2026-01-10', 'scope': scope, 'enrich': False}
                    conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)',
                                 ('fixture-' + scope, 'completed', json.dumps(request), '{}'))
    (root / 'fixture.json').write_text(json.dumps({'mode': mode}), encoding='utf-8')
