# Connect to ChatGPT and Codex

[Documentation](../README.md) · [Configuration](../configuration.md) · [OAuth setup](../deployment/oauth.md)

For phone access, follow the [Railway setup guide](../deployment/mobile-setup-guide.md),
including Garmin login, before connecting ChatGPT.

## Hosted OAuth (web/mobile)

Connect once on ChatGPT web or desktop:

1. Open **Plugins → + → Add custom MCP server**. Name it **Afterset**, paste your
   HTTPS `/mcp` URL, and leave authentication as **OAuth**.
2. Review the warning, select **I understand and want to continue**, then choose
   **Create as a plugin → Connect Afterset**.
3. Enter your Afterset owner passphrase on the consent page and choose
   **Allow access**. Finish installation if prompted.

Leave advanced OAuth settings at their defaults. Registration is discovered
automatically; no manual client ID, client secret or API key is needed.
Start a new chat, select Afterset with `@`, and ask “Check my Afterset data
coverage.” On your phone, use the same ChatGPT account/workspace and verify
an actual tool call. Account and workspace policies apply.

See the [screenshot walkthrough](../deployment/mobile-setup-guide.md#7-connect-chatgpt)
and [OpenAI’s connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt).
## Codex desktop: local plugin

This repository exposes `Afterset Local` through `.agents/plugins/marketplace.json`,
the Codex marketplace location. From the checkout root, install the
Python commands on your PATH, then register and install the plugin:

```sh
uv tool install --editable .
codex plugin marketplace add .
codex plugin add afterset@afterset-local
```

The MCP configuration launches the bare `afterset-mcp` executable. Ensure the
`uv` tool bin directory (`uv tool dir --bin`) is on the PATH seen by your desktop
host. Installing the plugin alone does not install its Python runtime. An editable
install follows the checkout; reinstall it if you move the checkout. Restart the
desktop app after changing PATH or environment settings.

Open a new local Work chat and select Afterset with `@`.
Try: “Use Afterset to check my data coverage and list my last five workouts.”
The desktop host launches the stdio process on demand. Model requests include
the tool results.
Verify the connection by checking the returned coverage before requesting a sync
or saving workout corrections.
