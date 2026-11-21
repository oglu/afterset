from unittest.mock import patch

import pytest

from afterset import connection, db, store, workouts


@pytest.fixture
def session(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'training.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-11-01'}])
        db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 10, 'setType': 'ACTIVE'}]})
        yield


def test_retry_receipts_and_review_survive_restart_and_later_revision(session):
    note = store.save_note(1, 'Synthetic report', request_key='note-1')
    assert store.save_note(1, 'Synthetic report', request_key='note-1') == note
    assert store.save_note(1, 'Synthetic report')['note_id'] != note['note_id']
    with pytest.raises(ValueError, match='different input'):
        store.save_note(1, 'Changed report', request_key='note-1')
    workout = workouts.Workout(source_activity_ids=[1], blocks=[])
    draft = store.propose_workout(1, note['note_id'], workout, 'Remove sets', 0, request_key='draft-1')
    store.initialize()
    review = store.get_proposal(draft['proposal_id'], include_workout=True)
    assert review['workout'] == workout.model_dump()
    assert review['changes'] == draft['changes']
    assert review['lifecycle'] == 'draft' and review['applicability'] == 'current'
    applied = store.apply_proposal(draft['proposal_id'])
    store.restore_sets(1, 0, applied['revision'], 'Restore raw')
    assert store.propose_workout(1, note['note_id'], workout, 'Remove sets', 0, request_key='draft-1') == draft
    retry = store.apply_proposal(draft['proposal_id'])
    assert retry['revision'] == applied['revision'] and retry['already_applied']
    assert retry['lifecycle'] == 'applied' and retry['current_revision'] != retry['applied_revision']
    with pytest.raises(ValueError, match='different input'):
        store.propose_workout(1, note['note_id'], workout, 'Changed', 0, request_key='draft-1')


def test_stale_status_and_filtered_pagination(session):
    note = store.save_note(1, 'Synthetic report')['note_id']
    workout = workouts.Workout(source_activity_ids=[1], blocks=[])
    first = store.propose_workout(1, note, workout, 'First', 0)
    second = store.propose_workout(1, note, workout, 'Second', 0)
    db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 11, 'setType': 'ACTIVE'}]})
    assert store.get_proposal(first['proposal_id'])['applicability'] == 'recording_changed'
    with pytest.raises(ValueError, match='Recording changed'):
        store.apply_proposal(first['proposal_id'])
    current = store.propose_workout(1, note, workout, 'Current', 0)
    store.apply_proposal(current['proposal_id'])
    assert store.get_proposal(second['proposal_id'])['applicability'] == 'workout_changed'
    page = store.list_workout_proposals(1, status='draft', limit=1)
    assert page['total'] == 2 and len(page['items']) == 1 and page['next_offset'] == 1
    assert store.list_workout_proposals(1, status='applied')['total'] == 1
    assert 'workout' not in page['items'][0]


def test_replacement_is_atomic_retryable_and_requires_new_evidence(session):
    note = store.save_note(1, 'Four squats, then five')['note_id']
    old = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
        label='Squat', evidence=workouts.Evidence(source='user', text='Report', note_ids=[note]),
        efforts=[workouts.Effort(exercise='Squat', reps=4, evidence='Reported')])])
    document = workouts.convert_workout(old)
    document.blocks[0].efforts[0].provenance['reps'] = workouts.Evidence(source='user', text='Four reported', note_ids=[note])
    for field in ('exercise', 'kind'):
        document.blocks[0].efforts[0].provenance[field] = workouts.Evidence(source='user', text='Reported', note_ids=[note])
    draft = store.propose_workout(1, note, document, 'Reported', 0)
    effort = document.blocks[0].efforts[0]
    invalid = effort.model_copy(update={'reps': 5})
    from afterset.store.corrections import revise_workout_proposal
    with pytest.raises(ValueError, match='provenance'):
        revise_workout_proposal(draft['proposal_id'], 'effort', effort.id, invalid, draft['fingerprint'], note)
    updated = effort.model_dump()
    updated['reps'] = 5
    updated['provenance']['reps'] = {'source': 'user', 'text': 'Five confirmed', 'note_ids': [note]}
    args = (draft['proposal_id'], 'effort', effort.id, workouts.EffortV3.model_validate(updated), draft['fingerprint'], note)
    successor = revise_workout_proposal(*args, request_key='edit-1')
    assert revise_workout_proposal(*args, request_key='edit-1') == successor
    assert successor['workout']['blocks'][0]['efforts'][0]['reps'] == 5
    assert store.get_proposal(draft['proposal_id'])['lifecycle'] == 'superseded'
    with pytest.raises(ValueError, match='superseded'):
        store.apply_proposal(draft['proposal_id'])
    with pytest.raises(ValueError, match='active draft'):
        revise_workout_proposal(*args)
    store.apply_proposal(successor['proposal_id'])
    assert store.get_workout(1)['workout']['blocks'][0]['efforts'][0]['reps'] == 5


