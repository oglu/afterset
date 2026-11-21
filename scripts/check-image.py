"""Classify a Docker manifest lookup; registry errors must never authorize a push."""
import argparse
import json
import re
import subprocess


def image_state(image: str) -> str:
    result = subprocess.run(
        ['docker', 'manifest', 'inspect', image],
        capture_output=True, text=True, encoding='utf-8', timeout=60,
    )
    if result.returncode == 0:
        return 'exists'
    # Docker's explicit missing-manifest responses. Do not match fragments:
    # authentication failures, TLS errors and timeouts must stop publication.
    missing = {f'no such manifest: {image}', 'manifest unknown'}
    if result.stderr.strip() in missing:
        return 'missing'
    raise RuntimeError('Registry lookup failed; refusing to build or replace the release image')


def stable_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('Expected a stable major.minor.patch version')
    return tuple(map(int, value.split('.')))


def _pull_metadata(image: str) -> dict:
    subprocess.run(['docker', 'pull', image], check=True, timeout=180)
    result = subprocess.run(['docker', 'image', 'inspect', image], check=True,
                            capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)[0]


def _registry_digest(image: str) -> str:
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', image,
                             '--format', '{{json .Manifest}}'], check=True,
                            capture_output=True, text=True, timeout=60)
    digest = json.loads(result.stdout)['digest']
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Registry returned an invalid digest')
    return digest


def promote_latest(digest: str, version: str) -> None:
    """Advance latest under the workflow's shared publication lock."""
    candidate_version = stable_version(version)
    if not re.fullmatch(r'ghcr\.io/[^@\s:]+/[^@\s:]+@sha256:[0-9a-f]{64}', digest):
        raise ValueError('Promotion requires a pinned GHCR image digest')
    latest = digest.split('@')[0] + ':latest'
    candidate = _pull_metadata(digest)
    if stable_version(candidate['Config']['Labels']['org.opencontainers.image.version']) != candidate_version:
        raise ValueError('Candidate image version does not match the release')
    if image_state(latest) == 'exists':
        current_digest = _registry_digest(latest)
        current = _pull_metadata(digest.split('@')[0] + '@' + current_digest)
        current_version = stable_version(current['Config']['Labels']['org.opencontainers.image.version'])
        if current_version > candidate_version:
            print('Keeping latest: a newer stable release is already published.')
            return
        if current_version == candidate_version:
            if current_digest != digest.split('@')[1]:
                raise ValueError('Refusing to replace the same version with a different image')
            print('Latest already points to this release.')
            return
    subprocess.run(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                    '--tag', latest, digest], check=True, timeout=180)
    if _registry_digest(latest) != digest.split('@')[1]:
        raise ValueError('Promoted latest digest differs from the tested image')
    print('Latest advanced to the tested stable release.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--promote-latest', metavar='VERSION')
    args = parser.parse_args()
    try:
        if args.promote_latest is not None:
            promote_latest(args.image, args.promote_latest)
        else:
            print(image_state(args.image))
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError, OSError) as error:
        raise SystemExit('Cannot confirm image state; check registry access and retry. '
                         f'Failure: {type(error).__name__}') from None


if __name__ == '__main__':
    main()
