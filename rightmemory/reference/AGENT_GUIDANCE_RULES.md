# Agent Guidance

## Core

**Learn how agents can work with this user more effectively across future situations.** Preserve the intent and circumstances behind useful habits, preferences, instructions, and user redirections, so later agents can apply them without repeating the same misunderstanding.

Useful context can remain scoped or uncertain when it changes future understanding. Ground guidance in the evidence, keep its scope accurate, and revise the existing understanding when new evidence changes it.

This core governs both candidate updates and requested maintenance. The task determines the scope; the learning purpose remains the same.

## Judgment

**Retain what a capable agent would otherwise miss or get wrong beyond the request, current instructions, and readily available project context.** Most candidate content should be filtered out. A conversation-specific instruction may need no separate record; stating its scope accurately does not by itself make it useful across sessions. Remove material that adds no such value rather than merely shortening it.

An explicit instruction may be enough to establish a scoped preference. Recurrence across independent situations can support broader guidance, but repeated copies of one interaction, an agent's own habits, and user silence add no independent support. Distinguish what the user said or chose from an interpretation of why.

Preserve durable user intent separately from model-specific workarounds, which may become unnecessary as models improve. Refer to maintained instructions when they already carry the guidance, retaining any useful intent or context they omit.

Keep a concrete example when its circumstances are more informative than a generalization. Reusable contrasts between prior agent work and the user's redirection belong in the existing Agent Correction collections under `AGENT_CORRECTION_MEMORY_RULES.md`; do not create a second library of the same cases.

## Stored State

Use natural-language documents under `learning/guidance/` for current guidance and useful supporting examples. Keep enough context to apply or reconsider the guidance. No fixed body template is required.

The fixed Agent Correction collections are also owned by this curator. Root `corrections.md` remains feedback about RightMemory curation, not a collection of ordinary agent failures.

## Maintenance

Reconcile related evidence with current guidance before adding another entry. Narrow overbroad rules, preserve useful exceptions, and remove misleading or superseded interpretations. Keep settled guidance stable when the new evidence does not change its meaning.

Update starts from shared candidate evidence; Maintain starts from existing material within the requested scope. Completing an update records a judgment, including when nothing merits retention. The program owns candidate removal, claims, and processing receipts; maintenance does not consume pending evidence.
