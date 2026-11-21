from contextlib import ExitStack, closing
import json
import sqlite3
from unittest.mock import patch

import pytest
from afterset import connection, db, store, workouts


class TestWorkout:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            stack.enter_context(patch.object(connection, 'DB_PATH', tmp_path / 'records.db'))
            store.initialize()
            db.upsert_activities([{'activity_id': 1, 'date': '2026-11-02', 'duration_seconds': 1000}, {'activity_id': 2, 'date': '2026-11-02', 'duration_seconds': 100}])
            db.upsert_exercise_sets(1, {'exerciseSets': [
                {'duration': 30, 'setType': 'ACTIVE', 'repetitionCount': 10, 'startTime': '2026-11-02T08:00:00Z'},
                {'duration': 970, 'setType': 'REST', 'startTime': '2026-11-02T08:00:30Z'}]})
            db.upsert_exercise_sets(2, {'exerciseSets': [{'duration': 100, 'setType': 'ACTIVE'}]})
            self.note = store.save_note(1, 'Eight lunges per leg, 10kg per hand. Then forgot to stop the watch.')['note_id']
            yield

    def document(self):
        return workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(kind='straight_sets', label='Lunges',
            evidence=workouts.Evidence(source='user', text='User description', note_ids=[self.note]), efforts=[
                workouts.Effort(exercise='Dumbbell lunge', exercise_key='dumbbell_lunge', reps=8, reps_basis='per_leg',
                    load=workouts.Load(kg=10, basis='per_hand', implement_count=2),
                    evidence='User confirmed load and reps; watch interval covers the set.',
                    segments=[workouts.SegmentRef(activity_id=1, index=0)],
                    provenance={'reps': workouts.Evidence(source='user', text='Eight per leg', note_ids=[self.note])}),
                workouts.Effort(exercise='Timer overrun', kind='excluded', evidence='User confirmed forgotten timer',
                    segments=[workouts.SegmentRef(activity_id=1, index=1)])])])

    def proposal(self, document=None):
        return store.propose_workout(1, self.note, document or self.document(), 'Structured reconstruction', 0)

    def timed_block(self, kind, **format_fields):
        return workouts.Block(kind=kind, label=f'Synthetic {kind}',
            evidence=workouts.Evidence(source='user', text='User description', note_ids=[self.note]),
            format=workouts.BlockFormat(**format_fields),
            efforts=[workouts.Effort(exercise='Burpee', round=1, reps=10, evidence='User reported',
                                     segments=[workouts.SegmentRef(activity_id=1, index=0)])])

    def test_amrap_format_survives_apply_projection_and_compact_view(self):
        block = self.timed_block('amrap', time_cap_seconds=720, rounds_completed=5, extra_reps=7,
            provenance={'rounds_completed': workouts.Evidence(source='user', text='Five rounds plus seven', note_ids=[self.note])})
        document = workouts.Workout(source_activity_ids=[1], blocks=[block])
        result = store.apply_proposal(self.proposal(document)['proposal_id'])
        assert store.get_workout(1)['workout'] == document.model_dump()
        with store.connect() as c:
            row = c.execute('SELECT b.kind,b.details_json FROM workout_blocks b JOIN workout_versions v USING(version_id) WHERE v.activity_id=1 AND v.revision_id=?', (result['revision'],)).fetchone()
        assert row['kind'] == 'amrap'
        assert json.loads(row['details_json'])['format']['rounds_completed'] == 5
        compact = store.get_activity(1)['workout']['blocks'][0]['format']
        assert (compact['time_cap_seconds'], compact['rounds_completed'], compact['extra_reps']) == (720, 5, 7)
        assert compact['field_sources'] == {'rounds_completed': 'user'}

    @pytest.mark.parametrize('kind, fields', [
        ('circuit', {'time_cap_seconds': 600}),
        ('amrap', {'interval_seconds': 60}),
        ('emom', {'rounds_completed': 3}),
        ('emom', {'interval_seconds': 120, 'time_cap_seconds': 60}),
        ('emom', {'interval_seconds': 0}),
        ('amrap', {'provenance': {'score': workouts.Evidence(source='user', text='x')}}),
    ])
    def test_format_must_fit_block_kind(self, kind, fields):
        with pytest.raises(ValueError):
            self.timed_block(kind, **fields)

    def test_structured_apply_idempotency_normalized_fields_and_restore(self):
        proposal = self.proposal()
        assert store.get_workout(1)['revision'] == 0
        result = store.apply_proposal(proposal['proposal_id'])
        assert store.apply_proposal(proposal['proposal_id'])['already_applied']
        view = store.get_workout(1)
        assert view['workout'] == self.document().model_dump()
        assert view['summary']['effective_duration_seconds'] == 30
        assert view['summary']['excluded_duration_seconds'] == 970
        assert view['unmapped_recording_segments'] == []
        with store.connect() as c:
            effort = c.execute("SELECT e.* FROM workout_efforts e JOIN workout_blocks b USING(block_id) JOIN workout_versions v USING(version_id) WHERE v.activity_id=1 AND v.revision_id=? AND e.kind='working'", (result['revision'],)).fetchone()
            assert (effort['reps'], effort['reps_basis'], effort['load_kg'], effort['load_basis']) == (8, 'per_leg', 10, 'per_hand')
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        # Compact analysis preserves per-leg reps and per-hand load conventions.
        effort = store.get_activity(1)['workout']['blocks'][0]['efforts'][0]
        assert (effort['reps'], effort['reps_basis']) == (8, 'per_leg')
        assert effort['load']['basis'] == 'per_hand'
        undo = store.restore_sets(1, 0, result['revision'], 'Raw mode')
        assert store.get_activity(1)['source'] == 'garmin'
        redo = store.restore_sets(1, result['revision'], undo['revision'], 'Restore structured workout')
        assert store.get_workout(1)['workout'] == view['workout']
        assert store.get_workout(1, result['revision'])['summary'] == view['summary']
        assert redo['revision'] > undo['revision']

    def test_snapshot_and_redo_preserve_recording_after_sync(self):
        revision = store.apply_proposal(self.proposal()['proposal_id'])['revision']
        original = store.get_workout(1)
        db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 5, 'setType': 'ACTIVE'}]})
        with store.connect() as conn:
            count = conn.execute('SELECT count(*) FROM recording_segments s JOIN workout_versions v USING(version_id) WHERE v.activity_id=1 AND v.revision_id=0').fetchone()[0]
            assert count == 1
        unchanged = store.get_workout(1, include_recording_payload=True)
        assert unchanged['workout'] == original['workout']
        assert unchanged['summary'] == original['summary']
        assert unchanged['recording_segments'][1]['payload']['duration'] == 970
        assert unchanged['recording_changed_since_revision']
        raw_revision = store.restore_sets(1, 0, revision, 'Use refreshed raw')['revision']
        assert len(store.get_workout(1)['recording_segments']) == 1
        store.restore_sets(1, revision, raw_revision, 'Redo historical correction')
        restored = store.get_workout(1)
        assert restored['recording_segments'] == original['recording_segments']
        assert restored['summary'] == original['summary']

    @pytest.mark.parametrize('historical_raw', [True, False])
    def test_restore_rejects_missing_historical_snapshot(self, historical_raw):
        corrected = store.apply_proposal(self.proposal()['proposal_id'])['revision']
        raw = store.restore_sets(1, 0, corrected, 'Use recording')['revision']
        target = raw if historical_raw else corrected
        current = store.restore_sets(1, corrected, raw, 'Use correction')['revision']
        before = store.get_workout(1)
        with store.connect() as conn:
            conn.execute('DELETE FROM workout_versions WHERE activity_id=? AND revision_id=?', (1, target))
            revision_count = conn.execute('SELECT count(*) FROM set_revisions').fetchone()[0]

        with pytest.raises(ValueError, match='Revision snapshot is unavailable'):
            store.restore_sets(1, target, current, 'Restore missing snapshot')

        assert store.get_workout(1) == before
        with store.connect() as conn:
            assert conn.execute('SELECT count(*) FROM set_revisions').fetchone()[0] == revision_count
            assert conn.execute('SELECT revision_id FROM set_heads WHERE activity_id=1').fetchone()[0] == current

        # Explicitly choosing the current recording still works without historical snapshots.
        restored = store.restore_sets(1, 0, current, 'Use current recording')
        assert restored['revision'] > current
        assert store.get_activity(1)['source'] == 'garmin'

    def test_read_missing_frozen_head_does_not_substitute_current_recording(self):
        raw = store.restore_sets(1, 0, 0, 'Use recording')['revision']
        db.upsert_exercise_sets(1, {'exerciseSets': [{'duration': 20, 'setType': 'ACTIVE'}]})
        frozen = store.restore_sets(1, raw, raw, 'Use historical recording')['revision']
        assert store.get_workout(1)['recording_segments'][0]['duration_seconds'] == 30
        with store.connect() as conn:
            conn.execute('DELETE FROM workout_versions WHERE activity_id=? AND revision_id=?', (1, frozen))

        with pytest.raises(ValueError, match='Workout revision does not belong to this activity'):
            store.get_workout(1)
        with store.connect() as conn:
            assert conn.execute('SELECT revision_id FROM set_heads WHERE activity_id=1').fetchone()[0] == frozen

        # An explicit choice of the live recording remains a valid recovery.
        store.restore_sets(1, 0, frozen, 'Use current recording')
        assert store.get_workout(1)['recording_segments'][0]['duration_seconds'] == 20

    def test_proposal_rejects_stale_heads_and_changed_recordings(self):
        a, b = self.proposal(), self.proposal()
        store.apply_proposal(a['proposal_id'])
        with pytest.raises(ValueError, match='Stale'):
            store.apply_proposal(b['proposal_id'])
        with pytest.raises(ValueError, match='Revision changed'):
            self.proposal()
        with pytest.raises(ValueError, match='does not belong'):
            store.restore_sets(2, 1, 0, 'Wrong session')
        # A recording can change without a correction-head change.
        note = store.save_note(2, 'Warmup')['note_id']
        workout = workouts.Workout(source_activity_ids=[2], blocks=[])
        draft = store.propose_workout(2, note, workout, 'Empty corrected session', 0)
        db.upsert_exercise_sets(2, {'exerciseSets': []})
        with pytest.raises(ValueError, match='Recording changed'):
            store.apply_proposal(draft['proposal_id'])

    def test_multi_activity_and_many_to_many_recording_links(self):
        document = self.document()
        document.source_activity_ids.append(2)
        effort = document.blocks[0].efforts[0]
        effort.segments.append(workouts.SegmentRef(activity_id=2, index=0))
        second = effort.model_copy(deep=True)
        second.exercise = 'Second exercise sharing the combined interval'
        document.blocks[0].efforts.append(second)
        store.apply_proposal(self.proposal(document)['proposal_id'])
        result = store.get_workout(1)
        assert result['workout']['source_activity_ids'] == [1, 2]
        assert result['summary']['effective_duration_seconds'] is None
        with store.connect() as c:
            count = c.execute('SELECT count(*) FROM effort_segments l JOIN workout_efforts e USING(effort_id) JOIN workout_blocks b USING(block_id) JOIN workout_versions v USING(version_id) WHERE v.revision_id>0').fetchone()[0]
        assert count == 5

    def test_invalid_links_conflicting_exclusion_and_foreign_notes(self):
        invalid = self.document()
        invalid.blocks[0].efforts[0].segments[0].index = 20
        with pytest.raises(ValueError, match='does not exist'):
            self.proposal(invalid)
        invalid = self.document()
        invalid.blocks[0].efforts[1].segments[0].index = 0
        with pytest.raises(ValueError, match='excluded and included'):
            self.proposal(invalid)
        invalid = self.document()
        invalid.blocks[0].evidence.note_ids = [store.save_note(2, 'Other activity')['note_id']]
        with pytest.raises(ValueError, match='Evidence note'):
            self.proposal(invalid)
        with pytest.raises(ValueError):
            workouts.Effort(exercise='Run', distance_meters=float('nan'), evidence='Invalid')
        with pytest.raises(ValueError):
            workouts.Effort(exercise='Run', reps=True, evidence='Invalid')

    def test_failed_initialization_rolls_back_schema_and_projection(self, tmp_path):
        # Force a failure after the snapshot refresh writes its projections.
        path = tmp_path / 'rollback.db'
        with closing(sqlite3.connect(path)) as c, c:
            c.executescript(db.SCHEMA)
            c.execute("INSERT INTO activities(activity_id,date) VALUES(7,'2026-01-01')")
        original = workouts.refresh_raw_snapshots
        def broken(conn):
            original(conn)
            raise RuntimeError('Injected failure')
        with patch.object(connection, 'DB_PATH', path), patch.object(workouts, 'refresh_raw_snapshots', side_effect=broken):
            with pytest.raises(RuntimeError, match='Injected'):
                store.initialize()
        with closing(sqlite3.connect(path)) as c, c:
            assert c.execute('SELECT count(*) FROM activities').fetchone()[0] == 1
            assert c.execute("SELECT name FROM sqlite_master WHERE name='workout_versions'").fetchone() is None
            assert c.execute("SELECT name FROM sqlite_master WHERE name='set_proposals'").fetchone() is None

    def test_initialization_is_idempotent(self):
        store.initialize()
        first = store.get_workout(1)
        with store.connect() as c:
            counts = [c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('workout_versions', 'workout_efforts', 'recording_segments')]
        store.initialize()
        assert store.get_workout(1) == first
        with store.connect() as c:
            assert counts == [c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('workout_versions', 'workout_efforts', 'recording_segments')]
