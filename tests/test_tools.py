from datetime import datetime

import pytest

from backend.app.tools.calculator import CalculationError, calculate
from backend.app.tools.clock import server_time


@pytest.mark.parametrize(('expression', 'expected'), [
    ('500 * 1.15 / 2', '287.5'), ('0.1 + 0.2', '0.3'), ('(2 + 3) * 4', '20'),
    ('-5 + 2', '-3'), ('100 / 4', '25'), ('0', '0'), ('1 / 3', '0.3333333333333333333333333333'),
])
def test_safe_math(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize('expression', [
    '__import__("os").system("id")', 'open(".env").read()', 'x + 1', '().__class__',
    '[x for x in range(10)]', '2**999999', '1 / 0', 'True + 1', '1e999', '1e-999',
    '9999999999999', '0xFF', '3,5 + 1', '1_000', '1+' * 110 + '1', '(' * 1000 + '1' + ')' * 1000,
])
def test_rejects_code_and_expensive_operations(expression):
    with pytest.raises(CalculationError):
        calculate(expression)


def test_clock_has_explicit_timezone():
    result = server_time('America/Mexico_City')
    assert result['timezone'] == 'America/Mexico_City'
    assert datetime.fromisoformat(result['datetime']).utcoffset() is not None