def test_newer_database_rejects_initialization_and_ingestion_without_changes(session):
    import sqlite3
    with sqlite3.connect(connection.DB_PATH) as raw:
        raw.execute('PRAGMA user_version=999')
    with pytest.raises(ValueError, match='newer'):
        store.initialize()
    with pytest.raises(ValueError, match='newer'):
        db.upsert_activities([{'activity_id': 2, 'date': '2026-11-02'}])
    with sqlite3.connect(connection.DB_PATH) as raw:
        assert raw.execute('SELECT count(*) FROM activities').fetchone()[0] == 1
        assert raw.execute('PRAGMA user_version').fetchone()[0] == 999


def test_concurrent_retry_keys_and_competing_replacements(session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from afterset.store.corrections import revise_workout_proposal

    def concurrent(call):
        barrier = Barrier(2)
        def run():
            barrier.wait()
            try:
                return call()
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _: run(), range(2)))

    notes = concurrent(lambda: store.save_note(1, 'Concurrent report', request_key='same-note'))
    assert notes[0] == notes[1]
    note = notes[0]['note_id']
    old = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
        label='Squat', evidence=workouts.Evidence(source='user', text='Reported', note_ids=[note]),
        efforts=[workouts.Effort(exercise='Squat', reps=4, evidence='Reported')])])
    document = workouts.convert_workout(old)
    document.blocks[0].efforts[0].provenance['reps'] = workouts.Evidence(source='user', text='Four', note_ids=[note])
    for field in ('exercise', 'kind'):
        document.blocks[0].efforts[0].provenance[field] = workouts.Evidence(source='user', text='Reported', note_ids=[note])
    drafts = concurrent(lambda: store.propose_workout(1, note, document, 'Reported', 0, request_key='same-draft'))
    assert drafts[0] == drafts[1]
    draft = drafts[0]
    effort = document.blocks[0].efforts[0]
    edited = effort.model_dump()
    edited['reps'] = 5
    edited['provenance']['reps'] = {'source': 'user', 'text': 'Five', 'note_ids': [note]}
    args = (draft['proposal_id'], 'effort', effort.id, workouts.EffortV3.model_validate(edited), draft['fingerprint'], note)
    results = concurrent(lambda: revise_workout_proposal(*args))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, str) and 'active draft' in result for result in results) == 1
    assert store.list_workout_proposals(1, status='draft')['total'] == 1


