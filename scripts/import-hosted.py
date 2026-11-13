"""Import an initial private snapshot into an otherwise empty hosted installation.

Run inside the container after uploading a SQLite backup and token archive.
Stop the server and all sync writers before running this offline import.
Existing OAuth settings are retained. Refuses to overwrite training records.
Optionally copies a separate personal context database with --context.
"""
import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import tarfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--db', required=True)
p.add_argument('--tokens', required=True)
p.add_argument('--db-sha256', required=True)
p.add_argument('--tokens-sha256', required=True)
p.add_argument('--context', help='Optional personal-context.db backup to install separately.')
p.add_argument('--context-sha256', help='Required with --context.')
a = p.parse_args()
assert (a.context is None) == (a.context_sha256 is None), 'Pass --context and --context-sha256 together'

os.umask(0o077)
incoming = Path(a.db)
archive = Path(a.tokens)
target = Path(os.environ['AFTERSET_DB'])
tokens = Path(os.environ['AFTERSET_TOKEN_STORE'])
assert incoming.resolve() != target.resolve(), 'Use a separate staging file'
uploads = [(incoming, a.db_sha256), (archive, a.tokens_sha256)]
if a.context:
    # Mirrors afterset.config.personal_context_path for hosted installs.
    context_target = Path(os.environ.get('AFTERSET_CONTEXT_DB') or target.with_name('personal-context.db'))
    context_incoming = Path(a.context)
    assert context_incoming.resolve() != context_target.resolve(), 'Use a separate staging file'
    assert context_target.resolve() != target.resolve(), 'Personal context must stay separate from training'
    assert not context_target.exists(), 'Hosted personal context already exists'
    uploads.append((context_incoming, a.context_sha256))
for path, expected in uploads:
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Upload hash mismatch'
if a.context:
    with sqlite3.connect(context_incoming.resolve().as_uri() + '?mode=ro', uri=True) as src:
        assert src.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        context_events = src.execute('SELECT count(*) FROM personal_events').fetchone()[0]
with tarfile.open(archive) as bundle:
    members = bundle.getmembers()
    assert members and all(m.isfile() and len(Path(m.name).parts) == 2
                           and Path(m.name).parts[0] == 'garmin_tokens'
                           and '..' not in Path(m.name).parts for m in members), 'Unexpected archive paths'
    assert not tokens.exists(), 'Garmin session already exists'
    token_files = [(Path(m.name).name, bundle.extractfile(m).read()) for m in members]
with sqlite3.connect(incoming) as src, sqlite3.connect(target, timeout=30) as dst:
    assert src.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    # Reject any existing application data, including time series and future
    # tables. Only SQLite internals and the OAuth tables preserved below may
    # contain rows. Check before creating a backup or changing either database.
    training_tables = [row[0] for row in dst.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT GLOB 'oauth_*' AND name NOT GLOB 'sqlite_*'"
    )]
    for table in training_tables:
        identifier = '"' + table.replace('"', '""') + '"'
        if dst.execute(f'SELECT 1 FROM {identifier} LIMIT 1').fetchone() is not None:
            raise SystemExit('Hosted training data already exists; import refused')
    backup = target.with_name('before-initial-import.db')
    assert not backup.exists(), 'Pre-import backup already exists'
    with sqlite3.connect(backup) as old:
        dst.backup(old)
        schema = old.execute("SELECT type,name,sql FROM sqlite_master WHERE name GLOB 'oauth_*' AND sql IS NOT NULL ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END").fetchall()
        for kind, name, sql in schema:
            src.execute(sql)
            if kind == 'table':
                rows = old.execute(f'SELECT * FROM "{name}"').fetchall()
                if rows:
                    src.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in rows[0])})', rows)
        src.commit()
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'oauth_*' AND name NOT GLOB 'sqlite_*'")]
    expected = {t: src.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
    src.backup(dst)
    assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert all(dst.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] == n for t, n in expected.items())
tokens.mkdir(mode=0o700, parents=True)
for name, data in token_files:
    path = tokens / name
    path.write_bytes(data)
    path.chmod(0o600)
target.chmod(0o600)
backup.chmod(0o600)
if a.context:
    with sqlite3.connect(context_incoming.resolve().as_uri() + '?mode=ro', uri=True) as src, \
            sqlite3.connect(context_target) as dst:
        src.backup(dst)
        assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert dst.execute('SELECT count(*) FROM personal_events').fetchone()[0] == context_events
    context_target.chmod(0o600)
print('IMPORT VERIFIED: all training table counts match; SQLite quick_check=ok')
for name in ('activities', 'session_notes', 'set_revisions', 'heart_rate_readings'):
    print(f'{name}: {expected[name]}')
print(f'Garmin session: {len(token_files)} private files installed')
if a.context:
    print(f'Personal context: {context_events} events installed separately')
print('Existing OAuth configuration retained; pre-import backup saved on volume')
