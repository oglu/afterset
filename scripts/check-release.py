"""Check publishable files, documentation and built release archives without extracting them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'output', 'backups', 'tmp', '.venv', '.git', '.tunnel',
                'garmin_tokens', '.garmin_tokens', '__pycache__'}
ROOT_FILES = {'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE', 'SECURITY.md',
              'CHANGELOG.md', 'Dockerfile', 'railway.json', '.dockerignore',
              '.gitignore', '.python-version', '.env.example', 'PKG-INFO'}
SOURCE_DIRS = {'afterset', 'plugin', 'examples', 'scripts', 'tests', 'docs',
               '.github', '.agents', '.claude-plugin'}


def check_path(name: str, data: bytes = b'') -> None:
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or '..' in parts or '\\' in name:
        raise ValueError(f'Unsafe path: {name}')
    if set(parts) & PRIVATE_DIRS or name == '.claude/settings.local.json':
        raise ValueError(f'Private/generated path: {name}')
    if ((path.name.startswith('.env') or '.env.' in path.name)
            and not path.name.endswith('.example')):
        raise ValueError(f'Environment secrets file: {name}')
    if re.search(r'\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal)|\.sync\.lock)?$', name):
        raise ValueError(f'Database file: {name}')
    if path.suffix in {'.ipynb', '.pyc', '.pyo', '.pem', '.key'} or path.name == '.DS_Store':
        raise ValueError(f'Private/generated file: {name}')
    if path.name in {'garmin_tokens.json', 'api-key', 'bearer-token.json'}:
        raise ValueError(f'Credential file: {name}')
    if data.startswith(b'SQLite format 3\x00'):
        raise ValueError(f'SQLite payload: {name}')


def check_archives(directory: Path, version: str) -> None:
    sdist = directory / f'afterset-{version}.tar.gz'
    wheel = directory / f'afterset-{version}-py3-none-any.whl'
    with tarfile.open(sdist) as archive:
        names = set()
        for member in archive.getmembers():
            prefix = f'afterset-{version}/'
            if not member.name.startswith(prefix) or not member.isfile():
                raise ValueError(f'Unexpected source archive member: {member.name}')
            name = member.name[len(prefix):]
            check_path(name, archive.extractfile(member).read(16))
            if name not in ROOT_FILES and PurePosixPath(name).parts[0] not in SOURCE_DIRS:
                raise ValueError(f'Unexpected source file: {name}')
            if name in names:
                raise ValueError(f'Duplicate source member: {name}')
            names.add(name)
        required = {'.gitignore', '.python-version', 'README.md', 'LICENSE',
                    'pyproject.toml', 'uv.lock', 'afterset/server.py'}
        if required - names:
            raise ValueError(f'Missing source files: {sorted(required - names)}')
    with zipfile.ZipFile(wheel) as archive:
        names = set()
        metadata = f'afterset-{version}.dist-info'
        for member in archive.infolist():
            name = member.filename
            with archive.open(member) as stream:
                check_path(name, stream.read(16))
            if PurePosixPath(name).parts[0] not in {'afterset', metadata}:
                raise ValueError(f'Unexpected wheel file: {name}')
            if name in names:
                raise ValueError(f'Duplicate wheel member: {name}')
            names.add(name)
        if f'{metadata}/licenses/LICENSE' not in names or 'afterset/server.py' not in names:
            raise ValueError('Wheel is missing application or license')
    print('Wheel and source archive contents verified.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, required=True)
    args = parser.parse_args()
    candidates = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT,
    ).decode().split('\0')
    for name in filter(None, candidates):
        check_path(name)
        path = ROOT / name
        if path.is_symlink():
            raise ValueError(f'Symlink in release source: {name}')
        with path.open('rb') as stream:
            check_path(name, stream.read(16))
        if path.suffix == '.md':
            content = path.read_text(encoding="utf-8")
            for target in re.findall(r'\]\(([^)]+)\)', content):
                target = target.split('#')[0]
                if target and not re.match(r'[a-z]+:', target) and not (path.parent / target).exists():
                    raise ValueError(f'Broken documentation link in {name}: {target}')
            if name.startswith('docs/') or name == 'README.md':
                if re.search(r'afterset:\d+\.\d+|sha256:PENDING', content):
                    raise ValueError(f'Stale release reference in {name}')
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding="utf-8"))['project']
    version = project['version']
    for name in ('plugin/.claude-plugin/plugin.json', 'plugin/.codex-plugin/plugin.json'):
        if json.loads((ROOT / name).read_text(encoding="utf-8"))['version'] != version:
            raise ValueError(f'Release metadata mismatch: {name}')
    if f'org.opencontainers.image.version="{version}"' not in (ROOT / 'Dockerfile').read_text(encoding="utf-8"):
        raise ValueError('Docker image version differs from package metadata')
    lock = tomllib.loads((ROOT / 'uv.lock').read_text(encoding="utf-8"))
    if not any(p['name'] == 'afterset' and p['version'] == version for p in lock['package']):
        raise ValueError('Lockfile release metadata differs from package metadata')
    check_archives(args.dist, version)
    print('Repository paths, documentation links and release metadata verified.')


if __name__ == '__main__':
    main()
