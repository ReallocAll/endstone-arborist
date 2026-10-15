"""Guard against ambiguous Endstone command overloads.

Endstone registers each usage as a separate Bedrock overload. Even without
duplicate enum names, distinct enums containing the same first-argument value
can prevent the client from parsing valid subcommands.
"""

import ast
import re
from collections import Counter
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1] / "src" / "endstone_arborist" / "plugin.py"


def _usages() -> list[str]:
    module = ast.parse(_PLUGIN.read_text(encoding="utf-8"))
    plugin = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "ArboristPlugin")
    command = next(
        node.value
        for node in plugin.body
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "commands" for target in node.targets
        )
    )
    return ast.literal_eval(command)["arborist"]["usages"]


def test_arborist_command_enums_are_unique():
    usages = _usages()
    assert usages == [
        "/arborist",
        "/arborist <status|reload>",
        "/arborist <tree> [on|off|status|density] [compact|default|sparse|server]",
        "/arborist <sow> [on|off|status]",
    ]
    enums = [
        "Arborist" + "".join(word.title() for word in literal.split("|"))
        for usage in usages
        for literal in re.findall(r"[<\[]([a-z|]+)[>\]]", usage)
    ]
    assert all(count == 1 for count in Counter(enums).values()), enums


def test_arborist_first_argument_is_disjoint_across_overloads():
    groups = [
        set(usage.split()[1].strip("<>").split("|"))
        for usage in _usages()
        if len(usage.split()) > 1
    ]
    for index, group in enumerate(groups):
        others = set().union(*(groups[:index] + groups[index + 1:]))
        assert group.isdisjoint(others), (group, others)


def _accepts(usage: str, command: str) -> bool:
    """A bounded model of the documented positional enum grammar, not native BDS."""
    parts = usage.split()[1:]
    words = command.split()[1:]
    required = sum(p.startswith("<") for p in parts)
    return (
        required <= len(words) <= len(parts)
        and all(word in part[1:-1].split("|") for word, part in zip(words, parts))
    )


def test_documented_commands_resolve_to_exactly_one_overload():
    commands = [
        "arb",
        "arb status",
        "arb reload",
        "arb tree",
        "arb tree on",
        "arb tree off",
        "arb tree status",
        "arb tree density",
        "arb tree density compact",
        "arb tree density default",
        "arb tree density sparse",
        "arb tree density server",
        "arb sow",
        "arb sow on",
        "arb sow off",
        "arb sow status",
    ]
    for command in commands:
        matches = [usage for usage in _usages() if _accepts(usage, command)]
        assert len(matches) == 1, (command, matches)
