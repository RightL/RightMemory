import os
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.install_core import _posix_data_home


class InstallCoreTests(unittest.TestCase):
    def test_empty_xdg_data_home_uses_standard_default(self):
        home = Path("test-home")

        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": ""}, clear=False),
            patch("rightmemory.install_core.Path.home", return_value=home),
        ):
            data_home = _posix_data_home()

        self.assertEqual(data_home, home / ".local" / "share")

    def test_nonempty_xdg_data_home_is_respected(self):
        configured = Path("custom-data-home")

        with patch.dict(os.environ, {"XDG_DATA_HOME": str(configured)}, clear=False):
            data_home = _posix_data_home()

        self.assertEqual(data_home, configured)


if __name__ == "__main__":
    unittest.main()
