import traceback

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_text2text.action_sql_runner.action_db_sql_runner import (
    ActionDBSqlQueryRunner,
    ActionDBSqlRunnerConfig,
)
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


# this is 8c45fca2-9d1a-4657-b782-674195cc8a99 my user id so all these tests will run only if this is given
# because i have done database auth for tiny_fog for my own account
# function for calling run_action method of ActionDBSqlQueryRunner
async def _run(action, ctx, db, config):
    action_input = DataBlock(text=config.sql_query)
    return await action.run_action(ctx=ctx, app_state=AppState(), action_input=action_input)


# function for assertion and also tracing the error or exception we
# get in ActionDBSqlQueryRunner class with traceback also for easy debugging
def _assert_not_exception(result):
    """
    If action returned an Exception, print it clearly in pytest output.
    """
    if isinstance(result, Exception):
        print("\n\n\n================ ACTION RETURNED EXCEPTION ================")
        print(f"Type: {type(result)}")
        print(f"Repr: {repr(result)}")
        print(f"Str : {str(result)}")

        tb = getattr(result, "__traceback__", None)
        if tb:
            print("\n---------------- Traceback ----------------")
            traceback.print_tb(tb)

        print("===========================================================\n\n\n")
        pytest.fail(f"Action returned Exception: {repr(result)}")


# single statement sql query test
@pytest.mark.asyncio
async def test_sql_runner_single_statement(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(connection_name="tiny_fog", sql_query="SELECT 1 AS result;")
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert result.blocks
    assert "result" in result.blocks[0].text


# test case for insertion in db
@pytest.mark.asyncio
async def test_sql_runner_create_insert_select(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "statements": [
            {"sql":"CREATE TABLE IF NOT EXISTS test_tbl (id INT)"},
            {"sql":"INSERT INTO test_tbl VALUES(42)"},
            {"sql":"SELECT * FROM test_tbl LIMIT 1","fetch":"all"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert len(result.blocks) >= 3
    assert "42" in result.blocks[-2].text or "42" in result.blocks[-1].text


# test case for batch insertion and fetching inserted data also
@pytest.mark.asyncio
async def test_json_batch_insert_and_fetch(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "statements": [
            {"sql":"CREATE TABLE IF NOT EXISTS batchTbl(x INT)"},
            {"sql":"INSERT INTO batchTbl(x) VALUES(:val)", "params":[{"val":100}]},
            {"sql":"SELECT * FROM batchTbl", "fetch":"all"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert any("100" in block.text for block in result.blocks)


@pytest.mark.asyncio
async def test_stop_on_error_should_abort(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "stop_on_error": true,
          "statements":[
            {"sql":"CREATE TABLE IF NOT EXISTS errTbl(v INT)"},
            {"sql":"INSERT INTO errTbl(v) VALUES(1)"},
            {"sql":"SELECT * FROM errTbl"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    summary = result.blocks[-1].text
    assert '"failed": 1' in summary or '"ok": false' in summary


@pytest.mark.asyncio
async def test_stop_on_error_false_continue(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "stop_on_error": false,
          "statements":[
            {"sql":"CREATE TABLE IF NOT EXISTS contTbl(a INT)"},
            {"sql":"INSERT INTO contTbl VALUES(5)"},
            {"sql":"SELECT * FROM contTbl","fetch":"all"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert any("5" in block.text for block in result.blocks)


@pytest.mark.asyncio
async def test_sql_runner_delete_statement(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "statements":[
            {"sql":"CREATE TABLE IF NOT EXISTS delTbl(x INT)"},
            {"sql":"INSERT INTO delTbl VALUES(10)"},
            {"sql":"DELETE FROM delTbl WHERE x=10"},
            {"sql":"SELECT * FROM delTbl WHERE x=10","fetch":"all"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert '"rows": []' in result.blocks[-2].text or '"rows": null' in result.blocks[-2].text


@pytest.mark.asyncio
async def test_sql_runner_update_statement(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "statements":[
            {"sql":"CREATE TABLE IF NOT EXISTS updTbl(id INT, name TEXT)"},
            {"sql":"INSERT INTO updTbl VALUES (1,'before')"},
            {"sql":"UPDATE updTbl SET name='after' WHERE id=1"},
            {"sql":"SELECT * FROM updTbl WHERE id=1","fetch":"all"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert any("after" in block.text for block in result.blocks)


# delete all the tables we created during running above tests
@pytest.mark.asyncio
async def test_cleanup_drop_all_tables(ctx, db, set_test_settings):
    ctx = Context()
    user = UserORM(id="8c45fca2-9d1a-4657-b782-674195cc8a99")
    # db = next(get_mongo_db())

    config = ActionDBSqlRunnerConfig(
        connection_name="tiny_fog",
        sql_query="""
        {
          "transaction": true,
          "statements":[
            {"sql":"DROP TABLE IF EXISTS test_tbl"},
            {"sql":"DROP TABLE IF EXISTS batchTbl"},
            {"sql":"DROP TABLE IF EXISTS errTbl"},
            {"sql":"DROP TABLE IF EXISTS contTbl"},
            {"sql":"DROP TABLE IF EXISTS delTbl"},
            {"sql":"DROP TABLE IF EXISTS updTbl"}
          ]
        }
        """,
    )
    action = ActionDBSqlQueryRunner(action_config=config, user=user, db=db)

    result = await _run(action, ctx, db, config)
    _assert_not_exception(result)

    assert result.blocks
    assert all("DROP" in block.text or "success" in block.text.lower() for block in result.blocks)
