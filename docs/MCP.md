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

These are the two tools exposed by ordinary `rightmemory mcp`. Curation runs through the configured worker; inspection, maintenance, recovery, and withdrawal use the CLI.

Tool and parameter descriptions carry the ordinary-agent contract. An internal MCP client need not load the command-backed daily skill as well; native assistant MCP keeps the shared `rightmemory-assistant` skill for delegation.

Capture favors useful evidence before demanding a settled general rule. Preserve scope and uncertainty, and capture distinct later occurrences when they add evidence. Pending candidates participate in relevant retrieval. At parent publication, the program records processing for all three domains and removes the fully processed raw sources. Useful cases or experiences remain where the curators retained them.

Configured submission wakes the curation worker. Successful ordinary submission returns empty success without candidate identifiers or routine curation state. Retrieval returns relevant context and actionable warnings, without routine curation notices. Failures and partial success that require attention remain visible. Acceptance of a submission confirms capture; publication can be inspected through the CLI.

Native assistant MCP also exposes `rightmemory_begin_curation` and `rightmemory_complete_curation` for the parent task. Update selects all three domains. Maintain can select all three or a single domain. Use `rightmemory_begin_curation_worker` to prepare each selected domain in the shared worktree. Record its result with `rightmemory_complete_curation_worker`, using the task ID, domain, and attempt returned by preparation. The parent reviews the combined result, commits any changes, and calls `rightmemory_complete_curation` to publish it.

`rightmemory_curation_status` reports readiness and existing tasks, and `rightmemory_withdraw_candidate` withdraws pending evidence. `rightmemory_count_curation_tokens` takes a domain and an optional task ID to measure retained text against the active root's maintenance limit. Updates have no token cap. Responses retain readiness and active-task notices for native delegation. After interruption, inspect the same task and retry its completion. An `effects_pending` notice requires completion recovery without another model turn. Configured execution uses the CLI's `rightmemory curate run` for the parent task and `rightmemory curate worker-run` for a domain contribution.

## Scope

The adapters use the same candidate store, domain cores, and curation lifecycle as the CLI. The CLI remains available for inspection, maintenance, task recovery, withdrawal, worker controls, and clients without MCP.

Ordinary curation leaves the user's Pursuit map unchanged. Explicit map requests use the human editor or `maintain-pursuit-map`; no additional MCP map-write tools are provided.

This command currently serves stdio only. RightMemory does not expose a Streamable HTTP MCP endpoint.

The assistant transport also exposes `rightmemory_assistant_sync_repair` with `begin`, `status`, `validate`, and `complete` actions over the same model-free repair service. Native repair notices are relayed to the host main agent for delegation; see [Assistant Execution](ASSISTANT_EXECUTION.md#sync-and-mcp).