def test_failed_migration_rolls_back_schema_and_version(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE sentinel (value TEXT)')
        raw.execute("INSERT INTO sentinel VALUES ('intact')")
    with patch.object(connection, 'DB_PATH', path), patch('afterset.workouts.refresh_raw_snapshots', side_effect=RuntimeError('injected')):
        with pytest.raises(RuntimeError, match='injected'):
            store.initialize()
    with sqlite3.connect(path) as raw:
        assert raw.execute('PRAGMA user_version').fetchone()[0] == 0
        assert raw.execute('SELECT value FROM sentinel').fetchone()[0] == 'intact'
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name='request_receipts'").fetchone()[0] == 0


def test_oauth_rejects_newer_schema_before_writing(session):
    import sqlite3
    from afterset.oauth import OwnerOAuthProvider
    with sqlite3.connect(connection.DB_PATH) as raw:
        raw.execute('PRAGMA user_version=999')
    with pytest.raises(ValueError, match='newer'):
        OwnerOAuthProvider('long-enough-passphrase', 'https://example.test/mcp')
    with sqlite3.connect(connection.DB_PATH) as raw:
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name='oauth_settings'").fetchone()[0] == 0


def test_guard_covers_cursor_cte_comments_and_multi_statement_script(session):
    import sqlite3
    with store.connect() as conn:
        conn.executescript('CREATE TABLE guard_a (x); CREATE TABLE guard_b (x);')
    with sqlite3.connect(connection.DB_PATH) as raw:
        raw.execute('PRAGMA user_version=999')
    for sql in ('-- comment\nINSERT INTO guard_a VALUES (1)',
                'WITH value(x) AS (SELECT 1) INSERT INTO guard_a SELECT x FROM value'):
        with pytest.raises(ValueError, match='newer'):
            with store.connect() as conn:
                conn.cursor().execute(sql)
    with pytest.raises(ValueError, match='newer'):
        with store.connect() as conn:
            conn.executescript('CREATE TABLE guard_c (x); INSERT INTO guard_a VALUES (1);')
    with sqlite3.connect(connection.DB_PATH) as raw:
        assert raw.execute('SELECT count(*) FROM guard_a').fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name='guard_c'").fetchone()[0] == 0


def test_review_exposes_recorded_disagreement_and_real_changes(session):
    note = store.save_note(1, 'Eight reps')['note_id']
    db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 10, 'setType': 'ACTIVE', 'repetitionCount': 7}]})
    effort = workouts.Effort(exercise='Squat', reps=8, reps_basis='total', evidence='Eight reported',
                             segments=[workouts.SegmentRef(activity_id=1, index=0)],
                             provenance={'reps': workouts.Evidence(source='user', text='Eight', note_ids=[note])})
    document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
        label='Squat', evidence=workouts.Evidence(source='user', text='Report', note_ids=[note]), efforts=[effort])])
    draft = store.propose_workout(1, note, document, 'Reported count', 0)
    assert draft['changes'] and draft['unresolved_fields'] == []
    assert draft['conflicts'][0]['reported']['value'] == 8
    assert draft['conflicts'][0]['recorded']['value'] == 7
    assert store.get_proposal(draft['proposal_id'], True)['conflicts'] == draft['conflicts']


def test_preupgrade_v2_draft_keeps_legacy_format_note_compatibility(session):
    note = store.save_note(1, 'Session report')['note_id']
    document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
        kind='amrap', label='AMRAP', evidence=workouts.Evidence(source='user', text='Reported', note_ids=[note]),
        format=workouts.BlockFormat(time_cap_seconds=60, provenance={'time_cap_seconds': workouts.Evidence(source='user', text='Earlier report', note_ids=['old-missing-note'])}), efforts=[])])
    with pytest.raises(ValueError, match='Evidence note'):
        store.propose_workout(1, note, document, 'Report', 0)
    with store.connect() as conn:
        conn.execute('INSERT INTO set_proposals(proposal_id,activity_id,base_revision,sets_json,note_id,explanation,workout_json,source_hash) VALUES(?,?,?,?,?,?,?,?)',
                     ('legacy', 1, 0, workouts.dumps(workouts.legacy_sets(document)), note, 'Previously reviewed',
                      workouts.dumps(document.model_dump()), workouts.source_hash(conn, [1])))
    result = store.apply_proposal('legacy')
    assert store.get_workout(1)['revision'] == result['revision']
    assert store.get_workout(1)['workout'] == document.model_dump()


def test_v3_provenance_and_format_removal():
    from copy import deepcopy
    from afterset.store.corrections import _validate_v3_changes
    e = {'source': 'user', 'text': 'Old report', 'note_ids': ['n']}
    old = {'source_activity_ids': [1], 'blocks': [{'id': 'b', 'kind': 'amrap', 'label': 'AMRAP', 'evidence': e,
        'drop_sets': [], 'format': {'time_cap_seconds': 60, 'provenance': {'time_cap_seconds': e}},
        'efforts': [{'id': 'e', 'exercise': 'Squat', 'reps': 8, 'details': {}, 'provenance': {'reps': e}}]}]}
    changed = deepcopy(old)
    changed['blocks'][0]['efforts'][0]['provenance']['reps'] = {'source': 'unknown', 'text': 'Lost', 'note_ids': []}
    with pytest.raises(ValueError, match='downgraded'):
        _validate_v3_changes(old, changed)
    changed = deepcopy(old)
    changed['blocks'][0]['efforts'][0]['details'] = {'back_support': 'bench'}
    with pytest.raises(ValueError, match='details'):
        _validate_v3_changes(old, changed)
    changed = deepcopy(old)
    changed['blocks'][0].update(kind='straight_sets', format=None,
                                evidence={'source': 'user', 'text': 'Corrected format', 'note_ids': ['n']})
    _validate_v3_changes(old, changed)


