"""infect/_infect_rt.py (the INFECTION map's runtime) on hand-registered nodes."""

import enum
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_infect_rt_test", Path(__file__).resolve().parents[2] / "infect" / "_infect_rt.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

EQ, NE, IS, ISNOT, LT = 0, 1, 6, 7, 2


class Color(enum.Enum):
    RED = 1
    BLUE = 2


class Loud:
    def __eq__(self, other):  # user code: must never be called for an alternative
        raise AssertionError("user __eq__ called")

    __hash__ = object.__hash__


def node(kind, op, pure=None):
    nid = len(rt.NODES)
    rt.NODES.append(("x.py", 1, nid, 1, 1, kind, op))
    rt.NODE_OP.append(op)
    rt.FULL.append((rt.ALL & ~(1 << op)) if kind == "cmp" else 1)
    if pure is not None:
        rt.PURE[nid] = pure
    return nid


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        rt.cur = {}

    def test_enum_is_vs_eq_evaluated_for_real(self):
        n = node("cmp", IS)
        self.assertTrue(rt.c(n, Color.RED, Color.RED))
        self.assertFalse(rt.c(n, Color.RED, Color.BLUE))
        m = rt.cur[n]
        self.assertFalse(m >> EQ & 1)  # == agrees with `is` on enum members
        self.assertTrue(m >> ISNOT & 1 and m >> NE & 1)  # negations differ
        self.assertTrue(m >> LT & 1)  # ordering: not evaluated -> differs

    def test_user_eq_is_never_called(self):
        n = node("cmp", IS)
        a = Loud()
        self.assertTrue(rt.c(n, a, a))
        self.assertTrue(rt.cur[n] >> EQ & 1)  # unknown -> differs

    def test_none_and_classes(self):
        n = node("cmp", IS)
        rt.c(n, None, None)
        rt.c(n, int, str)
        self.assertFalse(rt.cur[n] >> EQ & 1)

    def run_bool(self, nid, op_and, values):
        """Evaluate values[0] OP values[1] ... through b1/bv/be, short-circuiting like Python."""
        res = rt.b1(nid, values[0])
        for i, v in enumerate(values[1:], start=1):
            if bool(res) != op_and:
                break
            res = rt.bv(nid, i, v)
        return rt.be(nid, res)

    def test_and_to_or_same_when_all_truthy_and_rest_pure(self):
        n = node("bool", 0, (False, True))
        self.run_bool(n, True, [1, 2])
        self.assertEqual(rt.cur[n], 0)

    def test_and_to_or_differs_when_an_impure_operand_ran(self):
        n = node("bool", 0, (False, False))
        self.run_bool(n, True, [1, 2])
        self.assertEqual(rt.cur[n], 1)  # or would skip the call the original made

    def test_and_to_or_differs_when_first_is_falsy(self):
        n = node("bool", 0, (True, True))
        self.run_bool(n, True, [0, 2])
        self.assertEqual(rt.cur[n], 1)  # or would need the second operand

    def test_or_to_and_same_when_all_falsy(self):
        n = node("bool", 1, (False, True, True))
        self.run_bool(n, False, [0, "", None])
        self.assertEqual(rt.cur[n], 0)
