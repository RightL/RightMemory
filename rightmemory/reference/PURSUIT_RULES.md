# Pursuit Rules

## Purpose

A Pursuit is live intent organized into a hierarchy: what should become true, why it matters, and how the pursuit currently decomposes into active directions. It is neither a backlog, a work log, nor a detailed execution record. Task duration does not determine whether something belongs in Pursuit.

A candidate belongs in Pursuit when the intent should remain part of the active or deliberately parked pursuit structure after the current update. Incompleteness alone is insufficient.

Pursuit preserves the objective, its meaning, its position in the hierarchy, and the current direction needed to understand it. Detailed execution state and instructions for resuming project work belong primarily in project-local artifacts.

## Structure

```md
## <Title> {#<stable-id>} → [<optional typed edges>]

<What should become true and why it matters.>

**State:** <Optional current context that materially affects the pursuit's present position or direction.>

**Next:**
- `do` <Action an agent may perform now within existing authority.>
- `ask` <Information, judgment, or authorization needed from a person.>
- `wait` <External condition that must change first.>

**Done when:** <Optional observable completion condition.>
```

A deliberately parked Pursuit additionally uses:

```md
**Status:** parked
```

Rules:

- The heading body states the objective and why it matters.
- State is optional when the objective and Next already preserve enough context to understand the pursuit's present position or direction.
- A Pursuit must remain interpretable from stored context under the model's reading-context rules.
- An active leaf has an ordered Next list, normally one to three items. Its first valid item is the default movement.
- A parent may omit Next when an active child contains the movement.
- Next records the current movement, not a detailed resume plan. Refer to project-local artifacts when execution detail matters. It grants no authority beyond the current task and environment.
- Use `Done when` only when there is a clear observable endpoint.
- Use `Status: parked` only when later reconsideration is still intended and the Pursuit is deliberately outside current movement.
- Tree nesting expresses decomposition. Add edges only for useful relationships not already clear from containment.
- Keep one canonical Pursuit for each intent. Never copy its State or Next into another node.

## State Quality

Good State is the minimum current context required to understand the pursuit's present position, constraints, or direction.

Supporting evidence, completed history, detailed experiments, implementation narration, and operational resume instructions belong in project artifacts or durable Memory when they independently satisfy Memory admission.

State should describe what is true now. Do not preserve every transition that led there.

## Admission And Removal

Work that starts and finishes within one reconciled candidate thread normally leaves no Pursuit.

Create or retain a Pursuit when an objective remains meaningfully active, under reconsideration, deliberately parked, blocked, waiting, handed off, or otherwise part of the pursuit hierarchy being carried forward.

Do not create a Pursuit merely because a task is unfinished or because detailed work may need to resume later. Project-local artifacts should preserve that operational continuity.

A parked Pursuit remains only while future reconsideration is genuinely intended. Importance alone does not justify keeping inactive intent.

When a Pursuit completes, is abandoned, or is superseded:

1. preserve only independently durable consequences in Memory;
2. repair affected references and Focus entries;
3. remove the terminal Pursuit from the active tree.

Git history preserves its former state. Pursuit should not accumulate completed history.

## Focus

`## Focus` lists the Pursuits currently receiving attention or expected to guide near-term work when no newer user instruction takes precedence.

Each entry is a backticked id of an addressable Pursuit heading. Order expresses the default attention order, not a global ranking of importance.

Important active Pursuits may remain outside Focus. Parked Pursuits do not appear in Focus.

## Maintenance

When acting from Pursuit:

1. Read the selected Pursuit, relevant ancestors and active descendants, and reachable Memory context.
2. Verify claims that may have changed against current files, tests, tools, or external reality.
3. Reconsider the Pursuit when assumptions fail, cost changes materially, it becomes infeasible, or a better direction replaces it.
4. If it remains valid, perform the first valid `do`, surface an `ask` rather than guessing, or check the condition behind a `wait`.
5. After material work, update the canonical node and remove stale State or Next content.
6. Update ancestors only when their objective, direction, blockage, or completion changed.
