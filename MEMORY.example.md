> This starter template is copied into a new RightMemory root during bootstrap.
> Add real user/project Memory before this block, and do not treat sample nodes as user facts.
> Requested `maintain-rightmemory` work may remove the block once real Memory provides a clear working structure. <!-- rightmemory:example:start -->

# Sample Project Graph — Example Domain {#sample-project-graph}

> Replace this whole `#` section with your own domains. Canonical schema and module rules
> live in the installed RightMemory package, not in this file.

## Example Application {#sample-app} → [rel:sample-infra]

This example shows durable context that affects future interpretation, decisions, or
verification. Repository files remain the primary source for ordinary implementation
detail, and live release intent belongs in Pursuit.

### Durable Architecture Context {#sample-architecture-context} → [rel:sample-release-runbook]

Keep relationships here when they are expensive to reconstruct or easy to misread.
Ordinary source layout, dependency versions, and component inventories remain in
project artifacts.

- `proj-web` The browser client depends on `proj-api`; API compatibility is therefore release-relevant. → [dep:proj-api, rel:proj-deploy]
- `proj-api` The API service is the only production writer to `db-postgres`; maintenance tooling should preserve its validation path. → [dep:db-postgres, rel:proj-deploy]
- `proj-deploy` A production release combines compatible client and API artifacts; deploy either independently only when contract compatibility is verified. → [agg:proj-web, agg:proj-api]

### Release Runbook {#sample-release-runbook} → [dep:sample-architecture-context]

Release-facing changes should leave concise verification evidence covering the
checklist, rollout, rollback, and environment-specific considerations.

### Interface Contracts {#sample-interface-contracts}

- `api-public-contract` Public API responses use stable snake_case JSON keys so generated clients do not churn across releases. → [dep:proj-api, rel:proj-web]
- `auth-session-contract` Browser sessions are stored as signed HTTP-only cookies and refreshed through the API service. → [dep:proj-api, rel:proj-web]

## Example Infrastructure {#sample-infra} → [rel:sample-app]

### Deployment Environments {#sample-deployment-environments}

Store environment facts only when they materially affect future work rather than
merely restating deployment configuration.

- `sample-env-staging` Staging mirrors production authentication and schema migration behavior but uses synthetic data. → [ver:proj-deploy]
- `sample-env-production` Production releases require staging verification and a rollback note. → [dep:sample-env-staging, rel:proj-deploy]

### Database Safety {#sample-database-safety}

- `db-postgres` PostgreSQL is the application's authoritative state store. → [rel:proj-api]
- `db-backup-job` Nightly logical backups protect `db-postgres`; restore drills, not job success alone, establish recoverability. → [bak:db-postgres, rel:sample-backup-drill]
- `sample-backup-drill` Restore drills verify that the nightly backup is usable before major releases; keep detailed run evidence with the project. → [ver:db-backup-job]

---

# User Context — Example Domain {#sample-user-context}

This example preserves circumstances and choices that help a later agent understand the user's priorities.

## User Direction {#sample-user-direction}

Use this area for evidence-based context about what the user is pursuing, what matters
across sessions, and why.

- `sample-user-current-focus` Sample user is shaping a local-first memory system for AI agents. → [rel:sample-project-graph]
- `sample-user-memory-direction` Sample user chose local-first storage to keep long-term context inspectable across tools; convenience alone would not explain that choice. → [rel:sample-user-goals]

## User Goals And Priorities {#sample-user-goals}

Store active goals here only when they express durable direction rather than
momentary task state.

- `sample-user-goal-durable-memory` Sample user wants memory to distinguish durable direction from momentary task state. → [rel:sample-user-memory-direction]

---

# Open Context Questions {#open-context-questions}

This section stores loose ends as short questions. They are not declarative Memory
facts; connect them with `todo:` when they concern a specific item, then remove or
revise them after the answer is stored.

- `q-rightmemory-project-context` Which checkout path and runtime environment should agents use on each machine involved in RightMemory development? → [todo:sample-project-graph]

---

<!-- rightmemory:example:end -->
