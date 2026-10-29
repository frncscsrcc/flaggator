"""The package layout imports, and an unknown process argument does not listen."""

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

    def test_an_unknown_argument_does_not_serve(self) -> None:
        """An argument other than bootstrap exits before a port is bound.

        No arguments is the serve command. This test does not call it,
        because that call listens until the process is killed.
        """
        with self.assertRaises(SystemExit) as caught:
            main.main(["listen"])
        self.assertEqual(caught.exception.code, "Usage: python -m main [bootstrap]")
