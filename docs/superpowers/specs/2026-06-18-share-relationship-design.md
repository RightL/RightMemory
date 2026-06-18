# Share Relationship Design

## Summary

RightMemory shared views currently expose too much setup plumbing to the user. A real provider/consumer workflow requires hub setup, publish credentials, file-view build and approval, file invitation, Web Studio startup, question-view build and approval, question invitation, consumer accept, pull, ask, and note/inbox commands.

The product fix is to make `share` the normal user-facing abstraction:

```bash
rightmemory share create auth-api
rightmemory share join <invite-url>
rightmemory share status auth-api
```

`MF#` and `MQ#` remain implementation primitives. A share relationship groups one or more shared-view parts into one relationship that can be created, approved, published, joined, inspected, and later extended.

The first version should be intentionally small:

- one durable `shares.toml` registry;
- optional file and question parts;
- one bundled hub invitation per share;
- flag-driven CLI-first create/join/status flow;
- low-level `shared-view` commands preserved as advanced commands.

## Goals

- Replace the normal many-step MF/MQ workflow with one provider flow and one consumer flow.
- Let a share contain `file`, `question`, or both parts.
- Give consumers one invite URL for the relationship.
- Keep provider approval explicit before publishing model-built sharing scope.
- Keep the current low-level MF/MQ implementation usable and testable.
- Leave enough runway for later Web Studio and agent-native flows without designing those now.

## Non-Goals

- Do not remove existing `rightmemory shared-view ...` commands in this change.
- Do not implement a broad diagnostic or doctor product as the primary UX.
- Do not design curated/sanitized shared artifacts yet.
- Do not redesign retrieve, memory governance, pruning, or node provenance.
- Do not make Web Studio the first implementation target.

## User Model

Users should think in terms of one relationship:

```text
Alice shares auth-api context with frontend.
```

The relationship may have file context, live question access, or both. The user should not need to remember whether the underlying implementation uses `recipe.toml`, `question.toml`, connection tokens, question tokens, package versions, or per-view invitations.

Low-level commands still exist for debugging, scripting, tests, and advanced workflows, but documentation should teach `share` as the normal path once it is available.

## Relationship Registry

Add a small durable registry at the memory root:

```text
shares.toml
```

This registry records user-facing share relationships. It is separate from `shared_views.toml`:

- `shares.toml` records relationships the local root provides or has joined.
- `shared_views.toml` records low-level accepted `MF#` / `MQ#` connections.

Example provider-side registry:

```toml
[shares.auth-api]
version = 1
role = "provider"
title = "Auth API"
provider_id = "alice"
hub_url = "http://127.0.0.1:8765"
credential_id = "alice-publish"
state = "draft"
parts = ["file", "question"]

[shares.auth-api.file]
view_id = "auth-api-files"
intent = "Expose auth API integration context for frontend agents"
approved = false

[shares.auth-api.question]
view_id = "auth-api-ask"
intent = "Let frontend agents ask temporary auth API questions"
question_base_url = "http://127.0.0.1:8766"
approved = false
```

Example consumer-side registry:

```toml
[shares.auth-api]
version = 1
role = "consumer"
title = "Auth API"
provider_id = "alice"
hub_url = "http://127.0.0.1:8765"
state = "joined"
parts = ["file", "question"]
accepted_from = "http://127.0.0.1:8765/i/share/<token>"

[shares.auth-api.file]
heading_id = "auth-api-files"

[shares.auth-api.question]
heading_id = "auth-api-ask"
```

The registry should stay small. It should not duplicate all recipe, question, credential, or package data. It only records the relationship and pointers to the underlying parts.

## Provider Flow

The provider creates a share:

```bash
rightmemory share create auth-api
```

The first implementation should be flag-driven for tests and automation. Interactive prompts can be added later on top of the same command.

Example:

```bash
rightmemory share create auth-api \
  --title "Auth API" \
  --provider alice \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --file "Expose auth API integration context for frontend agents" \
  --question "Let frontend agents ask temporary auth API questions" \
  --question-base-url http://127.0.0.1:8766
```

Behavior:

1. Validate or create the `shares.toml` relationship entry.
2. Build the file part if requested, using the existing file-view builder.
3. Build the question part if requested, using the existing question-view builder.
4. Leave generated parts unapproved.
5. Print a concise next step to review and approve.

Approval remains explicit:

```bash
rightmemory share approve auth-api
```

Approval should approve the requested parts after validating that each underlying source is canonical and nonempty where required.

Publishing creates one bundled invite:

```bash
rightmemory share publish auth-api --label frontend
```

Behavior:

1. Render and publish the file part if present.
2. Register the question part if present.
3. Create a bundled hub invitation for the share.
4. Print the bundled invitation URL.
5. Store the token-bearing invitation URL only in runtime state, not in `shares.toml`.

