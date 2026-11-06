"""Replay safety checks; deliberately no assertions about agent-facing text."""
import json
from pathlib import Path
import tempfile
import unittest

from experiments.curation_recording import (
    changed_paths, contained_path, git, load_input, load_redaction_values, out_of_scope, prepare_root,
    redact_copy, reject_residual_values, sha256, snapshot, snapshot_hash, verify_candidates,
)


class CurationRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_containment_rejects_traversal_and_windows_aliases(self):
        for name in ("../escape", "/absolute", "C:/external", "learning\\outside", "a/../b",
                     "a//b", "file:stream", "file.", "file ", "./file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                contained_path(self.root, name)
        self.assertEqual(contained_path(self.root, "learning/skills/sample/file.py"),
                         self.root / "learning/skills/sample/file.py")

    def test_input_and_evidence_hashes_detect_changes(self):
        evidence = "Synthetic evidence."
        candidate_id = "a" * 64
        candidate = {"candidate_id": candidate_id, "session_id": "session", "request_id": "request",
                     "evidence": evidence, "evidence_sha256": sha256(evidence.encode())}
        relative = f"learning/candidates/{candidate_id}.json"
        path = contained_path(self.root, relative)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(candidate), encoding="utf-8")
        item = {key: candidate[key] for key in ("candidate_id", "session_id", "request_id", "evidence_sha256")}
        item.update(source_file=relative, source_id=f"L#candidates/{candidate_id}.json", evidence_chars=len(evidence))
        task = self.root / "task.json"
        data = {"domain": "guidance", "mode": "update", "scope": "", "candidates": [item]}
        task.write_text(json.dumps({"input": data}), encoding="utf-8")
        loaded, proof = load_input(task)
        self.assertEqual(verify_candidates(self.root, loaded)[0]["file_sha256"], sha256(path.read_bytes()))
        data["scope"] = "changed scope"
        task.write_text(json.dumps({"input": data}), encoding="utf-8")
        self.assertNotEqual(proof["input_sha256"], load_input(task)[1]["input_sha256"])
        candidate["evidence"] = "Mutated evidence."
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "proof mismatch"):
            verify_candidates(self.root, loaded)

    def test_export_preserves_bytes_and_omits_live_state(self):
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "--template=", "--initial-branch=main")
        git(source, "config", "core.autocrlf", "false")
        original = b"# Synthetic memory\r\nExact bytes.\r\n"
        (source / "MEMORY.md").write_bytes(original)
        (source / ".runtime").mkdir()
        (source / ".runtime" / "config.json").write_bytes(b"{}")
        (source / "learning" / "claims").mkdir(parents=True)
        (source / "learning" / "claims" / "skills.json").write_bytes(b"{}")
        git(source, "add", "--all")
        git(source, "commit", "-m", "Synthetic historical state")
        git(source, "remote", "add", "origin", "https://invalid.example/no-network")
        replay = self.root / "replay"
        data = {"domain": "skills", "mode": "maintain", "scope": "", "candidates": []}
        prepared = prepare_root(source, "HEAD", replay, data)
        self.assertEqual((replay / "MEMORY.md").read_bytes(), original)
        self.assertFalse((replay / ".runtime").exists())
        self.assertFalse((replay / "learning" / "claims").exists())
        self.assertEqual(git(replay, "remote"), b"")
        self.assertIn(".runtime/config.json", prepared["omitted_paths"])

    def test_changed_paths_include_committed_added_and_deleted_files(self):
        git(self.root, "init", "--template=", "--initial-branch=main")
        (self.root / "MEMORY_old.md").write_bytes(b"original")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-m", "Before")
        before = snapshot(self.root)
        (self.root / "MEMORY_old.md").unlink()
        (self.root / "MEMORY_new.md").write_bytes(b"new")
        git(self.root, "add", "--all")
        git(self.root, "commit", "-m", "Model commits edits")
        after = snapshot(self.root)
        self.assertEqual(git(self.root, "status", "--porcelain"), b"")
        self.assertEqual(changed_paths(before, after), ["MEMORY_new.md", "MEMORY_old.md"])
        self.assertNotEqual(snapshot_hash(before), snapshot_hash(after))

    def test_canonical_role_selector_rejects_cross_domain_changes(self):
        paths = ["MEMORY.md", "PURSUITS.md", "learning/guidance/example.md",
                 "learning/skills/example/SKILL.md", "learning/claims/guidance.json"]
        self.assertEqual(out_of_scope(self.root, "guidance", paths),
                         [name for name in paths if name != "learning/guidance/example.md"])
        self.assertEqual(out_of_scope(self.root, "skills", paths),
                         [name for name in paths if name != "learning/skills/example/SKILL.md"])
        self.assertEqual(out_of_scope(self.root, "memory", paths),
                         [name for name in paths if name != "MEMORY.md"])

    def test_redaction_changes_only_copy_and_git_contains_sanitized_base(self):
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "--template=", "--initial-branch=main")
        git(source, "config", "core.autocrlf", "false")
        original = b"Synthetic credential: synthetic-private-value\r\n"
        (source / "MEMORY.md").write_bytes(original)
        git(source, "add", "--all")
        git(source, "commit", "-m", "Synthetic fixture")
        replay = self.root / "replay"
        data = {"domain": "memory", "mode": "maintain", "scope": "", "candidates": []}
        prepared = prepare_root(source, "HEAD", replay, data, ["synthetic-private-value"])
        expected = original.replace(b"synthetic-private-value", b"[REDACTED]")
        self.assertEqual((replay / "MEMORY.md").read_bytes(), expected)
        self.assertEqual(git(replay, "show", "HEAD:MEMORY.md"), expected)
        self.assertEqual((source / "MEMORY.md").read_bytes(), original)
        self.assertEqual(git(source, "show", "HEAD:MEMORY.md"), original)
        self.assertFalse(prepared["redaction"]["semantic_snapshot_byte_exact"])
        self.assertEqual(prepared["redaction"]["value_count"], 1)
        self.assertEqual([item["path"] for item in prepared["redaction"]["changed_files"]], ["MEMORY.md"])

    def test_redaction_rejects_selected_evidence_before_changing_files(self):
        evidence = "Synthetic private evidence"
        item = {"candidate_id": "a" * 64, "session_id": "session", "request_id": "request",
                "source_file": "learning/candidates/" + "a" * 64 + ".json",
                "evidence_sha256": sha256(evidence.encode()), "evidence_chars": len(evidence)}
        candidate = {key: item[key] for key in ("candidate_id", "session_id", "request_id", "evidence_sha256")}
        candidate["evidence"] = evidence
        path = contained_path(self.root, item["source_file"])
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(candidate), encoding="utf-8")
        (self.root / "MEMORY.md").write_bytes(b"Synthetic private evidence")
        before = snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "selected candidate evidence: learning/candidates/"):
            redact_copy(self.root, {"candidates": [item]}, ["private"])
        self.assertEqual(snapshot(self.root), before)

    def test_redaction_rejects_values_file_in_output_and_residual_values(self):
        values = self.root / "values.json"
        values.write_text('["synthetic-private-value"]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the output"):
            load_redaction_values(values, self.root)
        with self.assertRaisesRegex(ValueError, "still contains"):
            reject_residual_values(["synthetic-private-value"], "prefix synthetic-private-value suffix")
        reject_residual_values(["synthetic-private-value"], "sanitized")


if __name__ == "__main__":
    unittest.main()
