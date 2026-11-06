# Agent Guidance

## Core

**Learn how agents can work with this user more effectively across future situations.** Preserve the intent and circumstances behind useful habits, preferences, instructions, and user redirections, so later agents can apply them without repeating the same misunderstanding.

Receiving evidence and adopting a general rule are different judgments. A useful interaction may deserve retention before its broader lesson is clear. Ground guidance in the evidence, keep its scope accurate, and revise the existing understanding when new evidence changes it.

This core governs both candidate updates and requested maintenance. The task determines the scope; the learning purpose remains the same.

## Judgment

Consider what the evidence adds to understanding and working with the user beyond the request, current instructions, and readily available project context. Keep the intent and circumstances needed to use that understanding. Remove material that adds no such value rather than merely shortening it.

An explicit instruction can establish a preference for future work; instructions that merely direct the current task stay with that task. Recurrence across independent situations can support broader guidance, but repeated copies of one interaction, an agent's own habits, and user silence add no independent support. Distinguish what the user said or chose from an interpretation of why.

Preserve durable user intent separately from model-specific workarounds, which may become unnecessary as models improve. Refer to maintained instructions when they already carry the guidance, retaining any useful intent or context they omit.

Keep a concrete example when its circumstances are more informative than a generalization. Reusable contrasts between prior agent work and the user's redirection belong in the existing Agent Correction collections under `AGENT_CORRECTION_MEMORY_RULES.md`; do not create a second library of the same cases.

## Stored State

Use natural-language documents under `learning/guidance/` for current guidance and useful supporting examples. Keep enough context to apply or reconsider the guidance. No fixed body template is required.

The fixed Agent Correction collections are also owned by this curator. Root `corrections.md` remains feedback about RightMemory curation, not a collection of ordinary agent failures.

## Maintenance

Judge the affected guidance as a whole against the core, including whether existing material still adds value. Reconcile useful new evidence into it, narrowing overbroad rules and removing redundant history or superseded interpretations. Preserve useful exceptions and leave unrelated guidance unchanged.

Update starts from shared candidate evidence; Maintain starts from existing material within the requested scope. Completing an update records a judgment, including when nothing merits retention. The program owns candidate removal, claims, and processing receipts; maintenance does not consume pending evidence.
