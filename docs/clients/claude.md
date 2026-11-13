# Connect to Claude

[Documentation](../README.md) · [Configuration](../configuration.md) · [OAuth setup](../deployment/oauth.md)

## Claude Desktop: local stdio

Install the runtime (`uv tool install --editable .` from the checkout) and complete
Garmin authentication first. Merge an Afterset entry into
`claude_desktop_config.json` under **Settings → Developer → Edit Config**:

```json
{
  "mcpServers": {
    "afterset": {
      "command": "/absolute/path/to/afterset-mcp",
      "args": []
    }
  }
}
```

Replace the command with the output of `command -v afterset-mcp` on macOS/Linux. This avoids depending on the desktop application's PATH. The per-user
Afterset [storage configuration](../configuration.md#storage-and-timezone) selects your database without putting a local path in
shared plugin files. Fully restart Claude Desktop, enable Afterset, and ask it to
call `data_coverage`. See the [local MCP setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Claude Code: local plugin

The repository root holds a Claude Code marketplace in
`.claude-plugin/marketplace.json`. It lists one plugin, `plugin/`, with a training
skill and one MCP server. From the checkout root, add the marketplace and install
the plugin in Claude Code:

```text
/plugin marketplace add ./
/plugin install afterset@afterset
```

To try the plugin without installing it, load the folder for one session:

```sh
claude --plugin-dir ./plugin
```

The Codex marketplace lives separately in `.agents/plugins/marketplace.json`.
The plugin still requires `afterset-mcp` on PATH and a configured runtime.
See the
[Claude plugin reference](https://code.claude.com/docs/en/plugins-reference).

## claude.ai: remote connector

Configure an HTTPS hosted endpoint using the [OAuth setup guide](../deployment/oauth.md). In
**Customize → Connectors → Add custom connector**, enter your `/mcp` URL. Select
OAuth sign-in and **Register automatically** (DCR) for the OAuth client, rather
than Claude's published identity/CIMD option, which Afterset does not implement.
Connect, enter the owner passphrase on your own Afterset consent page, verify the
client/return address, and allow access. Enable the connector for the conversation,
then ask for `data_coverage` and a short `training_summary`.

No custom request headers are needed. Follow the
[custom connector instructions](https://claude.com/docs/connectors/custom/add-unlisted).
Verify the deployed connection by requesting data coverage and a sync from the
client you intend to use.
