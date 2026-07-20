> This starter template is managed by RightMemory install. Add real user/project memory before this template block. Do not treat sample nodes as user facts. Once real memory before this template section exceeds 50 lines, the dreamer may remove this entire example/template section from the installed `MEMORY.md`. <!-- rightmemory:example:start -->

# Sample Project Graph — Example Domain {#sample-project-graph}

> Replace this whole `#` section with your own domains. The memory schema lives in the installed RightMemory skills, not in this file.

## Example Application {#sample-app} → [rel:sample-infra]

This example domain shows durable product context with graph-addressable headings, compact fact nodes, and project-scoped preferences. Live release intent belongs in Pursuit rather than this durable tree.

### Project Working Preferences {#sample-project-working-preferences}

Use project-scoped preferences when the guidance belongs to one project or domain rather than the user's broader cross-session behavior guidance.

- `proj-pref-contract-first` When changing frontend/backend data flow, update the API contract before changing UI call sites. → [rel:api-public-contract, rel:proj-web, rel:proj-api]
- `proj-pref-release-proof` Release-facing changes should leave a short verification note in the release runbook. → [rel:sample-release-runbook, rel:proj-deploy]

### Deployable Units {#sample-deployable-units} → [rel:sample-release-runbook]

This group tracks the example application's deployable services and shared code.

- `proj-web` web-app — example frontend, TypeScript + Vite, calls `proj-api` for data. → [dep:lib-utils, dep:proj-api, rel:proj-deploy]
- `proj-api` api-server — example backend service, Python + FastAPI, reads from `db-postgres`. → [dep:lib-utils, dep:db-postgres, rel:proj-deploy]
- `lib-utils` shared-utils — small utility library reused by both frontend and backend. → [rel:proj-web, rel:proj-api]
- `proj-deploy` deploy-bundle — production deployment package combining frontend and backend artifacts. → [agg:proj-web, agg:proj-api]

### Release Runbook {#sample-release-runbook} → [dep:sample-deployable-units]

Release-facing changes should leave concise verification evidence covering the checklist, rollout, rollback, and environment-specific considerations.

### Interface Contracts {#sample-interface-contracts}

- `api-public-contract` Public API responses use stable snake_case JSON keys so generated clients do not churn across releases. → [dep:proj-api, rel:proj-web]
- `auth-session-contract` Browser sessions are stored as signed HTTP-only cookies and refreshed through the API service. → [dep:proj-api, rel:proj-web]

## Example Infrastructure {#sample-infra} → [rel:sample-app]

### Database Stack {#sample-database-stack}

- `db-postgres` postgres-db — PostgreSQL database used by the API service. → [rel:proj-api]
- `db-backup-job` backup-job — nightly logical backup for `db-postgres`, verified by restore drills before major releases. → [bak:db-postgres, rel:sample-backup-drill]

- `sample-backup-drill` Restore drills verify that the nightly backup is usable before major releases; keep the latest durable conclusion here and detailed run evidence with the project. → [ver:db-backup-job]

---

# User Context — Example Domain {#sample-user-context}

This example domain stores a compact, evidence-grounded context profile for the user.

## User Direction {#sample-user-direction}

Use this area for evidence-based context about what the user is pursuing, what matters across sessions, and why.

- `sample-user-current-focus` Sample user is shaping a local-first memory system for AI agents. → [rel:sample-project-graph, rel:sample-agent-behavior]
- `sample-user-memory-direction` Sample user wants memory to preserve durable context while staying coherent, reviewable, and useful for future agents. → [rel:sample-user-goals, rel:sample-agent-behavior]

## User Goals And Priorities {#sample-user-goals}

Store active goals when they express durable direction rather than a momentary task list.

- `sample-user-goal-durable-memory` Sample user wants memory to distinguish durable direction from momentary task state. → [rel:sample-user-memory-direction, rel:sample-agent-behavior]

---

# Cross-Session Agent Behavior — Example Domain {#sample-agent-behavior}

## User and Workflow Preferences {#sample-user-workflow-preferences}

Store cross-project behavior memory here when it should change how future agents work with this user.

- `pref-principle-first` The user prefers principle-first instructions over long category lists; examples are interpretation aids, neither required nor sufficient, and the governing decision test controls each case. → []
- `pref-env-check` Future agents should check durable memory and local project context for the intended runtime environment before installing dependencies or guessing a Python environment. → []

---

# Open Context Questions {#open-context-questions}

This section stores loose ends in memory as short questions for future agents. These questions are not declarative memory facts; they point to related memory with `todo:` and should be removed or revised after the answer is saved as ordinary memory.

- `q-rightmemory-project-context` Which local project path is the active RightMemory checkout, and which runtime environment should agents use when installing, testing, or debugging it? → [todo:pref-env-check]

---

<!-- rightmemory:example:end -->
