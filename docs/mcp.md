# Connect the GenPixy MCP server

The GenPixy MCP server lets an AI agent make whiteboard animation videos with tools.

- URL: `https://www.genpixy.com/api/mcp`
- Transport: Streamable HTTP
- Authentication: the header `Authorization: Bearer <your API key>`. Create a key at [genpixy.com → Account → API keys](https://www.genpixy.com/account/api-keys).

| Tool | What it does |
| --- | --- |
| `create_video` | Starts a video. Takes `prompt`, `seconds`, `orientation`, `resolution`, `style`, `language`, `voice`, `subtitles`, `music`, `big_board`, `zoom_dive`, and an optional `idempotency_key`. |
| `get_video` | Returns one video. Call it every 10 seconds until `status` is `completed`, `failed`, or `canceled`. |
| `list_videos` | Lists your videos, newest first. |
| `get_account` | Returns your balance and the videos in progress. |
| `get_options` | Returns every option, its default, and the cost of each length and resolution. |

A completed video has `result.files` (download links) and `app_url` (the video page on GenPixy).

## Claude Code

```bash
claude mcp add --transport http genpixy https://www.genpixy.com/api/mcp \
  --header "Authorization: Bearer $GENPIXY_API_KEY"
```

## Codex

```bash
codex mcp add genpixy --url https://www.genpixy.com/api/mcp --bearer-token-env-var GENPIXY_API_KEY
```

## Cursor and other clients

Add this to `~/.cursor/mcp.json` in Cursor. Most other clients take the same entry; check their docs for the file name. Put your key in place of `gpx_…`:

```json
{
  "mcpServers": {
    "genpixy": {
      "url": "https://www.genpixy.com/api/mcp",
      "headers": { "Authorization": "Bearer gpx_…" }
    }
  }
}
```

## VS Code

Add this to `.vscode/mcp.json`. VS Code asks for the key once and keeps it out of the file:

```json
{
  "inputs": [
    { "type": "promptString", "id": "genpixy-key", "description": "GenPixy API key", "password": true }
  ],
  "servers": {
    "genpixy": {
      "type": "http",
      "url": "https://www.genpixy.com/api/mcp",
      "headers": { "Authorization": "Bearer ${input:genpixy-key}" }
    }
  }
}
```

## Claude Desktop

Claude Desktop connects to remote servers with a header through [`mcp-remote`](https://github.com/geelen/mcp-remote) (needs Node.js). Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "genpixy": {
      "command": "npx",
      "args": ["mcp-remote", "https://www.genpixy.com/api/mcp", "--header", "Authorization:${GENPIXY_AUTH}"],
      "env": { "GENPIXY_AUTH": "Bearer gpx_…" }
    }
  }
}
```

There is no space after `Authorization:` because some clients break arguments that contain spaces. The space inside the environment variable is fine.

## Try it

Ask your agent:

```text
Make a 60-second whiteboard video about how black holes form, then give me the download link.
```
