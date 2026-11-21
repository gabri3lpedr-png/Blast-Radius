"""Map v4: per-test CLASS ATTRIBUTE reads and dict-attribute KEY lookups.

Imported by blast_plugin BEFORE sqlglot, so every sqlglot class is wrapped as
soon as its `class` statement finishes (builtins.__build_class__ hook) and
reads made while later modules import (subclass bodies, metaclasses) are seen.
Follows the "v4 contract" in docs/GUIDE_B_Selector.md:

  attr_users  "module:Owner.ATTR"        tests that read the attribute
  key_users   "module:Owner.ATTR[key]"   tests that looked `key` up (hit OR miss)
              "module:Owner.ATTR[*]"     tests that used the dict as a whole
  attr_import / key_import               the same keys, read outside any test

Owner = the class the attribute was LOOKED UP ON (type(obj) for instance reads,
the class itself for class reads), not the class that defines it. A non-sqlglot
owner (a test's own Dialect subclass) is recorded as its first sqlglot ancestor.

How: every plain class attribute (no __get__: not a function, property,
classmethod, nested class...) in a sqlglot class __dict__ is replaced by a
non-data descriptor that records (owner, name) and returns the original value.
Dict values become _TrackedDict (a dict subclass) that records looked-up keys
per dict object; at export a key lookup is credited to every (owner, ATTR) the
same test read that resolves to that dict (or, if the test never read it, to
every (owner, ATTR) that ever did).

Class-creation hooks (_Dialect.__new__, __init_subclass__) read the NEW class's
own attributes before the class can be wrapped (e.g. TIME_MAPPING -> TIME_TRIE,
KEYWORDS -> _KEYWORD_TRIE). Their attribute names are found by an AST scan of
those hooks, and each such own attribute is listed in attr_import (+ "[*]" in
key_import for dicts), so the selector falls back to class_users for it.
"""
import ast
import builtins
import enum
import os
import sys

ROOT = "/opt/sqlglot"
STAR = object()

_ctx = ""                   # current context: "" (import/outside tests) or a node id key
_reads = {"": set()}        # ctx -> {(id(owner), name)}
_keys = {"": {}}            # ctx -> {id(tracked dict): set(raw keys | STAR)}
_cur_reads = _reads[""]
_cur_keys = _keys[""]
_owners = {}                # id(owner) -> owner (keeps it alive, so ids stay unique)
_tracked = {}               # id(original dict) -> _TrackedDict (aliases share one)
_keep = []                  # originals, kept alive so their ids are never reused
_wrapped = {}               # class -> set of wrapped attribute names
_fixed_alias = {}           # id(tracked dict) -> {"module:Owner.ATTR"} it stands for
skipped_classes = []        # "module:qualname: reason"
skipped_modules = set()


# ------------------------------------------------------------------ recording
def set_context(key):
    global _ctx, _cur_reads, _cur_keys
    _ctx = key
    _cur_reads = _reads.setdefault(key, set())
    _cur_keys = _keys.setdefault(key, {})


def _note_key(d, k):
    s = _cur_keys.get(id(d))
    if s is None:
        s = _cur_keys[id(d)] = set()
    try:
        s.add(k)
    except TypeError:       # unhashable key: lookup will fail anyway; "can't spell it"
        s.add(STAR)


def _note_all(d):
    s = _cur_keys.get(id(d))
    if s is None:
        s = _cur_keys[id(d)] = set()
    s.add(STAR)


