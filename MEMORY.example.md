# Memory File — Schema and Maintenance Rules
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
- `##` is the top-level organization within a domain (e.g. `Nodes`, `Projects`, `Pending`); it serves reading and navigation.
- `###` / `####` / `#####` mark finer reading groups (project type, theme, sub-theme, phase); heading depth shows "where to put it" but does not replace nodes or edges.
- Headings form a text tree — they express layout and reading context only. Being "under" a heading does NOT imply dependency, containment, backup, verification, or any other graph relation.
- Real graph relations live only in the `→ [...]` at the end of each node line. Cross-heading, cross-directory, cross-project, cross-theme relations should always be written as edges, not as scattered prose.
- Use the typed prefixes above (`dep:`, `emb:`, `bak:`, `agg:`, `ver:`, `ext:`, `up:`, `rel:`, `loc:`, `run:`, `cfg:`, `out:`, `in:`, `doc:`, `todo:`); fall back to `rel:` only when no typed prefix fits.
- When uncertain about which edge type to use, copy the precedent from the "Example" column of the table rather than inventing a new interpretation; only consider adding a new type when no example reasonably fits.
- If A has an edge to B and the relation isn't explicitly one-way (`bak:`, `up:`, `out:`, `in:`), B should have a matching edge back to A — keep the graph bidirectional and traceable.
- Node content describes "what this node is"; edges describe "how it relates to other nodes". Don't hide relations inside node descriptions.
- When adding a memory, first decide which `#` domain it belongs to, then place it in the closest existing `###` group; if no group fits, add a new heading — but don't create deep heading hierarchies for a single fact.
- When the same entity appears in different machines, paths, or build forms, prefer separate nodes connected by `rel:` / `loc:` / `out:` / `dep:` / `emb:` edges, rather than mashing multiple environments into one untraceable description.
- When updating existing facts, prefer editing the existing node and its edges; create new nodes only when a genuinely new entity, output, config, task, or doc appears.
- Before writing, check whether related nodes already have reverse edges; if not and the relation is not one-way, add the reverse edge to keep the graph from fragmenting.



# Sample Project Graph — Example Domain

> Replace this whole `#` section with your own domains. Keep the schema and maintenance rules above intact; the curator and dreamer skills both treat that preamble as authoritative.

## Nodes

### sample-projects — Example projects

- `proj-web` web-app — example frontend, TypeScript + Vite, calls `proj-api` for data → [dep:lib-utils, agg:proj-deploy]
- `proj-api` api-server — example backend service, Python + FastAPI → [dep:lib-utils, dep:db-postgres, agg:proj-deploy]
- `proj-deploy` deploy-bundle — production deployment package combining frontend and backend artifacts → [agg:proj-web, agg:proj-api]

### sample-libs — Example libraries

- `lib-utils` shared-utils — small utility library reused by both frontend and backend → [rel:proj-web, rel:proj-api]

### sample-infra — Example infrastructure

- `db-postgres` postgres-db — PostgreSQL database used by the API → [rel:proj-api]

---



# User Pending Task and Thoughts (user-edited only — AI agents must not modify this section)
