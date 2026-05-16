> This starter template is managed by RightMemory install. Add real user/project memory before this template block. Do not treat sample nodes as user facts. Once real memory before this template section exceeds 50 lines, the dreamer may remove this entire example/template section from the installed `MEMORY.md`. <!-- rightmemory:example:start -->

# Sample Project Graph — Example Domain {#sample-project-graph}

> Replace this whole `#` section with your own domains. The memory schema lives in the installed RightMemory skills, not in this file.

## Example Application {#sample-app} → [rel:sample-infra]

This example domain shows a product memory with graph-addressable headings, compact fact nodes, and detail-file pointers for topics that are too large for the root file.

### Deployable Units {#sample-deployable-units} → [rel:sample-release-runbook]

This group tracks the example application's deployable services and shared code.

- `proj-web` web-app — example frontend, TypeScript + Vite, calls `proj-api` for data. → [dep:lib-utils, agg:proj-deploy]
- `proj-api` api-server — example backend service, Python + FastAPI, reads from `db-postgres`. → [dep:lib-utils, dep:db-postgres, agg:proj-deploy]
- `lib-utils` shared-utils — small utility library reused by both frontend and backend. → [rel:proj-web, rel:proj-api]
- `proj-deploy` deploy-bundle — production deployment package combining frontend and backend artifacts. → [agg:proj-web, agg:proj-api]

### Release Runbook {F#sample-release-runbook} → [dep:sample-deployable-units]

Release checklist, rollout steps, rollback notes, and environment-specific commands live in `MEMORY_sample-release-runbook.md`; the root keeps this short summary so agents can discover the topic without loading the long runbook.

### Interface Contracts {#sample-interface-contracts}

- `api-public-contract` Public API responses use stable snake_case JSON keys so generated clients do not churn across releases. → [dep:proj-api, rel:proj-web]
- `auth-session-contract` Browser sessions are stored as signed HTTP-only cookies and refreshed through the API service. → [dep:proj-api, rel:proj-web]

## Example Infrastructure {#sample-infra} → [rel:sample-app]

### Database Stack {#sample-database-stack}

- `db-postgres` postgres-db — PostgreSQL database used by the API service. → [rel:proj-api]
- `db-backup-job` backup-job — nightly logical backup for `db-postgres`, verified by restore drills before major releases. → [bak:db-postgres, ver:sample-backup-drill]

#### Backup Drill Notes {F#sample-backup-drill}

---

# Cross-Session Agent Behavior — Example Domain {#sample-agent-behavior}

## User and Workflow Preferences {#sample-user-workflow-preferences}

Store cross-project behavior memory here when it should change how future agents work.

- `pref-principle-first` The user prefers principle-first instructions over long category lists; examples are interpretation aids, neither required nor sufficient, and the governing decision test controls each case. → []
- `pref-env-check` Future agents should check durable memory and local project context for the intended runtime environment before installing dependencies or guessing a Python environment. → []

---

<!-- rightmemory:example:end -->

# User Pending Task and Thoughts (user-edited only — AI agents must not modify this section)
