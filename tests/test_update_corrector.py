from __future__ import annotations

import unittest

from pydantic import ValidationError

from rightmemory.update_corrector import (
    UpdateCorrectionResult,
    parse_update_correction_result,
    render_update_correction_result,
)


class UpdateCorrectionResultTests(unittest.TestCase):
    def test_accepts_each_terminal_status_and_normalizes_message(self):
        for status in ("applied", "no_change", "needs_input"):
            with self.subTest(status=status):
                result = UpdateCorrectionResult(status=status, message="  concise result  ")
                self.assertEqual(result.message, "concise result")

    def test_rejects_blank_message_extra_fields_and_unknown_status(self):
        with self.assertRaises(ValidationError):
            UpdateCorrectionResult(status="applied", message=" ")
        with self.assertRaises(ValidationError):
            UpdateCorrectionResult.model_validate(
                {"status": "applied", "message": "done", "extra": True}
            )
        with self.assertRaises(ValidationError):
            UpdateCorrectionResult(status="resolved", message="done")

    def test_parse_and_render_use_canonical_json(self):
        result = parse_update_correction_result(
            '{"status":"no_change","message":"already correct"}'
        )

        self.assertEqual(result.status, "no_change")
        self.assertEqual(
            render_update_correction_result(result),
            '{"status":"no_change","message":"already correct"}',
        )


if __name__ == "__main__":
    unittest.main()
