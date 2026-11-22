# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# From requirements.lock (the labelling env), at its versions: every non-stdlib module sqlglot
# imports (dateutil, mypy_extensions, typing_extensions; sqlglotrs is absent there too), so the
# HEAVY_REPLAY cheap phase takes the same paths as the real tests, and what tests/test_executor.py
# imports for the build-time trace (duckdb, numpy, pandas, pytz, tzdata). The rest of the lock
# (mypy, ruff, pdoc, ...) is never imported and would push the build past its 10 minutes.
COPY requirements.lock /tmp/requirements.lock
RUN grep -iE '^(duckdb|numpy|pandas|python-dateutil|pytz|tzdata|mypy_extensions|typing_extensions)==' \
        /tmp/requirements.lock > /tmp/heavy.lock \
    && pip install --no-cache-dir --no-deps -r /tmp/heavy.lock \
    && python -c "import duckdb, numpy, pandas, dateutil, pytz, mypy_extensions, typing_extensions" \
    && rm /tmp/requirements.lock /tmp/heavy.lock

# The pinned sqlglot source (originals for the Prover, tests for the fixture fallback).
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
COPY blast/srctree.py /tmp/srctree.py
RUN python /tmp/srctree.py /tmp/sqlglot.tgz /opt/sqlglot \
    && rm /tmp/sqlglot.tgz /tmp/srctree.py \
    && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests \
    && chmod -R a+rX /opt/sqlglot
ENV SQLGLOT_SRC=/opt/sqlglot

COPY selector.py /app/selector.py
COPY blast/ /app/blast/
COPY maps/ /app/maps/
# Name index (rule b) and fixture-name index, built from the pinned source.
RUN python /app/blast/build_index.py /opt/sqlglot /app/index \
    && rm -rf /app/blast/tests \
    && SKIP_INTEGRATION=0 python /app/blast/heavy.py build /opt/sqlglot /app/index/heavy.json \
    && chmod -R a+rX /app
# Rule families forced back to FULL (blast/safe.py): comma list of A-E, empty = all rules on.
ENV SAFE_GROUPS=""
# Rule s, setUpClass stopgap (blast/select.py): 1 on, 0 off. Map v3 credits class setup itself.
ENV SETUP_STOPGAP="1"
# Narrowing knobs (blast/select.py, blast/prover.py), measured on sample/ and mutants_out/:
# FUNC_LEVEL 2 = hybrid line/def; SYNTAX_LAZY = rule x; IMPORT_FUNC, MODULE_NAMES, MAX_DEPTH = rule b
# name chain; UNUSED_FILES = files the suite never runs. WIDE_NAMES and NOCU_NAMES stay off;
# ATTR_LEVEL (rule b', map v4 attr_users/key_users): on, 0 misses on sample + 313 mutants.
ENV FUNC_LEVEL="2" SYNTAX_LAZY="1" IMPORT_FUNC="1" MODULE_NAMES="1" MAX_DEPTH="4" UNUSED_FILES="1" \
    WIDE_NAMES="0" NOCU_NAMES="0" ATTR_LEVEL="1"
# HEAVY_REPLAY (blast/heavy.py): drop TestExecutor's TPC tests when they provably cannot fail.
# static = only drops that need no replay (no subprocess); 1 = also replay the cheap phase (the
# board: replays saved 0 s and cost ~7.5 s CPU on the practice set); 0 = off.
ENV HEAVY_REPLAY="static"
# INFECTION (blast/infection.py, maps/infection_map.json.gz built by infect/make_infection_map.sh):
# a one-operator change keeps only the tests where the new operator would change the outcome.
ENV INFECTION="1"
# PROBE BRANCH ONLY: encode each FULL commit's reason class in its cost (selector.py PROBE_E).
ENV PROBE_E="0" PROBE_F="1"
CMD ["python", "/app/selector.py"]
