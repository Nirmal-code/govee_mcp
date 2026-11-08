# govee-mcp

An MCP server that lets an AI assistant (Claude Desktop, Claude Code, etc.) control Govee lights through the official Govee Platform API.

## Tools

| Tool | What it does |
|---|---|
| `list_devices` | Lists devices and what each one supports |
| `get_state` | Shows whether a device is online, on or off, and its brightness and color |
| `set_power` | Turns devices on or off |
| `set_brightness` | Sets brightness from 1 to 100% |
| `set_color` | Sets color by name (`"purple"`), hex (`"#FF8800"`), or `"r,g,b"` |
| `set_color_temperature` | Sets white temperature in Kelvin (2700 is warm, 6500 is daylight) |
| `list_scenes` / `set_scene` | Lists or activates built-in and DIY scenes |

Every control tool accepts `device` as a name (case-insensitive, partial matches allowed), a device id, a comma-separated list, or `all`.

## Setup

### Requirements

- Python 3.10 or newer
- A Govee API key from the Govee Home app
- Optional: a public URL and password if you want to expose the server over HTTP for remote clients

### 1. Clone and install

```sh
git clone <your-repo-url>
cd govee-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

### 2. Set your environment variables

Set the API key locally in your shell or in a `.env` file that is not checked into Git.

```sh
export GOVEE_API_KEY="your-govee-api-key"
```

Optional for HTTP mode:

```sh
export GOVEE_MCP_PUBLIC_URL="https://your-domain.example"
export GOVEE_MCP_PASSWORD="change-me-to-something-long"
```

A sample env file is included at [deploy/govee-mcp.env.example](deploy/govee-mcp.env.example).

### 3. Start the server

```sh
govee-mcp
```

This starts the MCP server in stdio mode, which works with local AI clients such as Claude Desktop or Claude Code.

### 4. Connect it to your AI client

#### Claude Code

```sh
claude mcp add govee -e GOVEE_API_KEY=your-govee-api-key -- /absolute/path/to/govee_mcp/.venv/bin/govee-mcp
```

#### Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "govee": {
      "command": "/absolute/path/to/govee_mcp/.venv/bin/govee-mcp",
      "env": { "GOVEE_API_KEY": "your-govee-api-key" }
    }
  }
}
```

Then ask something like: *"Set the bedroom lights to a warm 30%"* or *"Turn everything off."*

## Running over HTTP

Set `GOVEE_MCP_PUBLIC_URL` and the server switches from stdio to HTTP at `/mcp`, with its own sign-in flow. When a client connects, it prompts for `GOVEE_MCP_PASSWORD` before issuing tokens. This is useful if you want to expose the server to remote clients or a proxy in front of it.

For a standard deployment, place the real values in a host-managed environment file or service configuration, not in this repository. A reverse proxy such as Caddy or nginx can be used in front of the server, but the app itself does not require any one specific hosting platform.

Example:

```sh
export GOVEE_API_KEY="your-govee-api-key"
export GOVEE_MCP_PUBLIC_URL="https://govee.example.com"
export GOVEE_MCP_PASSWORD="a-long-random-password"
export GOVEE_MCP_PORT="8000"

govee-mcp
```

If you are behind a reverse proxy, point it at the local port used by the server and terminate HTTPS at the proxy if desired.

## Notes

- Govee limits accounts to about 10,000 API requests per day. The device list is cached for 5 minutes.
- Some older models aren't supported by the cloud API. If a light doesn't show up in `list_devices`, that's why.
- Keep secrets out of GitHub: use local environment variables, a host environment file, or a secret manager.
