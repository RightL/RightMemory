# Agent Files Collector

`rightmemory agent-files` keeps a local inventory of agent instruction files. It is separate from Memory, Pursuit, Agent Corrections, and ordinary retrieval, and it never modifies the files it collects.

Register a few workspace directories and any standalone global files:

```bash
rightmemory agent-files register ~/code ~/.codex/AGENTS.md ~/.claude/CLAUDE.md
```

Then refresh and inspect the inventory:

```bash
rightmemory agent-files collect
rightmemory agent-files list
rightmemory agent-files show <content-id>
```

`collect` recursively finds files named exactly `AGENTS.md` or `CLAUDE.md`. Exact duplicates share one content entry after UTF-8 BOM removal and line-ending normalization; every source path remains attached to that entry.

State lives in `~/.rightmemory-agent-files`, or in `RIGHTMEMORY_AGENT_FILES_ROOT` when set. It is independent of RightMemory profiles and sync.

This first version has no watcher, agent-side discovery, semantic analysis, rule merging, or deployment.
