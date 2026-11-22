"""共享夹具。"""
import pytest

from tests.support import SlowValidator


@pytest.fixture
def slow_validator():
    return SlowValidator()
