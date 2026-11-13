"""Classify a Docker manifest lookup; registry errors must never authorize a push."""
import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    args = parser.parse_args()
    try:
        print(image_state(args.image))
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        raise SystemExit('Cannot confirm image state; check registry access and retry. '
                         f'Failure: {type(error).__name__}') from None


if __name__ == '__main__':
    main()
