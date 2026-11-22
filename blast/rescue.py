"""Static checks behind RESCUE_IMPORT / RESCUE_NOTHING (blast/select.py).

writes_only_owned(nodes, tree)
    True when the code in `nodes` can only change state owned by the module `tree` (or locals /
    parameters of its own defs): no global/nonlocal, every attribute / subscript / del target and
    every setattr / mutating method call (append, update, register, ...) is rooted at a name the
    module binds itself by def, class or an assignment that makes a NEW object (not an alias
    `X = other.attr`). Then the change's effect is visible only to code holding the module's
    objects, i.e. code that imported it: the importer closure (rule x) is a superset. Anything
    else (writing into another module's registry, an alias of a foreign object, an unknown root)
    -> False: a test that never imports the module could observe it after an earlier test in the
    same worker imported it.

mentions(src, needles)
    Whether any .py file under tests/ or sqlglot/ contains one of the strings, anywhere (code,
    string or comment: the scan is deliberately coarse, a false hit only keeps FULL).

lists_directories(src)
    Whether any test file lists a directory (os.walk, glob, listdir, iterdir, scandir): a data
    file could then be read without its name appearing anywhere.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

MUTATORS = frozenset({
    "append", "extend", "insert", "update", "add", "discard", "remove", "pop", "popitem", "clear",
    "setdefault", "sort", "reverse", "register", "__setitem__", "__delitem__", "__setattr__",
    "__delattr__",
})


def _owned(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound only by def / class / assignment of a fresh value."""
    good: set[str] = set()
    bad: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            good.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            alias = isinstance(node.value, (ast.Name, ast.Attribute, ast.Subscript)) or node.value is None
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        (bad if alias else good).add(n.id)
        else:
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    bad.add(n.id)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    for a in n.names:
                        bad.add((a.asname or a.name).split(".")[0])
    return frozenset(good - bad)


def _root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _locals(func: ast.AST) -> set[str]:
    a = func.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    names |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
    for n in ast.walk(func):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not func:
            names.add(n.name)
    return names


def writes_only_owned(nodes, tree: ast.Module) -> bool:
    owned = _owned(tree)

    def ok_root(node: ast.AST, scope: set[str]) -> bool:
        r = _root(node)
        return r is not None and (r in owned or r in scope)

    def check(node: ast.AST, scope: set[str]) -> bool:
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = scope | (_locals(node) if not isinstance(node, ast.Lambda)
                             else {x.arg for x in node.args.args})
            return all(check(c, inner) for c in ast.iter_child_nodes(node))
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = node.targets
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    if not ok_root(n, scope):
                        return False
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in ("setattr", "delattr", "exec", "eval", "globals", "vars"):
                if f.id in ("exec", "eval", "globals", "vars") or not node.args or not ok_root(node.args[0], scope):
                    return False
            if isinstance(f, ast.Attribute) and f.attr in MUTATORS and not ok_root(f.value, scope):
                return False
        return all(check(c, scope) for c in ast.iter_child_nodes(node))

    return all(check(n, set()) for n in nodes if n is not None)


@lru_cache(maxsize=None)
def _py_texts(src: str) -> tuple[str, ...]:
    root = Path(src)
    out = []
    for sub in ("tests", "sqlglot"):
        for p in sorted((root / sub).rglob("*.py")):
            try:
                out.append(p.read_text("utf-8", "replace"))
            except OSError:
                out.append("")
    return tuple(out)


def mentions(src: Path, needles) -> bool:
    needles = [n for n in needles if n]
    return any(n in text for text in _py_texts(str(src)) for n in needles)


_LISTING = re.compile(r"(\bos\.walk\b|\bglob\(|\.glob\(|\blistdir\b|\biterdir\b|\bscandir\b|\brglob\b)")


@lru_cache(maxsize=None)
def lists_directories(src: Path) -> bool:
    root = Path(src) / "tests"
    for p in root.rglob("*.py"):
        try:
            if _LISTING.search(p.read_text("utf-8", "replace")):
                return True
        except OSError:
            return True
    return False
