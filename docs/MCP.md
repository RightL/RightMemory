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

The ordinary tools separate context from shared learning:

- `rightmemory_retrieve` retrieves context that could materially affect the work, including relevant raw learning evidence and complete skills.
- `rightmemory_submit_update` submits ordinary Memory evidence, including user and people context, to the asynchronous Update queue.
- `rightmemory_submit_learning` durably captures behavioral or procedural evidence for the guidance and skills curators, including explicit requests to remember it.

Learning control tools are `rightmemory_begin_learning`, `rightmemory_complete_learning`, and `rightmemory_learning_status`. Internal MCP also exposes `rightmemory_run_learning` to run a prepared curator through the configured executor. Native assistant MCP shares the model-free controls and lets the current assistant delegate its own native agent.

The tool and parameter descriptions carry the ordinary-agent contract. An internal MCP client should not also load the command-backed orchestrator skill; native assistant MCP keeps the shared `rightmemory-assistant` skill for delegation.

Capture favors useful evidence before demanding a settled general rule. Preserve uncertainty and scope, and capture independent later occurrences when they add evidence. Raw learning remains available to relevant retrieval before curation. A captured candidate is not an adopted rule or skill.

Learning responses include readiness or active-task notices. The daily entry acts on ready domains with the configured executor or native begin/delegate/complete workflow. Domain curators make their own retention and generalization judgments without a per-item manual inbox review. Only the completion receipt confirms publication.

## Scope

The MCP adapter calls the same runtime, async Memory Update store, and shared learning implementation as the CLI. The CLI remains available for human inspection, explicit maintenance, queue status, retry, undo, and clients that cannot use MCP.

Ordinary MCP work does not create, rename, reorganize, focus, or remove Pursuits. Explicit map requests use the human editor or the independent `maintain-pursuit-map` skill; no additional MCP map-write tools are provided.

This command currently serves stdio only. RightMemory does not expose a Streamable HTTP MCP endpoint.
