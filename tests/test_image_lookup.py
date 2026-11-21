"""Only an explicit missing manifest permits building a new release image."""
from pathlib import Path
import runpy
import subprocess
from unittest.mock import Mock, patch

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


DIGEST = 'ghcr.io/example/afterset@sha256:' + 'a' * 64


def metadata(version, image_id):
    return {'Id': image_id, 'Config': {'Labels': {'org.opencontainers.image.version': version}}}


@pytest.mark.parametrize('current,expected_push', [
    (None, True),
    (metadata('0.9.9', 'old'), True),
    (metadata('0.10.0', 'candidate'), False),
    (metadata('0.11.0', 'newer'), False),
    (metadata('1.0.0', 'newer'), False),
])
def test_latest_only_advances_numerically(current, expected_push):
    namespace = lookup['promote_latest'].__globals__
    candidate = metadata('0.10.0', 'candidate')
    current_digest = DIGEST.split('@')[1] if current == candidate else 'sha256:' + 'b' * 64
    with patch.dict(namespace, {
        '_pull_metadata': Mock(side_effect=[candidate, current]),
        '_registry_digest': Mock(side_effect=([current_digest] if current else []) + [DIGEST.split('@')[1]]),
        'image_state': lambda image: 'exists' if current else 'missing',
    }), patch('subprocess.run') as run:
        lookup['promote_latest'](DIGEST, '0.10.0')
    commands = [call.args[0] for call in run.call_args_list]
    assert commands == ([['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                         '--tag', 'ghcr.io/example/afterset:latest', DIGEST]] if expected_push else [])


@pytest.mark.parametrize('current', [
    metadata('0.10.0', 'candidate'), metadata('invalid', 'old'),
    metadata('0.11.0rc1', 'old'), {'Id': 'old', 'Config': {'Labels': {}}},
])
def test_ambiguous_latest_is_not_overwritten(current):
    namespace = lookup['promote_latest'].__globals__
    with patch.dict(namespace, {
        '_pull_metadata': lambda image: metadata('0.10.0', 'candidate') if image == DIGEST else current,
        '_registry_digest': lambda image: 'sha256:' + 'b' * 64,
        'image_state': lambda image: 'exists',
    }), patch('subprocess.run') as run:
        with pytest.raises((ValueError, KeyError)):
            lookup['promote_latest'](DIGEST, '0.10.0')
        run.assert_not_called()


def test_latest_registry_failure_never_pushes():
    namespace = lookup['promote_latest'].__globals__
    with patch.dict(namespace, {'_pull_metadata': lambda image: metadata('0.10.0', 'candidate')}), \
            patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'unauthorized')) as run:
        with pytest.raises(RuntimeError):
            lookup['promote_latest'](DIGEST, '0.10.0')
        assert all(call.args[0][:3] == ['docker', 'manifest', 'inspect'] for call in run.call_args_list)


@pytest.mark.parametrize('version', ['0.10.0rc1', 'v0.10.0', '0.10', '01.0.0'])
def test_latest_rejects_nonstable_versions_before_registry_access(version):
    with patch('subprocess.run') as run:
        with pytest.raises(ValueError):
            lookup['promote_latest'](DIGEST, version)
        run.assert_not_called()


def test_latest_rejects_mislabeled_candidate():
    namespace = lookup['promote_latest'].__globals__
    with patch.dict(namespace, {'_pull_metadata': lambda image: metadata('0.9.0', 'wrong')}), \
            patch('subprocess.run') as run:
        with pytest.raises(ValueError, match='does not match'):
            lookup['promote_latest'](DIGEST, '0.10.0')
        run.assert_not_called()


def test_latest_verifies_promoted_registry_digest():
    namespace = lookup['promote_latest'].__globals__
    with patch.dict(namespace, {
        '_pull_metadata': lambda image: metadata('0.10.0', 'candidate'),
        'image_state': lambda image: 'missing',
        '_registry_digest': lambda image: 'sha256:' + 'b' * 64,
    }), patch('subprocess.run'):
        with pytest.raises(ValueError, match='differs from the tested image'):
            lookup['promote_latest'](DIGEST, '0.10.0')


@pytest.mark.parametrize('value', ['sha256:' + 'a' * 64, 'invalid', None])
def test_registry_digest_requires_valid_sha256(value):
    import json
    with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps({'digest': value}), '')):
        if isinstance(value, str) and value.startswith('sha256:'):
            assert lookup['_registry_digest'](IMAGE) == value
        else:
            with pytest.raises(ValueError):
                lookup['_registry_digest'](IMAGE)
