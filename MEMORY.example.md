# Sample Project Graph — Example Domain {#sample-project-graph}

> Replace this whole `#` section with your own domains. The memory schema lives in the installed RightMemory skills, not in this file.

## Example Application {#sample-app}

### Projects {#sample-projects} → [rel:sample-infra]

- `proj-web` web-app — example frontend, TypeScript + Vite, calls `proj-api` for data → [dep:lib-utils, agg:proj-deploy]
- `proj-api` api-server — example backend service, Python + FastAPI → [dep:lib-utils, dep:db-postgres, agg:proj-deploy]
- `proj-deploy` deploy-bundle — production deployment package combining frontend and backend artifacts → [agg:proj-web, agg:proj-api]

#### Deployment Details {#sample-deploy}

### Libraries {#sample-libs}

- `lib-utils` shared-utils — small utility library reused by both frontend and backend → [rel:proj-web, rel:proj-api]

#### Utility Internals {#sample-utils}

## Example Infrastructure {#sample-infra} → [rel:sample-projects]

### Databases {#sample-databases}

- `db-postgres` postgres-db — PostgreSQL database used by the API → [rel:proj-api]

---



# Cross-Session Agent Behavior — Example Domain {#sample-agent-behavior}

## User and Workflow Preferences {#sample-user-workflow-preferences}

Store cross-project behavior memory here when it should change how future agents work. The body gives the section purpose; child nodes capture specific preferences or constraints.

- `pref-principle-first` The user prefers principle-first instructions over long category lists; examples are interpretation aids, neither required nor sufficient, and the governing decision test controls each case. → []
- `pref-env-check` Future agents should check durable memory and local project context for the intended runtime environment before installing dependencies or guessing a Python environment. → []

---



# User Pending Task and Thoughts (user-edited only — AI agents must not modify this section)
