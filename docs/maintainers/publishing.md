# Publishing a release

[Documentation](../README.md) · [Development checks](../development.md)

Users install the image linked from GitHub Releases. Keep release tags and image
digests in release notes, not in setup guides.

## Prepare

1. Update the release metadata in `pyproject.toml`, the Dockerfile image label,
   and both plugin manifests. Refresh `uv.lock` with `uv lock`.
2. Add the matching release heading and changes to `CHANGELOG.md`. Run the
   [development checks](../development.md) and review new screenshots manually.
3. Commit and push the reviewed source. Enable GitHub private vulnerability
   reporting so the route in `SECURITY.md` is available.

## Publish from GitHub

Open the repository's **Releases → Draft a new release**:

1. Choose or create a tag consisting of `v` followed by the version in
   `pyproject.toml`, targeting the reviewed commit.
2. Enter the title and release notes (or use GitHub's generated notes).
3. Leave **Set as a pre-release** unchecked and choose **Publish release**.

Publishing a stable GitHub Release starts the
[release workflow](../../.github/workflows/release.yml). Saving a draft or pushing
a tag alone does not publish artifacts. Do not move an existing release tag to a
different commit.

The release page appears immediately; its downloadable packages and Docker image
reference are added after the workflow succeeds. Wait for that completion before
announcing the release.

## Automated checks and publication

The workflow reruns the complete OS/Python test matrix, archive checks, secret
scan and container smoke tests. After those pass, it:

1. Builds and checks the Python wheel and source archive.
2. Builds a Linux amd64 image with source-repository and commit labels, then
   tests bearer and OAuth authentication and persistence across restarts.
3. Pushes that tested image to GHCR using the repository's `GITHUB_TOKEN`.
4. Verifies anonymous access, checks the pulled image matches the tested image,
   and repeats the smoke tests using its digest reference.
5. Attaches the packages and checksums to your published release and adds a
   Downloads section with the image reference and digest, preserving your notes.

No personal access token or PyPI credential is required. Python packages are
attached to GitHub Releases; the workflow does not upload them to PyPI or deploy
an Afterset server. Branch pushes and pull requests only run checks.

## First publication and retries

The repository's Actions policy must permit `contents: write` and
`packages: write` for the publishing job. For an existing GHCR package, grant
this repository Actions access in the package settings if needed. See
[GitHub's container publishing guide](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

GHCR may create the package as private. If the anonymous pull fails, make the
package public in GitHub package settings, then rerun the failed workflow.
The workflow can reuse an image labeled with the same source commit; it refuses
to replace one from a different commit. Registry authentication, network or
unexpected lookup errors stop publication; only an explicit missing manifest
permits building a new image. Rerunning the workflow replaces its generated package/checksum assets and updates
its Downloads section without duplicating it. Other assets and your release notes
are retained. Keep release assets editable until the workflow has completed.

Confirm the public release page and anonymous image pull work before announcing
it. The image contains its license and application source/build archive under
`/usr/share/doc/afterset/`.
