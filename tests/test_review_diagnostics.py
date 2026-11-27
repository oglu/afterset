"""Synthetic review and applied receipt behavior."""
from unittest.mock import patch

from afterset import connection, db, store, workouts


def test_review_diagnostics_and_saved_receipt_follow_applied_snapshot(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02', 'duration_seconds': 300}])
        db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 200, 'setType': 'ACTIVE'}, {'duration': 100, 'setType': 'REST'}]})
        note = store.save_note(1, 'Synthetic session description')['note_id']
        evidence = workouts.Evidence(source='user', text='Synthetic report', note_ids=[note])
        def effort(name, **kwargs):
            return workouts.Effort(exercise=name, evidence='Synthetic report', **kwargs)
        workout = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
            kind='circuit', label='Circuit', evidence=evidence, efforts=[
                effort('Unknown convention', load=workouts.Load(kg=20), details={'reps': 8}),
                effort('One-hand count unknown', load=workouts.Load(kg=10, basis='per_hand'), details={'distance_meters': 25}),
                effort('Known per-hand', load=workouts.Load(kg=10, basis='per_hand', implement_count=2),
                       segments=[workouts.SegmentRef(activity_id=1, index=0)]),
                effort('Rest interval work', segments=[workouts.SegmentRef(activity_id=1, index=1)],
                       details={'duration_seconds': 15, 'load_kg': 5, 'power_watts': 120}),
                effort('Unknowns allowed', details={'reps': 'eight', 'load_kg': True}),
            ])])
        draft = store.propose_workout(1, note, workout, 'Synthetic reconstruction', 0, request_key='draft-1')
        diagnostics = draft['unresolved_fields']
        kinds = [(d['kind'], d['effort_index'], d.get('details_key')) for d in diagnostics]
        assert ('unknown_load_basis', 0, None) in kinds
        assert ('unknown_implement_count', 1, None) in kinds
        assert {(i, key) for kind, i, key in kinds if kind == 'auxiliary_structured_value'} == {
            (0, 'reps'), (1, 'distance_meters'), (3, 'duration_seconds'), (3, 'load_kg'), (3, 'power_watts')}
        assert all(index != 2 and index != 4 for _, index, _ in kinds)
        power = next(d for d in diagnostics if d.get('details_key') == 'power_watts')
        assert power['candidate_targets'] == ['power_watts', 'reported.power_watts', 'targets.power_watts']
        assert power['source_unresolved']
        assert draft['workout'] == workout.model_dump()
        assert store.propose_workout(1, note, workout, 'Synthetic reconstruction', 0, request_key='draft-1')['proposal_id'] == draft['proposal_id']
        receipt = store.apply_proposal(draft['proposal_id'])
        saved = store.get_workout(1, include_recording_payload=True)
        assert receipt['saved_workout_available'] and not receipt['saved_workout']['truncated']
        assert [e['exercise'] for e in receipt['saved_workout']['blocks'][0]['efforts']] == [e.exercise for e in workout.blocks[0].efforts]
        assert receipt['saved_workout']['blocks'][0]['efforts'][1]['load']['basis'] == 'per_hand'
        assert [d['kind'] for d in receipt['unresolved_fields']] == [d['kind'] for d in diagnostics]
        assert saved['recording_segments'][1]['payload']['setType'] == 'REST'
        assert saved['workout'] == workout.model_dump()
        store.restore_sets(1, 0, receipt['revision'], 'Synthetic restore')
        retry = store.apply_proposal(draft['proposal_id'])
        assert retry['already_applied'] and retry['saved_workout'] == receipt['saved_workout']
        assert retry['current_revision'] != retry['revision']
        assert retry['unresolved_fields'] == receipt['unresolved_fields']


