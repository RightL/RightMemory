# Web Studio Share-First Design

## Summary

RightMemory now has a first-class `share` relationship workflow in the CLI, but Web Studio still presents the older low-level `MF#` / `MQ#` machinery. The current `Shared Views` page asks users to build file views, build question views, approve individual views, create file invitations, publish question invitations, accept views, then pull or ask.

That is accurate to the implementation, but it is not the product model users should learn. The Web Studio sharing surface should become share-first:

```text
Create share -> review generated scope -> revise or approve -> publish -> copy invite
Join share -> pull context -> ask questions or send note
```

`MF#` and `MQ#` remain implementation primitives and advanced tools.

## Goals

- Make the normal Web Studio sharing workflow match `rightmemory share create / approve / publish / join / status`.
- Let providers create a share from one natural-language description.
- Show the builder final message clearly, because it explains what was selected and what changed.
- Support a simple natural-language revision loop before approval.
- Let consumers join with one bundled invite URL.
- Keep the existing low-level shared-view operations available under an advanced section.
- Keep CLI and Web behavior aligned by using the same backend functions.

## Non-Goals

- Do not build a full chat UI.
- Do not add a custom conversation-history store.
- Do not make diagnostics or doctor output the main UX.
- Do not redesign Hub Console now.
- Do not remove existing `shared-view` CLI commands or low-level service methods.
- Do not design curated shared artifacts in this change.

## Current Web Studio Behavior

The current `Shared Views` page is implementation-first. It exposes separate panels for:

- build file view;
- build question view;
- approve view;
- create file invitation;
- publish question invitation;
- accept view;
- pull, status, ask, and note;
- provider inbox;
- publish events.

This is useful for debugging, but it makes ordinary sharing feel like eight separate operations. It also hides the useful agent final message behind a generic command result.

## Target User Model

Users should think in terms of one relationship:

```text
Share auth API context with frontend agents.
```

That relationship may include:

- file context, backed by `MF#`;
- live questions, backed by `MQ#`;
- both capabilities together, published through one bundled invite.

The UI should make the relationship and its capabilities the visible object. The underlying `MF#` / `MQ#` parts should be visible only as advanced details.

## Provider Flow

The provider starts from one natural-language prompt, plus an optional capability constraint:

```text
Share auth API context with frontend agents. Include stable integration docs and allow live questions.
```

The default capability is `Auto`. Users can also explicitly constrain the share to:

- file context only;
- live questions only;
- both file context and live questions.

This is a user-level capability choice, not an `MF#` / `MQ#` implementation choice. If the user does not specify it, the share-level builder agent decides the capability from the request.

Generation should be one share-level builder operation. It is not a Python planner followed by a second builder pass. Web Studio sends the natural-language request and setup fields to the builder agent, and the agent decides:

- `share_id`, such as `auth-api`;
- title, such as `Auth API`;
- capability, such as file context, live questions, or both;
- file intent;
- question intent;
- view ids, such as `auth-api-files` and `auth-api-ask`.

The agent must call builder/compiler tools to write canonical share and shared-view artifacts. It must not hand-write `shares.toml`, `recipe.toml`, or `question.toml`. Python tools remain responsible for validation and canonical file writes.

The user should only need to fill setup fields that cannot be inferred:

- hub credential, if no usable credential is saved;
- question base URL, if live questions are selected and no endpoint is known.

After generation, Web Studio shows a review panel:

```text
Builder summary
<agent final message>

Generated file context
<preview>

Generated question scope
<preview>

[Revise] [Approve] [Publish]
```

Approval remains explicit. Publishing creates one bundled invite and exposes a copyable invite URL.

## Revision Flow

Revision is intentionally small. The user can type one natural-language correction:

```text
Include the profile endpoint, but exclude deployment notes.
```

Web Studio sends that correction to the same share-level builder session for the same share id. The agent decides which underlying artifacts need to change. The user can optionally constrain capability again, but does not need to target a file part or question part.

The existing runtime history handles context:

- standalone mode uses Pydantic AI message history under `.runtime/sessions/...`;
- CLI-agent mode stores and resumes Codex or Claude provider sessions under `.runtime/agent_cli_sessions/...`;
- the share builder should use a stable session id derived from the share id.

