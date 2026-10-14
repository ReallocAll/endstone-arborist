"""Catch duplicate anonymous Endstone command enum types before deployment.

Endstone derives the enum type name from the command and every short-form
choice. Reusing a choice list in separate overloads registers the same enum
twice, producing an 'already exists' warning.
"""

import ast
import re
from collections import Counter
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "src" / "endstone_arborist" / "plugin.py"


def test_arborist_command_overloads_register_distinct_enums():
    module = ast.parse(_PLUGIN.read_text(encoding="utf-8"))
    plugin = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "ArboristPlugin")
    command_definition = next(
        node.value
        for node in plugin.body
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "commands" for target in node.targets
        )
    )
    usages = ast.literal_eval(command_definition)["arborist"]["usages"]

    assert usages == [
        "/arborist",
        "/arborist <status|reload>",
        "/arborist <tree|sow> [on|off|status]",
        "/arborist <tree> <density> [compact|default|sparse|server]",
    ]
    short_enums = [
        "Arborist" + "".join(word.title() for word in literal.split("|"))
        for usage in usages
        for literal in re.findall(r"[<\\[]([a-z]+(?:\\|[a-z]+)*)[>\\]]", usage)
    ]
    assert all(n == 1 for n in Counter(short_enums).values()), short_enums
