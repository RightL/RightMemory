# Shared Views Usage Guide

Shared views let one RightMemory root expose a scoped collaboration surface to another root. Use them when a teammate, project, team space, or agent root owns context that your local memory should be able to retrieve without treating the provider's private memory as local files.

The provider owns the view. The consumer records a local `M#` heading that explains why that view matters in the consumer's own work.

## Before You Start

Decide which memory root is the provider and which one is the consumer. If you use profiles, pass `--profile <name>` on each command. If the current project has a `.rightmemory-profile`, the command can use that profile automatically.

Pick a stable view id, such as `alice-auth-api`. The same id normally becomes the provider view id, the exported package name, and the consumer's local `M#` heading id.

## Quick Start

Provider root:

```bash
rightmemory --profile alice shared-view define alice-auth-api \
  --title "Alice Auth API" \
  --description "Auth API collaboration context" \
  --maintainer Alice \
  --audience "frontend agents integrating login" \
  --instructions "Answer API contract questions from this view. Keep unrelated private notes out." \
  --term auth \
  --term token

rightmemory --profile alice shared-view build alice-auth-api
rightmemory --profile alice shared-view export alice-auth-api --target /tmp/alice-auth-api-view
```

Consumer root:

```bash
rightmemory --profile frontend shared-view accept-invite /tmp/alice-auth-api-view
rightmemory --profile frontend shared-view retrieve alice-auth-api "When does the auth token expire?"
rightmemory --profile frontend shared-view note alice-auth-api --confirm --task "login migration" \
  "The shared view answers token expiry but not refresh behavior."
```

That creates a local `M#` heading in the consumer's `MEMORY.md`, records resolver metadata in `shared_views.toml`, and stores cache and interaction records under `.runtime/shared_views/`.

## Provider Workflow

### 1. Define The View

`define` creates provider-owned files under `shared_views/<view-id>/`:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  export.toml
  dist/
```

Use `--term` to name the scope that may be exported from `MEMORY*.md`. Add `--instructions` when the view needs policy-guided retrieval behavior.

```bash
rightmemory --profile alice shared-view define alice-auth-api \
  --title "Alice Auth API" \
  --description "Auth API handoff context for frontend login work" \
  --maintainer Alice \
  --instructions "Focus on auth API contracts, test fixtures, and integration risks." \
  --term auth \
  --term token
```

By default, the builder reads active `MEMORY*.md` files, excluding skill files. Use `--source <glob>` if this view should use a narrower source set. Use `--include-all` for a deliberately broad view.

### 2. Build And Inspect

`build` materializes a filtered Markdown preview in `shared_views/<view-id>/dist/`.

```bash
rightmemory --profile alice shared-view build alice-auth-api
```

For a one-off export scope, add a query:

```bash
rightmemory --profile alice shared-view build alice-auth-api --query "refresh token expiry"
```

If the build says it requires `--term`, `--query`, or `--include-all`, add explicit scope before publishing. That guard exists so broad private memory does not get exported by accident.

### 3. Export A Package

Use package export when you want to send a folder to another person or agent.

```bash
rightmemory --profile alice shared-view export alice-auth-api --target /tmp/alice-auth-api-view
```

The package contains `view.md`, optional `retriever.md`, `export.toml`, generated `dist/`, and `rightmemory-shared-view.toml`. The consumer can accept either the package directory or the invitation TOML inside it.

When refreshing an existing package, use `--replace`:

```bash
rightmemory --profile alice shared-view export alice-auth-api --target /tmp/alice-auth-api-view --replace
```

`--replace` is guarded so it works for an existing shared-view package target and refuses dangerous targets such as a repository root.

### 4. Publish To A Local Hub

Use a hub when a local team wants a shared directory of view packages and invitations.

```bash
rightmemory --profile alice shared-view publish alice-auth-api --hub /shared/rightmemory-hub --replace
```

The package is published under `/shared/rightmemory-hub/views/alice-auth-api`, and the invitation appears at `/shared/rightmemory-hub/invitations/alice-auth-api.toml`.

Notes sent through a hub are written under the hub's `interactions/` directory. Provider-local `shared-view inbox` reads the provider root's `.runtime/shared_views/inbox/`, so hub records may need a separate team process until a hub inbox command exists.

### 5. Publish To An HTTP Hub

Use the HTTP hub when the shared view needs a network URL rather than a mounted folder. The hub root is separate infrastructure, while provider and consumer memory roots keep their own `MEMORY.md`, `shared_views.toml`, and runtime credential state.

```bash
rightmemory hub init /srv/rightmemory-hub --public-base-url http://hub.local:8765
rightmemory hub token create /srv/rightmemory-hub --provider alice --label publish
rightmemory hub serve /srv/rightmemory-hub --host 0.0.0.0 --port 8765
```

Store the printed provider token in the provider memory root through a hidden prompt, then publish by credential id:

```bash
rightmemory --profile alice shared-view credential set alice-publish \
  --kind http-publish \
  --hub-url http://hub.local:8765 \
  --provider alice \
  --token-prompt

rightmemory --profile alice shared-view publish-http alice-auth-api \
  --hub-url http://hub.local:8765 \
  --credential-id alice-publish
```

The same provider flow is available in Web Studio: save the HTTP hub credential in Settings, then publish from the Shared Views panel with the credential id.

HTTP invitations are accepted with the normal invitation command:

```bash
rightmemory --profile frontend shared-view accept-invite http://hub.local:8765/i/<invite-token>
```

## Consumer Workflow

### 1. Accept An Invitation

Accept a package directory:

```bash
rightmemory --profile frontend shared-view accept-invite /tmp/alice-auth-api-view
```

Accept an invitation file:

```bash
rightmemory --profile frontend shared-view accept-invite /tmp/alice-auth-api-view/rightmemory-shared-view.toml
```

Accepting a package copies it into the consumer's `.runtime/shared_views/imports/<heading-id>/` by default. That gives the consumer a local snapshot and keeps the transport details out of `MEMORY.md`.

Use overrides when the local heading should differ from the provider id:

```bash
rightmemory --profile frontend shared-view accept-invite /tmp/alice-auth-api-view \
  --heading-id auth-api \
  --title "Backend Auth API" \
  --body "Use this before changing frontend login or token refresh behavior."
