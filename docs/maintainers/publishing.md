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
6. Promotes that tested image to the optional `latest` tag if its version is newer
   than the image currently carrying that tag. The versioned image stays available.

Publication jobs share a concurrency group so checking and updating `latest`
cannot race across releases. Versions are compared numerically, not by publication
date or GitHub's “latest release” designation. Publishing or retrying an older
release cannot move the tag backwards. A retry of the same image is a no-op;
a different image with the same version, an unreadable version label or a registry
error stops promotion. Only an explicitly missing manifest allows initial creation.
Prereleases never reach publication. Do not move `latest` manually outside this
workflow. Publication jobs queue instead of replacing an already pending release.
Promotion copies the tested registry manifest by digest and verifies the resulting
tag, preserving the exact image rather than rebuilding it.

Recommend specific releases or pinned digests and
[explicit updates](../deployment/docker.md#update-explicitly) to users. A moving
tag does not update a running container, and persistent databases need a backup
before upgrading.

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
