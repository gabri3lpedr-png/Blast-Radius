# mapgen: the labelling environment (Person A)

Rebuilds the environment the organisers labelled commits in, so we can run
sqlglot's real tests to build maps. Nothing here ships in the submitted image.

- `Dockerfile.mapgen`: the pinned `python:3.13-slim` digest, sqlglot at
  `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` in `/opt/sqlglot` (downloaded as a
  tarball, no git or apt), `requirements.lock`, plus `coverage` for map building.
  `SKIP_INTEGRATION=0`, `PYTHONPATH=/opt/sqlglot`, `COVERAGE_CORE=sysmon`.
- `run_baseline.sh`: runs inside the container. Full test suite, one process
  (no xdist), JUnit XML to `/out/baseline.xml`, wall-clock to `/out/baseline_time.txt`.
- `build_and_baseline.sh`: builds the image for linux/amd64 and runs the
  baseline with `./mapgen_out` mounted at `/out`.

Check that our test ids match the organisers':

```sh
sh mapgen/build_and_baseline.sh
python3 tools/compare_ids.py mapgen_out/baseline.xml sample/tests.json
```

## Stage A2: the coverage map

```sh
sh mapgen/make_map.sh                       # full suite -> maps/coverage_map.json.gz
sh mapgen/make_map.sh tests/test_optimizer.py   # quick slice -> mapgen_out/slice_map.json.gz
python3 mapgen/sanity_check.py              # the guide's three checks
```

- `blast_plugin.py` (pytest plugin): coverage with one dynamic context per test
  node id, `files_read` (non-.py files opened during a test), `class_users`
  (wraps `__init__` of `Expr`, `Dialect`, `Tokenizer`, `Parser`, `Generator`,
  `Schema`, ... and every subclass with its own `__init__`; Tokenizer / Parser /
  Generator also credit their `dialect`), and failures. It imports every sqlglot
  module before tests run, so lazily loaded dialects count as import-time lines
  instead of being credited to the first test that loaded them.
- `export_map.py`: turns the raw data into the contract format, fails if > 12 MB.
- **`make_map.sh` sets `COVERAGE_CORE=ctrace`, overriding the image's `sysmon`.**
  coverage 7.17 warns "Dynamic contexts aren't supported with core=sysmon": with
  sysmon, each line is credited only to the first test that ran it, which would
  make the map wrong. ctrace is slower but correct.
- `sanity_check.py` needs `.cache/sqlglot` (a copy of `/opt/sqlglot` from the image).

`mapgen_out/` and `.cache/` are git-ignored.

## Map v3 (A5 fixes from the mutant set)

- **setUpClass / fixtures:** setup and teardown run under contexts
  `setup@<nodeid>` / `teardown@<nodeid>`, and everything recorded there (lines,
  files_read, class_users) is credited to every test of that class (or module).
  Before, `TestExecutor.setUpClass` reading `tpc-h.sql` was credited to the
  class's first test only.
- **`Dialect["name"]` / `Dialect.get(name)`** hand out a class without building
  an instance; those lookups now count in class_users.
- **Multi-line statements:** coverage reports a statement on its first line only;
  export_map copies that line's tests to every other line of the statement.
- `tools/check_map.py`: a patched `.py` that no longer compiles -> run everything.

## Map v4: class attribute reads and table key lookups

Follows the "v4 contract" in `docs/GUIDE_B_Selector.md` (branch selector). Code:
`mapgen/attr_tracker.py`, installed by blast_plugin before sqlglot is imported.

| key | meaning |
|---|---|
| `attr_users["mod:Owner.ATTR"]` | tests that read class attribute ATTR looked up on Owner (`type(obj)` for instance reads, the class for class reads; inherited reads count; also `hasattr`) |
| `key_users["mod:Owner.ATTR[key]"]` | tests that asked the dict attribute about `key`: `d[k]`, `get`, `in`, `setdefault`, `pop`, `d[k] = v`, hits **and misses** |
| `key_users["mod:Owner.ATTR[*]"]` | tests that used the dict as a whole: iteration, keys/values/items, len, bool, copy, `**d`, `dict(d)`, `update(d)`, `\|`, `==`, repr, pickling; and keys that can't be spelled |
| `attr_import` / `key_import` | the same keys, read outside any test (import time, collection) |

Key spelling: Enum member -> `TypeQualname.MEMBER` (checked before str),
class -> its `__qualname__`, str/int/float/bool/None/bytes -> `repr(k)`, else `*`.

How it is recorded:
- A `builtins.__build_class__` hook wraps each sqlglot class the moment its
  `class` statement finishes: every plain attribute (no `__get__`: not a
  function, property, classmethod or nested class; not a dunder; not `*_TRIE`)
  becomes a non-data descriptor that records `(owner, ATTR)`. Dict values become
  a recording `dict` subclass (aliases of one dict share one wrapper). Classes
  made with `type(...)` and re-assigned attributes are (re)wrapped before every
  test. Enums, exceptions and modules using dataclasses are not wrapped
  (`meta.skipped_classes`).
- A key lookup is credited to every `Owner.ATTR` the same test read that
  resolves to that dict (or every one that ever did, if the test kept a
  reference). A non-sqlglot owner (a test's own Dialect subclass) is recorded as
  its first sqlglot ancestor.
- `Generator` looks expressions up in a per-class copy of TRANSFORMS
  (`_DISPATCH_CACHE`, built once). That copy is a recording dict standing for
  `<Generator class>.TRANSFORMS`, so each test's lookups count, not just the
  first test's.
- Reads a descriptor can't see, listed in attr_import (+ `[*]` in key_import for
  dicts): attributes a class-creation hook reads on the new class itself
  (`_Dialect.__new__`, `__init_subclass__`; names found by an AST scan), and
  names a class body reads after assigning them (`SHOW_TRIE = new_trie(...
  SHOW_PARSERS)`, `X = Y - {...}`).

Grade the v4 keys (runs in the image, needs sqlglot importable):

```sh
docker run --rm --platform linux/amd64 -v "$(pwd):/repo" -w /repo -e PYTHONPATH=/opt/sqlglot \
  blast-radius-mapgen python tools/check_map.py --v4 maps/coverage_map.json.gz mutants_out /opt/sqlglot
```

`--v4` uses attr_users / key_users for class-body attribute edits (class and
every subclass), falling back to class_users when the attribute has no key at
all, or is read at import. For dicts it falls back only on `K[*]` or
`K[changed key]` in key_import, not on a bare import read of K: `_Dialect.__new__`
reads every dialect generator's TRANSFORMS at import just to pop JSONPath keys.