def test_v3_plates_only_and_unknown_bar_mass_are_valid(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets': []})
        note = store.save_note(1, 'Synthetic bar and plates')['note_id']
        evidence = workouts.Evidence(source='user', text='Reported', note_ids=[note])
        workout = workouts.WorkoutV3(source_activity_ids=[1], blocks=[workouts.BlockV3(
            id='block', kind='straight_sets', label='Press', evidence=evidence, efforts=[
                workouts.EffortV3(id='a', exercise='Press', evidence='Reported',
                    load=workouts.LoadV3(kg=10, basis='plates_per_side', side_count=2),
                    provenance={'load': evidence, 'exercise': evidence, 'kind': evidence}),
                workouts.EffortV3(id='b', exercise='Press', evidence='Reported',
                    load=workouts.LoadV3(kg=20, basis='plates_only'), provenance={'load': evidence, 'exercise': evidence, 'kind': evidence})])])
        draft = store.propose_workout(1, note, workout, 'Synthetic report', 0)
        assert [(d['kind'], d['effort_index']) for d in draft['unresolved_fields']] == [('unknown_bar_mass', 0)]
        receipt = store.apply_proposal(draft['proposal_id'])
        assert receipt['saved_workout']['blocks'][0]['efforts'][0]['load']['bar_kg'] is None
        assert receipt['saved_workout']['blocks'][0]['efforts'][1]['load']['basis'] == 'plates_only'
        assert receipt['unresolved_count'] == 1


def test_large_auxiliary_numbers_and_unresolved_power_category(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets': []})
        note = store.save_note(1, 'Synthetic report')['note_id']
        evidence = workouts.Evidence(source='user', text='Reported', note_ids=[note])
        workout = workouts.WorkoutV3(source_activity_ids=[1], blocks=[workouts.BlockV3(
            id='b', label='Work', evidence=evidence, efforts=[workouts.EffortV3(id='e', exercise='Row', evidence='Reported',
                details={'distance_meters': 10**400, 'power_watts': 120},
                provenance={'exercise': evidence, 'kind': evidence, 'details': evidence})])])
        draft = store.propose_workout(1, note, workout, 'Synthetic report', 0)
        diagnostics = {d['details_key']: d for d in draft['unresolved_fields']}
        assert diagnostics['distance_meters']['value_omitted'] is True
        assert 'value' not in diagnostics['distance_meters']
        power = diagnostics['power_watts']
        assert power['source_unresolved'] and 'target' not in power
        assert power['candidate_targets'] == ['measured.power_watts', 'reported.power_watts', 'targets.power_watts']


def test_receipt_is_bounded_and_legacy_missing_snapshot_is_not_substituted(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 1, 'setType': 'ACTIVE'} for _ in range(12)]})
        note = store.save_note(1, 'Synthetic report')['note_id']
        evidence = workouts.Evidence(source='user', text='Reported', note_ids=[note])
        refs = [workouts.SegmentRef(activity_id=1, index=i) for i in range(12)]
        workout = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
            label='Work', evidence=evidence, efforts=[workouts.Effort(exercise=f'Effort {i}', evidence='Reported', segments=refs) for i in range(51)])])
        draft = store.propose_workout(1, note, workout, 'Synthetic report', 0)
        receipt = store.apply_proposal(draft['proposal_id'])
        saved = receipt['saved_workout']
        assert saved['truncated'] and saved['effort_count'] == 51
        assert len(saved['blocks'][0]['efforts']) == 50
        assert len(saved['blocks'][0]['efforts'][0]['segments']) == 10
        assert saved['segment_link_count'] == 612 and saved['shown_segment_link_count'] == 500
        assert len(store.get_workout(1, revision=receipt['revision'])['workout']['blocks'][0]['efforts']) == 51
        # Simulate an older applied proposal whose revision predates structured snapshots.
        with store.connect() as conn:
            conn.execute('DELETE FROM workout_versions WHERE activity_id=? AND revision_id=?', (1, receipt['revision']))
        retry = store.apply_proposal(draft['proposal_id'])
        assert retry['already_applied'] and retry['revision'] == receipt['revision']
        assert retry['saved_workout'] is None and retry['saved_workout_available'] is False


def test_receipt_omits_arbitrary_conflict_payload_and_format_evidence(tmp_path):
    import json
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets': []})
        note = store.save_note(1, 'Synthetic report')['note_id']
        evidence = workouts.Evidence(source='user', text='z' * 4000, note_ids=[note])
        workout = workouts.WorkoutV3(source_activity_ids=[1], blocks=[workouts.BlockV3(
            id='b', kind='amrap', label='Work', evidence=evidence,
            format=workouts.BlockFormatV3(time_cap_seconds=60, provenance={'time_cap_seconds': evidence}),
            efforts=[])], conflicts=[{'status': 'unresolved', 'metric': 'x' * 10000,
                                      'effort_id': 'effort', 'payload': 'q' * 300000}])
        draft = store.propose_workout(1, note, workout, 'Synthetic report', 0)
        receipt = store.apply_proposal(draft['proposal_id'])
        assert len(json.dumps(receipt)) < 10000
        assert receipt['conflicts'][0]['details_omitted']
        assert len(receipt['conflicts'][0]['metric']) == 200
        nested = receipt['unresolved_fields'][0]['conflict']
        assert nested['details_omitted'] and 'payload' not in nested
        fmt = receipt['saved_workout']['blocks'][0]['format']
        assert fmt['field_sources'] == {'time_cap_seconds': 'user'}
        assert 'provenance' not in fmt and 'z' * 100 not in json.dumps(receipt)
        full = store.get_proposal(draft['proposal_id'], include_workout=True)
        assert full['workout']['conflicts'][0]['payload'] == 'q' * 300000


def test_receipt_discloses_omitted_drop_sets_and_conditions_after_edit(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 1, 'date': '2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets': []})
        note = store.save_note(1, 'Synthetic report')['note_id']
        evidence = workouts.Evidence(source='user', text='Initial report', note_ids=[note])
        efforts = [workouts.EffortV3(id=f'e{i}', exercise='Row', evidence='Reported',
                   provenance={'exercise': evidence, 'kind': evidence}) for i in range(2)]
        workout = workouts.WorkoutV3(source_activity_ids=[1], blocks=[workouts.BlockV3(
            id='b', label='Rows', evidence=evidence, efforts=efforts)])
        first = store.apply_proposal(store.propose_workout(1, note, workout, 'Initial', 0)['proposal_id'])
        updated = workout.model_copy(deep=True)
        updated_evidence = workouts.Evidence(source='user', text='Confirmed change', note_ids=[note])
        updated.blocks[0].evidence = updated_evidence
        updated.blocks[0].drop_sets = [workouts.DropSetGroup(id='g', effort_ids=['e0', 'e1'], evidence=updated_evidence)]
        updated.blocks[0].efforts[0].details = {'pause': 2}
        updated.blocks[0].efforts[0].provenance['details'] = updated_evidence
        draft = store.propose_workout(1, note, updated, 'Add drop set and pause', first['revision'])
        receipt = store.apply_proposal(draft['proposal_id'])
        assert receipt['saved_workout']['truncated'] is False
        assert receipt['saved_workout']['projection_complete'] is False
        assert {'drop_sets', 'details'} <= set(receipt['saved_workout']['omitted_fields'])
        actual = store.get_workout(1, revision=receipt['revision'])['workout']['blocks'][0]
        assert actual['drop_sets'][0]['effort_ids'] == ['e0', 'e1']
        assert actual['efforts'][0]['details']['pause'] == 2
