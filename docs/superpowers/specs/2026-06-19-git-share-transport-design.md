# Git Share Transport Design

## Summary

RightMemory should support Git as a clean alternate transport for public-network
file shares. The existing HTTP hub remains the rich collaboration transport for
bundled invitations, tokens, inboxes, audit records, and `MQ#` live questions.
Git transport is intentionally smaller: it publishes approved `MF#` file-share
packages into a normal Git repository so consumers can join from a copyable Git
URL without running or reaching an HTTP hub.

V1 is plaintext and file-only. Repository access is the privacy boundary.

## Goals

- Let a provider publish an approved file share through a public or private Git
  repository.
- Keep the user model centered on `share`, not on low-level `MF#` machinery.
- Use the existing natural-language share builder for file-context generation.
- Reuse the existing `MF#` package and import path so retrieve behavior stays
  unchanged.
- Push by default so Git publish normally makes the share available.
- Avoid Git-specific ceremony in the common path.

## Non-Goals

- Do not support `MQ#` live questions over Git.
- Do not add inbox, notes, accepted-connection tokens, audit records, or
  revocation workflows to Git mode.
- Do not add encryption in v1.
- Do not create a `versions/` directory; Git commits are the history.
- Do not replace the HTTP hub.

## Product Model

Git transport is an alternate publish/join path for file shares:

```bash
rightmemory share create auth-api \
  --request "Share auth API context" \
  --provider alice \
  --git https://github.com/user/rightmemory-shares.git
rightmemory share approve auth-api
rightmemory share publish auth-api
```

The current share-first CLI already uses `--request` for natural-language share
creation. Git transport should extend that flow instead of adding a separate
command. `--git <repo-url>` is mutually exclusive with `--hub-url` and
`--credential-id`; it creates a Git-targeted share and stores the Git target on
the share relationship. Because the target is Git, the builder is constrained to
file context only. It must not build `MQ#`. If the natural-language request asks
for live questions, the builder should fail with a clear message that Git
transport supports file context only.

Publish commits and pushes by default. Local-only publish is explicit:

```bash
rightmemory share publish auth-api --no-push
```

Branch selection is optional. When omitted, RightMemory uses the repository
default branch:

```bash
rightmemory share create auth-api \
  --request "Share auth API context" \
  --provider alice \
  --git https://github.com/user/rightmemory-shares.git \
  --branch gh-pages
```

Consumer join uses one clean URL:

```bash
rightmemory share join https://github.com/user/rightmemory-shares.git#share=auth-api
```

Optional branch selection stays in the URL fragment:

```text
https://github.com/user/rightmemory-shares.git#share=auth-api&branch=gh-pages
```

`share join` can infer Git transport from the `share` fragment. Existing HTTP
hub invite URLs keep their current behavior.

## Git Repository Layout

Each Git repository may host many shares:

```text
shares/
  auth-api/
    share.toml
    package/
      view.md
      recipe.toml
      rightmemory-shared-view.toml
      dist/
        MEMORY.md
        manifest.toml
      ...
```

`share.toml` is the public entry point for one share. It stays small and records
only share-level metadata plus the package location:

```toml
version = 1
share_id = "auth-api"
title = "Auth API"
provider_id = "alice"
transport = "git"
parts = ["file"]

[file]
view_id = "auth-api-files"
title = "Auth API Files"
description = "Stable integration context for frontend agents."
path = "package"
manifest = "package/dist/manifest.toml"
```

`package/` contains the canonical exported `MF#` package. It is intentionally a
package boundary, not another user-facing layout. `manifest.toml` remains
internal package metadata used for validation and import. Users should not need
to think about it.

Git commit history is the version history. Rollback, diff, provenance, and
forensics use normal Git operations rather than a separate package version tree.

## Provider Flow

Creation:

1. User runs `rightmemory share create ... --git <repo-url>`.
2. RightMemory invokes the existing share-level natural-language builder with
   capability forced to file context.
3. The builder creates or revises the normal share relationship and `MF#`
   artifacts through compiler tools.
4. The share records the Git target and optional branch.
5. The share remains draft until explicit approval.

For HTTP shares, the existing `--hub-url` and `--credential-id` fields remain
required. For Git shares, `--git` replaces those HTTP fields. `--provider`
remains required because it is share provenance, not transport configuration.

Approval:

1. User reviews generated file context.
2. `rightmemory share approve <share-id>` approves the underlying file view.

Publish:

