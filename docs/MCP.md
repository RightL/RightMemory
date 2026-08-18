# RightMemory MCP

RightMemory exposes a local MCP stdio server for ordinary agent work using the official MCP Python SDK v2:

```bash
rightmemory mcp
```

The server resolves its Memory root once at startup through the same rules as the CLI:

1. an explicit `--profile`;
2. the nearest project `.rightmemory-profile`;
3. `RIGHTMEMORY_ROOT`;
4. the default RightMemory root.

Use an explicit profile when the MCP host does not launch the server from the project directory:

```bash
rightmemory --profile my-project mcp
```

A typical MCP host entry is:

```json
{
  "mcpServers": {
    "rightmemory": {
      "command": "rightmemory",
      "args": ["mcp"]
    }
  }
}
```

For a named profile, use `"args": ["--profile", "my-project", "mcp"]`.

## Ordinary-agent tools

The server exposes exactly three tools:

- `rightmemory_retrieve` retrieves cross-session context when it could materially affect the current work.
- `rightmemory_submit_update` submits durable Memory or Pursuit evidence to the asynchronous unified Update queue.
- `rightmemory_capture_guidance` captures plausible reusable agent-behavior evidence, including explicit and implicit user redirections.

The tool and parameter descriptions contain the complete automatic ordinary-agent contract. An MCP client should not also load the RightMemory orchestrator skill; that skill remains the CLI transport for clients without MCP support.

Successful writes return no model-visible content. A write result contains text only when the agent must act, such as when evidence was saved but the Update worker could not start, or when queued work requires manual recovery. Update submission never reports synchronous semantic acceptance: the updater reconciles submitted evidence later and may change any relevant module or none.

Guidance capture favors recall. A signal need not already be a fully settled general rule, and independent later occurrences of a similar pattern may be captured again. Capture does not replace applying the user's direction to the current work.

## Scope

The MCP adapter calls the same runtime, async Update store, and guidance capture implementation as the CLI. The CLI remains available for human inspection, explicit maintenance, queue status, retry, undo, and clients that cannot use MCP.

This command currently serves stdio only. RightMemory does not expose a Streamable HTTP MCP endpoint.
