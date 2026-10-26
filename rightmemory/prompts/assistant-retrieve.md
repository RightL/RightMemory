# Retrieve for an Assistant

Gather stored context that materially helps the caller understand or approach the current work. Return a concise handoff with sources; leave the user's task to the caller.

## Inspect the Sources

Use the supplied context's memory root, source paths, imported-view paths, and pending file with the assistant's native read and search tools. Follow the package schema when resolving graph references. Read enough to understand a relevant item's scope, evidence, and relationships; expand linked detail when the root description is insufficient.

Consider durable Memory, the user's Pursuit map, and relevant Agent Corrections. Include guidance that could change the caller's next action. Agent Corrections belong in ordinary retrieval when the same failure pattern could matter. Root `corrections.md` is edit feedback for Update, not ordinary retrieval content.

Treat Open Context Questions as questions. Treat pending candidates as unsettled evidence and preserve their status and provenance. An `MQ#` heading provides local relationship context; it does not authorize calling an external provider.

## Hand Off What Matters

Summarize useful context accurately and give the source file and heading or line range for consequential claims. Keep current stored state, pending evidence, inference, and unresolved conflicts distinguishable. If an `S#` resource is needed as an instruction, supply the complete instruction or direct the caller to its full file.

Explain a material gap or inaccessible source rather than guessing its contents. Stop when further reading adds little to the caller's work.

Keep retrieval read-only.
