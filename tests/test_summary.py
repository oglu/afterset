"""Compact analysis output and explicit audit expansion."""
from contextlib import ExitStack
import json
from unittest.mock import patch

import pytest
from afterset import connection, db, store, workouts


class TestSummary:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            setting = patch.object(connection, 'DB_PATH', tmp_path / 'records.db')
            stack.enter_context(setting)
            store.initialize()
            db.upsert_activities([{'activity_id': 1, 'date': '2026-10-16', 'name': 'Strength', 'duration_seconds': 120},
                                  {'activity_id': 2, 'date': '2026-10-16', 'name': 'Run'},
                                  {'activity_id': 3, 'date': '2026-10-18', 'name': 'Outside range'}])
            db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 30, 'setType': 'ACTIVE', 'repetitionCount': 9,
                                                      'exerciseCandidates': ['x' * 10000]}]})
            store.store_payload(2, 'summary', {'distance': 10000, 'averageSpeed': 3.2,
                                             'movingDuration': 3125, 'elapsedDuration': 3300})
            self.note = store.save_note(1, '[subjective_feedback v1] No nap yet; recovery ongoing.')['note_id']
            document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(
                label='Lunges', kind='straight_sets', evidence=workouts.Evidence(source='user', text='User report', note_ids=[self.note]),
                efforts=[workouts.Effort(exercise='Lunge', reps=8, reps_basis='per_leg',
                                        load=workouts.Load(kg=10, basis='per_hand', implement_count=2),
                                        evidence='User confirmed', segments=[workouts.SegmentRef(activity_id=1, index=0)])])])
            self.draft = store.propose_workout(1, self.note, document, 'Reported workout', 0)
            self.revision = store.apply_proposal(self.draft['proposal_id'])['revision']
            store.propose_workout(1, self.note, document, 'Pending draft', self.revision)
            db.upsert_rows([{'date': '2026-10-16', 'sleep_score': 80, 'sleep_hrv': 45},
                            {'date': '2026-10-18', 'sleep_score': 90, 'sleep_hrv': 55}])
            yield

    def test_weekly_summary_includes_effective_sessions_notes_gaps_and_next_morning(self):
        result = store.training_summary('2026-10-16', '2026-10-17')
        assert [row['activity']['activity_id'] for row in result['activities']] == [1, 2]
        run = result['activities'][1]['activity']
        assert run['distance_meters'] == 10000
        assert run['moving_duration_seconds'] == 3125
        assert run['elapsed_duration_seconds'] == 3300
        session = result['activities'][0]
        assert session['revision'] == self.revision
        assert 'No nap yet' in session['notes'][0]['note']
        effort = session['workout']['blocks'][0]['efforts'][0]
        assert (effort['reps'], effort['reps_basis']) == (8, 'per_leg')
        assert effort['load'] == {'kg': 10, 'basis': 'per_hand', 'implement_count': 2}
        assert effort['segments'] == [{'activity_id': 1, 'index': 0}]
        assert session['workout']['blocks'][0]['classification_source'] == 'user'
        assert 'evidence' not in effort
        assert 'details' not in effort
        assert len(result['days']) == 2
        assert result['days'][0]['sleep_hrv'] == 45
        assert not result['days'][1]['recorded']
        assert result['days'][1]['sleep_hrv'] is None
        assert result['following_morning']['date'] == '2026-10-18'
        assert result['following_morning']['sleep_score'] == 90
        for session in result['activities']:
            for field in ('garmin_sets_response', 'revision_history', 'proposals', 'cleaned_sets'):
                assert field not in session

    def test_get_activity_default_is_lean_and_audit_parts_are_independent(self):
        lean = store.get_activity(1)
        for field in ('garmin_sets_response', 'revision_history', 'proposals', 'cleaned_sets'):
            assert field not in lean
        raw = store.get_activity(1, include=['raw'])
        assert raw['garmin_sets_response']['exerciseSets'][0]['repetitionCount'] == 9
        assert 'revision_history' not in raw
        history = store.get_activity(1, include=['history'])
        assert history['revision_history'][0]['revision_id'] == self.revision
        assert 'sets_json' not in history['revision_history'][0]
        proposals = store.get_activity(1, include=['proposals'])
        assert len(proposals['proposals']) == 2
        assert 'workout' in proposals['proposals'][0]
        assert 'sets' not in proposals['proposals'][0]
        full = store.get_activity(1, include=['raw', 'history', 'proposals'])
        assert len(json.dumps(lean)) < len(json.dumps(full)) / 3
        with pytest.raises(ValueError):
            store.get_activity(1, include=['typo'])

    def test_raw_workout_compact_view_does_not_hide_large_payloads_in_details(self):
        store.restore_sets(1, 0, self.revision, 'Use Garmin')
        result = store.get_activity(1)
        assert 'x' * 100 not in json.dumps(result)
        detailed = store.get_workout(1, include_recording_payload=True)
        assert 'x' * 100 in json.dumps(detailed)

    def test_summary_is_read_only_and_empty_days_are_reported(self):
        with connection.connect() as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE activities SET name='Uncommitted' WHERE activity_id=1")
            summary = store.training_summary('2026-10-16', '2026-10-17')
            assert summary['activities'][0]['activity']['name'] == 'Strength'
        empty = store.training_summary('2026-01-01', '2026-01-07')
        assert empty['activities'] == []
        assert len(empty['days']) == 7
        assert not any(day['recorded'] for day in empty['days'])

    @pytest.mark.parametrize('start, end', [
        ('2026-01-01', '2026-02-01'), ('2026-02-01', '2026-01-01'), ('9999-12-31', '9999-12-31'),
    ])
    def test_summary_bounds_are_enforced(self, start, end):
        with pytest.raises(ValueError):
            store.training_summary(start, end)
