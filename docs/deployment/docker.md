# Docker deployment

[Documentation](../README.md) · [OAuth setup](oauth.md) · [Railway](railway.md)

Afterset is distributed as a prebuilt image. Users deploy the image; only the
maintainer needs the source checkout and registry publishing permissions.

Choose an image reference from [GitHub Releases](https://github.com/oglu/afterset/releases).
Each release records the tested image and registry digest. Use that exact reference
to keep deployments reproducible.

## Run a published image

Run the published release:

```sh
export AFTERSET_IMAGE="PASTE_IMAGE_REFERENCE_FROM_RELEASE"
docker pull "$AFTERSET_IMAGE"
docker volume create afterset-data
# Set AFTERSET_OAUTH_PASSPHRASE privately using the OAuth guide first.
docker run -d --name afterset --restart unless-stopped \
  -p 127.0.0.1:8765:8080 \
  --env AFTERSET_OAUTH_PASSPHRASE \
  --env AFTERSET_PUBLIC_URL=http://localhost:8765/mcp \
  --env AFTERSET_TIMEZONE=Europe/London \
  --volume afterset-data:/data "$AFTERSET_IMAGE"
```

The loopback address serves local clients. For phone access, use the
[Railway setup guide](mobile-setup-guide.md). For an HTTPS proxy, set
`AFTERSET_PUBLIC_URL` to the externally reachable URL including `/mcp` and forward
the whole application, including OAuth routes; see [OAuth setup](oauth.md).

The named volume preserves the training database, the Garmin cache and the
optional personal context database across container replacements. Personal
context defaults to `/data/personal-context.db`. Back up both databases from the
volume. The image runs as an unprivileged user by default. Do not mount a
live local database into another concurrent server. The host must remain running.

[Railway setup](railway.md) uses this same image via **New → Docker Image**, with
no GitHub repository connection. Its volume permissions require a Railway-specific
UID override documented there.

## Update explicitly

We recommend choosing a specific release and updating deliberately. The optional
`latest` tag follows the highest stable version successfully promoted by the
release workflow. It is a moving reference, not automatic container updating.
Release notes include the image references; use the pinned digest for an exact
deployment. Prereleases do not update `latest`.

Before updating, read the release notes and follow the
[backup and upgrade guidance](../configuration.md#backups-and-upgrades).
Stop the existing container and back up its persistent data. Pull your chosen
image, remove the stopped container, and recreate it with the same volume,
environment variables and port settings. Keep the volume; do not delete it.
Confirm the replacement is healthy before reconnecting clients.

Even when using `latest`, explicitly pull the image and recreate the container.
Restarting an existing container does not replace its image. Avoid unattended
updates for a persistent installation: database changes may require a compatible
backup to roll back.

## Log in to Garmin

Once the container is healthy, open an interactive login:

```sh
docker exec -it afterset afterset login
```

Enter your Garmin email and password at the hidden prompts, then complete MFA
if requested. Wait for “Garmin session saved.” Only the session is saved, under
`/data/garmin_tokens/garmin_tokens.json` on the named volume. Your password is not
saved, and login does not import training records. Connect your client and
request a sync afterward. The session survives container replacements using the
same volume; run the command again if Garmin requires reauthentication.

For Railway's web console, use `/opt/venv/bin/afterset login` instead. See the
[login walkthrough](mobile-setup-guide.md#6-log-in-to-garmin).

## Retrieve the bundled source

Retrieve the source without starting the server:

```sh
docker create --name afterset-source "$AFTERSET_IMAGE"
docker cp afterset-source:/usr/share/doc/afterset/source.tar.gz ./afterset-source.tar.gz
docker cp afterset-source:/usr/share/doc/afterset/LICENSE ./afterset-LICENSE
docker rm afterset-source
```

Keep image publishing credentials separate from each user's owner passphrase and
Garmin session. No registry credential belongs in the image or the guide.
