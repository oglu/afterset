"""Synthetic later recall, using persisted workouts rather than raw labels."""
from unittest.mock import patch

from afterset import connection, db, store, workouts
from afterset.store.history import compare_workouts, get_exercise_history
from afterset.store.signals import compare_activity_heart_rate


def test_saved_ranges_conditions_feedback_and_shared_recording_recalled(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        for aid, date, lower in ((1, '2026-01-02', 100), (2, '2026-01-09', 110)):
            db.upsert_activities([{'activity_id': aid, 'date': date, 'duration_seconds': 10}])
            db.upsert_exercise_sets(aid, {'exerciseSets': [{'duration': 10, 'setType': 'ACTIVE', 'category': 'Unidentified'}]})
            store.store_payload(aid, 'summary', {'elapsedDuration': 10})
            store.store_payload(aid, 'samples', {'metricDescriptors': [
                {'key': 'sumElapsedDuration', 'unit': 's', 'metricsIndex': 0},
                {'key': 'directHeartRate', 'unit': 'bpm', 'metricsIndex': 1}],
                'activityDetailMetrics': [{'metrics': [t, lower + t]} for t in (0, 5, 10)]})
            note = store.save_note(aid, f'Synthetic feedback {aid}: felt steady.')['note_id']
            evidence = workouts.Evidence(source='user', text='Reported', note_ids=[note])
            effort = workouts.EffortV3(id='row', exercise='Seated row', exercise_key='seated row',
                evidence='Reported', segments=[workouts.SegmentRef(activity_id=aid, index=0)],
                reported=workouts.MetricsV3(power_watts=workouts.BoundedValue(kind='range', minimum=lower,
                    maximum=lower + 20, approximate=True)), details={'variant': 'seated', 'pause': 2},
                provenance={key: evidence for key in ('exercise', 'exercise_key', 'kind', 'segments', 'reported.power_watts', 'details')})
            second = workouts.EffortV3(id='squat', exercise='Squat', evidence='Reported',
                segments=[workouts.SegmentRef(activity_id=aid, index=0)],
                provenance={key: evidence for key in ('exercise', 'kind', 'segments')})
            document = workouts.WorkoutV3(source_activity_ids=[aid], blocks=[workouts.BlockV3(
                id='circuit', kind='circuit', label='Circuit', evidence=evidence, efforts=[effort, second])])
            draft = store.propose_workout(aid, note, document, 'Synthetic report', 0)
            store.apply_proposal(draft['proposal_id'])
        # Re-open through independent reads as a later conversation would; no assistant is evaluated.
        compact = store.get_activity(1)['workout']['blocks'][0]['efforts']
        assert compact[0]['exercise'] == 'Seated row'
        assert compact[0]['segments'] == compact[1]['segments']
        assert compact[0]['comparison_conditions'] == {'variant': 'seated', 'pause': 2}
        history = get_exercise_history('seated row', '2026-01-01', '2026-01-10')
        assert history['total'] == 2
        assert history['items'][0]['effort']['conditions']['pause'] == 2
        assert history['sessions'][1]['notes']['items'][0]['text'] == 'Synthetic feedback 1: felt steady.'
        result = compare_workouts(1, 2)
        pair = result['pairs'][0]
        metric = pair['metric_comparisons']['reported.power_watts']
        assert metric['first']['minimum'] == 100 and metric['second']['maximum'] == 130
        assert metric['delta'] == {'kind': 'range', 'minimum': -10, 'maximum': 30, 'approximate': True, 'percent': None}
        assert metric['sources'] == {'first': 'user', 'second': 'user'}
        assert pair['first']['duration_seconds'] is None and pair['first']['conditions']['pause'] == 2
        assert 'heart_rate' not in pair['first']
        hr = compare_activity_heart_rate(1, 2)
        assert 'Whole raw activity' in hr['recording_scope'] and hr['mean_difference_bpm'] == 10
        assert 'efforts' not in hr['sessions'][0]
        assert store.get_workout(1)['summary']['effective_duration_seconds'] == 10
