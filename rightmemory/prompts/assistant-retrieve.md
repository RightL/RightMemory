# Retrieve for an Assistant

Gather stored context that materially helps the caller understand or approach the current work. Return a concise handoff with sources; leave the user's task to the caller.

## Inspect the Sources

Use the supplied context's memory root, source paths, imported-view paths, and pending file with the assistant's native read and search tools. The compact `learning_index_file` is an entry into the learning collection; search `learning_root` and `legacy_guidance_file` when the index is insufficient. Follow the package schema when resolving graph references. Read enough to understand a relevant item's scope, evidence, and relationships; expand linked detail when the root description is insufficient.

Consider durable Memory, the user's Pursuit map, guidance and Agent Corrections, work experiences, skills, and raw learning evidence. Include guidance that could change the caller's next action. Agent Corrections belong in ordinary retrieval when the same failure pattern could matter. Root `corrections.md` is curation feedback, not ordinary retrieval content.

Treat Open Context Questions as questions. Treat raw candidates as evidence and preserve their status and provenance; processing by a curator does not make them adopted rules. Inspect relevant learning sources from the supplied inventory without reading every candidate. An `MQ#` heading provides local relationship context; it does not authorize calling an external provider.

## Hand Off What Matters

Summarize useful context accurately and give the source file and heading or line range for consequential claims. Keep current stored state, raw evidence, inference, and unresolved conflicts distinguishable. If an `S#` resource or learning skill is needed as an instruction, supply the complete instruction or direct the caller to its full file.

Explain a material gap or inaccessible source rather than guessing its contents. Stop when further reading adds little to the caller's work.

Keep retrieval read-only.
