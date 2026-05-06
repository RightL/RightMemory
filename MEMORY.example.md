# Memory File Set — Schema and Maintenance Rules
## Reference Schema

Each entry is a **node** with this shape:

```
- `<node-id>` <free-form description> → [ref1, ref2, ...]
```

- `node-id`: unique identifier, format `<type>-<slug>` (e.g. `proj-web`, `lib-utils`).
- `<free-form description>`: any content — directory description, preference, fact, note, etc.
- `→ [ref1, ref2, ...]`: **edges** to other node-ids, expressing typed relationships.
- Edges are **bidirectional by default**: when A references B, B should reference A back (one-way exceptions noted below).
- No edges? Write `→ []`.
- Memory files live as `MEMORY.md` plus optional sibling detail files named `MEMORY_<slug>.md`.
- `MEMORY.md` is the root memory file, but it remains normal memory: it may contain real nodes, not just links to other files.

Edge type prefixes (when uncertain, copy the convention from the "Example" column rather than inventing new semantics):

| Prefix | Meaning | Direction | Example |
|--------|---------|-----------|---------|
| `dep:` | depends on / based on | A → B means A depends on B | `proj-api → dep:lib-utils` |
| `emb:` | embeds / contains | A → B means A embeds a copy of B | — |
| `bak:` | backup / snapshot (one-way) | A → B means A is a backup of B | — |
| `agg:` | aggregates | A → B means A aggregates B's outputs | `proj-deploy → agg:proj-web` |
| `ver:` | verifies / tests | A → B means A verifies/tests B | — |
| `ext:` | extends / enhances | A → B means A is an extended version of B | — |
| `up:` | upstream (one-way) | A → B means A is upstream of B | — |
| `rel:` | related (default) | general association | `lib-utils → rel:proj-web` |
| `loc:` | located at | A → B means A is located inside B | — |
| `run:` | runs / launches | A → B means A is run via B | — |
| `cfg:` | configures | A → B means A uses B as configuration | — |
| `out:` | outputs (one-way) | A → B means A produces B | — |
| `in:` | inputs / sources (one-way) | A → B means A consumes B as input | — |
| `doc:` | documents | A → B means A documents B | — |
| `todo:` | todo / blocker | A → B means A is a todo / blocker for B | — |



## Maintenance Rules

- `#` marks the root of a memory domain (e.g. `Sample Project Graph`); nodes under different `#` domains are not assumed related unless explicitly connected by an edge.
- `##` is a normal tree layer inside a domain (e.g. project, person, system, topic); it may contain normal memory content.
- `###` is a normal tree layer inside a `##` section (e.g. theme, component, phase); it may contain normal memory content.
- `####` is not a normal content layer. It is a title-only external child pointer with the exact form `#### Human Title {#short-slug}`.
- A `####` pointer maps to a sibling detail file named `MEMORY_<short-slug>.md`. For example, `#### Deployment Details {#sample-deploy}` maps to `MEMORY_sample-deploy.md`.
- Do not write content under a `####` heading in the current file. Put detailed nodes in the pointed `MEMORY_<short-slug>.md` file instead.
- Detail files use the same rules: `#`, `##`, and `###` are normal tree layers; `####` is title-only and points to the next detail file.
- Headings form a text tree — they express layout and reading context only. Being "under" a heading does NOT imply dependency, containment, backup, verification, or any other graph relation.
- Real graph relations live only in the `→ [...]` at the end of each node line. Cross-heading, cross-directory, cross-project, cross-theme relations should always be written as edges, not as scattered prose.
- Use the typed prefixes above (`dep:`, `emb:`, `bak:`, `agg:`, `ver:`, `ext:`, `up:`, `rel:`, `loc:`, `run:`, `cfg:`, `out:`, `in:`, `doc:`, `todo:`); fall back to `rel:` only when no typed prefix fits.
- When uncertain about which edge type to use, copy the precedent from the "Example" column of the table rather than inventing a new interpretation; only consider adding a new type when no example reasonably fits.
- If A has an edge to B and the relation isn't explicitly one-way (`bak:`, `up:`, `out:`, `in:`), B should have a matching edge back to A — keep the graph bidirectional and traceable.
- Node content describes "what this node is"; edges describe "how it relates to other nodes". Don't hide relations inside node descriptions.
- When adding a memory, first decide which `#` domain it belongs to, then place it in the closest existing `##` or `###` group. Add a new normal heading only when no group fits.
- When a `##` or `###` group becomes too detailed, create a `#### Human Title {#short-slug}` pointer and move the deeper details into `MEMORY_<short-slug>.md`.
- When the same entity appears in different machines, paths, or build forms, prefer separate nodes connected by `rel:` / `loc:` / `out:` / `dep:` / `emb:` edges, rather than mashing multiple environments into one untraceable description.
- When updating existing facts, prefer editing the existing node and its edges; create new nodes only when a genuinely new entity, output, config, task, or doc appears.
- Before writing, check whether related nodes already have reverse edges; if not and the relation is not one-way, add the reverse edge to keep the graph from fragmenting.

## Commit Rules

- Routine curator edits are saved in place but not committed unless explicitly requested.
- Before the curator's first write in a session, if the memory repo already has pending changes limited to `MEMORY*.md` and `dream_logs/*.md`, commit those pre-existing changes first as a baseline. Then refresh from disk and apply the curator edit.
- If pre-existing dirty files include paths outside `MEMORY*.md` and `dream_logs/*.md`, ask before committing.
- Dreamer consolidation commits touched `MEMORY*.md` files plus the dream report after the dream cycle.



# Sample Project Graph — Example Domain

> Replace this whole `#` section with your own domains. Keep the schema and maintenance rules above intact; the curator and dreamer skills both treat that preamble as authoritative.

## Example Application

### Projects

- `proj-web` web-app — example frontend, TypeScript + Vite, calls `proj-api` for data → [dep:lib-utils, agg:proj-deploy]
- `proj-api` api-server — example backend service, Python + FastAPI → [dep:lib-utils, dep:db-postgres, agg:proj-deploy]
- `proj-deploy` deploy-bundle — production deployment package combining frontend and backend artifacts → [agg:proj-web, agg:proj-api]

#### Deployment Details {#sample-deploy}

### Libraries

- `lib-utils` shared-utils — small utility library reused by both frontend and backend → [rel:proj-web, rel:proj-api]

#### Utility Internals {#sample-utils}

## Example Infrastructure

### Databases

- `db-postgres` postgres-db — PostgreSQL database used by the API → [rel:proj-api]

---



# User Pending Task and Thoughts (user-edited only — AI agents must not modify this section)
