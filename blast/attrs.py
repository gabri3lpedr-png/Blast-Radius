"""ATTR_LEVEL: which class attributes (and dict keys) a class-body edit changes. Static, ast only.

class_diff(old ClassDef, new ClassDef) compares the two bodies:
  - simple assignments `NAME = value` / `NAME: T = value` are attributes; anything else in the body
    (defs with their bodies stripped, nested class headers, docstrings, if/for/...) must be equal,
    and the class header (bases, keywords, decorators) too, else the edit is ambiguous (None);
  - an attribute whose value is a dict literal in both versions, with the same `**spread`s in the
    same places, changes key by key (added, removed, value changed); any other changed value
    changes the attribute as a whole;
  - an attribute whose value reads a changed attribute by bare name in the class body
    (`U = {**T}`) changes as a whole too (the read happens at class creation, invisible to the map);
    a changed attribute read by anything else in the body (a decorator, a default, a nested class
    header) is ambiguous.

Dict keys are written the way the map records them (docs/GUIDE_B_Selector.md, attr_users):
  str/int/float/bool/None/bytes constants -> repr(value);
  a dotted name (exp.Alias, TokenType.SELECT, exp.DataType.Type.INT) -> every dotted suffix of it,
  plus the same with import / module-level aliases undone (TT.SELECT with `TT = TokenType` ->
  TokenType.SELECT): a class key is recorded by __qualname__, an Enum member as
  "<type qualname>.<member name>", and the true spelling is one of those suffixes.
  A dotted name that looks like a module constant (lowercase head, ALL_CAPS tail: exp.SOME_CONST)
  or a bare lowercase/ALL_CAPS name could be any value -> ambiguous. Tuples and other literals the
  map cannot spell are recorded under "*", which is always selected.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field

_CONST = (str, int, float, bool, bytes, type(None))


@dataclass
class ClassDiff:
    whole: set[str] = field(default_factory=set)  # attributes changed as a whole
    keys: dict[str, set[str]] = field(default_factory=dict)  # dict attribute -> changed key spellings


def class_node(tree: ast.Module, classes: list[str]) -> ast.ClassDef | None:
    body, node = tree.body, None
    for name in classes:
        found = [n for n in body if isinstance(n, ast.ClassDef) and n.name == name]
        if len(found) != 1:
            return None
        node = found[0]
        body = node.body
    return node


def aliases(tree: ast.Module) -> dict[str, str]:
    """Names that are another name's alias at module level: `import x as y`, `Y = a.b.X`."""
    out: dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.asname and a.asname != a.name.rsplit(".", 1)[-1]:
                    out[a.asname] = a.name.rsplit(".", 1)[-1]
        elif (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
              and _dotted(n.value) is not None):
            out[n.targets[0].id] = _dotted(n.value)[-1]
    return out


def _dotted(node: ast.AST) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def key_spellings(node: ast.AST, alias: dict[str, str]) -> set[str] | None:
    """How the map may spell this dict key; None if we cannot tell (ambiguous)."""
    if isinstance(node, ast.Constant):
        return {repr(node.value)} if isinstance(node.value, _CONST) else None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        return set()  # unspellable: recorded under "*", which is always selected
    parts = _dotted(node)
    if parts is None:
        return None
    tail = parts[-1]
    if len(parts) == 1:
        if not tail[:1].isupper() or tail.isupper():
            return None  # a variable or a constant: its value could be anything
    elif tail.isupper() and not parts[-2][:1].isupper():
        return None  # module constant (exp.SOME_CONST), not an enum member
    out: set[str] = set()
    for variant in (parts, [alias.get(p, p) for p in parts]):
        for i in range(len(variant)):
            out.add(".".join(variant[i:]))
    return out


def _attr_of(stmt: ast.stmt) -> tuple[str, ast.AST] | None:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None


def _stripped(stmt: ast.stmt) -> ast.stmt:
    """A def keeps its header (name, args, defaults, decorators, returns) and loses its body; a
    nested class keeps its header (its body is diffed on its own)."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        s = copy.copy(stmt)
        s.body = []
        return s
    return stmt


def _split(cls: ast.ClassDef):
    attrs: dict[str, ast.AST] = {}
    others: list[ast.stmt] = []
    for stmt in cls.body:
        a = _attr_of(stmt)
        if a is None:
            others.append(_stripped(stmt))
        elif a[0] in attrs:
            return None  # assigned twice: order matters, keep it simple
        else:
            attrs[a[0]] = a[1]
    return attrs, others


def _dump(node: ast.AST | None) -> str:
    return "" if node is None else ast.dump(node, include_attributes=False)


def _header(cls: ast.ClassDef) -> str:
    return "|".join(_dump(n) for n in (*cls.bases, *cls.keywords, *cls.decorator_list)) + \
        "|".join(_dump(n) for n in getattr(cls, "type_params", []) or [])


def _reads(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _dict_entries(d: ast.Dict):
    """(spread dumps in order, {key dump: (key node, value dump, spreads before it)})."""
    spreads: list[str] = []
    entries: dict[str, tuple[ast.AST, str, int]] = {}
    for k, v in zip(d.keys, d.values):
        if k is None:
            spreads.append(_dump(v))
        else:
            entries[_dump(k)] = (k, _dump(v), len(spreads))
    return spreads, entries


def class_diff(old: ast.ClassDef, new: ast.ClassDef, old_alias: dict[str, str],
               new_alias: dict[str, str]) -> ClassDiff | None:
    if _header(old) != _header(new):
        return None
    so, sn = _split(old), _split(new)
    if so is None or sn is None:
        return None
    (ao, oo), (an, on) = so, sn
    if [_dump(s) for s in oo] != [_dump(s) for s in on]:
        return None
    changed = {n for n in ao.keys() | an.keys() if _dump(ao.get(n)) != _dump(an.get(n))}
    if not changed:
        return None
    diff = ClassDiff()
    for name in sorted(changed):
        vo, vn = ao.get(name), an.get(name)
        if isinstance(vo, ast.Dict) and isinstance(vn, ast.Dict):
            sp_o, eo = _dict_entries(vo)
            sp_n, en = _dict_entries(vn)
            if sp_o == sp_n:
                spell: set[str] = set()
                for k in eo.keys() | en.keys():
                    if eo.get(k, (None, None, None))[1:] == en.get(k, (None, None, None))[1:]:
                        continue
                    node = (en.get(k) or eo.get(k))[0]
                    s = key_spellings(node, new_alias if k in en else old_alias)
                    if s is None:
                        return None
                    spell |= s
                diff.keys[name] = spell
                continue
        diff.whole.add(name)
    # attributes that read a changed one at class creation change as a whole
    todo = set(changed)
    while todo:
        name = todo.pop()
        for attrs in (ao, an):
            for other, value in attrs.items():
                if other not in changed and name in _reads(value):
                    changed.add(other)
                    diff.whole.add(other)
                    todo.add(other)
        if any(name in _reads(s) for s in (*oo, *on)):
            return None
    for name in diff.whole:
        diff.keys.pop(name, None)
    return diff
