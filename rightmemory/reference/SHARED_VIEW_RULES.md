# Shared View Rules

Shared views connect a local memory root to collaboration context owned by another person, project, team space, or agent root. Use an `MF#` heading when this root should use provider-published mirrored files as external read context. Use an `MQ#` heading when this root may ask a provider-side retriever a synchronous question. In both cases, the heading body explains the relationship in local terms: what the view represents, when it helps, and how it relates to nearby work. Store resolver details in `shared_views.toml`, not in memory prose.

A published version-two file-view package has this layout:

```text
shared_views/<view-id>/
  view.md
  recipe.toml
  rightmemory-shared-view.toml
  dist/
    MEMORY.md
    MEMORY_<id>.md
    MEMORY_SKILL_<id>.md
    manifest.toml
```

Every included graph item must be interpretable from local reading context
contained in the package. Provider-only ancestry and resolver state outside
the package are not implicit consumer context.

`view.md` and `recipe.toml` are provider-owned file-view source files. The recipe records the approved source headings, nodes, or files chosen by the builder. The generated `dist/` directory is preview or publishing output; it is not active provider memory. A version-two `dist/MEMORY.md` is a schema-valid, Memory-only RightMemory document, not arbitrary Markdown. It has an id namespace local to that view, so its ids do not collide with local ids or ids in another view, and its edges remain inside that namespace.

An MF document may use ordinary headings and nodes plus F#, M#, and S# headings. Its F# and M# files live at `dist/MEMORY_<id>.md`; its S# files live at `dist/MEMORY_SKILL_<id>.md`. F# content participates in the MF graph, M# remains free-form evidence, and S# remains a complete instruction resource that is never installed or executed automatically. Every typed heading needs its package-local backing, and unreferenced backing files are invalid. Plain headings may group addressable descendants, but arbitrary prose under an unaddressable wrapper is invalid. Nested MF# and MQ# headings are invalid because an imported package has no authority to resolve another live connection.

Selecting the local outer MF# heading returns its local relationship context only. Imported graph items are selected by id within the `MF#<view-id>` source, including items reached through F#. Direct ranges over `dist/MEMORY.md` are invalid. Imported M# ranges use a qualified source such as `MF#auth-api/M#incident-evidence`; imported S# uses a qualified source such as `MF#auth-api/S#review-checklist` and returns the complete instruction.

A provider root may define question views under `shared_views/<view-id>/`:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  question.toml
```

`retriever.md` belongs only to provider question views. It is the provider-side retrieval prompt used when an accepted consumer asks an `MQ#` question.

Consumers pull accepted `MF#` file views into `.runtime/shared_views/imports/<view-id>/` before retrieve runs, then inspect the validated graph and backing files through typed MF reads and structured selection. Consumers call `MQ#` question views through explicit ask commands or UI actions outside retrieve. Record local consequences in ordinary memory only when those consequences become durable.

Providers validate the complete version-two package before approval or publication. Consumers validate the exact downloaded candidate before atomically replacing a prior import. Invalid or version-one packages are unavailable unless a previously validated version-two import can remain as stale context.
