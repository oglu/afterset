"""Read synthetic evidence outside the agent's session; never access owner records."""
from pathlib import Path
import json
import sqlite3


def snapshot(root: Path) -> dict:
    root = root.resolve()
    if not (root / 'fixture.json').is_file():
        raise ValueError('Expected a prepared synthetic fixture')
    with sqlite3.connect(f'file:{root / "training.db"}?mode=ro', uri=True) as conn:
        counts = {table: conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ('session_notes', 'set_proposals', 'set_revisions', 'sync_jobs')}
        notes = [{'note_id': row[0], 'activity_id': row[1], 'text': row[2]} for row in
                 conn.execute('SELECT note_id,activity_id,note FROM session_notes')]
        proposals = [{'proposal_id': row[0], 'activity_id': row[1], 'applied_revision': row[2],
                      'workout': json.loads(row[3]) if row[3] else None}
                     for row in conn.execute('SELECT proposal_id,activity_id,applied_revision,workout_json FROM set_proposals')]
        heads = {row[0]: row[1] for row in conn.execute('SELECT activity_id,revision_id FROM set_heads')}
        versions = [{'activity_id': row[0], 'revision': row[1], 'workout': json.loads(row[2])}
                    for row in conn.execute('SELECT activity_id,revision_id,document_json FROM workout_versions')]
    return {'counts': counts, 'notes': notes, 'proposals': proposals, 'heads': heads, 'versions': versions, 'personal_context_exists': (root / 'personal-context.db').exists()}
