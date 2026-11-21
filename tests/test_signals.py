import pytest
from unittest.mock import patch

from afterset import connection, db, signals, store
from afterset.store.signals import get_activity_signals, compare_activity_heart_rate


def sample(rows):
    return {'metricDescriptors': [
        {'key':'directHeartRate','unit':'bpm','metricsIndex':1},
        {'key':'sumElapsedDuration','unit':'s','metricsIndex':0},
        {'key':'directSpeed','unit':'m/s','metricsIndex':2},
        {'key':'directRunCadence','unit':'unknown','metricsIndex':3}],
        'activityDetailMetrics': [{'metrics': row} for row in rows]}


def test_irregular_samples_and_unknown_units():
    data = sample([[0,100,0,80], [5,110,1,80], [20,120,2,80], [25,0,0,80], [30,130,1,80]])
    normalized = signals.normalize(data)
    assert normalized['samples'][0]['values'] == {'heart_rate': 100, 'speed': 0}
    assert normalized['unknown_descriptor_count'] == 1
    stats = signals.summarize(normalized, 30)
    assert stats['mean_bpm'] == 105
    assert stats['covered_duration_seconds'] == 5
    assert stats['coverage'] == pytest.approx(1/6)
    assert stats['observed_min_bpm'] == 100
    assert stats['observed_max_bpm'] == 120
    assert stats['gap_threshold_seconds'] == 10


def test_duplicates_missing_and_unknown_time_exclude_interpolation():
    data = sample([[0,100,0], [5,110,0], [5,120,0], [10,130,0], [None,140,0]])
    stats = signals.summarize(signals.normalize(data), 10)
    assert stats['mean_bpm'] is None
    assert stats['duplicate_timestamps'] == 1
    assert stats['conflicting_timestamps'] == 1
    assert stats['missing_timestamps'] == 1
    data['metricDescriptors'][1]['unit'] = 'unknown'
    assert signals.summarize(signals.normalize(data), 10)['mean_bpm'] is None


