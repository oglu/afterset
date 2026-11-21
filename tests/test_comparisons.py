import pytest
from pydantic import ValidationError

from afterset.comparisons import compare_documents


def doc(efforts, source=1):
    return {'schema_version': 3, 'source_activity_ids': [source], 'blocks': [{'efforts': efforts}]}


def item(key='squat', **kwargs):
    return {'exercise': 'Squat', 'exercise_key': key, 'kind': 'working', **kwargs}


def test_match_changed_load_and_conditions_with_supported_deltas():
    provenance = {'reps': {'source': 'user'}, 'load': {'source': 'user'}}
    a = item(provenance=provenance, reps=5, reps_basis='total', load={'basis': 'per_hand', 'kg': 10, 'implement_count': 2}, assistance='band')
    b = item(provenance=provenance, reps=6, reps_basis='total', load={'basis': 'per_hand', 'kg': 12, 'implement_count': 2}, assistance='none')
    result = compare_documents(doc([a]), doc([b], 2))
    pair = result['pairs'][0]
    assert pair['deltas']['load_kg']['difference'] == 2
    assert pair['deltas']['reps']['difference'] == 1
    assert 'assistance differs' in pair['metric_comparisons']['reps']['limitations']
    assert pair['metric_comparisons']['reps']['sources']['first'] == 'user'
    assert 'assistance differs' in pair['limitations']
    assert not result['unmatched_first']


def test_ambiguous_matches_and_explicit_mapping():
    first = doc([item(), item()])
    second = doc([item(), item()])
    assert len(compare_documents(first, second)['unmatched_first']) == 2
    paired = compare_documents(first, second, [{'first': [0, 0], 'second': [0, 1]}])
    assert len(paired['pairs']) == 2
    assert paired['shared_source_activity_ids'] == [1]
    for mappings in ([{'first': [0, True], 'second': [0, 0]}],
                     [{'first': [0, 9], 'second': [0, 0]}],
                     [{'first': [0, 0], 'second': [0, 0]}, {'first': [0, 0], 'second': [0, 1]}]):
        with pytest.raises((ValueError, ValidationError)):
            compare_documents(first, second, mappings)


def test_unknown_identity_and_measurement_conventions_are_not_guessed():
    first = doc([item(None, reps=5, reps_basis='per_leg', load={'basis': 'unknown', 'kg': 5})])
    second = doc([item(None, reps=5, reps_basis='total', load={'basis': 'unknown', 'kg': 7})])
    assert compare_documents(first, second)['pairs'] == []
    pair = compare_documents(first, second, [{'first': [0, 0], 'second': [0, 0]}])['pairs'][0]
    assert pair['deltas'] == {}


def test_bounded_metrics_source_and_conflicts():
    known = {'source': 'watch', 'text': 'Recorded'}
    def measurement(value, source=known):
        return item(id='one', measured={'power_watts': value}, provenance={'measured.power_watts': source})
    left = measurement({'kind': 'range', 'minimum': 100, 'maximum': 120, 'approximate': True})
    right = measurement({'kind': 'range', 'minimum': 130, 'maximum': 150})
    pair = compare_documents(doc([left]), doc([right], 2))['pairs'][0]
    metric = pair['metric_comparisons']['measured.power_watts']
    assert metric['delta'] == {'kind': 'range', 'minimum': 10, 'maximum': 50, 'approximate': True, 'percent': None}
    assert metric['first']['minimum'] == 100
    assert 'execution conditions incomplete' in metric['limitations']
    right['provenance']['measured.power_watts'] = {'source': 'unknown'}
    metric = compare_documents(doc([left]), doc([right]))['pairs'][0]['metric_comparisons']['measured.power_watts']
    assert metric['delta'] is None and 'source unknown' in metric['limitations']
    right['provenance']['measured.power_watts'] = known
    first = doc([left]); first['conflicts'] = [{'effort_id': 'one', 'metric': 'power_watts', 'status': 'unresolved'}]
    metric = compare_documents(first, doc([right]))['pairs'][0]['metric_comparisons']['measured.power_watts']
    assert metric['delta'] is None and 'unresolved legacy conflict' in metric['limitations']


def test_unmatched_reasons_and_approximate_scalar():
    a = doc([item(None), item('squat'), item('squat'), item('row')])
    b = doc([item('squat')])
    reasons = [e['reason'] for e in compare_documents(a, b)['unmatched_first']]
    assert reasons == ['missing_key', 'ambiguous', 'ambiguous', 'no_counterpart']
    source = {'source': 'user', 'text': 'Reported'}
    def metric(value):
        return item(reported={'speed_meters_per_second': value}, provenance={'reported.speed_meters_per_second': source})
    p = compare_documents(doc([metric({'kind': 'approximate', 'value': 2})]), doc([metric({'kind': 'exact', 'value': 3})]))['pairs'][0]
    assert p['metric_comparisons']['reported.speed_meters_per_second']['delta'] == {'kind': 'approximate', 'difference': 1, 'percent': None}


