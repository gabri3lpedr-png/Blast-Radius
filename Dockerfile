# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The pinned sqlglot source (originals for the Prover, tests for the fixture fallback).
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tgz
COPY blast/srctree.py /tmp/srctree.py
RUN python /tmp/srctree.py /tmp/sqlglot.tgz /opt/sqlglot \
    && rm /tmp/sqlglot.tgz /tmp/srctree.py \
    && chmod -R a+rX /opt/sqlglot
ENV SQLGLOT_SRC=/opt/sqlglot

COPY selector.py /app/selector.py
COPY blast/ /app/blast/
COPY maps/ /app/maps/
# Name index (rule b) and fixture-name index, built from the pinned source.
RUN python /app/blast/build_index.py /opt/sqlglot /app/index \
    && rm -rf /app/blast/tests \
    && chmod -R a+rX /app
# Rule families forced back to FULL (blast/safe.py): comma list of A-E, empty = all rules on.
ENV SAFE_GROUPS=""
# Rule s, setUpClass stopgap (blast/select.py): 1 on, 0 off. Map v3 credits class setup itself.
ENV SETUP_STOPGAP="1"
# Narrowing knobs (blast/select.py, blast/prover.py), measured on sample/ and mutants_out/:
# FUNC_LEVEL 2 = hybrid line/def; SYNTAX_LAZY = rule x; IMPORT_FUNC, MODULE_NAMES, MAX_DEPTH = rule b
# name chain; UNUSED_FILES = files the suite never runs. WIDE_NAMES and NOCU_NAMES stay off;
# ATTR_LEVEL (rule b', needs map v4 attr_users/key_users) off until measured.
ENV FUNC_LEVEL="2" SYNTAX_LAZY="1" IMPORT_FUNC="1" MODULE_NAMES="1" MAX_DEPTH="4" UNUSED_FILES="1" \
    WIDE_NAMES="0" NOCU_NAMES="0" ATTR_LEVEL="0"
CMD ["python", "/app/selector.py"]
