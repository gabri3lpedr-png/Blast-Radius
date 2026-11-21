"""Where is a line in a Python file? (ast, cached per file)

locate(line) gives:
  stmt_lines  the lines to look up in the map for this line: all lines of the smallest
              enclosing simple statement; for a compound statement's header (def/class/if/...
              up to its body, decorators included) the header lines; for a line inside no
              statement of its own (blank, comment, `else:`), the line plus the enclosing header
              and the nearest statements before and after it.
  kind        "module"      module level, outside any class or def (runs at import)
              "class"       class body or class header, the class not inside a def (import)
              "def_header"  def line/decorator/defaults of a def not inside another def (import)
              "def_body"    inside a def body (runs when called)
  func        innermost def whose body holds the line (def_body), or the def (def_header)
  classes     enclosing class names, outer -> inner (only when no def lies between)
  top         the module-level statement holding the line (or None)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

_COMPOUND = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def module_of(path: str) -> str:
    mod = path[:-3] if path.endswith(".py") else path
    mod = mod.replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _start(node: ast.AST) -> int:
    decos = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decos])


def _header_end(node: ast.AST) -> int | None:
    """Last header line of a compound statement, None for a simple one."""
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body or not isinstance(body[0], ast.stmt):
        return None
    return max(node.lineno, body[0].lineno - 1)


@dataclass
class Loc:
    line: int
    stmt_lines: list[int]
    kind: str
    func: ast.AST | None = None
    classes: list[str] = field(default_factory=list)
    top: ast.stmt | None = None

    def class_keys(self, module: str) -> list[str]:
        return [f"{module}:{'.'.join(self.classes[: i + 1])}" for i in range(len(self.classes))]


class Locator:
    def __init__(self, text: str, path: str):
        self.path = path
        self.module = module_of(path)
        self.tree = ast.parse(text.encode("utf-8"), filename=path)
        # (start line incl. decorators, end line, header end or None, node) for every statement
        self._stmts: list[tuple[int, int, int | None, ast.stmt]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.stmt):
                self._stmts.append((_start(node), node.end_lineno, _header_end(node), node))

    @staticmethod
    def _anchor(start: int, end: int, hdr: int | None) -> range:
        return range(start, (hdr if hdr is not None else end) + 1)

    def _stmt_lines(self, line: int) -> list[int]:
        best = None
        for s, e, h, node in self._stmts:
            if s <= line <= e and (best is None or e - s < best[1] - best[0]):
                best = (s, e, h, node)
        if best is not None:
            s, e, h, node = best
            if h is None or line <= h:
                return list(self._anchor(s, e, h))
        # Not inside a statement of its own: the enclosing header + nearest neighbours.
        lines = {line}
        if best is not None:
            lines.update(self._anchor(best[0], best[1], best[2]))
        prev = nxt = None
        for s, e, h, node in self._stmts:
            if e < line and (prev is None or e > prev[1] or (e == prev[1] and s > prev[0])):
                prev = (s, e, h)
            if s > line and (nxt is None or s < nxt[0] or (s == nxt[0] and e < nxt[1])):
                nxt = (s, e, h)
        for nb in (prev, nxt):
            if nb is not None:
                lines.update(self._anchor(*nb))
        return sorted(lines)

    def locate(self, line: int) -> Loc:
        stmt_lines = self._stmt_lines(line)
        kind, func, classes, top = "module", None, [], None
        body: list[ast.stmt] = self.tree.body
        depth_in_def = False
        while True:
            node = next((n for n in body if _start(n) <= line <= n.end_lineno), None)
            if node is None:
                break
            if top is None:
                top = node
            if isinstance(node, _COMPOUND):
                hdr = _header_end(node)
                in_header = hdr is not None and line <= hdr
                if isinstance(node, ast.ClassDef):
                    if not depth_in_def:
                        classes.append(node.name)
                        kind = "class"
                    if in_header:
                        break
                else:
                    if in_header:
                        if not depth_in_def:
                            kind, func = "def_header", node
                        break
                    depth_in_def = True
                    kind, func = "def_body", node
            # descend into whichever child list holds the line
            children = []
            for name in ("body", "orelse", "finalbody", "handlers", "cases"):
                for c in getattr(node, name, None) or []:
                    if isinstance(c, ast.stmt):
                        children.append(c)
                    else:  # ExceptHandler / match_case: look one level deeper
                        children.extend(getattr(c, "body", None) or [])
            if not children:
                break
            body = children
        return Loc(line, stmt_lines, kind, func, classes if kind != "def_body" else [], top)


def names_bound(stmt: ast.stmt) -> list[str] | None:
    """Names a module-level statement binds; None if we cannot say (-> FULL)."""
    out: list[str] = []

    def target(t: ast.AST) -> bool:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            return all(target(e) for e in t.elts)
        elif isinstance(t, ast.Starred):
            return target(t.value)
        elif isinstance(t, (ast.Attribute, ast.Subscript)):
            base = t.value
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if not isinstance(base, ast.Name):
                return False
            out.append(base.id)  # mutating X.attr / X[k] changes what X's users see
        else:
            return False
        return True

    if isinstance(stmt, ast.Assign):
        ok = all(target(t) for t in stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        ok = target(stmt.target)
    elif isinstance(stmt, ast.TypeAlias):
        ok = target(stmt.name)
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.append(stmt.name)
        ok = True
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        ok = True
        for a in stmt.names:
            if a.name == "*":
                return None
            out.append(a.asname or a.name.split(".")[0])
    else:
        ok = False
    return out if ok and out else None
