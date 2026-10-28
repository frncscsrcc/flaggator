"""The package layout imports, and the process entry does not serve yet."""

import sys
import unittest
from pathlib import Path

# unittest does not read pyproject.toml. The test adds src itself so the
# suite runs without an editable install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import api
import cache
import composition
import domain
import domain.models
import domain.services
import main
import repositories


class LayoutTests(unittest.TestCase):
    def test_packages_import(self) -> None:
        """Importing the layout has a docstring and no listener behind it.

        A missing package would fail at import time, above this method.
        """
        packages = (
            api,
            cache,
            composition,
            domain,
            domain.models,
            domain.services,
            main,
            repositories,
        )
        for package in packages:
            self.assertTrue(package.__doc__)

    def test_main_does_not_serve(self) -> None:
        """main exits before a listener exists.

        A successful return would look like a running server. The process
        entry phase replaces this behavior.
        """
        with self.assertRaises(SystemExit) as caught:
            main.main()
        self.assertEqual(
            caught.exception.code,
            "flaggator cannot serve yet. The HTTP listener is added in a later phase.",
        )
