from unittest.mock import patch

from afterset import connection, db, store, workouts


def test_explicit_conversion_is_read_only_and_compact_conditions_survive(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'training.db'):
        store.initialize()
        db.upsert_activities([{'activity_id': 81, 'date': '2026-03-02'}])
        note = store.save_note(81, 'Synthetic seated press without back support.')
        document = workouts.Workout(source_activity_ids=[81], blocks=[workouts.Block(
            label='Press', evidence=workouts.Evidence(source='user', text='Synthetic report'),
            efforts=[workouts.Effort(exercise='Press', reps=6, evidence='Synthetic report',
                details={'back_support': False, 'equipment': 'dumbbells',
                         'raw_payload': {'large': [1, 2, 3]}})])])
        proposed = store.propose_workout(81, note['note_id'], document, 'Synthetic correction', 0)
        store.apply_proposal(proposed['proposal_id'])
        original = store.get_workout(81)
        converted = store.get_workout(81, target_schema_version=3)
        assert converted['converted_for_review']
        assert converted['original_schema_version'] == 2
        assert converted['workout']['schema_version'] == 3
        assert converted['workout'] == store.get_workout(81, target_schema_version=3)['workout']
        assert store.get_workout(81) == original
        compact = store.get_activity(81)['workout']['blocks'][0]['efforts'][0]
        assert compact['comparison_conditions'] == {'back_support': False, 'equipment': 'dumbbells'}
        assert 'details' not in compact
        assert 'raw_payload' not in str(compact)
        with connection.connect() as conn:
            assert conn.execute('SELECT count(*) FROM set_revisions').fetchone()[0] == 1
