"""Only an explicit missing manifest permits building a new release image."""
from pathlib import Path
import runpy
import subprocess
from unittest.mock import patch

import pytest

lookup = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/check-image.py'))
IMAGE = 'ghcr.io/example/afterset:release'


@pytest.mark.parametrize('status,stderr,expected', [
    (0, '', 'exists'),
    (1, f'no such manifest: {IMAGE}\n', 'missing'),
    (1, 'manifest unknown\n', 'missing'),
])
def test_explicit_registry_results(status, stderr, expected):
    with patch('subprocess.run', return_value=subprocess.CompletedProcess([], status, '', stderr)):
        assert lookup['image_state'](IMAGE) == expected


@pytest.mark.parametrize('stderr', [
    'unauthorized: authentication required', 'denied: permission denied',
    'dial tcp: i/o timeout', 'TLS handshake timeout', '500 Internal Server Error',
    'manifest unknown\nadditional unexpected failure', '',
    'no such manifest: ghcr.io/someone/else:release',
])
def test_registry_errors_do_not_authorize_publication(stderr):
    with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', stderr)):
        with pytest.raises(RuntimeError, match='refusing'):
            lookup['image_state'](IMAGE)


def test_lookup_timeout_stops_publication():
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 60)):
        with pytest.raises(subprocess.TimeoutExpired):
            lookup['image_state'](IMAGE)
