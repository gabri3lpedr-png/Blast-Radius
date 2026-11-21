"""INFECTION (weak mutation): is a file's change exactly one operator, and which tests can it reach?

single_op_change(old_text, new_text) -> (lineno, col, kind, new_op) of the ORIGINAL node when the
two ASTs are equal except for one single-operator Compare whose operator changed (same operands)
or one BoolOp whose and/or flipped (same operands); else None.

InfectionMap (maps/infection_map.json.gz, built by infect/make_infection_map.sh on the pinned
sqlglot): per node, the tests that evaluated it and, per alternative operator, the tests in which
that operator would have changed the outcome (or what got evaluated) at least once.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
from pathlib import Path

CMP_NAMES = {ast.Eq: "Eq", ast.NotEq: "NotEq", ast.Lt: "Lt", ast.LtE: "LtE", ast.Gt: "Gt", ast.GtE: "GtE",
             ast.Is: "Is", ast.IsNot: "IsNot", ast.In: "In", ast.NotIn: "NotIn"}
MAP_PATH = Path(os.environ.get("INFECTION_MAP", Path(__file__).resolve().parent.parent / "maps" / "infection_map.json.gz"))


class _Ambiguous(Exception):
    pass


def _diff(a, b, found: list) -> None:
    """Walk two ASTs together; collect operator-only differences, raise on anything else."""
    if type(a) is not type(b):
        raise _Ambiguous
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            found.append(("cmp", a, CMP_NAMES[type(b.ops[0])]))
            _diff(a.left, b.left, found)
            _diff(a.comparators, b.comparators, found)
            return
        if isinstance(a, ast.BoolOp) and type(a.op) is not type(b.op):
            found.append(("bool", a, "And" if isinstance(b.op, ast.And) else "Or"))
            _diff(a.values, b.values, found)
            return
        for f in a._fields:
            _diff(getattr(a, f, None), getattr(b, f, None), found)
        return
    if isinstance(a, list):
        if len(a) != len(b):
            raise _Ambiguous
        for x, y in zip(a, b):
            _diff(x, y, found)
        return
    if a != b:
        raise _Ambiguous


def single_op_change(old_text: str, new_text: str):
    try:
        found: list = []
        _diff(ast.parse(old_text), ast.parse(new_text), found)
    except (_Ambiguous, SyntaxError, ValueError, RecursionError):
        return None
    if len(found) != 1:
        return None
    kind, node, new_op = found[0]
    return node.lineno, node.col_offset, kind, new_op


def _innermost(tree, line: int):
    """The innermost statement whose span holds `line`, descending by line range (not a walk)."""
    best, body = None, tree.body
    while body:
        node = next((n for n in body if isinstance(n, ast.stmt)
                     and min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]) <= line
                     <= n.end_lineno), None)
        if node is None:
            break
        best = node
        children = []
        for name in ("body", "orelse", "finalbody", "handlers", "cases"):
            for c in getattr(node, name, None) or []:
                if isinstance(c, ast.stmt):
                    children.append(c)
                else:  # ExceptHandler / match_case
                    children.extend(getattr(c, "body", None) or [])
        body = children
    return best


def single_op_change_at(old_tree, new_tree, line: int):
    """As single_op_change, for a patch that replaced exactly one line (`line`, same number in
    both texts): only the innermost statement holding it can differ, and it must span the same
    lines in both (else the edit reached beyond it); diff just that statement."""
    a, b = _innermost(old_tree, line), _innermost(new_tree, line)
    if a is None or b is None or (a.lineno, a.end_lineno) != (b.lineno, b.end_lineno):
        return None
    try:
        found: list = []
        _diff(a, b, found)
    except (_Ambiguous, RecursionError):
        return None
    if len(found) != 1:
        return None
    kind, node, new_op = found[0]
    return node.lineno, node.col_offset, kind, new_op


class InfectionMap:
    def __init__(self, number_by_id: dict[str, int], path: Path = MAP_PATH):
        self.ok = False
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if raw.get("failed"):
            return  # a map built from a run with failures is not trusted
        self._num = [number_by_id.get(t) for t in raw["tests"]]
        self._sets = raw["sets"]
        self._nodes = raw["nodes"]
        self._cache: dict[int, frozenset[int]] = {}
        self.ok = True

    def _set(self, i: int) -> frozenset[int]:
        s = self._cache.get(i)
        if s is None:
            s = self._cache[i] = frozenset(n for n in (self._num[t] for t in self._sets[i]) if n is not None)
        return s

    def lookup(self, path: str, line: int, col: int, kind: str, new_op: str):
        """(tests that evaluated the node, tests where new_op differs) or (None, why)."""
        e = self._nodes.get(f"{path}:{line}:{col}:{kind}")
        if e is None:
            return None, "node not in the infection map"
        if e["op"] == new_op:
            return None, "same operator"
        if e["import_exec"] and new_op in e["import_alt"]:
            return None, "the new operator differs at import time"
        differs = self._set(e["alt"][new_op]) if new_op in e["alt"] else frozenset()
        return self._set(e["exec"]), differs
