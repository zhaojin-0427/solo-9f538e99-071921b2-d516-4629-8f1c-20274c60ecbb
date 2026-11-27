"""Pure-function tests for placeholder parsing, rendering and selection."""

import pytest

from app import domain


def test_parse_supports_chinese_names_and_escaped_braces():
    tokens = domain.parse("欢迎{观众}来到{{博物馆}}")
    assert tokens == [
        ("lit", "欢迎"),
        ("var", "观众"),
        ("lit", "来到{博物馆}"),
    ]


def test_variables_are_sorted_and_distinct():
    assert domain.variables("{a}{b}{a}") == ["a", "b"]


@pytest.mark.parametrize("bad", ["{", "{unclosed", "}", "a}b", "{}", "{a b}", "{a{b}}"])
def test_invalid_syntax_is_rejected(bad):
    with pytest.raises(domain.TemplateSyntaxError):
        domain.parse(bad)


def test_render_substitutes_and_strips():
    assert domain.render("请到{地点}", {"地点": "东门"}) == "请到东门"


def test_render_missing_raises():
    with pytest.raises(KeyError):
        domain.render("{a}", {})


def candidate(lang, variables, seq, content="x"):
    return {
        "id": seq,
        "lang": lang,
        "variables": variables,
        "content": content,
        "publish_seq": seq,
    }


def test_language_preference_then_fallback():
    candidates = [
        candidate("zh", [], 1),
        candidate("en", [], 2),
    ]
    assert domain.choose(candidates, "en", [])["lang"] == "en"
    assert domain.choose(candidates, "zh", [])["lang"] == "zh"


def test_english_falls_back_to_chinese_only_without_english():
    candidates = [candidate("zh", [], 1)]
    assert domain.choose(candidates, "en", [])["lang"] == "zh"


def test_fallback_skips_english_template_with_unfillable_variables():
    candidates = [
        candidate("en", ["gate"], 5),
        candidate("zh", [], 1),
    ]
    chosen = domain.choose(candidates, "en", [])
    assert chosen["lang"] == "zh"


def test_more_variety_wins_then_latest_publish():
    candidates = [
        candidate("en", ["a"], 10),
        candidate("en", ["a", "b"], 2),
        candidate("en", ["a", "b"], 3),
    ]
    chosen = domain.choose(candidates, "en", ["a", "b"])
    assert chosen["publish_seq"] == 3


def test_ineligible_template_is_ignored_even_if_best():
    candidates = [
        candidate("en", ["a", "b", "c"], 99),
        candidate("en", [], 1),
    ]
    chosen = domain.choose(candidates, "en", ["a", "b"])
    assert chosen["publish_seq"] == 1


def test_no_candidate_returns_none():
    assert domain.choose([candidate("zh", ["x"], 1)], "en", []) is None
