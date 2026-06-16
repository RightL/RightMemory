# Shared Views Usage

Shared views let one RightMemory root use collaboration context owned by another root without treating the provider's private memory as local memory. The current model has two explicit kinds:

- `MF#`: mirrored file view. The provider publishes a scoped file package over HTTP. The consumer syncs that package into `.runtime/shared_views/imports/<mf-id>/`, and ordinary retrieve reads it with normal file tools.
- `MQ#`: provider question view. The consumer asks a live provider-side question over HTTP. This is synchronous ask-or-unavailable, not a queued note flow.

All normal transport is HTTP, even when provider and consumer are on the same machine. Direct provider filesystem access and mounted-folder hub flows are not part of the current product path.

## Provider: Build an `MF#` File View

Create a file-view recipe from natural language:

```bash
rightmemory --profile alice shared-view build-file auth-api-files \
  "Expose auth API integration context for frontend agents" \
  --title "Auth API Files" \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish
```

The builder writes:

```text
shared_views/auth-api-files/
  view.md
  recipe.toml
  .gitignore
```

Review the files, then approve:

```bash
rightmemory --profile alice shared-view approve auth-api-files --type file
```

Approved file views rebuild and publish automatically after successful memory-write roles. Generated `dist/` output is not committed.

## Provider: Build an `MQ#` Question View

Create a question-view endpoint from natural language:

```bash
rightmemory --profile alice shared-view build-question auth-api-ask \
  "Let frontend agents ask temporary auth API questions" \
  --title "Auth API Questions"
```

The builder writes:

```text
shared_views/auth-api-ask/
  view.md
  retriever.md
  question.toml
```

`retriever.md` is provider-side only. It is the prompt used when the provider receives a live question. Approve the question view after review:

```bash
rightmemory --profile alice shared-view approve auth-api-ask --type question
```

## HTTP Hub Setup For `MF#`

Initialize and serve a hub:

```bash
rightmemory hub init ./rightmemory-hub --public-base-url http://127.0.0.1:8765
rightmemory hub token create ./rightmemory-hub --provider alice --label publish
rightmemory hub serve ./rightmemory-hub --host 127.0.0.1 --port 8765
```

Store the printed provider token in the provider memory root:

```bash
rightmemory --profile alice shared-view credential set alice-publish \
  --kind http-publish \
  --hub-url http://127.0.0.1:8765 \
  --provider alice \
  --token-prompt
```

The file-view recipe stores the hub URL and credential id. Tokens stay in `.runtime/shared_views/credentials.json`.

## Consumer: Accept And Use `MF#`

Accept an HTTP invitation:

```bash
rightmemory --profile frontend shared-view accept-invite http://127.0.0.1:8765/i/<invite-token>
```

This creates an `MF#` heading in `MEMORY.md` and stores resolver metadata in `shared_views.toml`. Pull manually when you want to inspect the mirror immediately:

```bash
rightmemory --profile frontend shared-view pull auth-api-files
rightmemory --profile frontend shared-view status auth-api-files
```

Ordinary retrieve also pulls accepted `MF#` views before the retrieve agent starts:

```bash
rightmemory --profile frontend retrieve --session codex-frontend \
  "Find auth API context for login token expiry"
```

The sync result is intentionally not added to retrieve session history, which keeps cache hits stable.

## Consumer: Ask `MQ#`

An `MQ#` invitation must point at the provider's Web Studio question endpoint, not at a file-package hub. When retrieve reports relevant provider-question context, the main agent can ask the provider explicitly:

```bash
rightmemory --profile frontend shared-view ask auth-api-ask \
  "How should login refresh tokens work?"
```

The ask command returns either an answer or an unavailable result. It does not create a queued note automatically.

## Notes And Inbox

Notes are explicit, one-way interactions and can target either `MF#` or `MQ#` connections:

```bash
rightmemory --profile frontend shared-view note auth-api-files \
  --confirm \
  --task "frontend login migration" \
  "Docs are missing token_expires_at."

rightmemory --profile alice shared-view inbox-http \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --provider alice
```

Failed `MQ#` asks do not become notes. Send a note only when the user or agent intentionally wants to tell the provider something.

## Web Studio

Web Studio exposes the same flow:

- Build File View
- Build Question View
- Approve View
- Accept HTTP Invitation
- Pull `MF#`
- Ask `MQ#`
- Send Note

The UI does not expose generic shared-view retrieval, direct provider filesystem access, local package invitations, or mounted-folder publication.

## File Ownership

Commit source files:

```text
shared_views/<view-id>/view.md
shared_views/<view-id>/recipe.toml
shared_views/<view-id>/question.toml
shared_views/<view-id>/retriever.md
shared_views/<view-id>/.gitignore
shared_views.toml
```

Do not commit runtime state:

```text
.runtime/shared_views/credentials.json
.runtime/shared_views/imports/
.runtime/shared_views/notes/
.runtime/shared_views/inbox/
shared_views/<view-id>/dist/
```
