import pytest
from pydantic import ValidationError

from afterset import workouts


def evidence(source='user'):
    return {'source': source, 'text': 'Reported in notes'}


def effort(identifier, **changes):
    return {'id': identifier, 'exercise': 'Press', 'evidence': 'Reported', **changes}


def document(efforts=None, groups=None):
    return {'schema_version': 3, 'source_activity_ids': [1], 'blocks': [{
        'id': 'block', 'label': 'Press', 'kind': 'straight_sets', 'evidence': evidence(),
        'efforts': efforts or [effort('a'), effort('b')], 'drop_sets': groups or []}]}


def test_v2_compatibility_and_explicit_conversion():
    legacy = {'source_activity_ids': [1], 'blocks': [{'label': 'Work', 'evidence': evidence(), 'efforts': [{
        'exercise': 'Row', 'evidence': 'Reported', 'power_watts': 150,
        'reported': {'power_watts': 140}, 'details': {'power_watts': 160}, 'reps': 5}]}]}
    v2 = workouts.parse_workout(legacy)
    assert type(v2) is workouts.Workout
    converted = workouts.convert_workout(v2, snapshot_scope='1:7')
    e = converted.blocks[0].efforts[0]
    assert e.measured.power_watts is None
    assert e.reported.power_watts.value == 140
    assert e.details['power_watts'] == 160
    assert converted.conflicts[0]['status'] == 'unresolved'
    assert converted.conflicts[0]['legacy_values'] == {'power_watts': 150, 'reported.power_watts': 140, 'details.power_watts': 160}
    assert e.provenance['reps'].source == 'unknown'
    assert workouts.convert_workout(v2, snapshot_scope='1:7') == converted
    assert workouts.convert_workout(v2, snapshot_scope='1:8').blocks[0].id != converted.blocks[0].id
    assert workouts.parse_workout(converted.model_dump()) == converted
    assert 'measured' not in v2.blocks[0].efforts[0].model_dump()
    with pytest.raises(ValueError, match='Unsupported'):
        workouts.parse_workout({'schema_version': 4})


def test_drop_groups_and_shared_intervals():
    ref = {'activity_id': 1, 'index': 0}
    group = {'id': 'g', 'effort_ids': ['a', 'b'], 'evidence': evidence()}
    d = document([effort('a', segments=[ref]), effort('b', segments=[ref])], [group])
    parsed = workouts.parse_workout(d)
    assert parsed.blocks[0].drop_sets[0].effort_ids == ['a', 'b']
    for ids in (['b', 'a'], ['a', 'a'], ['a', 'missing']):
        with pytest.raises(ValidationError):
            workouts.parse_workout(document(groups=[{**group, 'effort_ids': ids}]))
    with pytest.raises(ValidationError):
        workouts.parse_workout(document(groups=[group, {**group, 'id': 'g2'}]))


def test_load_and_bounded_metrics():
    load = workouts.LoadV3(basis='plates_per_side', kg=20, side_count=2)
    assert load.total_external_kg() is None
    assert workouts.LoadV3(basis='plates_per_side', kg=20, side_count=2, bar_kg=20).total_external_kg() == 60
    with pytest.raises(ValidationError):
        workouts.LoadV3(basis='plates_per_side', kg=20)
    value = {'kind': 'range', 'minimum': 100, 'maximum': 120, 'approximate': True, 'original_text': 'about 100–120 W'}
    d = document([effort('a', measured={'power_watts': value}, provenance={'measured.power_watts': evidence()})])
    assert workouts.parse_workout(d).blocks[0].efforts[0].measured.power_watts.original_text == 'about 100–120 W'
    for invalid in ({'kind': 'range', 'minimum': 2, 'maximum': 1}, {'kind': 'range', 'value': 2, 'minimum': 1, 'maximum': 3}, {'kind': 'exact', 'value': float('inf')}):
        with pytest.raises(ValidationError):
            workouts.BoundedValue.model_validate(invalid)
    with pytest.raises(ValidationError, match='provenance'):
        workouts.parse_workout(document([effort('a', measured={'power_watts': value})]))


