"""Offline representation baseline; these tests do not evaluate an assistant."""
from unittest.mock import patch

from afterset import connection, db, store, workouts
from afterset.store.history import compare_workouts
from afterset.store.signals import compare_activity_heart_rate, get_activity_signals


def test_synthetic_reconstruction_survives_review_apply_and_compact(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 101, 'date': '2026-01-02', 'duration_seconds': 600},
                              {'activity_id': 102, 'date': '2026-01-09', 'duration_seconds': 600}])
        db.upsert_exercise_sets(101, {'exerciseSets': [
            {'duration': 400, 'setType': 'ACTIVE'}, {'duration': 200, 'setType': 'REST'}]})
        db.upsert_exercise_sets(102, {'exerciseSets': [{'duration': 600, 'setType': 'ACTIVE'}]})
        for aid, base in ((101, 100), (102, 110)):
            store.store_payload(aid, 'summary', {'elapsedDuration': 600})
            store.store_payload(aid, 'samples', {
                'metricDescriptors': [{'key': 'sumElapsedDuration', 'unit': 's', 'metricsIndex': 0},
                                      {'key': 'directHeartRate', 'unit': 'bpm', 'metricsIndex': 1}],
                'activityDetailMetrics': [{'metrics': [t, base + t // 60]} for t in range(0, 601, 5)]})
        note = store.save_note(101, 'Synthetic: squats and rows in the circuit; lunges during watch rest; a press at an uncertain point; band row in round one replaced by cable row in round two.')['note_id']
        evidence = workouts.Evidence(source='user', text='Synthetic account', note_ids=[note])
        def effort(name, key, segments, description='User account', **kwargs):
            return workouts.Effort(exercise=name, exercise_key=key, evidence=description, segments=segments,
                                   provenance=kwargs.pop('provenance', {'exercise': evidence}), **kwargs)
        active = [workouts.SegmentRef(activity_id=101, index=0)]
        rest = [workouts.SegmentRef(activity_id=101, index=1)]
        document = workouts.Workout(source_activity_ids=[101], blocks=[
            workouts.Block(kind='circuit', label='Circuit', evidence=evidence, efforts=[
                effort('Squat', 'squat', active, round=1, reps=10, reps_basis='total', provenance={'reps': evidence}),
                effort('Band row', 'band row', active, round=1, details={'variant': 'band'}),
                effort('Cable row', 'cable row', active, round=2, details={'variant': 'cable'},
                       description='User substituted cable row for band row in round two'),
                effort('Lunge', 'lunge', rest, round=2),
                effort('Press', 'press', [], details={'variant': 'seated'})])])
        proposal = store.propose_workout(101, note, document, 'Synthetic reconstruction', 0)
        reviewed = store.get_proposal(proposal['proposal_id'], include_workout=True)
        assert reviewed['workout']['blocks'][0]['efforts'][0]['segments'] == reviewed['workout']['blocks'][0]['efforts'][1]['segments']
        revision = store.apply_proposal(proposal['proposal_id'])['revision']
        saved = store.get_workout(101, include_recording_payload=True)
        assert saved['workout'] == document.model_dump()
        assert saved['recording_segments'][1]['payload']['setType'] == 'REST'
        assert saved['summary']['effective_duration_seconds'] == 600
        compact = store.get_activity(101)['workout']['blocks'][0]['efforts']
        assert [e['exercise'] for e in compact] == ['Squat', 'Band row', 'Cable row', 'Lunge', 'Press']
        assert compact[0]['segments'] == compact[1]['segments']
        assert compact[2]['round'] == 2 and compact[2]['comparison_conditions']['variant'] == 'cable'
        assert compact[3]['segments'][0]['index'] == 1
        assert compact[4]['segments'] == []
        assert compact[1]['comparison_conditions']['variant'] == 'band'
        # A later addition is a replacement proposal based on the current revision.
        updated = document.model_copy(deep=True)
        updated.blocks[0].efforts.append(effort('Carry', 'carry', []))
        second = store.propose_workout(101, note, updated, 'Synthetic addition', revision)
        store.apply_proposal(second['proposal_id'])
        assert store.get_workout(101)['workout']['blocks'][0]['efforts'][:5] == document.model_dump()['blocks'][0]['efforts']
        assert store.get_workout(101, revision)['workout'] == document.model_dump()

        # A later session provides comparable structure, feedback and independent HR.
        second_note = store.save_note(102, 'Synthetic feedback: felt steady.')['note_id']
        second_evidence = workouts.Evidence(source='user', text='Synthetic account', note_ids=[second_note])
        second_document = workouts.Workout(source_activity_ids=[102], blocks=[
            workouts.Block(kind='circuit', label='Circuit', evidence=second_evidence, efforts=[
                effort('Squat', 'squat', [workouts.SegmentRef(activity_id=102, index=0)],
                       round=1, reps=12, reps_basis='total', provenance={'reps': second_evidence})])])
        second_proposal = store.propose_workout(102, second_note, second_document, 'Synthetic comparison', 0)
        store.apply_proposal(second_proposal['proposal_id'])
        trace = get_activity_signals(101, include_samples=True, limit=2)
        assert trace['total'] == 121 and trace['next_offset'] == 2
        assert trace['samples'][0]['values']['heart_rate'] == 100
        assert trace['coverage']['coverage'] == 1
        heart_rate = compare_activity_heart_rate(101, 102)
        assert heart_rate['mean_difference_bpm'] == 10
        assert 'Whole raw activity' in heart_rate['recording_scope']
        assert heart_rate['sessions'][0]['effective_structure']['effort_count'] == 6
        comparison = compare_workouts(101, 102)
        squat = next(pair for pair in comparison['pairs'] if pair['first']['exercise_key'] == 'squat')
        assert squat['first']['segments'][0]['index'] == 0
        assert squat['first']['duration_seconds'] is None
        assert squat['metric_comparisons']['reps']['delta']['difference'] == 2
        assert comparison['second']['notes']['items'][0]['text'] == 'Synthetic feedback: felt steady.'
        assert any(item['effort']['exercise'] == 'Cable row' for item in comparison['unmatched_first'])
        # The whole-activity HR result and workout pairs contain no per-effort HR attribution.
        assert 'heart_rate' not in squat['first'] and 'heart_rate' not in squat['second']
        assert 'efforts' not in heart_rate['sessions'][0]
