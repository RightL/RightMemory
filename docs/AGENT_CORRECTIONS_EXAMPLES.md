# Agent Corrections Examples

These examples illustrate the two fixed Agent Correction collections. They are not
user state and are not copied into a new RightMemory root. A real entry should
preserve the reusable contrast and its scope without adopting a rigid field template.

## Expression Correction

Target collection: `MEMORY_agent-corrections-writing.md`.

Changing expression or presentation alone fully resolves the user's objection.

### Preserve the requested comparison structure

The agent reached a reasonable conclusion but replaced the requested side-by-side
comparison with a long narrative. The user redirected it to keep the comparison
structure and tighten the explanation. In similar requests, preserve an explicitly
requested comparison format unless it prevents a correct answer.

## Substance Correction

Target collection: `MEMORY_agent-corrections-design.md`.

The reasoning, assumptions, decision, action, omission, workflow, or behavior must
change.

### Verify ambiguous remote state before retrying

After a deployment command returned an ambiguous failure, the agent proposed running
it again. The user pointed out that the first command might already have succeeded and
was not known to be idempotent. The accepted direction was to inspect remote state
before deciding whether to retry. In similar situations, do not treat an ambiguous
failure as proof that no state change occurred.
