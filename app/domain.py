"""Pure template logic: placeholder parsing, rendering and selection.

Placeholder syntax
------------------
``{name}`` is a variable.  ``{{`` and ``}}`` are literal braces.  Variable
names may contain any non-whitespace characters except braces (Chinese names
are fine).  Anything else that looks like a malformed placeholder is rejected
when the template is written, so a bad template never enters the database.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

LANG_FALLBACK = {"en": ("en", "zh"), "zh": ("zh",)}


class TemplateSyntaxError(ValueError):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} (offset {position})")
        self.message = message
        self.position = position


def parse(content: str) -> list[tuple[str, str]]:
    """Tokenise template content into ``("lit"|"var", value)`` pairs."""
    tokens: list[tuple[str, str]] = []
    literal: list[str] = []
    i = 0
    length = len(content)

    def flush() -> None:
        if literal:
            tokens.append(("lit", "".join(literal)))
            literal.clear()

    while i < length:
        char = content[i]
        if char == "{":
            if content.startswith("{{", i):
                literal.append("{")
                i += 2
                continue
            end = content.find("}", i + 1)
            if end == -1:
                raise TemplateSyntaxError("unclosed '{'", i)
            name = content[i + 1 : end]
            if not name or any(ch.isspace() or ch in "{}" for ch in name):
                raise TemplateSyntaxError("invalid variable name", i)
            flush()
            tokens.append(("var", name))
            i = end + 1
        elif char == "}":
            if content.startswith("}}", i):
                literal.append("}")
                i += 2
                continue
            raise TemplateSyntaxError("unmatched '}'", i)
        else:
            next_stop = i
            while next_stop < length and content[next_stop] not in "{}":
                next_stop += 1
            literal.append(content[i:next_stop])
            i = next_stop
    flush()
    return tokens


def variables(content: str) -> list[str]:
    """Distinct variable names used by a template, sorted for stability."""
    return sorted({value for kind, value in parse(content) if kind == "var"})


def render(content: str, values: Mapping[str, object]) -> str:
    """Substitute every placeholder.  Missing keys propagate as ``KeyError``."""
    pieces: list[str] = []
    for kind, value in parse(content):
        if kind == "lit":
            pieces.append(value)
        else:
            if value not in values:
                raise KeyError(value)
            pieces.append(str(values[value]))
    return "".join(pieces)


def choose(
    candidates: Sequence[Mapping[str, object]],
    lang: str,
    available_variables: Iterable[str],
) -> Mapping[str, object] | None:
    """Pick the template for a firmware prompt.

    Only templates whose variables are all in ``available_variables`` are
    eligible.  The required language is tried first; English falls back to
    Chinese when no English template qualifies.  Within the chosen language,
    the template using the most distinct variables wins, and the most recently
    published template breaks a tie.
    """
    available = set(available_variables)
    for candidate_lang in LANG_FALLBACK[lang]:
        eligible = [
            template
            for template in candidates
            if template["lang"] == candidate_lang
            and set(template["variables"]) <= available
        ]
        if eligible:
            eligible.sort(
                key=lambda template: (
                    len(template["variables"]),
                    int(template["publish_seq"]),
                ),
                reverse=True,
            )
            return eligible[0]
    return None
