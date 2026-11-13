# Documentation

[Project overview and quick start](../README.md)

Commands assume the checkout root unless a guide says otherwise.

## Using Afterset

| Guide | Contents |
| --- | --- |
| [Configuration and Garmin login](configuration.md) | Database paths, personal context, persistent settings, timezone, keyring, headless login and the Personal API backend |
| [Syncing and analysis](usage.md) | CLI backfills, partial failures, optional analysis and example requests |
| [MCP tools and data semantics](mcp.md) | Tool reference, compact summaries, audit options, notes and missing data |
| [Structured workouts](workouts.md) | Workout schema, AMRAP/EMOM formats and correction review |

## Client setup

| Guide | Contents |
| --- | --- |
| [Claude](clients/claude.md) | Claude Desktop, Claude Code and hosted claude.ai OAuth |
| [ChatGPT and Codex](clients/chatgpt.md) | ChatGPT hosted connection and Codex local plugin installation |

## Deployment

| Guide | Contents |
| --- | --- |
| [OAuth and HTTP setup](deployment/oauth.md) | Authentication settings, proxy routes, consent and token lifecycle |
| [Docker](deployment/docker.md) | Prebuilt image usage, persistent storage and local launch |
| [Railway](deployment/railway.md) | Image, storage, variables and login values for the working setup |
| [Phone setup walkthrough](deployment/mobile-setup-guide.md) | Railway deployment, Garmin login and ChatGPT connection, with screenshots |
| [Railway variable example](deployment/railway.env.example) | Defaults for Railway’s variable editor |

## Contributing and maintenance

[Tests, CI and container verification](development.md)

[Release publishing](maintainers/publishing.md)
