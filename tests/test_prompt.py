from __future__ import annotations

import unittest
from unittest.mock import call, patch

from rightmemory.prompt import ROLE_PROMPTS, _role_reference_guidance


class PromptReferenceSelectionTests(unittest.TestCase):
    def test_each_role_reads_only_its_package_references(self):
        expected = {
            "dreamer": ["memory", "shared-view"],
            "historian": [],
            "insight": [],
            "pruner": ["memory", "shared-view"],
            "retrieve": ["retrieve-contract"],
            "reviewer": [],
            "shared-view-builder": ["memory", "shared-view"],
            "sync-reconciler": [
                "memory",
                "pursuit",
                "agent-correction",
                "shared-view",
                "edit-correction",
            ],
            "update": [
                "memory",
                "pursuit",
                "agent-correction",
                "shared-view",
                "edit-correction",
            ],
        }
        self.assertEqual(set(expected), ROLE_PROMPTS)

        for role, reference_names in expected.items():
            with self.subTest(role=role):
                with patch("rightmemory.prompt.read_reference", return_value="reference") as read:
                    _role_reference_guidance(role)
                self.assertEqual(read.call_args_list, [call(name) for name in reference_names])


if __name__ == "__main__":
    unittest.main()
