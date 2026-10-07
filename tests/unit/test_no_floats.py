"""Guard rail: no float in money paths. Cheap, and catches the classic billing bug early."""

import ast
from pathlib import Path

import metering.rating

MONEY_DIRS = [Path(metering.rating.__file__).parent]


def test_rating_code_never_uses_float() -> None:
    offenders = []
    for directory in MONEY_DIRS:
        for path in directory.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Name) and node.id == "float":
                    offenders.append(f"{path.name}:{node.lineno} uses float")
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    offenders.append(f"{path.name}:{node.lineno} float literal {node.value}")
    assert offenders == []