def test_inventory_pagination_and_comparison_are_versioned(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01', 'avg_hr':99}, {'activity_id':2, 'date':'2026-01-02'}])
        for aid in (1, 2):
            store.store_payload(aid, 'summary', {'elapsedDuration':10})
            store.store_payload(aid, 'samples', sample([[0,100,0], [5,110,0], [10,120,0]]))
        result = get_activity_signals(1, limit=1, include_samples=True)
        assert result['coverage']['mean_bpm'] == 110
        assert result['total'] == 3 and result['next_offset'] == 1
        assert get_activity_signals(1)['samples'] is None
        with pytest.raises(ValueError, match='fingerprint'):
            get_activity_signals(1, offset=1, include_samples=True)
        store.store_payload(1, 'summary', {'elapsedDuration':11})
        with pytest.raises(ValueError, match='changed'):
            get_activity_signals(1, offset=1, include_samples=True, expected_fingerprint=result['fingerprint'])
        store.store_payload(1, 'summary', {'elapsedDuration':10})
        store.store_payload(1, 'samples', sample([[0,101,0], [5,111,0], [10,121,0]]))
        with pytest.raises(ValueError, match='changed'):
            get_activity_signals(1, offset=1, include_samples=True, expected_fingerprint=result['fingerprint'])
        comparison = compare_activity_heart_rate(1, 2)
        assert comparison['mean_difference_bpm'] == -1
        assert comparison['sessions'][0]['recorded_summary']['average_bpm'] == 99
        assert comparison['sessions'][0]['sample_statistics']['mean_bpm'] == 111


def test_invalid_numbers_and_missing_columns_are_not_measurements():
    data = sample([[0, float('inf'), 0], [5, -1], [10, 100, float('nan')], [15, 120, 0]])
    stats = signals.summarize(signals.normalize(data), float('inf'))
    assert stats['requested_duration_seconds'] is None
    assert stats['mean_bpm'] == 110
    assert stats['covered_duration_seconds'] == 5
    assert signals.normalize(data)['samples'][1]['values'] == {}
    assert signals.normalize(data)['samples'][2]['values'] == {'heart_rate': 100}


def test_garmin_unit_objects_and_timestamp_synthetic():
    descriptors = [
        {'metricsIndex': 0, 'key': 'sumElapsedDuration', 'unit': {'id':40, 'key':'second', 'factor':1000}},
        {'metricsIndex': 1, 'key': 'directTimestamp', 'unit': {'id':120, 'key':'gmt', 'factor':0}},
        {'metricsIndex': 2, 'key': 'directHeartRate', 'unit': {'id':100, 'key':'bpm', 'factor':1}},
        {'metricsIndex': 3, 'key': 'sumDistance', 'unit': {'id':1, 'key':'meter', 'factor':100}},
        {'metricsIndex': 4, 'key': 'directSpeed', 'unit': {'id':20, 'key':'mps', 'factor':0.1}},
        {'metricsIndex': 5, 'key': 'directBikeCadence', 'unit': {'id':103, 'key':'rpm', 'factor':1}},
        {'metricsIndex': 6, 'key': 'directPower', 'unit': {'id':10, 'key':'watt', 'factor':1}},
        {'metricsIndex': 7, 'key': 'directRunCadence', 'unit': {'key':'rpm', 'factor':1}},
    ]
    data = {'metricDescriptors': descriptors, 'activityDetailMetrics': [
        {'metrics':[0, 1700000000000, 100, 1.25, 2.5, 80, 100, 180]},
        {'metrics':[5, 1700000005000, 110, 3.75, 3.5, 82, 110, 182]},
    ]}
    result = signals.normalize(data)
    assert result['samples'][0]['elapsed_seconds'] == 0
    assert result['samples'][1]['utc_time'] == '2023-11-14T22:13:25Z'
    assert result['samples'][0]['values'] == {'heart_rate':100, 'distance':1.25, 'speed':2.5, 'cycling_cadence':80, 'power':100}
    assert result['unknown_descriptor_count'] == 1
    assert signals.summarize(result, 5)['mean_bpm'] == 105
    descriptors[0]['unit']['factor'] = 1
    assert signals.normalize(data)['time_basis'] == 'unknown'


def test_missing_or_backward_timestamps_are_barriers():
    data = sample([[0,100,0], [None,110,0], [5,120,0], [10,130,0]])
    stats = signals.summarize(signals.normalize(data), 10)
    assert stats['covered_duration_seconds'] == 5
    assert stats['mean_bpm'] == 125
    data = sample([[0,100,0], [10,110,0], [5,120,0], [15,130,0]])
    stats = signals.summarize(signals.normalize(data), 15)
    assert stats['backward_timestamps'] is True
    assert stats['mean_bpm'] is None


def test_unrecognized_conflicting_descriptor_invalidates_known_channel():
    data = sample([[0,100,0], [5,110,0]])
    data['metricDescriptors'].append({'metricsIndex':1, 'key':'other', 'unit':{'key':'unknown'}})
    normalized = signals.normalize(data)
    stats = signals.summarize(normalized, 5)
    assert stats['mean_bpm'] is None
    assert stats['coverage'] is None
    assert stats['coverage_unavailable_reason'] == 'unknown_heart_rate_unit'


def test_half_open_extrema_include_endpoint_only_for_integration():
    stats = signals.summarize(signals.normalize(sample([[0,100,0], [5,200,0]])), 5)
    assert stats['mean_bpm'] == 150
    assert stats['observed_max_bpm'] == 100
    assert stats['coverage'] == 1


def test_comparison_exposes_actual_coverage_and_raw_scope(tmp_path):
    from afterset import workouts
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}, {'activity_id':2, 'date':'2026-01-02'}])
        db.upsert_exercise_sets(1, {'exerciseSets':[{'duration':5, 'setType':'ACTIVE'}]})
        note = store.save_note(1, 'Excluded a recording interval')['note_id']
        document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
            kind='straight_sets', label='Excluded', evidence=workouts.Evidence(source='user', text='User report'),
            efforts=[workouts.Effort(exercise='Excluded interval', kind='excluded', evidence='User report', segments=[workouts.SegmentRef(activity_id=1, index=0)])])])
        proposal = store.propose_workout(1, note, document, 'Exclude interval', 0)
        store.apply_proposal(proposal['proposal_id'])
        store.store_payload(1, 'summary', {'elapsedDuration':10})
        store.store_payload(2, 'summary', {'elapsedDuration':20})
        store.store_payload(1, 'samples', sample([[0,100,0], [5,110,0]]))
        comparison = compare_activity_heart_rate(1, 2)
        a, b = comparison['sessions']
        assert a['coverage_incomplete'] is True and b['coverage_incomplete'] is None
        assert a['effective_structure']['has_exclusions'] is True
        assert a['effective_structure']['source'] == 'structured_correction'
        assert b['elapsed_duration_seconds'] == 20 and b['sample_statistics'] is None
        assert comparison['differences']['elapsed_duration'] is True
        assert comparison['differences']['sample_coverage'] is None
        assert 'raw activity' in comparison['recording_scope']
