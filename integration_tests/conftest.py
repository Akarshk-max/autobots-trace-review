"""Fixtures for the integration suite.

These tests were split out of `tests/` and still need its fixtures, so they are
re-exported rather than duplicated.

`_init_test_crypto` is imported by name because `import *` skips underscore-prefixed
names, and it is autouse - without it the session runs with integrations crypto
uninitialised, and `start` cannot resolve it as an argument.
"""

from tests.conftest import *  # noqa: F401,F403
from tests.conftest import _init_test_crypto  # noqa: F401
