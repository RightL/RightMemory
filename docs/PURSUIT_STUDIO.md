# Pursuit Studio

Pursuit Studio is the editable, tree-first workspace for RightMemory's live Pursuit graph. It does not create a second Pursuit database: `PURSUITS.md` and reachable `PURSUIT_<id>.md` files remain canonical.

## Launch

```bash
rightmemory pursuit studio
```

The command binds to loopback only, prints a one-process access-token URL, and opens it in the default browser. Use `--no-open`, `--port`, or a loopback `--host` when needed.

The Studio provides:

- a mind-map-style hierarchy with Focus, parked, F# backing, relation, and task indicators;
- structured create, edit, move, reorder, Focus, park, delete, relation, F# split, and inline operations;
- a Markdown diff before apply;
- whole-graph validation through the canonical `rightmemory.graph` index;
- optimistic revision checks so an old browser tab cannot overwrite newer edits;
- `MemoryWriteLock`, candidate validation, atomic file replacement with rollback, and persistent undo/redo history;
- optional Git commits that stage only the changed Pursuit paths.

Visual layout is not semantic state. Moving a card changes nothing until an explicit structured move is staged and applied.

## Command Surface

The same mutation layer is available to local agents and scripts:

```bash
rightmemory pursuit show --json
rightmemory pursuit create retrieval-speed --title "Faster retrieval" --parent rightmemory
rightmemory pursuit edit retrieval-speed --state "Lexical prefilter prototype exists." \
  --next "do: Benchmark recall and latency"
rightmemory pursuit move retrieval-speed --parent retrieval
rightmemory pursuit focus retrieval-speed
rightmemory pursuit split retrieval
rightmemory pursuit preview --operations-json '[{"op":"park","id":"retrieval-speed"}]'
rightmemory pursuit apply --operations-json '[{"op":"park","id":"retrieval-speed"}]' \
  --revision <workspace-revision>
rightmemory pursuit undo
rightmemory pursuit redo
```

Structured operations are the shared contract used by the Web Studio, CLI, and task reconciliation. A candidate is rendered and validated before the canonical files change.

## Task Links

Task execution is operational state, not Pursuit prose. Links live in root `pursuit_tasks.toml`; the writer adds the file to the root Git allowlist when necessary.

```bash
rightmemory pursuit task link --pursuit retrieval-speed --current \
  --title "Benchmark current retrieval" --project /path/to/RightMemory

rightmemory pursuit task plan --pursuit retrieval-speed \
  --action "Implement and benchmark the lexical prefilter" \
  --project /path/to/RightMemory

rightmemory pursuit task run task-0123456789ab
```

A task records its provider, provider thread ID, linked Pursuits, project, host, prompt, status, and result. Re-linking the same provider thread is idempotent. Planning also avoids another live task with the same Pursuit and action.

`task run` creates a Codex thread through the installed Codex SDK and executes its first turn on the machine running the command. `host` is recorded context; this version does not pretend to be an arbitrary cross-machine scheduler. A cross-device workflow can create a planned task and run it on the destination host.

## Result Reconciliation

Task completion never implies Pursuit completion. A Codex agent or user can propose the smallest justified structured update:

```bash
rightmemory pursuit reconcile propose task-0123456789ab \
  --summary "The benchmark settled the prefilter direction." \
  --operations-json '[
    {
      "op": "update",
      "id": "retrieval-speed",
      "state": "The lexical prefilter preserves target recall and reduces selection latency.",
      "next": ["do: Integrate it into production retrieval"]
    }
  ]'
```

The proposal is previewed against a specific Pursuit revision before it is registered. It can then be applied or dismissed:

```bash
rightmemory pursuit reconcile apply recon-0123456789ab
rightmemory pursuit reconcile dismiss recon-0123456789ab
```

A stale reconciliation fails rather than overwriting newer Pursuit work.

## Data Boundaries

- `PURSUITS.md` and reachable `PURSUIT_<id>.md`: canonical live intent.
- `pursuit_tasks.toml`: task identities, links, status, results, and pending reconciliation.
- `.runtime/pursuit-studio/history.json`: local undo/redo state; ignored by Git.
- project repositories and task threads: detailed execution state and artifacts.

Deleting a Pursuit with linked tasks is refused until those links are removed. Removing completed, abandoned, or superseded Pursuit still follows the normal Pursuit lifecycle: preserve independently durable consequences in Memory, repair references and Focus, then remove the live intent.
