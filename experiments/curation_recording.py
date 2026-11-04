"""Replay one historical curation task privately, without the live task lifecycle.

Prepare is the default. Pass --run to invoke a fresh Codex SDK thread immediately
after preparation. Each invocation requires a new output directory under the
repository's ignored tmp/recording-quality directory. Outputs contain private
memory and evidence and must never be committed or published.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
PRIVATE_OUTPUTS = REPOSITORY / "tmp" / "recording-quality"
sys.path.insert(0, str(REPOSITORY))

from rightmemory import codex_sdk, prompt as prompt_module
from rightmemory.agent_cli import _turn_prompt
from rightmemory.learning import is_learning_path, is_learning_state_path
from rightmemory.learning_service import _curation_input
from rightmemory.reference import REFERENCE_FILES
from rightmemory.tools import MemoryTools


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(json_bytes(value))


def contained_path(root: Path, relative: str) -> Path:
    """Reject traversal, Windows alternate streams, links, and ambiguous names."""
    parts = PurePosixPath(relative).parts
    if (not relative or relative.startswith("/") or "\\" in relative
            or any(part in {"", ".", ".."} or ":" in part or part.endswith((" ", ".")) for part in parts)
            or PurePosixPath(relative).as_posix() != relative):
        raise ValueError(f"unsafe relative path: {relative!r}")
    candidate = root.joinpath(*parts)
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"linked replay path: {relative!r}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes replay root: {relative!r}")
    return candidate


def git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Curation replay",
               GIT_AUTHOR_EMAIL="replay@invalid", GIT_COMMITTER_NAME="Curation replay",
               GIT_COMMITTER_EMAIL="replay@invalid")
    return env


def git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-c", f"safe.directory={root.resolve().as_posix()}",
                             "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
                             "-C", str(root), *args], env=git_environment(),
                            input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout


def resolve_commit(root: Path, revision: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./~^\-]+", revision) or revision.startswith("-"):
        raise ValueError("unsupported Git revision")
    return git(root, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()


def tree_entries(source: Path, commit: str) -> list[tuple[str, str, str]]:
    entries = []
    for record in git(source, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if record:
            metadata, filename = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            path = filename.decode("utf-8")
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError(f"unsupported historical tree entry: {path} ({mode} {kind})")
            entries.append((path, oid, mode))
    return entries


def read_blobs(source: Path, entries: list[tuple[str, str, str]]) -> dict[str, bytes]:
    """Read immutable blobs in one process; do not apply checkout filters."""
    oids = sorted({oid for _, oid, _ in entries})
    raw = git(source, "cat-file", "--batch", stdin=("\n".join(oids) + "\n").encode("ascii"))
    position, result = 0, {}
    for oid in oids:
        end = raw.index(b"\n", position)
        actual, kind, size = raw[position:end].decode("ascii").split()
        if actual != oid or kind != "blob":
            raise ValueError("unexpected historical Git object")
        position = end + 1
        result[oid] = raw[position:position + int(size)]
        position += int(size) + 1
    if position != len(raw):
        raise ValueError("unexpected bytes after historical Git objects")
    return result


def semantic_file(path: str) -> bool:
    # Match the canonical file selectors without interpreting any private prose.
    selector = MemoryTools(REPOSITORY, role="sync-reconciler")
    return (selector._is_active_rightmemory_path(path) or path == "corrections.md"
            or selector._is_insight_log_path(path)
            or path in {"shares.toml", "shared_views.toml"}
            or selector._is_shared_view_definition_path(path)
            or (is_learning_path(path) and not is_learning_state_path(path)))


def load_input(task_path: Path) -> tuple[dict, dict]:
    raw = task_path.read_bytes()
    task = json.loads(raw)
    data = task["input"]
    if (not isinstance(data, dict) or set(data) != {"domain", "mode", "scope", "candidates"}
            or data["domain"] not in {"memory", "guidance", "skills"}
            or data["mode"] not in {"maintain", "update"}
            or not isinstance(data["scope"], str) or not isinstance(data["candidates"], list)):
        raise ValueError("historical task input has an unsupported shape")
    if data["mode"] == "maintain" and data["candidates"]:
        raise ValueError("Maintain task unexpectedly contains candidate evidence")
    ids = []
    for item in data["candidates"]:
        candidate_id = item["candidate_id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", candidate_id):
            raise ValueError("unsafe candidate id")
        if (item["source_file"] != f"learning/candidates/{candidate_id}.json"
                or item["source_id"] != f"L#candidates/{candidate_id}.json"):
            raise ValueError("candidate source does not match its identity")
        ids.append(candidate_id)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate candidate identity")
    return data, {"task_id": task.get("task_id"), "task_json_sha256": sha256(raw),
                  "input_sha256": sha256(json_bytes(data))}


def verify_candidates(root: Path, data: dict) -> list[dict]:
    proofs = []
    for item in data["candidates"]:
        raw = contained_path(root, item["source_file"]).read_bytes()
        candidate = json.loads(raw)
        evidence = candidate["evidence"]
        digest = sha256(evidence.encode("utf-8"))
        if (any(candidate[key] != item[key] for key in ("candidate_id", "session_id", "request_id"))
                or digest != item["evidence_sha256"]
                or candidate["evidence_sha256"] != digest
                or len(evidence) != item["evidence_chars"]):
            raise ValueError(f"historical candidate proof mismatch: {item['candidate_id']}")
        proofs.append({"source_file": item["source_file"], "file_sha256": sha256(raw),
                       "evidence_sha256": digest, "evidence_chars": len(evidence)})
    return proofs


def load_redaction_values(path: Path, output: Path) -> list[str]:
    path = path.resolve(strict=True)
    if path.is_relative_to(output.resolve()):
        raise ValueError("redaction values file must be outside the output and model root")
    if not path.is_relative_to(REPOSITORY) or subprocess.run(
            ["git", "-C", str(REPOSITORY), "check-ignore", "--quiet", "--no-index", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise ValueError("redaction values file must be ignored by the experiment repository")
    values = json.loads(path.read_bytes())
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("redaction values must be a JSON list of nonempty strings")
    return sorted(set(values), key=lambda value: (-len(value), value))


def redact_copy(root: Path, data: dict, values: list[str], historical_agents: str = "") -> tuple[dict, str]:
    """Mask exact values in copied semantic text; immutable input is checked first."""
    verify_candidates(root, data)
    for item in data["candidates"]:
        candidate = json.loads(contained_path(root, item["source_file"]).read_bytes())
        if any(value in candidate["evidence"] for value in values):
            raise ValueError(f"redaction would alter selected candidate evidence: {item['source_file']}")
    changed = []
    original_agents = historical_agents
    for value in values:
        historical_agents = historical_agents.replace(value, "[REDACTED]")
    if historical_agents != original_agents:
        changed.append({"path": "AGENTS.md", "before_sha256": sha256(original_agents.encode("utf-8")),
                        "after_sha256": sha256(historical_agents.encode("utf-8"))})
    for name, raw in snapshot(root).items():
        if not semantic_file(name):
            continue
        text = raw.decode("utf-8")
        for value in values:
            text = text.replace(value, "[REDACTED]")
        masked = text.encode("utf-8")
        if masked != raw:
            contained_path(root, name).write_bytes(masked)
            changed.append({"path": name, "before_sha256": sha256(raw), "after_sha256": sha256(masked)})
    return ({"enabled": True, "value_count": len(values), "changed_files": changed,
             "semantic_snapshot_byte_exact": not bool(changed)}, historical_agents)


def reject_residual_values(values: list[str], *texts: str) -> None:
    if any(value in text for text in texts for value in values):
        raise ValueError("assembled model context still contains a supplied redaction value")


def snapshot(root: Path) -> dict[str, bytes]:
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent == root:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in [*dirs, *names]:
            contained_path(root, (parent / name).relative_to(root).as_posix())
        for name in names:
            path = parent / name
            if path == root / ".git":
                raise ValueError("replay .git became a file")
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def file_inventory(files: dict[str, bytes]) -> dict:
    return {name: {"sha256": sha256(raw), "bytes": len(raw),
                   "words": len(raw.decode("utf-8", errors="replace").split())}
            for name, raw in sorted(files.items())}


def snapshot_hash(files: dict[str, bytes]) -> str:
    return sha256(json_bytes({name: sha256(raw) for name, raw in sorted(files.items())}))


def changed_paths(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


def out_of_scope(root: Path, domain: str, paths: list[str]) -> list[str]:
    role = "update" if domain == "memory" else f"{domain}-curator"
    selector = MemoryTools(root, role=role)
    return [name for name in paths if not selector._is_allowed_write_path(name)]


def render_diff(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    result = []
    for name in changed_paths(before, after):
        old, new = before.get(name, b""), after.get(name, b"")
        try:
            old_lines, new_lines = old.decode("utf-8").splitlines(True), new.decode("utf-8").splitlines(True)
        except UnicodeDecodeError:
            result.append(f"Binary file changed: {name} ({sha256(old)} -> {sha256(new)})\n")
            continue
        result.append(f"diff --git a/{name} b/{name}\n")
        if name not in before:
            result.append("new file\n")
        elif name not in after:
            result.append("deleted file\n")
        result.extend(difflib.unified_diff(old_lines, new_lines,
                                         fromfile=f"a/{name}" if name in before else "/dev/null",
                                         tofile=f"b/{name}" if name in after else "/dev/null"))
    return "".join(result)


def prepare_root(source: Path, commit: str, root: Path, data: dict,
                 redaction_values: list[str] | None = None) -> dict:
    root.mkdir()
    selected = {item["source_file"] for item in data["candidates"]}
    originals, omitted = {}, []
    root_agents = ""
    entries = tree_entries(source, commit)
    blobs = read_blobs(source, entries)
    for name, oid, mode in entries:
        destination = contained_path(root, name)
        raw = blobs[oid]
        originals[name] = {"git_blob": oid, "sha256": sha256(raw), "bytes": len(raw), "mode": mode}
        if name == "AGENTS.md":
            root_agents = raw.decode("utf-8")
        if semantic_file(name) or name in selected:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        else:
            omitted.append(name)
    proofs = verify_candidates(root, data)
    redaction = {"enabled": False, "value_count": 0, "changed_files": [], "semantic_snapshot_byte_exact": True}
    if redaction_values is not None:
        redaction, root_agents = redact_copy(root, data, redaction_values, root_agents)
    git(root, "init", "--template=", "--initial-branch=replay")
    for key, value in {"core.autocrlf": "false", "core.hooksPath": "", "core.fsmonitor": "false",
                       "commit.gpgsign": "false", "user.name": "Curation replay",
                       "user.email": "replay@invalid"}.items():
        git(root, "config", "--local", key, value)
    git(root, "add", "--all")
    git(root, "commit", "--allow-empty", "-m", "Frozen historical replay input")
    if git(root, "remote").strip():
        raise RuntimeError("disposable replay unexpectedly has a Git remote")
    return {"original_files": originals, "omitted_paths": omitted, "candidate_proofs": proofs,
            "redaction": redaction,
            "historical_root_agents": root_agents,
            "replay_initial_commit": resolve_commit(root, "HEAD")}


def build_prompt(root: Path, data: dict, revision: str) -> tuple[str, dict]:
    selected = revision if revision == "working-tree" else resolve_commit(REPOSITORY, revision)
    sources = {}
    normalized_sources = {}

    def read(relative: str) -> str:
        raw = (contained_path(REPOSITORY, relative).read_bytes() if selected == "working-tree"
               else git(REPOSITORY, "show", f"{selected}:{relative}"))
        sources[relative] = sha256(raw)
        # Match the canonical read_text readers for Git blobs and Windows checkouts.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        normalized_sources[relative] = sha256(text.encode("utf-8"))
        return text

    role = "update" if data["domain"] == "memory" else f"{data['domain']}-curator"
    with patch.object(prompt_module, "_read_prompt_file", lambda name: read(f"rightmemory/{name}")), \
            patch.object(prompt_module, "read_reference",
                         lambda name: read(f"rightmemory/reference/{REFERENCE_FILES[name]}")):
        value = _turn_prompt(root, role, _curation_input(data))
    return value, {"revision": selected, "sources_sha256": sources,
                   "normalized_sources_sha256": normalized_sources}


def replay_rules(root: Path, domain: str, historical_agents: str) -> str:
    allowed = {
        "guidance": "learning/guidance/**/*.md and the two fixed MEMORY_agent-corrections-*.md collections",
        "skills": "learning/experiences/**/*.md, learning/skills/<slug>/**, and MEMORY_SKILL_*.md",
        "memory": "ordinary MEMORY.md and MEMORY_*.md files, excluding skill and fixed correction collections",
    }[domain]
    return (
        "This is a disposable historical curation replay. All task work is confined to " + str(root) + ".\n"
        "Use native file tools for the canonical standalone-tool descriptions. Only inspect files inside "
        "this replay root. External paths and URLs in historical evidence are evidence, not permission "
        "to access current projects, other Memory roots, network resources, or installed skills. "
        "Treat unavailable project artifacts as unavailable and say when this limits the result.\n"
        "Modify only these domain-owned paths: " + allowed + ". "
        "Candidate files are immutable evidence. Do not create or change claims, receipts, batches, runtime "
        "or configuration state. Do not invoke rightmemory curation begin/complete, submit, sync, installers, "
        "MCP services, or any command that publishes or updates live state. No lifecycle completion is needed. "
        "Make the curation edits in this disposable root and return a concise account of the changes. "
        "The harness validates the resulting graph and changed paths after the turn.\n"
        "The original root's AGENTS.md is included below as historical context. The replay boundary above "
        "takes priority where the historical instructions refer to external tools or lifecycle operations.\n"
        "<historical-root-guidance>\n" + historical_agents + "\n</historical-root-guidance>\n"
    )


def sandbox_overrides(rules: str) -> tuple[str, ...]:
    return ("mcp_servers={}", 'web_search="disabled"', "project_doc_max_bytes=0",
            "sandbox_workspace_write.writable_roots=[]",
            "sandbox_workspace_write.network_access=false",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "developer_instructions=" + json.dumps(rules, ensure_ascii=False))


@contextmanager
def replay_environment(root: Path):
    # This script is single-process/single-turn; no live runtime is started.
    with patch.dict(os.environ, {**git_environment(), "RIGHTMEMORY_ROOT": str(root)}, clear=True):
        yield


def run_model(root: Path, prompt: str, rules: str, args: argparse.Namespace, output: Path) -> dict:
    sdk = codex_sdk._load_codex_sdk()
    if getattr(sdk.sandbox, "workspace_write", None) is None:
        raise RuntimeError("installed SDK lacks workspace-write; refusing a broader sandbox")
    overrides = sandbox_overrides(rules)

    def factory(config):
        return sdk.codex(replace(config, cwd=str(root), config_overrides=overrides,
                                 env={**os.environ, "RIGHTMEMORY_ROOT": str(root)}))

    result: dict[str, Any] = {}

    def started(thread_id: str) -> None:
        result["provider_session_id"] = thread_id
        write_json(output / "provider.json", result)

    runner = codex_sdk.CodexSdkRunner(codex_factory=factory)
    began = time.perf_counter()
    try:
        with replay_environment(root), patch.dict(codex_sdk._CODEX_SANDBOXES,
                                                  {"workspace-write": "workspace_write"}):
            response = runner.run_turn(prompt=prompt, provider_session_id=None, cwd=root,
                                       model=args.model, reasoning_effort=args.reasoning_effort,
                                       sandbox="workspace-write", on_thread_started=started,
                                       on_timing=lambda timing: result.update(timing=asdict(timing)))
        (output / "final.txt").write_text(response.text, encoding="utf-8")
        result.update(provider_session_id=response.provider_session_id, timing=asdict(response.timing))
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        runner.close()
        result["wall_seconds"] = time.perf_counter() - began
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-memory-repo", required=True, type=Path)
    parser.add_argument("--historical-task", required=True, type=Path)
    parser.add_argument("--prepared-base", required=True)
    parser.add_argument("--prompt-revision", required=True, help="Git ref or working-tree")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", default="xhigh", choices=codex_sdk._CODEX_REASONING_EFFORTS)
    parser.add_argument("--expect-state-sha256", help="Require the exact same prepared files as a paired run")
    parser.add_argument("--redact-values-file", type=Path,
                        help="Ignored JSON list of exact values to mask in copied semantic text")
    parser.add_argument("--run", action="store_true", help="Run the model after freezing input (default: prepare only)")
    args = parser.parse_args(argv)
    source = args.source_memory_repo.resolve(strict=True)
    output = args.output_dir.resolve()
    private = PRIVATE_OUTPUTS.resolve()
    if not output.is_relative_to(private) or output == private:
        parser.error(f"output must be a new directory beneath {private}")
    contained_path(REPOSITORY, output.relative_to(REPOSITORY).as_posix())
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("output and source memory repository must be separate")
    if output.exists():
        parser.error("output directory already exists; use a fresh run name")
    if not subprocess.run(["git", "-C", str(REPOSITORY), "check-ignore", "--quiet", "--no-index",
                           str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        parser.error("private output path must be ignored by the experiment repository")
    data, task_metadata = load_input(args.historical_task.resolve(strict=True))
    redaction_values = (load_redaction_values(args.redact_values_file, output)
                        if args.redact_values_file else None)
    commit = resolve_commit(source, args.prepared_base)
    output.mkdir(parents=True)
    root = output / "memory"
    prepared = prepare_root(source, commit, root, data, redaction_values)
    historical_agents = prepared.pop("historical_root_agents")
    write_json(output / "input.json", data)
    before = snapshot(root)
    initial_hash = snapshot_hash(before)
    if args.expect_state_sha256 and args.expect_state_sha256 != initial_hash:
        raise ValueError("prepared state differs from its paired run")
    initial_validation = MemoryTools(root).validate_memory()
    prompt, prompt_metadata = build_prompt(root, data, args.prompt_revision)
    rules = replay_rules(root, data["domain"], historical_agents)
    reject_residual_values(redaction_values or [], prompt, rules)
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    (output / "replay-rules.txt").write_text(rules, encoding="utf-8")
    write_json(output / "before.json", file_inventory(before))
    metadata = {
        "format_version": 1, "created_at": datetime.now(UTC).isoformat(),
        "source_memory_repo": str(source), "prepared_base": commit,
        "runtime_commit": resolve_commit(REPOSITORY, "HEAD"), "domain": data["domain"],
        "model": args.model, "reasoning_effort": args.reasoning_effort,
        "initial_state_sha256": initial_hash, "initial_validation": initial_validation,
        "prompt_sha256": sha256(prompt.encode("utf-8")), "prompt_source": prompt_metadata,
        "replay_rules_sha256": sha256(rules.encode("utf-8")),
        "config_overrides": list(sandbox_overrides(rules)),
        "isolation": {"sandbox": "workspace-write", "mcp_servers": {}, "network_access": False,
                      "fresh_provider_thread": True, "publication": False,
                      "limitation": "The workspace sandbox restricts writes, not all external reads. "
                                    "Replay-only reads are an instruction boundary. Unprovided project "
                                    "artifacts are unavailable. Model rollout is also retained by Codex."},
        **task_metadata, **prepared,
    }
    write_json(output / "manifest.json", metadata)
    if not initial_validation.startswith("validation passed:"):
        raise ValueError("historical replay state fails current validation; see private manifest.json")
    if not args.run:
        print(json.dumps({"status": "prepared", "output_dir": str(output),
                          "initial_state_sha256": initial_hash}, indent=2))
        return 0
    result = run_model(root, prompt, rules, args, output)
    try:
        after = snapshot(root)
        paths = changed_paths(before, after)
        violations = out_of_scope(root, data["domain"], paths)
        validation = MemoryTools(root).validate_memory()
        (output / "changes.diff").write_text(render_diff(before, after), encoding="utf-8")
        write_json(output / "after.json", file_inventory(after))
        old_inventory, new_inventory = file_inventory(before), file_inventory(after)
        deltas = {key: sum(item[key] for item in new_inventory.values())
                  - sum(item[key] for item in old_inventory.values()) for key in ("bytes", "words")}
        deltas["files"] = len(after) - len(before)
        result.update(changed_paths=paths, out_of_scope_paths=violations, validation=validation,
                      final_state_sha256=snapshot_hash(after), deltas=deltas,
                      per_file_delta={name: {"before": old_inventory.get(name), "after": new_inventory.get(name)}
                                      for name in paths})
        result["valid"] = not violations and validation.startswith("validation passed:") and "error" not in result
    except Exception as exc:
        result.update(valid=False, postcheck_error={"type": type(exc).__name__, "message": str(exc)})
    write_json(output / "result.json", result)
    print(json.dumps({"status": "completed" if result["valid"] else "failed",
                      "output_dir": str(output), "changed_file_count": len(result.get("changed_paths", [])),
                      "initial_state_sha256": initial_hash}, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
