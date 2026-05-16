## Python environment
- For the standalone RightMemory app in this worktree, use `./.venv/bin/python` instead of `conda run -n part python`.

## Writing and documentation style
- When editing README, schema, prompt, or skill text, prefer coherence over patch-like accumulation. The result should read as if it was written fresh around the current idea, not as an old design with exceptions bolted on later.
- If a requested change modifies the conceptual model, integrate it into the surrounding explanation. Do not merely append a caveat such as "also this case" or "except now this other thing"; rewrite the relevant paragraph or bullet group so the rule feels native.
- Preserve the core framing of RightMemory: it is a tree + graph hybrid memory system designed primarily for AI agents. Human readability is useful, but it is not the main design center.
- When the user says wording is "not coherent", "patch-like", or "not newly written", treat that as a request to improve the conceptual shape of the prose, not only grammar. Look for old/new seams, repeated rules, awkward exceptions, and sentences that describe history instead of the final design.
- For important docs/schema changes, discuss the intended wording or show a concise proposed diff before applying broad edits. Small wording fixes can be applied directly, but larger rewrites should keep the user's framing visible.
- Why this matters: these docs and skills are instructions for future agents. Patch-like text causes future agents to inherit the order of edits instead of the intended model, while coherent text gives them a stable rule to follow.
