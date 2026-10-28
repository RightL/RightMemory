# RightMemory MCP

For native assistant execution, use `rightmemory assistant --root /absolute/memory-root mcp --transport stdio` with the shared `rightmemory-assistant` skill. This transport exposes the model-independent assistant core and returns native-agent handoffs and batch notices. See [Assistant execution](ASSISTANT_EXECUTION.md) for setup, filesystem access, and verification boundaries.

The `rightmemory mcp` server described below keeps the existing internal role backend and its ordinary-agent contract.

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

`rightmemory_retrieve` retrieves context that could materially affect the work, including relevant pending evidence and complete skills. `rightmemory_submit` records useful evidence once for Memory, guidance, and skills. The submitting agent does not need to choose its final lesson or destination.

Shared control tools are `rightmemory_begin_curation`, `rightmemory_complete_curation`, `rightmemory_curation_status`, and `rightmemory_withdraw_candidate`. Internal MCP also exposes `rightmemory_run_curation` for the configured executor. Native assistant MCP uses the same model-free controls and lets the current assistant delegate native agents.

Tool and parameter descriptions carry the ordinary-agent contract. An internal MCP client need not load the command-backed daily skill as well; native assistant MCP keeps the shared `rightmemory-assistant` skill for delegation.

Capture favors useful evidence before demanding a settled general rule. Preserve scope and uncertainty, and capture distinct later occurrences when they add evidence. Pending candidates participate in relevant retrieval. When all three domains finish, the program removes their raw sources; useful cases or experiences remain where the curators retained them.

Configured submission wakes the curation worker. Native responses include readiness and active-task notices for the main assistant. A submission receipt confirms capture, and a completion receipt confirms publication. After interruption, inspect the same task and retry its completion. A notice with `effects_pending` requires completion recovery without another model turn.

## Scope

The adapters use the same candidate store, domain cores, and curation lifecycle as the CLI. The CLI remains available for inspection, maintenance, task recovery, withdrawal, worker controls, and clients without MCP.

Ordinary curation leaves the user's Pursuit map unchanged. Explicit map requests use the human editor or `maintain-pursuit-map`; no additional MCP map-write tools are provided.

This command currently serves stdio only. RightMemory does not expose a Streamable HTTP MCP endpoint.
