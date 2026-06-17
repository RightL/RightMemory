# Shared Views Usage

Shared views let one RightMemory root use collaboration context owned by another root without reading the provider's private memory root.

There are two sharing modes:

- `MF#`: mirrored file context. The provider publishes a scoped file package to an HTTP hub. The consumer accepts an invitation, pulls the package into `.runtime/shared_views/imports/<mf-id>/`, and retrieve reads the mirrored files with normal file tools.
- `MQ#`: live provider questions. The provider publishes a question view invitation. The consumer accepts it, stores a provider question token, and asks the provider Web Studio endpoint synchronously when needed.

All normal sharing goes through HTTP, even on one machine. Direct provider filesystem access, mounted provider roots, local package invitations, and generic `M#` shared-view headings are not part of the product path.

## Scenario

Assume Alice owns provider memory and a frontend agent consumes selected auth API context.

Alice runs:

```bash
rightmemory --profile alice ...
```

The frontend consumer runs:

```bash
rightmemory --profile frontend ...
```

They also use an HTTP hub for invitations and file packages:

```bash
rightmemory hub init --public-base-url http://127.0.0.1:8765
rightmemory hub token create --provider alice --label publish
rightmemory hub serve --host 127.0.0.1 --port 8765
```

Alice stores the printed provider token in her memory root:

```bash
rightmemory --profile alice shared-view credential set alice-publish \
  --kind http-publish \
  --hub-url http://127.0.0.1:8765 \
  --provider alice \
  --token-prompt
```

Tokens are stored under `.runtime/shared_views/credentials.toml`.

## Share Stable Context With `MF#`

Use `MF#` when the consumer should retrieve from a scoped, mirrored snapshot of provider context.

Alice builds a file view from natural language:

```bash
rightmemory --profile alice shared-view build-file auth-api-files \
  "Expose auth API integration context for frontend agents" \
  --title "Auth API Files" \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish
```

This creates provider-owned source files:

```text
shared_views/auth-api-files/
  view.md
  recipe.toml
  .gitignore
```

Alice reviews the recipe, then approves it:

```bash
rightmemory --profile alice shared-view approve auth-api-files --type file
```

Alice creates an invitation:

```bash
rightmemory --profile alice shared-view invite auth-api-files --label frontend
```

`invite` publishes the current file-view package to the hub and prints an invitation URL. After approval, future successful memory-write roles also rebuild and publish approved file views automatically. Generated `dist/` output is runtime output and should not be committed.

The frontend consumer accepts the invitation:

```bash
rightmemory --profile frontend shared-view accept-invite http://127.0.0.1:8765/i/<invite-token>
```

This creates an `MF#` heading in `MEMORY.md`, stores resolver metadata in `shared_views.toml`, and stores the hub connection token in `.runtime/shared_views/credentials.toml`.

The frontend can pull or inspect manually:

```bash
rightmemory --profile frontend shared-view pull auth-api-files
rightmemory --profile frontend shared-view status auth-api-files
```

Retrieve also pulls accepted `MF#` views before the retrieve agent starts:

```bash
rightmemory --profile frontend retrieve --session codex-frontend \
  "Find auth API context for login token expiry"
```

The pull result is intentionally not added to retrieve session history, which keeps cache hits stable.

## Ask Live Questions With `MQ#`

Use `MQ#` when the consumer needs a temporary answer from the provider's current private memory and should not receive a mirrored file package.

Alice starts Web Studio so the provider question endpoint is reachable:

```bash
rightmemory --profile alice web start --host 127.0.0.1 --port 8766
```

Alice builds a question view:

```bash
rightmemory --profile alice shared-view build-question auth-api-ask \
  "Let frontend agents ask temporary auth API questions" \
  --title "Auth API Questions"
```

This creates:

```text
shared_views/auth-api-ask/
  view.md
  retriever.md
  question.toml
```

`retriever.md` is provider-side only. It is the prompt used when Alice's root receives a live question.

Alice reviews and approves the question view:

```bash
rightmemory --profile alice shared-view approve auth-api-ask --type question
```

Alice publishes a question invitation:

```bash
rightmemory --profile alice shared-view publish-question auth-api-ask \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --question-base-url http://127.0.0.1:8766 \
  --label frontend
```

This registers `MQ#` metadata with the hub, generates a raw question token, stores only its hash in `question.toml`, and prints an invitation URL.

The frontend accepts the invitation:

```bash
rightmemory --profile frontend shared-view accept-invite http://127.0.0.1:8765/i/<invite-token>
```

The consumer stores two separate credentials:

- the hub connection token, used for notes and interaction records;
- the provider question token, used only for live asks.

When retrieve reports that an `MQ#` connection is relevant, the main agent can ask explicitly:

```bash
rightmemory --profile frontend shared-view ask auth-api-ask \
  "How should login refresh tokens work?"
```

The result is either answered or unavailable. Failed asks do not create queued notes.

## Notes And Provider Inbox

Notes are explicit, one-way interactions. They can target either `MF#` or `MQ#` connections.

The frontend sends a note:

```bash
rightmemory --profile frontend shared-view note auth-api-files \
  --confirm \
  --task "frontend login migration" \
  "Docs are missing token_expires_at."
```

Alice checks the provider inbox through the hub:

```bash
rightmemory --profile alice shared-view inbox-http \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --provider alice
```

Local notes and local inbox records can also be inspected with:

```bash
rightmemory --profile frontend shared-view notes
rightmemory --profile alice shared-view inbox
```

## Operational Checks

Check the default hub root:

```bash
rightmemory hub status
```

List accepted shared-view connections:

```bash
rightmemory --profile frontend shared-view list
```

Pull all accepted file views:

```bash
rightmemory --profile frontend shared-view pull
```

Check all accepted connection statuses:

```bash
rightmemory --profile frontend shared-view status
```

Automatic file-view publish events are appended to:

```text
.runtime/shared_views/publish-events.jsonl
```

If an automatic publish fails, the approved file view remains locally renderable, but consumers only see the last successfully published package until the provider fixes the hub or credential problem and republishes.

## Web Studio

Web Studio exposes the main provider and consumer flow:

- save HTTP hub credentials without showing stored tokens;
- build, approve, and invite `MF#` file views;
- build, approve, and publish `MQ#` question views;
- accept HTTP invitations;
- pull or status one `MF#` connection;
- pull all `MF#` connections;
- status all shared-view connections;
- ask one `MQ#` connection;
- send provider-visible notes;
- Provider Inbox: read the provider HTTP inbox for the active provider root;
- Auto-Publish Events: inspect recent `MF#` auto-publish events.

Web Studio does not initialize or serve the hub, create global provider hub tokens, manage every provider on a hub, revoke invitations or accepted connections, show the full hub audit log, or expose generic shared-view retrieval. Use the CLI commands above for bootstrap operations. Use Hub Console for hub-wide runtime administration.

Hub Console is available at `/console` on the running hub service. It is the admin/operator surface for runtime hub state: health, providers, tokens, views, invitations, accepted connections, inbox records, and audit events. It does not build shared views or edit memory roots; use Web Studio for provider and consumer workflows.

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
.runtime/shared_views/credentials.toml
.runtime/shared_views/imports/
.runtime/shared_views/notes/
.runtime/shared_views/inbox/
.runtime/shared_views/publish-events.jsonl
shared_views/<view-id>/dist/
```