No separate chat transcript or build-history file is needed for this version.

After revision, Web Studio refreshes:

- builder final message;
- generated preview;
- capability status;
- available actions.

## Consumer Flow

The consumer path should be one field:

```text
Invite URL
```

After `Join`, Web Studio should:

- accept the bundled invite;
- create the local share relationship;
- create underlying `MF#` / `MQ#` connections;
- pull file context immediately when present;
- show question readiness as ready, unreachable, or unavailable;
- show actions on the joined share card: Pull, Ask, Status, Send Note.

## Page Structure

Rename or reframe the current `Shared Views` page as a share-first page.

Top-level sections:

- Create Share;
- My Shared Shares;
- Joined Shares;
- Join Share;
- Advanced Shared View Tools.

Share cards should show:

- title and share id;
- role: provider or consumer;
- provider id;
- state: draft, approved, published, joined;
- capabilities: file context, live questions, or both;
- file-context status;
- live-question status;
- last invite URL when available from runtime state;
- concise next action.

The advanced section can contain the current low-level panels, collapsed by default.

## CLI Alignment

CLI should show the builder final message clearly.

`rightmemory share create ...` should print:

```text
created share auth-api

Builder summary:
<agent final message>

Next:
rightmemory share approve auth-api
```

Add a matching revision command:

```bash
rightmemory share revise auth-api "include profile endpoint, exclude deployment notes"
```

The revision command can accept an optional capability constraint, but it should not require the user to target `file` or `question` internals.

It should print:

```text
revised share auth-api

Builder summary:
<agent final message>

Next:
rightmemory share approve auth-api
```

Web Studio and CLI should call the same share service functions so generation, revision, validation, and returned summaries stay consistent.

## Architecture

Add share-level service methods rather than duplicating CLI behavior in JavaScript:

- list share relationships with derived capability statuses;
- expose user-facing capability statuses derived from underlying `MF#` / `MQ#` state;
- create a share from a natural-language prompt, optional capability constraint, and setup fields;
- revise a share with a natural-language correction;
- approve a share;
- publish a share;
- join a share;
- pull or status a joined share;
- ask or note through a joined share.

The existing `shares.toml` remains the durable relationship registry. Existing `shared_views.toml`, provider view files, question files, credentials, and runtime invite records remain the lower-level storage.

Add a share-level builder entry point on top of the existing shared-view builder role. The builder agent should receive the share request, decide the capability when the user chose `Auto`, and call tools that write canonical share and shared-view artifacts. The clean tool boundary is:

- agent owns semantic decisions;
- Python tools own validation and file writes;
- neither Web JavaScript nor CLI code hand-builds TOML.

The Web API should return structured data for the UI and include the builder final message as a first-class field, not only as a generic text response. The result should include the share id, title, state, selected capability, builder final message, preview references or status summaries, invite URL when available, and next action. CLI formats the same data as text; Web Studio renders it directly.

## Error Handling

Errors should remain action-oriented:

- missing credential: ask the user to pick or save a credential;
- missing question base URL: ask only if question access is selected;
- unapproved share: offer Approve;
- empty file context: block approval and show the builder summary plus preview;
- hub unavailable: show publish or join failure near the share card;
- question endpoint unreachable: show question status as unreachable without hiding file context.

## Testing

Tests should cover:

- Web service share list returns provider and consumer share cards;
- Web service create share returns builder summaries, capability selection, and generated artifact metadata;
- Web service create share supports default `Auto` capability and explicit file-context, live-question, and both constraints;
- Web service revise uses the same share-level builder session id and returns the new final message;
- CLI `share create` includes builder final messages;
- CLI `share revise` updates existing generated artifacts and includes builder final messages;
- Web UI renders share-first sections and keeps advanced shared-view tools collapsed;
- consumer join with a bundled invite creates one share card with file-context and live-question statuses;
- existing low-level shared-view APIs continue to work.

## Later Work

Hub Console can later add a `Shares` tab that groups hosted records by bundled share invitation. That is useful for operators, but it is not the next main UX improvement. The normal user workflow should improve first in Web Studio.