class _TrackedDict(dict):
    """dict that records key lookups (hits and misses) and whole-dict use."""
    __slots__ = ("__weakref__",)

    def __getitem__(self, k):
        _note_key(self, k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _note_key(self, k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        _note_key(self, k)
        return dict.__contains__(self, k)

    def setdefault(self, k, default=None):
        _note_key(self, k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *default):
        _note_key(self, k)
        return dict.pop(self, k, *default)

    def __setitem__(self, k, v):
        _note_key(self, k)
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        _note_key(self, k)
        dict.__delitem__(self, k)

    # whole-dict use -> "[*]". Overriding __iter__ also makes dict(d), {**d},
    # x.update(d) and d | x go through keys() instead of the C fast path.
    def __iter__(self):
        _note_all(self)
        return dict.__iter__(self)

    def keys(self):
        _note_all(self)
        return dict.keys(self)

    def values(self):
        _note_all(self)
        return dict.values(self)

    def items(self):
        _note_all(self)
        return dict.items(self)

    def __len__(self):
        _note_all(self)
        return dict.__len__(self)

    def __bool__(self):
        _note_all(self)
        return dict.__len__(self) > 0

    def copy(self):
        _note_all(self)
        return dict(dict.items(self))

    def __or__(self, other):
        _note_all(self)
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        _note_all(self)
        return dict(other) | dict(dict.items(self))

    def __ior__(self, other):
        _note_all(self)
        dict.update(self, other)
        return self

    def __eq__(self, other):
        _note_all(self)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _note_all(self)
        return dict.__ne__(self, other)

    __hash__ = None

    def __repr__(self):
        _note_all(self)
        return dict.__repr__(self)

    def update(self, *a, **k):
        _note_all(self)
        dict.update(self, *a, **k)

    def popitem(self):
        _note_all(self)
        return dict.popitem(self)

    def clear(self):
        _note_all(self)
        dict.clear(self)

    def __reduce_ex__(self, protocol):
        _note_all(self)
        return (dict, (dict(dict.items(self)),))

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        import copy
        _note_all(self)
        return copy.deepcopy(dict(dict.items(self)), memo)


class _Attr:
    """Non-data descriptor standing in for a plain class attribute."""
    __slots__ = ("name", "value")

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __get__(self, obj, owner=None):
        if owner is None:
            owner = type(obj)
        i = id(owner)
        _cur_reads.add((i, self.name))
        if i not in _owners:
            _owners[i] = owner
        return self.value

    # `klass.__dict__.get("X") or {}` style code sees the descriptor itself.
    def __bool__(self):
        return bool(self.value)

    def __or__(self, other):
        return self.value | other

    def __ror__(self, other):
        return other | self.value


# ------------------------------------------------------------------ wrapping
def _is_sqlglot(cls):
    return getattr(cls, "__module__", "").startswith("sqlglot")


def _plain(v):
    return not hasattr(type(v), "__get__") and not isinstance(v, type)


def wrap_class(cls):
    if cls in _wrapped or not _is_sqlglot(cls):
        return
    if cls.__module__ in skipped_modules:
        return
    if issubclass(cls, (enum.Enum, BaseException)) or "__dataclass_fields__" in cls.__dict__:
        _wrapped[cls] = set()
        return
    names = _wrapped.setdefault(cls, set())
    _rewrap(cls, names)


def _rewrap(cls, names):
    for name, v in list(cls.__dict__.items()):
        if name.startswith("__") or isinstance(v, _Attr) or not _plain(v):
            continue
        if name.endswith("_TRIE"):      # nested tries: hot, derived at import
            continue
        if type(v) is dict:
            td = _tracked.get(id(v))
            if td is None:
                td = _tracked[id(v)] = _TrackedDict(v)
                _keep.append(v)
            v = td
        try:
            setattr(cls, name, _Attr(name, v))
            names.add(name)
        except (TypeError, AttributeError) as e:
            skipped_classes.append(f"{cls.__module__}:{cls.__qualname__}.{name}: {e}")


def sweep():
    """Wrap classes made without a class statement (type(...)) and re-wrap
    attributes that were re-assigned. Cheap enough to run before every test."""
    stack, seen = list(_wrapped), set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        if c not in _wrapped:
            wrap_class(c)
        elif _wrapped[c] or not issubclass(c, (enum.Enum, BaseException)):
            if "__dataclass_fields__" not in c.__dict__:
                _rewrap(c, _wrapped[c])
        try:
            stack.extend(type.__subclasses__(c))
        except TypeError:
            pass


_real_build_class = builtins.__build_class__


def _build_class(func, name, *bases, **kw):
    cls = _real_build_class(func, name, *bases, **kw)
    g = getattr(func, "__globals__", {})
    mod = g.get("__name__", "")
    if isinstance(cls, type) and mod.startswith("sqlglot"):
        # dataclass decorators run AFTER this and must see the real defaults
        if "dataclass" in g or "dataclasses" in g:
            skipped_modules.add(mod)
        else:
            try:
                wrap_class(cls)
            except Exception as e:  # never break an import
                skipped_classes.append(f"{mod}:{name}: {type(e).__name__}: {e}")
    return cls


def install():
    if "sqlglot" in sys.modules:
        raise RuntimeError("attr_tracker must be installed before sqlglot is imported")
    builtins.__build_class__ = _build_class


# ------------------------------------------------------------------ derived tables
def track_dispatch():
    """Generator.__init__ looks expressions up in a per-class copy of TRANSFORMS
    (generator._DISPATCH_CACHE, built once per class), so after the first test
    nobody reads TRANSFORMS itself. Make each dispatch table a tracked dict that
    stands for "<Generator class>.TRANSFORMS": a lookup in it during a test is
    a TRANSFORMS[key] lookup (and a TRANSFORMS read) by that test."""
    import sqlglot.generator as g
    orig = g._build_dispatch

    def _build_dispatch(cls):
        d = orig(cls)
        td = _TrackedDict(d)
        _keep.append(d)
        ok = _owner_key(cls)
        if ok is not None:
            _fixed_alias[id(td)] = {f"{ok}.TRANSFORMS"}
        return td

    g._build_dispatch = _build_dispatch
    g._DISPATCH_CACHE.clear()


# ------------------------------------------------------------------ creation hooks
def _hook_attr_names():
    """Attribute names read on `cls`/`klass` (first parameter) by sqlglot's class
    creation hooks: metaclass __new__/__init__ and __init_subclass__."""
    names = set()
    for dirpath, _, files in os.walk(os.path.join(ROOT, "sqlglot")):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(dirpath, f), encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue
            for cdef in ast.walk(tree):
                if not isinstance(cdef, ast.ClassDef):
                    continue
                is_meta = any(isinstance(b, ast.Name) and b.id == "type" for b in cdef.bases)
                for fn in cdef.body:
                    if not isinstance(fn, ast.FunctionDef):
                        continue
                    if fn.name != "__init_subclass__" and not (
                            is_meta and fn.name in ("__new__", "__init__")):
                        continue
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Attribute) and not node.attr.startswith("__"):
                            names.add(node.attr)
                        # klass.__dict__.get("NAME")
                        if isinstance(node, ast.Call) and node.args and \
                                isinstance(node.args[0], ast.Constant) and \
                                isinstance(node.args[0].value, str):
                            names.add(node.args[0].value)
    return names


def _class_body_reads():
    """{"module:Qual.NAME"} for names a class body READS after assigning them
    itself (SHOW_TRIE = new_trie(... SHOW_PARSERS), X = Y - {...}): plain local
    reads at import time that no descriptor can see."""
    out = set()
    base = os.path.join(ROOT, "")
    for dirpath, _, files in os.walk(os.path.join(ROOT, "sqlglot")):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            mod = path[len(base):-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue

            def walk(node, prefix):
                for c in ast.iter_child_nodes(node):
                    if isinstance(c, ast.ClassDef):
                        q = f"{prefix}.{c.name}" if prefix else c.name
                        assigned = set()
                        for st in c.body:
                            targets = []
                            if isinstance(st, ast.Assign):
                                targets = st.targets
                            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                                targets = [st.target]
                            for tg in targets:
                                for n in ast.walk(tg):
                                    if isinstance(n, ast.Name):
                                        assigned.add(n.id)
                        for st in c.body:
                            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                continue
                            for n in ast.walk(st):
                                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                                        and n.id in assigned:
                                    out.add(f"{mod}:{q}.{n.id}")
                        walk(c, q)
                    elif not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk(c, prefix)

            walk(tree, "")
    return out


def _has_creation_hook(cls):
    if type(cls) is not type and _is_sqlglot(type(cls)):
        return True
    return any("__init_subclass__" in b.__dict__ for b in cls.__mro__[1:] if _is_sqlglot(b))


# ------------------------------------------------------------------ export
def _key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def _owner_key(owner):
    if _is_sqlglot(owner):
        return _key(owner)
    for b in getattr(owner, "__mro__", ())[1:]:
        if _is_sqlglot(b):
            return _key(b)
    return None


def spell(k):
    if k is STAR:
        return "*"
    if isinstance(k, enum.Enum):        # before str: AutoName members are str
        return f"{type(k).__qualname__}.{k.name}"
    if isinstance(k, type):
        return k.__qualname__
    if k is None or isinstance(k, (str, int, float, bool, bytes)):
        return repr(k)
    return "*"


def _resolve(owner, name):
    """The value an attribute read on `owner` returned (unwrapped)."""
    for c in getattr(owner, "__mro__", ()):
        v = c.__dict__.get(name)
        if v is not None:
            return v.value if isinstance(v, _Attr) else v
    return None


def export(scope_tests):
    """-> dict for blast_extra.json. scope_tests(ctx) -> list of node ids."""
    attr_users, key_users = {}, {}
    attr_import, key_import = set(), set()

    # every (owner key, name) that resolves to each tracked dict, in any context
    aliases = {}
    ctx_dicts = {}
    for ctx, reads in _reads.items():
        per = ctx_dicts[ctx] = {}
        for i, name in reads:
            owner = _owners.get(i)
            ok = _owner_key(owner) if owner is not None else None
            if ok is None:
                continue
            v = _resolve(owner, name)
            akey = f"{ok}.{name}"
            if isinstance(v, _TrackedDict):
                per.setdefault(id(v), set()).add(akey)
                aliases.setdefault(id(v), set()).add(akey)
            per.setdefault(None, set()).add(akey)

    for ctx in set(_reads) | set(_keys):
        tests = None if ctx == "" else scope_tests(ctx)
        per = ctx_dicts.get(ctx, {})
        akeys = per.get(None, set())
        for ak in akeys:
            if tests is None:
                attr_import.add(ak)
            else:
                attr_users.setdefault(ak, set()).update(tests)
        for did, ks in _keys.get(ctx, {}).items():
            owners = set(per.get(did) or aliases.get(did, ()))
            fixed = _fixed_alias.get(did, ())
            owners |= set(fixed)
            for ak in fixed:            # using the table = reading the attribute
                if tests is None:
                    attr_import.add(ak)
                else:
                    attr_users.setdefault(ak, set()).update(tests)
            spelled = {spell(k) for k in ks}
            for ak in owners:
                for s in spelled:
                    kk = f"{ak}[{s}]"
                    if tests is None:
                        key_import.add(kk)
                    else:
                        key_users.setdefault(kk, set()).update(tests)

    # own attributes read by class-creation hooks before the class was wrapped
    hook_names = _hook_attr_names()
    n_hook = 0
    for cls, names in _wrapped.items():
        if not names or not _has_creation_hook(cls):
            continue
        for name in names & hook_names:
            ak = f"{_key(cls)}.{name}"
            attr_import.add(ak)
            n_hook += 1
            if isinstance(cls.__dict__.get(name), _Attr) and \
                    isinstance(cls.__dict__[name].value, _TrackedDict):
                key_import.add(f"{ak}[*]")

    by_key = {_key(c): c for c in _wrapped}
    n_body = 0
    for ak in _class_body_reads():
        ckey, name = ak.rsplit(".", 1)
        cls = by_key.get(ckey)
        attr_import.add(ak)
        n_body += 1
        v = cls.__dict__.get(name) if cls is not None else None
        if isinstance(v, _Attr) and isinstance(v.value, _TrackedDict):
            key_import.add(f"{ak}[*]")

    return {
        "attr_users": {k: sorted(v) for k, v in sorted(attr_users.items())},
        "key_users": {k: sorted(v) for k, v in sorted(key_users.items())},
        "attr_import": sorted(attr_import),
        "key_import": sorted(key_import),
        "meta": {
            "wrapped_classes": sum(1 for n in _wrapped.values() if n),
            "wrapped_attrs": sum(len(n) for n in _wrapped.values()),
            "tracked_dicts": len(_tracked),
            "hook_attr_imports": n_hook,
            "class_body_self_reads": n_body,
            "skipped_classes": sorted(skipped_classes)
                               + [f"{m}: module uses dataclasses" for m in sorted(skipped_modules)],
        },
    }