def test_apply_restore_and_foreign_nested_evidence(tmp_path, monkeypatch):
    from afterset import connection, db, store
    monkeypatch.setattr(connection, 'DB_PATH', tmp_path / 'synthetic.db')
    store.initialize()
    db.upsert_activities([{'activity_id': 1, 'date': '2026-01-01', 'duration_seconds': 100},
                          {'activity_id': 2, 'date': '2026-01-01', 'duration_seconds': 100}])
    db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 100, 'setType': 'ACTIVE'}]})
    note = store.save_note(1, 'Two stages')['note_id']
    foreign = store.save_note(2, 'Other session')['note_id']
    ref = {'activity_id': 1, 'index': 0}
    group = {'id': 'g', 'effort_ids': ['a', 'b'], 'evidence': evidence()}
    structure = {'exercise': evidence(), 'kind': evidence(), 'segments': evidence()}
    d = document([effort('a', segments=[ref], load={'basis': 'plates_per_side', 'kg': 20, 'side_count': 2}, provenance={**structure, 'load': evidence()}),
                  effort('b', segments=[ref], measured={'power_watts': {'kind': 'range', 'minimum': 100, 'maximum': 120, 'approximate': True}}, provenance={**structure, 'measured.power_watts': evidence()})], [group])
    draft = store.propose_workout(1, note, workouts.parse_workout(d), 'Reconstruct', 0)
    rev = store.apply_proposal(draft['proposal_id'])['revision']
    saved = store.get_workout(1)
    assert saved['workout']['schema_version'] == 3
    assert saved['summary']['effective_duration_seconds'] == 100
    assert len(saved['recording_segments']) == 1
    raw = store.restore_sets(1, 0, rev, 'Raw')['revision']
    store.restore_sets(1, rev, raw, 'Restore')
    assert store.get_workout(1)['workout'] == saved['workout']
    assert store.get_workout(1, rev)['workout'] == saved['workout']

    bad = document([effort('x', provenance={'exercise': evidence(), 'kind': evidence()})])
    bad['blocks'][0].update(kind='amrap', format={'time_cap_seconds': 60, 'provenance': {'time_cap_seconds': {**evidence(), 'note_ids': [foreign]}}})
    with pytest.raises(ValueError, match='Evidence note'):
        store.propose_workout(1, note, workouts.parse_workout(bad), 'Invalid evidence', store.get_workout(1)['revision'])
    bad = document(groups=[{**group, 'evidence': {**evidence(), 'note_ids': [foreign]}}])
    with pytest.raises(ValueError, match='Evidence note'):
        store.propose_workout(1, note, workouts.parse_workout(bad), 'Invalid group evidence', store.get_workout(1)['revision'])


def test_bounded_value_rejects_boolean_and_contradictory_approximation():
    for value in ({'kind': 'exact', 'value': True}, {'kind': 'exact', 'value': 1, 'approximate': True},
                  {'kind': 'range', 'minimum': False, 'maximum': 3}):
        with pytest.raises(ValidationError):
            workouts.BoundedValue.model_validate(value)


def test_legacy_equal_numeric_power_does_not_conflict():
    legacy = {'source_activity_ids': [1], 'blocks': [{'label': 'Work', 'evidence': evidence(), 'efforts': [{
        'exercise': 'Row', 'evidence': 'Recorded', 'power_watts': 150, 'details': {'power_watts': 150},
        'provenance': {'power_watts': {'source': 'watch', 'text': 'Watch'}}}]}]}
    converted = workouts.convert_workout(legacy)
    assert converted.conflicts == []
    assert converted.blocks[0].efforts[0].measured.power_watts.value == 150
    assert converted.blocks[0].efforts[0].details['power_watts'] == 150


@pytest.mark.parametrize('value', [True, '20'])
def test_v3_measurements_reject_boolean_and_strings(value):
    with pytest.raises(ValidationError):
        workouts.LoadV3(basis='plates_per_side', kg=20, side_count=2, bar_kg=value)
    with pytest.raises(ValidationError):
        workouts.LoadV3(kg=value)
    with pytest.raises(ValidationError):
        workouts.MetricsV3(calories=value)
    with pytest.raises(ValidationError):
        workouts.BlockFormatV3(time_cap_seconds=value)
    with pytest.raises(ValidationError):
        workouts.EffortV3(id='e', exercise='Row', evidence='Synthetic', distance_meters=value, provenance={'distance_meters': evidence()})
    with pytest.raises(ValidationError):
        workouts.LoadV3(basis='plates_per_side', kg=20, side_count=value)
    # V2 keeps its historical coercion behavior.
    assert workouts.Load(kg=value).kg == float(value)
