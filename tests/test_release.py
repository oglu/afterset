"""Release guards must reject unsafe contents, even when an archive builds successfully."""
import io
from pathlib import Path
import runpy
import tarfile
import tempfile
import zipfile

import pytest

check = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/check-release.py'))


class TestRelease:
    def test_renamed_database_is_rejected(self):
        with pytest.raises(ValueError, match='SQLite payload'):
            check['check_path']('docs/example.bin', b'SQLite format 3\x00')

    @pytest.mark.parametrize('name', [
        'output/analysis/paired_metrics.csv', '.env.production',
        'garmin_tokens/garmin_tokens.json', '.claude/settings.local.json',
        'docs/records.db-wal', '../outside.py',
    ])
    def test_private_paths_are_rejected(self, name):
        with pytest.raises(ValueError):
            check['check_path'](name)

    @pytest.mark.parametrize('name', ['.env.example', 'docs/deployment/railway.env.example'])
    def test_examples_are_allowed(self, name):
        check['check_path'](name)

    def test_built_archives_reject_private_members(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / 'afterset-1.0.0.tar.gz'
            wheel = directory / 'afterset-1.0.0-py3-none-any.whl'
            required = ['.gitignore', '.python-version', 'README.md', 'LICENSE',
                        'pyproject.toml', 'uv.lock', 'afterset/server.py']
            for extra in (None, 'docs/export.db'):
                with tarfile.open(source, 'w:gz') as archive:
                    for name in required + ([extra] if extra else []):
                        info = tarfile.TarInfo('afterset-1.0.0/' + name)
                        info.size = 1
                        archive.addfile(info, io.BytesIO(b'x'))
                with zipfile.ZipFile(wheel, 'w') as archive:
                    archive.writestr('afterset/server.py', '')
                    archive.writestr('afterset-1.0.0.dist-info/licenses/LICENSE', '')
                if extra:
                    with pytest.raises(ValueError, match='Database file'):
                        check['check_archives'](directory, '1.0.0')
                else:
                    check['check_archives'](directory, '1.0.0')
                    with zipfile.ZipFile(wheel, 'a') as archive:
                        archive.writestr('afterset/garmin_tokens.json', '{}')
                    with pytest.raises(ValueError, match='Credential file'):
                        check['check_archives'](directory, '1.0.0')
