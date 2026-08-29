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


if __name__ == "__main__":
    unittest.main()
