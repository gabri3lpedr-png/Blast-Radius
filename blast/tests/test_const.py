"""CONST_INFECTION: one-literal Compare change detection and outcome check (no map needed)."""

import ast
import unittest

from blast import infection as I


def change(a, b):
    return I.single_const_change_at(ast.parse(a + "\n    pass"), ast.parse(b + "\n    pass"), 1)


class ConstTest(unittest.TestCase):
    def test_detects_one_literal(self):
        self.assertEqual(change("if x == 3:", "if x == 4:"), (1, 3, 1, "Eq", 3, 4))
        self.assertEqual(change('if x in ("a", "b"):', 'if x in ("a", "c"):')[3:], ("In", ("a", "b"), ("a", "c")))
        self.assertEqual(change("if -1 < y:", "if -2 < y:")[2:], (2, "Lt", -1, -2))

    def test_rejects_everything_else(self):
        for a, b in [("if x == 3:", "if y == 4:"), ("if x is None:", "if x is 3:"),
                     ("if x == 3 and q(5):", "if x == 3 and q(6):"), ("if x == 3:", "if x != 3:"),
                     ("if x == 3 and y == 1:", "if x == 4 and y == 2:"), ("if f(1) == 3:", "if f(2) == 3:")]:
            self.assertIsNone(change(a, b), (a, b))

    def test_outcome(self):
        self.assertTrue(I._all_same("Eq", 1, ["5", "'a'"], 3, 4))
        self.assertFalse(I._all_same("Eq", 1, ["3"], 3, 4))
        self.assertFalse(I._all_same("Eq", 1, "many", 3, 4))
        self.assertFalse(I._all_same("Lt", 1, ["'a'"], 3, 4))  # TypeError -> differs
        self.assertFalse(I._all_same("Eq", 1, ["nan"], 3, 4))  # not a literal -> differs
        self.assertTrue(I._all_same("In", 1, ["'z'"], ("a", "b"), ("a", "c")))
        self.assertFalse(I._all_same("In", 1, ["'c'"], ("a", "b"), ("a", "c")))


if __name__ == "__main__":
    unittest.main()