def test_review_keeps_shared_and_incompatible_watch_counts_as_evidence(session):
    from afterset.store.corrections import _review_changes
    db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 10, 'setType': 'ACTIVE', 'repetitionCount': 10}]})
    user = {'source': 'user', 'text': 'Reported', 'note_ids': []}
    def effort(reps, basis):
        return {'exercise': 'Squat', 'kind': 'working', 'reps': reps, 'reps_basis': basis,
                'segments': [{'activity_id': 1, 'index': 0}], 'provenance': {'reps': user}}
    with store.connect() as conn:
        shared = _review_changes(conn, {}, {'blocks': [{'efforts': [effort(6, 'total'), effort(4, 'total')]}]}, 'n', 'Reported')
        per_leg = _review_changes(conn, {}, {'blocks': [{'efforts': [effort(5, 'per_leg')]}]}, 'n', 'Reported')
        comparable = _review_changes(conn, {}, {'blocks': [{'efforts': [effort(8, 'total')]}]}, 'n', 'Reported')
    for review in (shared, per_leg):
        assert review['conflicts'] == []
        assert all(not item['comparable'] and item['value'] == 10 for item in review['recorded_repetitions'])
    assert comparable['conflicts'][0]['recorded']['value'] == 10


def test_review_surfaces_material_unresolved_evidence(session):
    from afterset.store.corrections import _review_changes
    document = {'conflicts': [{'effort_id': 'e', 'metric': 'power_watts', 'status': 'unresolved'}],
                'blocks': [{'efforts': [{'id': 'e', 'exercise': 'Unidentified recorded interval', 'reps': 8,
                                         'reps_basis': 'unknown', 'segments': []},
                                        {'id': 'run', 'exercise': 'Run', 'reps': None, 'segments': []}]}]}
    with store.connect() as conn:
        review = _review_changes(conn, {}, document, 'n', 'Reported')
    assert {item['kind'] for item in review['unresolved_fields']} == {'document_conflict', 'unknown_exercise', 'unknown_reps_basis'}
    assert review['unresolved_effort_count'] == 1


def test_drop_set_evidence_cannot_downgrade_and_new_effort_defaults_are_allowed():
    from copy import deepcopy
    from afterset.store.corrections import _validate_v3_changes, _validate_replacement_evidence
    evidence = {'source': 'user', 'text': 'Reported', 'note_ids': ['n']}
    group = {'id': 'g', 'effort_ids': ['e1', 'e2'], 'evidence': evidence}
    old = {'blocks': [{'id': 'b', 'kind': 'straight_sets', 'label': 'Sets', 'evidence': evidence,
                       'drop_sets': [group], 'format': None, 'efforts': [{'id': 'e1'}, {'id': 'e2'}]}]}
    revised = deepcopy(old)
    revised['blocks'][0]['drop_sets'][0]['evidence'] = {'source': 'unknown', 'text': 'Unknown', 'note_ids': []}
    with pytest.raises(ValueError, match='Drop-set evidence'):
        _validate_v3_changes(old, revised)
    revised['blocks'][0]['drop_sets'][0]['evidence'] = {'source': 'watch', 'text': 'Corrected source', 'note_ids': []}
    _validate_v3_changes(old, revised)
    effort = workouts.EffortV3(id='new', exercise='Squat', evidence='Reported',
        provenance={'exercise': workouts.Evidence(**evidence), 'kind': workouts.Evidence(**evidence)})
    _validate_replacement_evidence({}, effort.model_dump(mode='json'), 'n')


def test_executescript_preserves_caller_transaction_and_commits_standalone(session):
    with store.connect() as conn:
        conn.executescript('CREATE TABLE script_transaction_test (value INTEGER);')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO script_transaction_test VALUES (1)')
        conn.executescript('INSERT INTO script_transaction_test VALUES (2);')
        assert conn.in_transaction
        conn.rollback()
    with store.connect() as conn:
        assert conn.execute('SELECT value FROM script_transaction_test').fetchall() == []
        conn.executescript('INSERT INTO script_transaction_test VALUES (3);')
        assert not conn.in_transaction
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO script_transaction_test VALUES (4)')
        conn.rollback()
    with store.connect() as conn:
        assert [row[0] for row in conn.execute('SELECT value FROM script_transaction_test')] == [3]
