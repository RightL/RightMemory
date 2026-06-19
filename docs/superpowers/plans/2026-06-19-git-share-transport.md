# Git Share Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Git as a clean, plaintext, MF-only share transport for CLI and Web Studio.

**Architecture:** Extend the existing share-first model with `transport = "git"` metadata while keeping HTTP shares unchanged. Add a focused Git transport module that clones/fetches runtime checkouts, writes canonical exported `MF#` packages under `shares/<share-id>/package/`, imports Git packages into the existing `.runtime/shared_views/imports/<view-id>/` path, and lets retrieve keep using the current `MF#` flow. Web Studio stays share-first and switches create/publish/join behavior based on transport.

**Tech Stack:** Python standard library, `argparse`, TOML via `tomllib` plus existing local TOML render helpers, subprocess Git commands, existing `unittest` suite, existing static Web Studio JavaScript.

---

## File Structure

- Modify `rightmemory/share_models.py`: add persisted share transport fields and validation.
- Modify `rightmemory/shared_view_models.py`: add `git-file` target metadata so joined Git file shares can pull later.
- Create `rightmemory/git_share_transport.py`: all Git URL parsing, checkout management, publish copy/commit/push, share metadata parsing, and package import helpers.
- Modify `rightmemory/shared_view_files.py`: route `pull_file_view` to Git pull for `git-file` targets.
- Modify `rightmemory/tools.py`: let the share builder write Git-targeted provider shares and reject Git `MQ#`.
- Modify `rightmemory/share_builder.py`: include Git target context in builder messages and force file capability for Git shares.
- Modify `rightmemory/shares.py`: support Git create, publish, join, status, and runtime invitation recording.
- Modify `rightmemory/cli.py`: add `--git`, `--branch`, and `--no-push`; make HTTP target fields required only for HTTP shares.
- Modify `rightmemory/web/service.py`: support Git create/publish/join payloads and expose transport fields.
- Modify `rightmemory/web/static/app.js`: add share-first Git UI controls, Git publish action, Git join handling, and copyable join URLs.
- Modify `rightmemory/web/static/styles.css` only if the new compact controls need existing-form-compatible hiding/toggling styles.
- Test in `tests/test_shares.py`, `tests/test_cli.py`, `tests/test_web_service.py`, and `tests/test_shared_views.py`.

## Task 1: Persist Git Transport Metadata

**Files:**
- Modify: `rightmemory/share_models.py`
- Test: `tests/test_shares.py`

- [ ] **Step 1: Add failing tests for Git share registry round-trip**

Add a test near the existing share model tests:

```python
def test_git_provider_share_round_trips_transport_metadata(self):
    save_shares(
        self.root,
        {
            "auth-api": ShareRelationship(
                share_id="auth-api",
                role="provider",
                title="Auth API",
                provider_id="alice",
                state="draft",
                parts=("file",),
                transport="git",
                git_url="https://github.com/user/rightmemory-shares.git",
                git_branch="gh-pages",
                file=ShareFilePart(view_id="auth-api-files", intent="Share auth API context"),
            )
        },
    )

    loaded = load_shares(self.root)["auth-api"]

    self.assertEqual(loaded.transport, "git")
    self.assertEqual(loaded.git_url, "https://github.com/user/rightmemory-shares.git")
    self.assertEqual(loaded.git_branch, "gh-pages")
    self.assertNotIn("hub_url", (self.root / "shares.toml").read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the focused failing test**

Run:

```bash
python -m unittest tests.test_shares.ShareModelTests.test_git_provider_share_round_trips_transport_metadata
```

Expected: fail with `TypeError` or missing `transport`.

- [ ] **Step 3: Implement metadata**

In `ShareRelationship`, add:

```python
transport: str = "http"
git_url: str | None = None
git_branch: str | None = None
```

Add `SHARE_TRANSPORTS = {"http", "git"}`. Load/save `transport`, `git_url`, and `git_branch`. In `_validate_share`, normalize missing transport to `"http"`, validate Git shares require `git_url`, validate Git shares do not have `question` parts, and preserve the new fields in the returned dataclass.

- [ ] **Step 4: Run model tests**

Run:

```bash
python -m unittest tests.test_shares.ShareModelTests
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/share_models.py tests/test_shares.py
git commit -m "feat: persist Git share transport metadata"
```

## Task 2: Add Git Target Metadata For File Connections

**Files:**
- Modify: `rightmemory/shared_view_models.py`
- Test: `tests/test_shared_views.py`

- [ ] **Step 1: Add failing connection round-trip test**

Add a test that saves a file connection with a Git target:

```python
def test_git_file_target_round_trips(self):
    save_connections(
        self.root,
        {
            "auth-api-files": SharedViewConnection(
                heading_id="auth-api-files",
                view_type="file",
                ref="rightmemory://mf/auth-api-files",
                target=SharedViewTarget(
                    kind="git-file",
                    view_id="auth-api-files",
                    git_url="https://github.com/user/rightmemory-shares.git",
                    git_branch="gh-pages",
                    git_share_id="auth-api",
                    accepted_from_url="https://github.com/user/rightmemory-shares.git#share=auth-api&branch=gh-pages",
                ),
            )
        },
    )

    target = load_connections(self.root)["auth-api-files"].target

    self.assertEqual(target.kind, "git-file")
    self.assertEqual(target.git_url, "https://github.com/user/rightmemory-shares.git")
    self.assertEqual(target.git_branch, "gh-pages")
    self.assertEqual(target.git_share_id, "auth-api")
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewModelsTests.test_git_file_target_round_trips
```

Expected: fail until target fields exist.

- [ ] **Step 3: Implement target metadata**

Add `git-file` to `TARGET_KINDS`. Add to `SharedViewTarget`:

```python
git_url: str | None = None
git_branch: str | None = None
git_share_id: str | None = None
```

Load/save those fields. Validate `git-file` only works for `view_type == "file"`, requires `git_url`, `view_id`, and `git_share_id`, and does not run HTTP URL validation on Git URLs.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewModelsTests
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/shared_view_models.py tests/test_shared_views.py
git commit -m "feat: add Git file shared-view target metadata"
```