```

### 2. Retrieve From The View

Pass the local heading id and a concrete query:

```bash
rightmemory --profile frontend shared-view retrieve alice-auth-api "How should login refresh tokens work?"
```

The result reports whether it used fresh backing or cache, where the view came from, and the matching shared-view lines. The consumer retrieves through the shared-view endpoint; it does not inspect the provider's private memory root.

### 3. Leave A Note

Use notes for handoff questions, stale docs, or collaboration feedback.

```bash
rightmemory --profile frontend shared-view note alice-auth-api --confirm --task "login migration" \
  "The shared view does not mention refresh token rotation."
```

For human or external relationships, the first call without `--confirm` returns a confirmation prompt instead of sending. Re-run with `--confirm` once the message is intentional.

Check local note records:

```bash
rightmemory --profile frontend shared-view notes alice-auth-api
```

A note to a package snapshot is queued locally because a static package has no live inbox. Notes to a local provider root are delivered to the provider root's inbox when that root is reachable.

### 4. List Connections

```bash
rightmemory --profile frontend shared-view list
```

This shows the consumer's accepted shared-view headings, relationship type, maintainer, and description.

## Manual Connections

`accept-invite` is the normal path. Use manual `accept` when you already know the target and want to wire it directly.

Package target:

```bash
rightmemory --profile frontend shared-view accept alice-auth-api \
  --title "Alice Auth API" \
  --body "Use this for auth API handoff context." \
  --ref rightmemory://view/alice-auth-api \
  --package /tmp/alice-auth-api-view
```

Reachable provider root:

```bash
rightmemory --profile frontend shared-view accept alice-auth-api \
  --title "Alice Auth API" \
  --body "Use this for auth API handoff context." \
  --ref rightmemory://view/alice-auth-api \
  --provider-root /Users/alice/.rightmemory
```

Hub target:

```bash
rightmemory --profile frontend shared-view accept alice-auth-api \
  --title "Alice Auth API" \
  --body "Use this for auth API handoff context." \
  --ref rightmemory://view/alice-auth-api \
  --hub /shared/rightmemory-hub
```

Choose one target option: `--package`, `--provider-root`, or `--hub`.

## Updating A Shared View

Provider updates usually follow this rhythm:

```bash
rightmemory --profile alice shared-view build alice-auth-api
rightmemory --profile alice shared-view export alice-auth-api --target /tmp/alice-auth-api-view --replace
```

Consumers that accepted a copied package can accept the refreshed package again:

```bash
rightmemory --profile frontend shared-view accept-invite /tmp/alice-auth-api-view
```

The local `M#` heading stays focused on collaboration meaning. The refreshed package replaces the imported runtime copy.

## Troubleshooting

Build requires explicit scope:

Add `--term` when defining the view, pass `--query` to `build`, `export`, or `publish`, or use `--include-all` when the view is intentionally broad.

Retrieve says a query is required:

`retrieve` needs a concrete question after the heading id.

Note says confirmation is required:

For human and external relationships, repeat the note command with `--confirm` after checking the message.

Note is queued:

The target is a static package, missing, or not reachable. The local note record still exists under `.runtime/shared_views/interactions/`.

Consumer sees stale package content:

Re-export from the provider with `--replace`, then accept the refreshed package again from the consumer.

## Command Reference

```bash
rightmemory shared-view list
rightmemory shared-view define <view-id> --title "View Title" --term keyword
rightmemory shared-view build <view-id> [--query "..."] [--context-lines N] [--limit N]
rightmemory shared-view export <view-id> --target <package-dir> [--query "..."] [--replace]
rightmemory shared-view publish <view-id> --hub <hub-dir> [--query "..."] [--replace]
rightmemory shared-view credential set <credential-id> --kind http-publish --hub-url <url> --provider <provider-id> --token-prompt
rightmemory shared-view credential set <credential-id> --kind http-publish --hub-url <url> --provider <provider-id> --token-stdin
rightmemory shared-view publish-http <view-id> --hub-url <url> --credential-id <credential-id> [--query "..."]
rightmemory shared-view accept-invite <package-or-invitation>
rightmemory shared-view accept-invite <http-invitation-url>
rightmemory shared-view accept <heading-id> --title "Title" --body "..." --ref <ref> --package <package-dir>
rightmemory shared-view accept <heading-id> --title "Title" --body "..." --ref <ref> --provider-root <root>
rightmemory shared-view accept <heading-id> --title "Title" --body "..." --ref <ref> --hub <hub-dir>
rightmemory shared-view retrieve <heading-id> "query"
rightmemory shared-view note <heading-id> [--confirm] [--task "..."] "message"
rightmemory shared-view notes [heading-id]
rightmemory shared-view inbox [view-id]
rightmemory shared-view inbox-http --hub-url <url> --credential-id <credential-id> --provider <provider-id>

rightmemory hub init <hub-root> [--admin-token <token>] [--public-base-url <url>]
rightmemory hub status <hub-root>
rightmemory hub token list <hub-root>
rightmemory hub token create <hub-root> --provider <provider-id> [--label <label>]
rightmemory hub token revoke <hub-root> <token-id>
rightmemory hub serve <hub-root> [--host 127.0.0.1] [--port 8765]
```
