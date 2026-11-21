"""Static import graph of the pinned source (sqlglot/** and tests/**), built at docker BUILD time.

<index>/imports.json = {
  "edges": [[importer path, imported path, line, top]],   top=1: runs when the importer is
            imported (module level or class body, any `if`/`try` incl. TYPE_CHECKING), 0: inside
            a def (runs when that def is called)
  "dynamic": [[path, first line, last line]]               defs that call importlib.import_module
            or __import__ (a module that fails to import is retried on every call)
  "lazy": {package path: [names]}                           direct submodules of a lazy package
            (whose __init__ has __getattr__) and the names that load them: module basename and
            top-level class names
  "attr_refs": {name: {path: [lines]}}                      x.NAME attribute uses of those names
  "classes": {"module:qualname": [base names]}              every class (nested ones too, not in
            defs) with the last dotted component of each base: subclasses by NAME (over-includes)
}

Resolution is conservative (over-includes): `import a.b.c` imports a, a.b and a.b.c; `from P import
N` imports P's prefixes, P, and P.N when that is a module; when P is a package whose __init__ defines
a module-level __getattr__ (lazy attributes: sqlglot.dialects, sqlglot.optimizer), N resolves to
P/<n lower>.py if that exists, else to every direct submodule of P; `from P import *` on such a
package imports every direct submodule.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path


def _module_paths(src: Path) -> dict[str, str]:
    out = {}
    for root in ("sqlglot", "tests"):
        for py in sorted((src / root).rglob("*.py")):
            rel = py.relative_to(src).as_posix()
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            out[mod] = rel
    return out


def _targets(t: ast.AST, out: set[str]) -> None:
    for sub in ast.walk(t):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            out.add(sub.id)


def _bound_at_top(tree: ast.Module) -> set[str]:
    """Names a module binds when it runs: module-level statements (into if/try/with/for bodies),
    not def or class bodies, not `if TYPE_CHECKING:` bodies. Under-approximates (safe here: a
    name we miss is treated as lazy, which over-includes)."""
    out: set[str] = set()

    def stmts(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                out.update(a.asname or a.name.split(".")[0] for a in node.names if a.name != "*")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    _targets(t, out)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                _targets(node.target, out)
            elif isinstance(node, ast.If):
                if not _type_checking(node.test):
                    stmts(node.body)
                stmts(node.orelse)
            elif isinstance(node, (ast.For, ast.While)):
                stmts(node.body)
                stmts(node.orelse)
            elif isinstance(node, ast.With):
                stmts(node.body)
            elif isinstance(node, ast.Try):
                stmts(node.body)
                for h in node.handlers:
                    stmts(h.body)
                stmts(node.orelse)
                stmts(node.finalbody)

    stmts(tree.body)
    return out


def _lazy_packages(src: Path, mods: dict[str, str]) -> dict[str, set[str]]:
    """Packages whose __init__ defines __getattr__ -> names their __init__ binds itself."""
    lazy: dict[str, set[str]] = {}
    for mod, rel in mods.items():
        if not rel.endswith("__init__.py"):
            continue
        try:
            tree = ast.parse((src / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError):
            lazy[mod] = set()  # cannot tell: treat as lazy (over-includes)
            continue
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__getattr__"
               for n in tree.body):
            lazy[mod] = _bound_at_top(tree) - {"__getattr__"}
    return lazy


def build(src: Path) -> dict:
    mods = _module_paths(src)
    lazy = _lazy_packages(src, mods)
    children: dict[str, list[str]] = defaultdict(list)
    for mod in mods:
        if "." in mod:
            children[mod.rsplit(".", 1)[0]].append(mod)

    def prefixes(mod: str) -> list[str]:
        parts = mod.split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts))]

    def from_targets(pkg: str, names: list[str]) -> set[str]:
        out = {p for p in prefixes(pkg) if p in mods}
        for n in names:
            if n == "*":
                if pkg in lazy:
                    out.update(children.get(pkg, []))
                continue
            sub = f"{pkg}.{n}"
            if sub in mods:
                out.add(sub)
            elif pkg in lazy and n not in lazy[pkg]:
                low = f"{pkg}.{n.lower()}"
                out.update([low] if low in mods else children.get(pkg, []))
        return out

    lazy_names: dict[str, dict[str, list[str]]] = {}  # pkg path -> submodule path -> names
    for pkg in lazy:
        subs = {}
        for sub in children.get(pkg, []):
            rel = mods[sub]
            names = [sub.rsplit(".", 1)[-1]]
            try:
                tree = ast.parse((src / rel).read_bytes(), filename=rel)
                names += [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
            except (SyntaxError, ValueError):
                pass
            subs[rel] = sorted(set(names))
        lazy_names[mods[pkg]] = subs
    wanted = {n for subs in lazy_names.values() for names in subs.values() for n in names}
    attr_refs: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))

    edges: list[list] = []
    dynamic: list[list] = []
    classes: dict[str, list[str]] = {}
    for mod, rel in sorted(mods.items(), key=lambda kv: kv[1]):
        try:
            tree = ast.parse((src / rel).read_bytes(), filename=rel)
        except (SyntaxError, ValueError):
            continue
        is_pkg = rel.endswith("__init__.py")
        here_pkg = mod if is_pkg else mod.rsplit(".", 1)[0]

        def visit(node: ast.AST, in_def: ast.AST | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.If) and _type_checking(child.test):
                    for c in child.orelse:  # the body never runs
                        visit_one(c, in_def)
                    continue
                visit_one(child, in_def)

        def visit_one(child: ast.AST, in_def: ast.AST | None) -> None:
            fn = in_def
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                fn = in_def or child
            if isinstance(child, ast.Import):
                targets = {p for a in child.names for p in prefixes(a.name) if p in mods}
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = here_pkg.split(".")
                    base = base[: len(base) - (child.level - 1)] if child.level > 1 else base
                    pkg = ".".join(base + ([child.module] if child.module else []))
                else:
                    pkg = child.module or ""
                targets = from_targets(pkg, [a.name for a in child.names])
            else:
                targets = set()
                if isinstance(child, ast.Call) and in_def is not None:
                    f = child.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in ("import_module", "__import__"):
                        d = in_def
                        dynamic.append([rel, min([d.lineno] + [x.lineno for x in getattr(d, "decorator_list", [])]),
                                        d.end_lineno])
            for t in sorted(targets):
                edges.append([rel, mods[t], child.lineno, 0 if in_def is not None else 1])
            visit(child, fn)

        visit(tree, None)

        def classes_in(body, prefix: str) -> None:
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = f"{prefix}{n.name}"
                    bases = []
                    for b in n.bases:
                        while isinstance(b, ast.Subscript):  # Generic[T]
                            b = b.value
                        bases.append(b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "?"))
                    classes[f"{mod}:{q}"] = bases
                    classes_in(n.body, q + ".")
                elif isinstance(n, (ast.If, ast.Try)):
                    classes_in(n.body + n.orelse + getattr(n, "finalbody", []), prefix)

        classes_in(tree.body, "")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in wanted:
                attr_refs[node.attr][rel].add(node.end_lineno)
    return {"edges": edges, "dynamic": sorted({tuple(d) for d in dynamic}), "lazy": lazy_names,
            "attr_refs": {n: {p: sorted(ls) for p, ls in v.items()} for n, v in attr_refs.items()},
            "classes": classes}


def _type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


class ImportGraph:
    def __init__(self, path: Path):
        raw = json.loads(path.read_text("utf-8"))
        self.top_importers: dict[str, set[str]] = defaultdict(set)
        self.lazy_sites: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for importer, target, line, top in raw["edges"]:
            if importer == target:
                continue
            if top:
                self.top_importers[target].add(importer)
            else:
                self.lazy_sites[target].append((importer, line))
        self.dynamic: list[tuple[str, int, int]] = [tuple(d) for d in raw["dynamic"]]
        self.lazy_names: dict[str, list[str]] = {
            sub: names for subs in raw["lazy"].values() for sub, names in subs.items()}
        self.attr_refs: dict[str, dict[str, list[int]]] = raw["attr_refs"]
        self.classes: dict[str, list[str]] | None = raw.get("classes")
        self._by_base: dict[str, list[str]] | None = None

    def subclasses(self, key: str) -> list[str] | None:
        """Class keys that may subclass `key` ("module:qualname"), by base NAME, transitively
        (same-named classes elsewhere come along: over-includes). None: no class index."""
        if self.classes is None:
            return None
        if self._by_base is None:
            self._by_base = defaultdict(list)
            for k, bases in self.classes.items():
                for b in bases:
                    self._by_base[b].append(k)
        out: set[str] = set()
        todo = [key.rsplit(".", 1)[-1].rsplit(":", 1)[-1]]
        names = set(todo)
        while todo:
            for k in self._by_base.get(todo.pop(), ()):
                if k not in out and k != key:
                    out.add(k)
                    n = k.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
                    if n not in names:
                        names.add(n)
                        todo.append(n)
        return sorted(out)

    def never_imported(self, path: str) -> bool:
        """No static import of it, no lazy attribute that loads it, not a package, not a file
        pytest collects (test_*.py imports itself at collection)."""
        name = path.rsplit("/", 1)[-1]
        return not (self.top_importers.get(path) or self.lazy_sites.get(path) or path in self.lazy_names
                    or name == "__init__.py" or name.startswith("test_") or name.endswith("_test.py")
                    or name == "conftest.py")

    def closure(self, path: str) -> set[str]:
        """Every file whose import fails when `path` fails to import (incl. itself)."""
        seen, todo = {path}, [path]
        while todo:
            for imp in self.top_importers.get(todo.pop(), ()):
                if imp not in seen:
                    seen.add(imp)
                    todo.append(imp)
        return seen