## Task 3: Implement Git Share Transport Helpers

**Files:**
- Create: `rightmemory/git_share_transport.py`
- Test: `tests/test_shares.py`

- [ ] **Step 1: Add failing tests for URL parsing and runtime checkout paths**

Add tests:

```python
def test_parse_git_share_url_extracts_share_and_branch(self):
    parsed = parse_git_share_url("https://github.com/user/repo.git#share=auth-api&branch=gh-pages")

    self.assertEqual(parsed.repo_url, "https://github.com/user/repo.git")
    self.assertEqual(parsed.share_id, "auth-api")
    self.assertEqual(parsed.branch, "gh-pages")

def test_parse_git_share_url_requires_share_fragment(self):
    with self.assertRaisesRegex(ValueError, "share"):
        parse_git_share_url("https://github.com/user/repo.git")
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
python -m unittest tests.test_shares.GitShareTransportTests
```

Expected: import failure.

- [ ] **Step 3: Implement parser and checkout helpers**

Create `rightmemory/git_share_transport.py` with:

```python
@dataclass(frozen=True)
class GitShareReference:
    repo_url: str
    share_id: str
    branch: str | None = None

def parse_git_share_url(value: str) -> GitShareReference: ...
def git_join_url(repo_url: str, share_id: str, branch: str | None = None) -> str: ...
def checkout_path(memory_root: Path, repo_url: str, branch: str | None) -> Path: ...
def ensure_checkout(memory_root: Path, repo_url: str, branch: str | None, *, writable: bool) -> Path: ...
```

Use `urllib.parse` for fragments. Hash `repo_url + "\0" + (branch or "")` with SHA-256 for `.runtime/git_shares/<hash>`. Run Git with `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=true`.

- [ ] **Step 4: Run parser tests**

Run:

```bash
python -m unittest tests.test_shares.GitShareTransportTests
```

Expected: parser tests pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/git_share_transport.py tests/test_shares.py
git commit -m "feat: add Git share transport helpers"
```

## Task 4: Publish Git File Shares From CLI Service

**Files:**
- Modify: `rightmemory/git_share_transport.py`
- Modify: `rightmemory/shares.py`
- Modify: `rightmemory/cli.py`
- Modify: `rightmemory/shared_view_builder.py`
- Modify: `rightmemory/share_builder.py`
- Modify: `rightmemory/tools.py`
- Test: `tests/test_shares.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add failing service test for Git create and publish**

Use a local bare repository as the remote:

```python
def test_git_share_create_and_publish_writes_repo_package(self):
    remote = self.root / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)

    result = create_share(
        self.root,
        "auth-api",
        title="Auth API",
        provider_id="alice",
        request="Share auth API context",
        git_url=str(remote),
        capability="auto",
    )
    self.assertIn("capability=file_context", result)
    approve_share(self.root, "auth-api")

    published = publish_share(self.root, "auth-api", push=True)

    self.assertIn("published share auth-api", published)
    self.assertIn("#share=auth-api", published)
```

Patch the builder runtime in this test the same way existing share builder tests do: write a file view and a Git share relationship directly, then return a summary.

- [ ] **Step 2: Add failing CLI tests**

Add tests that assert:

```python
main([
    "share", "create", "auth-api",
    "--request", "Share auth API context",
    "--provider", "alice",
    "--git", "https://github.com/user/repo.git",
])
```

passes `git_url` and no HTTP target to `create_share`, and:

```python
main(["share", "publish", "auth-api", "--no-push"])
```

passes `push=False` to `publish_share`.

- [ ] **Step 3: Run failing tests**

Run:

```bash
python -m unittest tests.test_shares.GitSharePublishTests tests.test_cli.ShareCliTests
```

Expected: fail on missing parameters.

- [ ] **Step 4: Implement create path**

Update `run_file_view_builder` to accept optional `hub_url` and `credential_id`; only include `publish_hub_url` and `publish_credential_id` when both are present, and only require publish metadata for HTTP builds.

Update `run_share_builder`, `_share_build_message`, and `_share_revise_message` to accept optional `git_url` and `git_branch`. When `git_url` is present, normalize capability to `file_context`, include `transport: git`, `git_url`, and optional `git_branch` in the builder message, and omit HTTP fields.

Update `MemoryTools.create_or_update_share_relationship` so `hub_url` and `credential_id` become optional and `git_url` / `git_branch` are accepted. For Git, require file capability only, reject question inputs, and write `ShareRelationship(transport="git", git_url=..., git_branch=...)`.

Update `create_share` and `create_share_from_request` to accept `git_url` and `git_branch`. If `git_url` is present, make `hub_url` and `credential_id` optional and force file capability. If `git_url` is absent, preserve current HTTP requirements.

- [ ] **Step 5: Implement publish path**

Add Git publish helper:

```python
def publish_git_share(memory_root: Path, share: ShareRelationship, *, push: bool = True) -> str:
    ...
```

It should validate provider share, file-only parts, approved file view, export the file package with `export_file_view_package`, write `shares/<share-id>/share.toml`, copy package into `shares/<share-id>/package/`, commit when there are changes, push when requested, and return a join URL.

Update `publish_share(..., push: bool = True, git_url: str | None = None, git_branch: str | None = None)` to route to Git when the share transport is Git or a Git target is passed. Keep HTTP behavior unchanged.

- [ ] **Step 6: Implement CLI flags**

In `_share_main`, make `--hub-url` and `--credential-id` optional, add:

```python
create.add_argument("--git")
create.add_argument("--branch")
publish.add_argument("--git")
publish.add_argument("--branch")
publish.add_argument("--no-push", action="store_true")
```

Pass `git_url=args.git`, `git_branch=args.branch`, and `push=not args.no_push`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
python -m unittest tests.test_shares.GitSharePublishTests tests.test_cli.ShareCliTests
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add rightmemory/git_share_transport.py rightmemory/shares.py rightmemory/cli.py rightmemory/shared_view_builder.py rightmemory/share_builder.py rightmemory/tools.py tests/test_shares.py tests/test_cli.py
git commit -m "feat: publish file shares through Git"
```

## Task 5: Join And Pull Git File Shares

**Files:**
- Modify: `rightmemory/git_share_transport.py`
- Modify: `rightmemory/shares.py`
- Modify: `rightmemory/shared_view_files.py`
- Test: `tests/test_shares.py`
- Test: `tests/test_shared_views.py`

- [ ] **Step 1: Add failing join test**

Create a local remote with a published `shares/auth-api/share.toml` and canonical package, then assert:

```python
joined = join_share(self.root, f"{remote.as_uri()}#share=auth-api")

