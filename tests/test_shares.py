import tempfile
import unittest
from pathlib import Path

from rightmemory.share_models import (
    ShareFilePart,
    ShareQuestionPart,
    ShareRelationship,
    load_shares,
    save_shares,
    validate_share_id,
)


class ShareModelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_validate_share_id_accepts_portable_ids(self):
        self.assertEqual(validate_share_id("auth-api_1.dev"), "auth-api_1.dev")

    def test_validate_share_id_rejects_paths(self):
        with self.assertRaises(ValueError):
            validate_share_id("../auth")

    def test_save_and_load_provider_file_question_share(self):
        share = ShareRelationship(
            share_id="auth-api",
            role="provider",
            title="Auth API",
            provider_id="alice",
            hub_url="http://127.0.0.1:8765",
            credential_id="alice-publish",
            state="draft",
            parts=("file", "question"),
            file=ShareFilePart(
                view_id="auth-api-files",
                intent="Expose auth API integration context for frontend agents",
                approved=False,
            ),
            question=ShareQuestionPart(
                view_id="auth-api-ask",
                intent="Let frontend agents ask temporary auth API questions",
                question_base_url="http://127.0.0.1:8766",
                approved=False,
            ),
        )

        save_shares(self.root, {"auth-api": share})
        loaded = load_shares(self.root)

        self.assertEqual(loaded["auth-api"], share)
        text = (self.root / "shares.toml").read_text(encoding="utf-8")
        self.assertIn("[shares.auth-api]", text)
        self.assertIn('parts = ["file", "question"]', text)

    def test_load_rejects_part_without_config(self):
        (self.root / "shares.toml").write_text(
            '[shares.auth-api]\n'
            'version = 1\n'
            'role = "provider"\n'
            'title = "Auth API"\n'
            'state = "draft"\n'
            'parts = ["file"]\n',
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_shares(self.root)

        self.assertIn("file part requires [shares.auth-api.file]", str(caught.exception))
