"""AST instrumentation for the INFECTION map (dev-only). See _infect_rt.py for the runtime.

install(root) puts a meta-path finder first: every module under <root>/sqlglot/ is compiled from
its transformed AST (line numbers kept; no file is written; no .pyc is read).
"""
import ast
import importlib.abc
import importlib.machinery
import os
import sys

import _infect_rt as rt

CMP_INDEX = {ast.Eq: 0, ast.NotEq: 1, ast.Lt: 2, ast.LtE: 3, ast.Gt: 4, ast.GtE: 5,
             ast.Is: 6, ast.IsNot: 7, ast.In: 8, ast.NotIn: 9}


def _bool_context(tree) -> set:
    """ids of expression nodes whose value is only used for its truth."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.If, ast.While, ast.Assert, ast.IfExp)):
            out.add(id(n.test))
        elif isinstance(n, ast.comprehension):
            out.update(id(x) for x in n.ifs)
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.add(id(n.operand))
    changed = True
    while changed:  # a BoolOp in a boolean context passes the context to its operands
        changed = False
        for n in ast.walk(tree):
            if isinstance(n, ast.BoolOp) and id(n) in out:
                for v in n.values:
                    if id(v) not in out:
                        out.add(id(v))
                        changed = True
    return out


def _pure(e) -> bool:
    return isinstance(e, (ast.Name, ast.Constant))


class _Tx(ast.NodeTransformer):
    def __init__(self, path, boolctx):
        self.path, self.boolctx = path, boolctx

    def _reg(self, node, kind, op):
        nid = len(rt.NODES)
        rt.NODES.append((self.path, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind, op))
        rt.NODE_OP.append(op)
        rt.FULL.append((rt.ALL & ~(1 << op)) if kind == "cmp" else 1)
        return nid

    @staticmethod
    def _call(fn, args, like):
        call = ast.Call(func=ast.Attribute(value=ast.Name(id="_IRT", ctx=ast.Load()), attr=fn, ctx=ast.Load()),
                        args=args, keywords=[])
        return ast.copy_location(call, like)

    def visit_Compare(self, node):
        is_target = len(node.ops) == 1
        nid = self._reg(node, "cmp", CMP_INDEX[type(node.ops[0])]) if is_target else None
        self.generic_visit(node)
        if not is_target:
            return node
        return self._call("c", [ast.Constant(nid), node.left, node.comparators[0]], node)

    def visit_BoolOp(self, node):
        target = id(node) in self.boolctx and all(_pure(v) for v in node.values[1:])
        nid = self._reg(node, "bool", 0 if isinstance(node.op, ast.And) else 1) if target else None
        self.generic_visit(node)
        if not target:
            return node
        vals = [self._call("b1", [ast.Constant(nid), node.values[0]], node.values[0])]
        vals += [self._call("bv", [ast.Constant(nid), v], v) for v in node.values[1:]]
        inner = ast.copy_location(ast.BoolOp(op=node.op, values=vals), node)
        return self._call("be", [ast.Constant(nid), inner], node)


def transform(source: bytes, path: str, rel: str):
    tree = ast.parse(source, filename=path)
    tree = _Tx(rel, _bool_context(tree)).visit(tree)
    # `import _infect_rt as _IRT` after the docstring and __future__ imports
    body = tree.body
    i = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        i = 1
    while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
        i += 1
    imp = ast.Import(names=[ast.alias(name="_infect_rt", asname="_IRT")])
    body.insert(i, ast.copy_location(imp, body[i] if i < len(body) else tree))
    ast.fix_missing_locations(tree)
    return tree


class _Loader(importlib.machinery.SourceFileLoader):
    root = ""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        tree = transform(self.get_data(path), path, rel)
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.prefix = os.path.join(root, "sqlglot") + os.sep
        _Loader.root = root

    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not os.path.realpath(spec.origin).startswith(self.prefix):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


def install(root: str) -> None:
    sys.meta_path.insert(0, _Finder(os.path.realpath(root)))
