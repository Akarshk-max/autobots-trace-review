import pytest
import pytest_asyncio

from src.autobots.action.action_type.action_text2text.action_sql_runner.sql_validation import (
    validate_single_readonly_statement,
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def start():
    """
    Override the repository session fixture. These tests are pure parsing and
    require neither the database nor any external service.
    """
    yield


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id, name from users where id = 5",
        "SELECT * FROM users UNION SELECT * FROM admins",
        "SELECT * FROM users UNION ALL SELECT * FROM admins",
        "SELECT * FROM users EXCEPT SELECT * FROM admins",
        "SELECT * FROM users INTERSECT SELECT * FROM admins",
        "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
        "SELECT count(*) FROM users GROUP BY tenant HAVING count(*) > 1",
        "select 1;",  # a single trailing semicolon is still one statement
    ],
)
def test_allows_single_read_only_statements(sql: str):
    validate_single_readonly_statement(sql, dialect="postgres")


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "bigquery", "oracle"])
def test_allows_across_every_supported_dialect(dialect: str):
    validate_single_readonly_statement("SELECT 1", dialect=dialect)


@pytest.mark.parametrize("dialect", [None, "", "sqlite", "mssql", "generic"])
def test_rejects_missing_or_unsupported_dialect(dialect):
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement("SELECT 1", dialect=dialect)
    assert "dialect" in str(e.value).lower()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "select 1; drop table users",
        "select 1; delete from users",
        "SELECT * FROM users; UPDATE users SET admin = true",
    ],
)
def test_rejects_multiple_statements(sql: str):
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement(sql, dialect="postgres")
    assert "Exactly one SQL statement is required" in str(e.value)


@pytest.mark.parametrize(
    "sql,dialect",
    [
        # Generic-dialect parsing (the pre-fix behavior) nests block comments and
        # reads this as one Select. mysql/bigquery/oracle don't nest block comments,
        # so the real engine sees SELECT 1, then a second statement dropping a table.
        # Parsing under the connection's own dialect is what makes this fail closed.
        ("SELECT 1 /* /* */ ; DROP TABLE x; -- */", "mysql"),
        ("SELECT 1 /* /* */ ; DROP TABLE x; -- */", "bigquery"),
        ("SELECT 1 /* /* */ ; DROP TABLE x; -- */", "oracle"),
    ],
)
def test_rejects_nested_comment_smuggled_statement_on_non_nesting_dialects(sql: str, dialect: str):
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement(sql, dialect=dialect)
    assert "Exactly one SQL statement is required" in str(e.value)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users (id) VALUES (1)",
        "UPDATE users SET admin = true",
        "DELETE FROM users",
        "CREATE TABLE t (id int)",
        "DROP TABLE users",
        "ALTER TABLE users ADD COLUMN x int",
        "TRUNCATE TABLE users",
    ],
)
def test_rejects_write_and_ddl_statements(sql: str):
    with pytest.raises(ValueError):
        validate_single_readonly_statement(sql, dialect="postgres")


def test_rejects_select_into():
    # Parses as a Select carrying an Into node, so the statement-type check alone
    # would let it through even though it writes a new table.
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement("SELECT * INTO copied FROM users", dialect="postgres")
    assert "read-only" in str(e.value)


def test_rejects_writable_cte():
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement(
            "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x", dialect="postgres"
        )
    assert "read-only" in str(e.value)


@pytest.mark.parametrize(
    "sql,func",
    [
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT pg_sleep_for('1 minute')", "pg_sleep_for"),
        ("SELECT pg_sleep_until('2030-01-01')", "pg_sleep_until"),
        ("SELECT nextval('users_id_seq')", "nextval"),
        ("SELECT dblink_exec('dbname=x', 'drop table users')", "dblink_exec"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT id FROM users WHERE pg_advisory_lock(1)", "pg_advisory_lock"),
        ("SELECT benchmark(1000000, md5('x'))", "benchmark"),
        ("SELECT lo_unlink(12345)", "lo_unlink"),
        ("SELECT pg_notify('chan', 'payload')", "pg_notify"),
        ("SELECT set_config('search_path', 'public', false)", "set_config"),
    ],
)
def test_rejects_denied_functions(sql: str, func: str):
    with pytest.raises(ValueError) as e:
        validate_single_readonly_statement(sql, dialect="postgres")
    assert f"Function '{func}' is not allowed" in str(e.value)


def test_rejects_unparseable_sql():
    with pytest.raises(ValueError):
        validate_single_readonly_statement("this is not sql at all ((((", dialect="postgres")


def test_rejects_empty_sql():
    with pytest.raises(ValueError):
        validate_single_readonly_statement("", dialect="postgres")
