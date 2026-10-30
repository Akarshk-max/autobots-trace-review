# src/autobots/action/action_type/action_text2text/action_sql_runner/sql_validation.py
"""
Parser-backed read-only SQL validation (AUT-1593).

Used by the Daytona connections bridge, which executes SQL that originates inside a
sandbox. ActionSqlRunner's own prefix check ("starts with select") is not a parse:
psycopg2 executes several ';'-separated statements in a single `execute()`, so
"select 1; drop table users" passes a prefix check. This module parses instead and
is applied in front of the action; the action's own check is left untouched.

Callers MUST pass the target engine's `dialect` (a `DatabaseEnum` value — they map
1:1 onto sqlglot dialect names for every engine this bridge supports). Parsing with
sqlglot's generic dialect instead of the real one is not just imprecise, it's a
bypass: the generic dialect nests block comments, but MySQL/BigQuery/Oracle do not,
so "SELECT 1 /* /* */ ; DROP TABLE x; -- */" parses as one Select generically while
the actual engine sees a SELECT followed by a DROP.
"""

import sqlglot
from sqlglot import expressions as exp

_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    # `SELECT * INTO copied FROM users` parses as a Select carrying an Into node, so
    # the statement-type check above passes it; it still writes a new table.
    exp.Into,
)

# Functions that read/write outside the result set or burn server time, all of which
# are callable from inside an otherwise perfectly ordinary SELECT.
_DENIED_FUNCTIONS = {
    "pg_sleep",
    "pg_advisory_lock",
    "pg_advisory_xact_lock",
    "pg_advisory_unlock",
    "pg_try_advisory_lock",
    "pg_try_advisory_xact_lock",
    "nextval",
    "setval",
    "dblink",
    "dblink_exec",
    "lo_import",
    "lo_export",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "query_to_xml",
    "sleep",
    "benchmark",
    "pg_sleep_for",
    "pg_sleep_until",
    "lo_unlink",
    "pg_notify",
    "pg_stat_reset",
    "set_config",
}

# DatabaseEnum values (the only engines the bridge supports) map 1:1 onto sqlglot
# dialect names -- passed straight through as the `read=` dialect, not remapped here.
_SUPPORTED_DIALECTS = frozenset({"mysql", "postgres", "bigquery", "oracle"})


def validate_single_readonly_statement(sql: str, dialect: str) -> None:
    """
    Raise ValueError unless `sql` is exactly one read-only SELECT/set-operation
    statement, parsed under the target engine's own SQL dialect.

    `dialect` must be a `DatabaseEnum` value (e.g. "postgres", "mysql", "bigquery",
    "oracle") -- one of the four engines `forward_databases` supports. Parsing with
    sqlglot's generic dialect instead is a bypass (see module docstring): it accepts
    input MySQL/BigQuery/Oracle would split into multiple statements. There is no
    generic/default fallback here on purpose -- a caller that forgot to pass the
    real dialect should fail loudly, not silently validate against the wrong grammar.

    Walks the whole tree, so a writable CTE (e.g. `WITH x AS (DELETE ... RETURNING *)
    SELECT * FROM x`) is rejected too, as are denylisted side-effecting function calls.

    This is defense-in-depth, not a guarantee. Any installed extension or
    dialect-specific function is a potential new bypass, and this denylist can only
    ever name the ones we already know about. The real safety boundary for a
    `forward_databases` (bridge-mode) connection is a read-only role at the database
    level; this code cannot enforce that, so it is an operational requirement for
    anyone registering a database connection intended for `forward_databases` use.
    """
    if dialect not in _SUPPORTED_DIALECTS:
        raise ValueError(
            f"Unsupported or missing dialect {dialect!r}; expected one of {sorted(_SUPPORTED_DIALECTS)}"
        )

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as e:
        raise ValueError(f"Could not parse SQL: {e}")

    if len(statements) != 1:
        raise ValueError("Exactly one SQL statement is required")

    stmt = statements[0]
    # exp.SetOperation covers UNION/EXCEPT/INTERSECT (sqlglot >= 21 made Except and
    # Intersect siblings of Union, not subclasses -- isinstance(x, exp.Union) alone
    # under-accepts valid read-only queries here).
    if not isinstance(stmt, (exp.Select, exp.SetOperation)):
        raise ValueError("Only SELECT statements are allowed")

    # sqlglot >= 20 yields plain nodes from walk() (older versions yielded tuples).
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN):
            raise ValueError("Statement must be read-only")
        # Unknown/dialect functions land on Anonymous, which is where every entry of
        # the denylist shows up (pg_sleep, nextval, dblink_exec, ...).
        if isinstance(node, exp.Anonymous):
            func_name = (node.name or "").lower()
            if func_name in _DENIED_FUNCTIONS:
                raise ValueError(f"Function '{func_name}' is not allowed")