1. RightMemory renders or validates the approved `MF#` package.
2. It clones or fetches the Git target into a runtime checkout.
3. It writes `shares/<share-id>/share.toml`.
4. It copies the file package into `shares/<share-id>/package/`.
5. It commits the share directory.
6. It pushes by default unless `--no-push` is provided.

Publishing a share that contains a question part fails before writing Git
content.

## Consumer Flow

Join:

1. User runs `rightmemory share join <git-url>#share=<share-id>`.
2. RightMemory clones or fetches the repository into runtime storage.
3. It reads `shares/<share-id>/share.toml`.
4. It validates that `transport = "git"` and `parts = ["file"]`.
5. It imports `package/` into the existing `MF#` import location:

```text
.runtime/shared_views/imports/<view-id>/
```

6. It writes normal local `shared_views.toml` connection metadata and a normal
   local `MF#` memory heading.

Retrieve stays unchanged. Before retrieve, RightMemory fetches the Git target
and refreshes the local import. The retrieve agent then reads imported files
with ordinary file tools through the existing `MF#` path.

If fetch fails and a previous import exists, retrieve may keep using the stale
local import, matching current `MF#` stale-cache behavior. If no import exists,
the file surface is unavailable.

## Configuration And State

Provider-side `shares.toml` should keep only the relationship and transport
pointer:

```toml
[shares.auth-api]
version = 1
role = "provider"
title = "Auth API"
provider_id = "alice"
state = "draft"
parts = ["file"]
transport = "git"
git_url = "https://github.com/user/rightmemory-shares.git"

[shares.auth-api.file]
view_id = "auth-api-files"
intent = "Share auth API context"
approved = false
```

If a branch is configured, store it as `git_branch`. If omitted, use the Git
remote's default branch.

Runtime checkouts live under RightMemory runtime storage, for example:

```text
.runtime/git_shares/<repo-hash>/
```

The exact hash format is an implementation detail. Runtime paths should be
derived from repository URL and branch so multiple shares in the same repo reuse
one checkout.

## Web Studio

Git transport belongs inside the existing share-first page. It should not get a
separate Git page or expose low-level Git operations as the primary workflow.

Create Share should add a transport choice:

- HTTP Hub
- Git Repo

When Git Repo is selected:

- force capability to file context only;
- hide live-question fields and `MQ#` options;
- show one required Git repository URL field;
- keep branch and no-push controls under Advanced;
- pass the Git target into the same share-level builder flow with capability
  forced to file context.

Provider share cards should show the transport and the useful Git state:

- `transport: Git`;
- repository URL;
- branch only when configured;
- last publish status;
- Copy Join URL;
- Publish to Git.

Publish to Git should push by default. A no-push option may exist under
Advanced for local testing, but it should not be the visible default action.

Join Share should keep one invite field. The same field accepts HTTP hub invite
URLs and Git join URLs such as:

```text
https://github.com/user/rightmemory-shares.git#share=auth-api
```

After join, consumer share cards should look like ordinary joined file shares,
with Git transport shown as provenance and Pull using the Git fetch/import path.

## Error Handling

- Git-targeted create request asks for live questions: fail with "Git transport
  supports file context only."
- Git publish sees a `question` part: fail without writing Git content.
- Missing Git target on publish: ask for `--git <repo-url>`.
- Dirty runtime checkout with unexpected local edits: fail and explain the
  checkout path.
- Push failure after successful commit: report the local commit and suggest
  rerunning publish or pushing manually.
- Join URL has no `share` fragment and does not match an HTTP hub invite: fail
  as ambiguous.
- `share.toml` points outside its share directory: reject the share.
- Package metadata is missing or invalid: reject the join or publish.

## Testing

Tests should cover:

- Git-targeted share creation constrains the builder to file capability.
- Git-targeted share creation rejects or reports live-question requests.
- Git publish rejects shares with question parts before writing content.
- Git publish writes `shares/<share-id>/share.toml` and `package/`.
- Git publish commits and pushes by default, with `--no-push` skipping push.
- Git join parses `#share=<id>` and optional `branch`.
- Git join imports the package into `.runtime/shared_views/imports/<view-id>/`.
- Retrieve sees Git-joined shares through the existing `MF#` import path.
- Invalid `share.toml` paths and missing package manifests are rejected.
- Web Studio forces file-only capability when Git Repo is selected.
- Web Studio join accepts Git URLs in the existing share invite field.
