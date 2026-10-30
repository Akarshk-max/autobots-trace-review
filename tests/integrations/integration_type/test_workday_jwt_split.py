
from pathlib import Path


def test_workday_auth_uses_workday_jwt_module_for_connect():
    text = Path('src/autobots/integrations/integration_type/workday_auth.py').read_text()
    assert 'from src.autobots.conn.only_auth.workday.workday_jwt import connect_workday_jwt' in text


def test_workday_jwt_module_contains_connect_flow():
    text = Path('src/autobots/conn/only_auth/workday/workday_jwt.py').read_text()
    assert 'async def debug_workday_connection' in text
    assert 'async def connect_workday_jwt' in text
