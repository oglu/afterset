"""An offline import must not replace any existing application records."""
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest
from afterset import connection, store
from afterset.oauth import SCHEMA as OAUTH_SCHEMA

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/import-hosted.py'


@pytest.fixture
def import_files(tmp_path, monkeypatch):
    incoming, target = tmp_path / 'incoming.db', tmp_path / 'target.db'
    for path in (incoming, target):
        monkeypatch.setattr(connection, 'DB_PATH', path)
        store.initialize()
    archive = tmp_path / 'tokens.tar'
    with tarfile.open(archive, 'w') as bundle:
        member = tarfile.TarInfo('garmin_tokens/garmin_tokens.json')
        member.size = 2
        bundle.addfile(member, io.BytesIO(b'{}'))
    return incoming, target, archive


def invoke(files, optimized=False):
    incoming, target, archive = files
    return subprocess.run([
        sys.executable, *(['-O'] if optimized else []), str(SCRIPT),
        '--db', str(incoming), '--tokens', str(archive),
        '--db-sha256', hashlib.sha256(incoming.read_bytes()).hexdigest(),
        '--tokens-sha256', hashlib.sha256(archive.read_bytes()).hexdigest(),
    ], env={**os.environ, 'AFTERSET_DB': str(target),
            'AFTERSET_TOKEN_STORE': str(target.parent / 'tokens')},
        capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('optimized', [False, True])
@pytest.mark.parametrize('table', ['heart_rate_readings', 'future_records', 'oauthXrecords'])
def test_import_refuses_nonempty_training_tables_without_changes(import_files, table, optimized):
    incoming, target, _ = import_files
    with sqlite3.connect(target) as conn:
        if table == 'heart_rate_readings':
            conn.execute("INSERT INTO heart_rate_readings VALUES('2026-01-01T12:00:00Z','2026-01-01',70)")
        else:
            conn.execute(f'CREATE TABLE "{table}" (value TEXT)')
            conn.execute(f'INSERT INTO "{table}" VALUES (?)', ('Synthetic record',))
    original_source = incoming.read_bytes()
    result = invoke(import_files, optimized)
    assert result.returncode != 0
    assert 'Hosted training data already exists' in result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1
    assert incoming.read_bytes() == original_source
    assert not target.with_name('before-initial-import.db').exists()
    assert not (target.parent / 'tokens').exists()


def test_import_accepts_empty_training_store_and_preserves_oauth(import_files):
    incoming, target, _ = import_files
    with sqlite3.connect(incoming) as conn:
        conn.execute("INSERT INTO heart_rate_readings VALUES('2026-01-01T12:00:00Z','2026-01-01',70)")
    with sqlite3.connect(target) as conn:
        conn.executescript(OAUTH_SCHEMA)
        conn.execute("INSERT INTO oauth_settings VALUES(1, ?, 'synthetic-tag')", (b'synthetic-salt',))
    result = invoke(import_files)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT bpm FROM heart_rate_readings').fetchone() == (70,)
        assert conn.execute('SELECT salt,key_tag FROM oauth_settings').fetchone() == (b'synthetic-salt', 'synthetic-tag')
    assert (target.parent / 'tokens/garmin_tokens.json').read_bytes() == b'{}'
