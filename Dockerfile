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
ENV SAFE_GROUPS="B,D"
CMD ["python", "/app/selector.py"]