self.assertIn("joined share auth-api", joined)
self.assertTrue((self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist" / "MEMORY.md").is_file())
self.assertEqual(load_shares(self.root)["auth-api"].transport, "git")
self.assertEqual(load_connections(self.root)["auth-api-files"].target.kind, "git-file")
```

- [ ] **Step 2: Add failing pull test**

After a Git join, update the remote package, run `pull_file_view(self.root, "auth-api-files")`, and assert the import updates.

- [ ] **Step 3: Run failing tests**

Run:

```bash
python -m unittest tests.test_shares.GitShareJoinTests tests.test_shared_views.GitFilePullTests
```

Expected: fail until join/pull is implemented.

- [ ] **Step 4: Implement Git join**

In `join_share`, detect Git URLs with a `share` fragment before HTTP invite parsing. Fetch the repo, read `shares/<share-id>/share.toml`, validate `transport = "git"` and `parts = ["file"]`, import `package/`, write consumer `ShareRelationship(transport="git", git_url=..., git_branch=...)`, and call `accept_shared_view` with `SharedViewTarget(kind="git-file", ...)`.

- [ ] **Step 5: Implement Git pull**

In `shared_view_files.pull_file_view`, if the target kind is `git-file`, call the Git transport pull helper. It should fetch the runtime checkout, re-read `share.toml`, and atomically replace `.runtime/shared_views/imports/<view-id>/` from the package directory. Preserve stale-import fallback behavior.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m unittest tests.test_shares.GitShareJoinTests tests.test_shared_views.GitFilePullTests
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/git_share_transport.py rightmemory/shares.py rightmemory/shared_view_files.py tests/test_shares.py tests/test_shared_views.py
git commit -m "feat: join and pull Git file shares"
```

## Task 6: Add Web Studio Git Share UX

**Files:**
- Modify: `rightmemory/web/service.py`
- Modify: `rightmemory/web/static/app.js`
- Modify: `rightmemory/web/static/styles.css` if needed
- Test: `tests/test_web_service.py`

- [ ] **Step 1: Add failing service/API tests**

Add tests that assert Web create passes `git_url` and forces file capability, and Web relationships JSON includes `transport`, `git_url`, and a copyable Git join URL after publish.

- [ ] **Step 2: Add failing static app tests**

Extend the existing script-content tests to look for:

```python
self.assertIn("transport", script)
self.assertIn("Git Repo", script)
self.assertIn("git_url", script)
self.assertIn("Copy Join URL", script)
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
python -m unittest tests.test_web_service
```

Expected: fail until service and UI are updated.

- [ ] **Step 4: Implement Web service changes**

Update `create_share_relationship` to accept `transport`, `git_url`, and `git_branch`; when `transport == "git"`, pass Git fields to `create_share_from_request`, force file capability, and do not require HTTP hub or credential fields.

Add service method for publishing shares if one is not already present:

```python
def publish_share_relationship(self, share_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    message = publish_share(..., push=not bool(payload.get("no_push", False)))
    return {"message": message}
```

Expose it through `/api/share/relationships/{share_id}/publish` if missing.

- [ ] **Step 5: Implement Web UI changes**

In Create Share, add transport selector with HTTP Hub and Git Repo. Show HTTP credential/hub fields only for HTTP. Show Git repository URL and optional branch for Git. Disable or force capability to file context when Git is selected. In share cards, show transport, Git repo, branch, last/copyable invitation URL when present, and a Publish to Git button for Git provider shares. Keep Join Share as one URL field.

- [ ] **Step 6: Run focused Web tests**

Run:

```bash
python -m unittest tests.test_web_service
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/web/service.py rightmemory/web/static/app.js rightmemory/web/static/styles.css tests/test_web_service.py
git commit -m "feat: add Git share controls to Web Studio"
```

## Task 7: Full Verification And Review Gates

**Files:**
- No planned source edits unless verification or review finds issues.

- [ ] **Step 1: Run focused suite**

Run:

```bash
python -m unittest tests.test_shares tests.test_cli tests.test_shared_views tests.test_web_service
```

Expected: pass.

- [ ] **Step 2: Run full suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: pass.

- [ ] **Step 3: Run spec-compliance review**

Use inline-reviewed-development's spec reviewer gate against:

```text
docs/superpowers/specs/2026-06-19-git-share-transport-design.md
docs/superpowers/plans/2026-06-19-git-share-transport.md
```

Fix any issues, verify, and commit before re-review.

- [ ] **Step 4: Run code-quality review**

Use inline-reviewed-development's code-quality reviewer gate. Fix Critical and Important findings, verify, and commit before re-review.

- [ ] **Step 5: Final status**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: only unrelated pre-existing untracked files remain.

