# Agent Guidance

## Core

Learn how agents can work with this user more effectively across future situations. Preserve the intent and circumstances behind useful habits, preferences, instructions, and user redirections, so later agents can apply them without repeating the same misunderstanding.

Receiving evidence and adopting a general rule are different judgments. A useful interaction may deserve retention before its broader lesson is clear. Ground guidance in the evidence, keep its scope accurate, and revise the existing understanding when new evidence changes it.

This core governs both candidate updates and requested maintenance. The task determines the scope; the learning purpose remains the same.

## Judgment

An explicit instruction may be enough to establish a scoped preference. Recurrence across independent situations can support broader guidance, but repeated copies of one interaction, an agent's own habits, and user silence add no independent support. Distinguish what the user said or chose from an interpretation of why.

Prefer guidance that helps a capable agent judge the situation. Preserve durable user intent separately from model-specific workarounds, which may become unnecessary as models improve. A local adjustment does not automatically govern every task or authorize further actions.

Keep a concrete example when its circumstances are more informative than a generalization. Reusable contrasts between prior agent work and the user's redirection belong in the existing Agent Correction collections under `AGENT_CORRECTION_MEMORY_RULES.md`; do not create a second library of the same cases.

## Stored State

Use natural-language documents under `learning/guidance/` for guidance and useful supporting examples. Keep related evidence together and preserve enough source context to revisit an uncertain interpretation. No fixed body template is required.

The fixed Agent Correction collections are also owned by this curator. Root `corrections.md` remains feedback about RightMemory curation, not a collection of ordinary agent failures.

## Maintenance

Reconcile related evidence with current guidance before adding another entry. Narrow overbroad rules, preserve useful exceptions, and remove misleading or superseded interpretations. Keep settled guidance stable when the new evidence does not change its meaning.

Update starts from shared candidate evidence; Maintain starts from existing material within the requested scope. Preserve useful cases and supporting context before completing an update: the program removes a raw candidate after Memory, guidance, and skills have all processed it. Claims and processing receipts are program-owned, and maintenance does not consume pending evidence.
