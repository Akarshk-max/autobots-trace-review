
import ast
from pathlib import Path


def test_workday_auth_core_methods_are_class_methods():
    text = Path('src/autobots/integrations/integration_type/workday_auth.py').read_text()
    mod = ast.parse(text)
    cls = next(n for n in mod.body if isinstance(n, ast.ClassDef) and n.name == 'WorkdayAuth')
    method_names = [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    required = {
        'get_app',
        'get_api_domain',
        'get_creds_model_type',
        'get_auth_model_type',
        'create_auth',
        'refresh_auth',
        'is_refresh_required',
        'get_auth_header',
        'get_updated_url',
        'get_connection_info',
        'health_check',
    }
    missing = required - set(method_names)
    assert not missing, f'Missing class methods: {sorted(missing)}'

    module_level = {n.name for n in mod.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    leaked = {'get_app', 'get_api_domain', 'get_creds_model_type', 'get_connection_info', 'health_check', 'get_updated_url'} & module_level
    assert not leaked, f'Unexpected module-level functions: {sorted(leaked)}'