## Bundled Hub Invitation

The hub currently creates invitations per view. A share relationship needs one invitation for multiple optional parts.

Add a bundled invitation endpoint and storage model at the hub:

```text
POST /api/shares/{share_id}/invitations
GET  /api/share-invitations/{token}/view
POST /api/share-invitations/{token}/accept
```

The invitation payload should include:

```json
{
  "share_id": "auth-api",
  "title": "Auth API",
  "provider_id": "alice",
  "parts": [
    {"type": "file", "view_id": "auth-api-files"},
    {"type": "question", "view_id": "auth-api-ask"}
  ]
}
```

Accepting the invitation should create the same low-level connection tokens that individual MF/MQ accept would create today:

- one hub connection token per file part;
- one hub connection token plus one provider question token per question part.

The bundled invite should not replace per-view invites immediately. Per-view invites remain for low-level `shared-view` workflows.

## Consumer Flow

The consumer joins one relationship:

```bash
rightmemory share join http://127.0.0.1:8765/i/share/<token>
```

Behavior:

1. Fetch the bundled invitation description.
2. Show the provider, title, and parts.
3. Accept the invitation.
4. Create the local `shares.toml` consumer entry.
5. Create underlying `shared_views.toml` `MF#` and/or `MQ#` connections.
6. Store returned credentials under `.runtime/shared_views/credentials.toml`.
7. Pull the file part immediately if present.
8. Print a concise joined summary.

The consumer should be able to inspect status:

```bash
rightmemory share status auth-api
```

Status should summarize the relationship:

```text
auth-api provider=alice state=joined parts=file,question
file auth-api-files pulled
question auth-api-ask ready-or-unchecked
```

This status may reuse low-level status checks, but it should not be framed as a separate doctor product.

## Command Surface

Add:

```bash
rightmemory share create <share-id>
rightmemory share approve <share-id>
rightmemory share publish <share-id>
rightmemory share join <invite-url>
rightmemory share status [share-id]
rightmemory share list
```

`share approve` approves all parts in the share. A part-specific approval option can be added later only if real workflows need it.

Keep:

```bash
rightmemory shared-view build-file ...
rightmemory shared-view build-question ...
rightmemory shared-view approve ...
rightmemory shared-view invite ...
rightmemory shared-view publish-question ...
rightmemory shared-view accept-invite ...
rightmemory shared-view pull ...
rightmemory shared-view ask ...
```

The existing commands become advanced primitives. They should remain documented, but the normal walkthrough should move to the `share` flow after implementation.

## Error Handling

Errors should be action-oriented without making diagnostics the product:

- Missing publish credential: say which credential is missing and show the command to save it.
- Missing hub URL: ask for or require `--hub-url`.
- Unapproved part: fail publish and say `rightmemory share approve <share-id>` is required.
- Empty file recipe: fail approval.
- Hub unavailable: fail publish/join with concise hub error.
- Question endpoint missing: fail publish only if a question part is selected and no `question_base_url` is available.
- File pull failure during join: still record the relationship if credentials and connection were accepted, then report the file part as unavailable or stale.

Token-bearing bundled invitation URLs should not be committed. If the provider records them for convenience, store them under `.runtime/shares/`.

## Design Runway

This design intentionally keeps the durable model part-based:

```text
share relationship
  file part
  question part
```

That is enough runway for likely next steps:

- Web Studio can render `shares.toml` as guided share cards.
- Agent-native flow can call `share create`, `share approve`, and `share publish`.
- A future curated/sanitized artifact can become another file-part mode without changing the share concept.
- Drift handling can attach to the file part.
- Revocation can later operate at the share level or part level.
- Alternate transports can be represented later by adding transport fields to the share or part.

Do not implement those future features in this first change.

## Testing

Add focused unit tests for:

- loading, validating, and saving `shares.toml`;
- provider `share create` creating the expected relationship and underlying part source files;
- `share approve` validating and approving requested parts;
- `share publish` publishing selected parts and creating one bundled invite;
- hub bundled invitation describe and accept;
- consumer `share join` creating one relationship plus correct low-level connections;
- file-only, question-only, and file-plus-question shares;
- existing `shared-view` commands still working.

Add one realistic end-to-end simulation after unit coverage:

1. provider creates a file-plus-question share;
2. provider approves and publishes one bundled invite;
3. consumer joins with one URL;
4. consumer retrieves file context;
5. consumer asks the question part;
6. provider receives a note through the relationship.

## First-Version Decisions

- `share create` is flag-driven first; interactive prompts can be added later.
- Bundled invitation URLs use `/i/share/<token>`, visibly distinct from current per-view `/i/<token>` invitations.
- `share approve` approves all parts by default.
- Do not add part-specific approval until a real workflow needs it.
- Do not remove low-level `shared-view` commands in the first implementation.
