from __future__ import annotations

import unittest

from rightmemory.conversations.projection import project_notification, status_from_thread


class ThreadStatusProjectionTests(unittest.TestCase):
    def test_projects_current_active_flags(self):
        cases = (
            ("waitingOnApproval", "waiting_approval"),
            ("waitingOnUserInput", "waiting_input"),
        )

        for active_flag, expected in cases:
            with self.subTest(active_flag=active_flag):
                self.assertEqual(
                    status_from_thread(
                        {"type": "active", "activeFlags": [active_flag]}
                    ),
                    expected,
                )

    def test_user_input_takes_precedence_when_both_flags_are_present(self):
        self.assertEqual(
            status_from_thread(
                {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval", "waitingOnUserInput"],
                }
            ),
            "waiting_input",
        )

    def test_active_without_a_waiting_flag_is_running(self):
        self.assertEqual(
            status_from_thread(
                {"type": "active", "activeFlags": ["processing"]}
            ),
            "running",
        )

    def test_retains_legacy_boolean_waiting_fields(self):
        self.assertEqual(
            status_from_thread({"type": "active", "waitingOnApproval": True}),
            "waiting_approval",
        )
        self.assertEqual(
            status_from_thread({"type": "active", "waitingOnUserInput": True}),
            "waiting_input",
        )

    def test_notification_uses_current_status_shape(self):
        projected = project_notification(
            "thread/status/changed",
            {
                "threadId": "thread-1",
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                },
            },
        )

        self.assertEqual(projected.status, "waiting_approval")
        self.assertEqual(projected.thread_id, "thread-1")


class ReasoningSummaryProjectionTests(unittest.TestCase):
    def test_projects_provider_summary_delta_without_extra_reasoning_fields(self):
        projected = project_notification(
            "item/reasoning/summaryTextDelta",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "reasoning-1",
                "summaryIndex": 0,
                "delta": "Checking the attachment.",
                "content": "private reasoning",
            },
        )

        self.assertEqual(projected.kind, "reasoning.summary_delta")
        self.assertTrue(projected.persist)
        self.assertEqual(
            projected.payload,
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "reasoning-1",
                "summaryIndex": 0,
                "delta": "Checking the attachment.",
            },
        )

    def test_drops_raw_reasoning_and_raw_response_notifications(self):
        for method in (
            "item/reasoning/textDelta",
            "rawResponseItem/completed",
            "rawResponse/completed",
        ):
            with self.subTest(method=method):
                projected = project_notification(
                    method,
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "delta": "private reasoning",
                    },
                )
                self.assertFalse(projected.persist)
                self.assertEqual(projected.payload, {})

    def test_completed_reasoning_item_keeps_summary_and_strips_raw_content(self):
        projected = project_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "reasoning-1",
                    "type": "reasoning",
                    "summary": ["Checked the current state."],
                    "content": ["private chain of thought"],
                    "encryptedContent": "ciphertext",
                },
            },
        )

        self.assertEqual(
            projected.payload["item"],
            {
                "type": "reasoning",
                "id": "reasoning-1",
                "summary": ["Checked the current state."],
            },
        )
        self.assertNotIn("private chain of thought", str(projected.payload))
        self.assertNotIn("ciphertext", str(projected.payload))


if __name__ == "__main__":
    unittest.main()
