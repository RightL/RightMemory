# Pursuit Rules

A Pursuit is live intent: what should become true, why it matters, the current situation, and the next movement. It is neither a backlog nor a work log, and task duration does not determine whether it belongs here.

RightMemory is one graph organized into two document trees. Memory holds durable context; Pursuit holds intent and commitments still being carried forward. Addressable ids are globally unique across both trees, and typed edges may cross between them.

## Structure

```md
## <Title> {#<stable-id>} → [<optional typed edges>]

<What should become true and why it matters.>

**State:** <Current context that materially affects what to do next.>

**Next:**
- `do` <An action the agent may perform now within existing authority>
- `ask` <Information, judgment, or authorization needed from a person>
- `wait` <An external condition that must change first>

**Done when:** <Optional observable completion condition.>

**Status:** parked
```

- The heading body states the objective and its meaning.
- A Pursuit must remain interpretable across agents, devices, and sessions.
  Its objective, State, Next, and Done when may rely on the schema's local
  reading context, but every reference that materially affects continuation
  must be determined by stored context rather than unstated ambient execution
  context.
- State is optional when the body and Next already preserve enough context.
- An active leaf Pursuit has an ordered Next list, normally limited to one to three items. Its first valid item is the default movement.
- A parent may omit Next when an active child contains the movement.
- Next records direction; it grants no authority beyond the current task and environment.
- Use `Done when` only when a Pursuit has a clear observable endpoint.
- Use `Status: parked` only while later reconsideration is still intended and the Pursuit is deliberately outside the current movement.
- Tree nesting expresses decomposition. Edges express useful relationships not already clear from containment.
- Keep one canonical node for each Pursuit. Never copy its State or Next.

## Admission and Focus

The unified updater preserves a candidate as Pursuit only when a later agent should intentionally resume or re-evaluate it after the current update. Incompleteness alone is insufficient, and work that begins and finishes within the same reconciled candidate batch normally leaves no Pursuit.

Focus lists the active Pursuits the agent is committed to continuing when there is no newer user instruction. Each entry is a backticked id of an addressable Pursuit heading. Focus order is the default resume order, not a ranking of everything important.

Important active Pursuits may remain outside Focus. Parked Pursuits remain only while the commitment is intentionally being carried forward. Completed or dropped Pursuits leave the active tree after any independently durable conclusion is preserved in Memory and all affected graph references are repaired; Git history retains their past state.

## Files

Start with `PURSUITS.md`. When a subtree becomes hard to navigate, mark its heading with `{F#id}` and move its child content to `PURSUIT_id.md`, keeping the heading and optional summary in its containing file.

F# is root-relative: Memory F# uses `MEMORY_id.md`, while Pursuit F# uses `PURSUIT_id.md`. M#, S#, MF#, and MQ# are Memory-only forms. Free-form M# and S# backing files are not parsed as graph content.

## Maintenance

1. Read the selected Pursuit, its relevant ancestors and active children, and reachable Memory context.
2. Verify State against current code, files, tests, or external reality.
3. Reconsider the Pursuit when assumptions fail, cost becomes unreasonable, it becomes infeasible, or a better direction replaces it. Do not mechanically execute stale Next text.
4. If it remains valid, perform the first valid `do`, surface an `ask` instead of guessing, or check the condition behind a `wait`.
5. After material work, update the canonical node and remove stale content. Update ancestors only when their meaning, direction, blockage, or completion changes.
6. When the Pursuit ends, remove it from Focus and the active tree after preserving only facts, decisions, rules, or lessons that independently remain useful in Memory. Inspect incoming and outgoing cross-tree edges before changing ids or deleting graph objects.
