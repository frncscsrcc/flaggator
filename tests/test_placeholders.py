"""Placeholder rewrite for MySQL. No server and no PyMySQL connection."""

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from repositories.mysql import rewrite_placeholders


class PlaceholderTests(unittest.TestCase):
    def test_question_marks_become_percent_s(self) -> None:
        """Bound values outside quotes are rewritten. The SQL text stays readable."""
        sql = "SELECT `key`, name FROM projects WHERE project_id = ? AND `key` = ?"
        self.assertEqual(
            rewrite_placeholders(sql),
            "SELECT `key`, name FROM projects WHERE project_id = %s AND `key` = %s",
        )

    def test_quotes_and_comments_are_left_alone(self) -> None:
        """A ? or % inside a string or a comment is not a placeholder.

        A % outside quotes is escaped because PyMySQL formats the statement
        with Python's percent operator.
        """
        sql = "SELECT '100%', 'a?b', ? -- keep ?\n"
        self.assertEqual(
            rewrite_placeholders(sql),
            "SELECT '100%', 'a?b', %s -- keep ?\n",
        )
        self.assertEqual(rewrite_placeholders("SELECT 100%"), "SELECT 100%%")
