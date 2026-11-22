# INERT_CHECK: PASS

Ground truth for INERT_EMPTY (branch victor/full): 12 harsh patches labelled with the real
sqlglot suite (tools/mutants/label.py, 3-phase, failures confirmed serially):
8 defs the map credits no test with got `raise RuntimeError(...)` as their first statement;
4 `if TYPE_CHECKING:` blocks got `import blast_no_such_module`. All 12: 0 failing tests.

victor/full on them: 0 misses (still conservative: a line inserted at the top of a def / block
also looks up the def header / `if` line above it). Also 0 misses on sample (142.905 s),
mutants_out (313) and mutants_new (100). Unit tests: 86 pass.
