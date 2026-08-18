"""The schema is checked against the real PostgreSQL grammar when available.

``pglast`` wraps libpg_query — the actual server parser — so this catches
syntax errors that eyeballing misses. It is an optional dev dependency:
without it the test skips rather than giving false assurance.
"""

from __future__ import annotations

import pathlib

import pytest

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def test_schema_file_exists():
    assert SCHEMA.is_file(), f"missing {SCHEMA}"


def test_schema_parses_as_postgres():
    pglast = pytest.importorskip("pglast", reason="pglast not installed")
    stmts = pglast.parse_sql(SCHEMA.read_text())
    assert len(stmts) > 30


def test_every_driver_scoped_table_has_rls():
    sql = SCHEMA.read_text()
    scoped = ["sessions", "merchants", "buildings", "offers", "deliveries",
              "zone_stats", "policy_runs", "expenses"]
    for table in scoped:
        assert f"alter table {table} " in sql or f"alter table {table}\n" in sql, table
        assert f"'{table}'" in sql, f"{table} missing from the RLS policy loop"


def test_gate_codes_are_never_in_the_shared_view():
    sql = SCHEMA.read_text()
    view = sql.split("create view shared_buildings")[1].split(";")[0]
    assert "gate_code" not in view