def test_history_deduplicates_and_bounds_notes_and_reports_overlap(tmp_path, monkeypatch):
    from afterset import connection, db, store
    from afterset.store import history
    monkeypatch.setattr(connection, 'DB_PATH', tmp_path / 'history.db')
    store.initialize()
    db.upsert_activities([{'activity_id': 1, 'date': '2026-01-01'}, {'activity_id': 2, 'date': '2026-01-02'}])
    for _ in range(6):
        store.save_note(1, 'x' * 600)
    def synthetic(_conn, activity_id):
        workout = doc([item(), item()], 1)
        return {'revision': 0, 'workout': workout}, f'hash-{activity_id}'
    monkeypatch.setattr(history, '_effective', synthetic)
    result = history.get_exercise_history('squat', '2026-01-01', '2026-01-02', limit=3)
    assert result['total'] == 4 and result['next_offset'] == 3
    assert result['shared_source_activity_ids'] == [1]
    assert len(result['sessions'][1]['notes']['items']) == 5
    assert result['sessions'][1]['notes']['truncated']
    assert result['sessions'][1]['notes']['items'][0]['truncated']
    assert len(result['sessions'][1]['notes']['items'][0]['text']) == 500
    assert all('notes' not in e for e in result['items'])


def test_legacy_power_conversion_for_comparison_and_conflict():
    def legacy(top, reported=None):
        return {'source_activity_ids': [1], 'blocks': [{'label': 'Work', 'evidence': {'source': 'user', 'text': 'Reported'}, 'efforts': [{
            'exercise': 'Row', 'exercise_key': 'row', 'evidence': 'Watch', 'power_watts': top,
            'reported': {'power_watts': reported}, 'provenance': {
                'power_watts': {'source': 'watch', 'text': 'Watch'},
                'reported.power_watts': {'source': 'user', 'text': 'Reported'}}}]}]}
    first, second = legacy(150), legacy(170)
    pair = compare_documents(first, second, [{'first': [0, 0], 'second': [0, 0]}])['pairs'][0]
    assert pair['metric_comparisons']['measured.power_watts']['delta']['difference'] == 20
    assert first['blocks'][0]['efforts'][0]['power_watts'] == 150
    conflicted = legacy(150, 140)
    pair = compare_documents(conflicted, legacy(170, 170))['pairs'][0]
    for key in ('measured.power_watts', 'reported.power_watts'):
        assert pair['metric_comparisons'][key]['delta'] is None
        assert 'unresolved legacy conflict' in pair['metric_comparisons'][key]['limitations']


def test_history_projects_legacy_power_without_mutating_snapshot(tmp_path, monkeypatch):
    from afterset import connection, db, store
    from afterset.store import history
    monkeypatch.setattr(connection, 'DB_PATH', tmp_path / 'history-v2.db')
    store.initialize()
    db.upsert_activities([{'activity_id': 1, 'date': '2026-01-01'}])
    legacy = {'source_activity_ids': [1], 'blocks': [{'label': 'Work', 'evidence': {'source': 'watch', 'text': 'Watch'}, 'efforts': [{
        'exercise': 'Row', 'exercise_key': 'row', 'evidence': 'Watch', 'power_watts': 120,
        'provenance': {'power_watts': {'source': 'watch', 'text': 'Watch'}}}]}]}
    monkeypatch.setattr(history, '_effective', lambda _conn, _id: ({'revision': 1, 'workout': legacy}, 'hash'))
    row = history.get_exercise_history('row', '2026-01-01', '2026-01-01')['items'][0]
    assert row['schema_version'] == 2
    assert row['effort']['measured']['power_watts']['value'] == 120
    assert row['effort']['field_sources']['measured.power_watts'] == 'watch'
    assert legacy['blocks'][0]['efforts'][0]['power_watts'] == 120


def test_blank_keys_are_unknown_and_load_changes_limit_rep_and_power():
    assert not compare_documents(doc([item('  ')]), doc([item('  ')]))['pairs']
    unmatched = compare_documents(doc([item('  ')]), doc([item('  ')]))['unmatched_first']
    assert unmatched[0]['reason'] == 'missing_key'
    provenance = {'reps': {'source': 'user'}, 'measured.power_watts': {'source': 'watch'}}
    def with_load(kg):
        return item(reps=5, reps_basis='total', load={'basis': 'total_external', 'kg': kg},
                    measured={'power_watts': {'kind': 'exact', 'value': 200}}, provenance=provenance)
    pair = compare_documents(doc([with_load(10)]), doc([with_load(100)]))['pairs'][0]
    assert pair['deltas']['reps']['difference'] == 0
    for key in ('reps', 'measured.power_watts'):
        assert 'load or load convention differs' in pair['metric_comparisons'][key]['limitations']
