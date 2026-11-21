"""Runtime of the INFECTION instrumentation (dev-only, map building). Imported as _IRT.

c(nid, a, b)    a single-operator Compare: returns `a OP b` (operands already evaluated once, in
                order) and records, for the current context, which alternative operators would
                have given a different truth value (or a non-bool, or an exception).
b1 / bv / be    a BoolOp in a boolean context whose later operands are names/constants:
                b1(first operand) ... be(whole result). Records whether swapping and <-> or could
                change the outcome or what gets evaluated.
`cur` is the current context's {nid: bitmask of differing alternatives}; presence = executed.
"""
import operator
import sys
import types

# Compare operators, by index. The bit of index i in a mask = "alternative i differs".
NAMES = ["Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn"]
OPS = [operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge,
       operator.is_, operator.is_not, lambda a, b: a in b, lambda a, b: a not in b]
NEG = {0: 1, 1: 0, 6: 7, 7: 6, 8: 9, 9: 8}
ALL = (1 << len(OPS)) - 1
IDENTITY = (1 << 6) | (1 << 7)

# registry, filled by the loader: nid -> (path, lineno, col, end_lineno, end_col, kind, op index)
NODES: list = []
NODE_OP: list = []
FULL: list = []

cur: dict = {}

_SCALARS = frozenset({type(None), bool, int, float, complex, str, bytes})
_CONTAINERS = frozenset({tuple, list, set, frozenset, dict})
_CMP_DUNDERS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__", "__contains__")
_builtin_types = {}


def _builtin_methods(t) -> bool:
    """No comparison/hash/contains dunder of type t is written in Python (no user code runs)."""
    r = _builtin_types.get(t)
    if r is None:
        r = not any(isinstance(getattr(t, n, None), types.FunctionType) for n in _CMP_DUNDERS)
        _builtin_types[t] = r
    return r


def _safe(x, depth=2) -> bool:
    t = type(x)
    if t in _SCALARS:
        return True
    if isinstance(x, type):
        return _builtin_methods(t)  # the metaclass compares classes
    if t in _CONTAINERS:
        if depth <= 0 or len(x) > 16:
            return False
        if t is dict:
            return all(_safe(k, depth - 1) and _safe(v, depth - 1) for k, v in x.items())
        return all(_safe(e, depth - 1) for e in x)
    # enum members and other plain objects: identity/builtin comparisons only
    return _builtin_methods(t) and not hasattr(x, "__len__") and not hasattr(x, "__iter__")


def c(nid, a, b):
    op = NODE_OP[nid]
    try:
        r = OPS[op](a, b)
    except BaseException:
        cur[nid] = FULL[nid]
        raise
    m = cur.get(nid)
    full = FULL[nid]
    if m == full:
        return r
    m = m or 0
    if type(r) is not bool:
        m = full
    else:
        neg = NEG.get(op)
        if neg is not None:
            m |= 1 << neg
        alts = full & ~m
        if alts:
            if not (_safe(a) and _safe(b)):
                m |= alts & ~IDENTITY  # only identity checks run no user code
                alts &= IDENTITY
            i = 0
            while alts:
                if alts & 1:
                    try:
                        ar = OPS[i](a, b)
                        if type(ar) is not bool or ar != r:
                            m |= 1 << i
                    except BaseException:
                        m |= 1 << i
                alts >>= 1
                i += 1
    cur[nid] = m
    return r


# ---------------------------------------------------------------- BoolOp
_stack: dict = {}


def _plain_truth(v) -> bool:
    t = type(v)
    if t in _SCALARS or t in _CONTAINERS:
        return True
    return not (isinstance(getattr(t, "__bool__", None), types.FunctionType)
                or isinstance(getattr(t, "__len__", None), types.FunctionType))


def b1(nid, v):
    ok = _plain_truth(v)
    _stack.setdefault(nid, []).append([id(sys._getframe(1)), bool(v) if ok else None, ok])
    return v


def bv(nid, v):
    st = _stack.get(nid)
    if st:
        f = id(sys._getframe(1))
        for e in reversed(st):
            if e[0] == f:
                if not _plain_truth(v):
                    e[2] = False
                break
    return v


def be(nid, res):
    st = _stack.get(nid)
    f = id(sys._getframe(1))
    entry = None
    while st:
        e = st.pop()
        if e[0] == f:
            entry = e
            break
    if entry is None:
        cur[nid] = 1
        return res
    _, t1, ok = entry
    if not ok or not _plain_truth(res):
        differs = True
    else:
        truth = bool(res)
        if NODE_OP[nid] == 0:  # and -> or: same only if the first is truthy and so is the result
            differs = not t1 or not truth
        else:  # or -> and: same only if the first is falsy and so is the result
            differs = t1 or truth
    cur[nid] = cur.get(nid, 0) | (1 if differs else 0)
    return res
