"""Signal-word matching must be word-based, not fragment-based.

`_select_assistant` promotes an earlier assistant message into the continuation
bundle when it looks like a proposal or an open item.  That test used to be a
plain substring scan, so `option` fired on `optional`, `have not` fired on
`have nothing`, and an unrelated sentence could displace the real open item.

The fix is a boundary rule drawn on **ASCII word characters only**.  Python's
``\\b`` is not usable here: it counts CJK as word characters, so ``\\boption\\b``
misses ``选项option`` — and these messages are routinely mixed CJK/English.

The other half of the contract matters just as much: the rule must not be so
strict that it starts missing real signals.  `options`, `proposed`, `todos` are
signals, so the signal lists spell out the inflected forms instead of leaning on
substring matching to catch them.
"""

from __future__ import annotations

from voyager.continuity import (
    _SIGNAL_OPEN,
    _SIGNAL_PROPOSAL,
    _keyword_hit,
    _select_assistant,
    _signal_tag,
)


# --- 1. the reported bug: a fragment must not fire --------------------------

def test_option_does_not_fire_inside_optional():
    """The exact case from the issue."""
    assert _keyword_hit("the optional field is fine", "option") is False
    assert _keyword_hit("optional", "option") is False
    assert _keyword_hit("option_parser", "option") is False
    assert _keyword_hit("--optional-flag", "option") is False


def test_test_does_not_fire_inside_testing():
    """Same rule, different word: the boundary is about the word, not `option`."""
    assert _keyword_hit("we are testing the parser", "test") is False
    assert _keyword_hit("tests passed", "test") is False
    assert _keyword_hit("run the test", "test") is True


def test_multi_word_signals_need_both_edges():
    assert _keyword_hit("i have nothing to add", "have not") is False
    assert _keyword_hit("it has nothing to do with it", "has not") is False
    assert _keyword_hit("still total of three", "still to") is False
    assert _keyword_hit("i have not run it", "have not") is True


def test_exact_keyword_still_fires():
    assert _keyword_hit("option", "option") is True
    assert _keyword_hit("an option is to cache", "option") is True
    assert _keyword_hit("i propose a sliding window", "propose") is True


# --- 2. and it must not become so strict that signals are missed ------------

def test_inflected_forms_are_signals_in_their_own_right():
    """Recall is kept by listing the forms, not by matching fragments."""
    for text, tag in [
        ("two options are on the table", "proposal"),
        ("i proposed a sliding window", "proposal"),
        ("we suggested an absolute ttl", "proposal"),
        ("the alternatives are x and y", "proposal"),
        ("recommended approach: cache it", "proposal"),
        ("still need to wire the rotation endpoint", "open"),
        ("todos: replay detection, rotation tests", "open"),
    ]:
        assert _signal_tag(text) == tag, text


def test_a_longer_word_that_merely_starts_the_same_is_not_a_signal():
    """The other direction: no promotion for near-misses."""
    assert _signal_tag("the optional field defaults to none") == "earlier"
    assert _signal_tag("we are testing the parser") == "earlier"
    assert _signal_tag("i have nothing to add") == "earlier"
    assert _signal_tag("still total of three files") == "earlier"


# --- 3. the boundary must survive CJK, paths and commands -------------------

def test_a_signal_next_to_cjk_still_fires():
    """`\\b` would miss this: CJK counts as a word character for Python's re."""
    assert _keyword_hit("选项option", "option") is True
    assert _keyword_hit("先看option再决定", "option") is True
    assert _keyword_hit("还有一个option", "option") is True


def test_mixed_cjk_and_english_sentences():
    assert _signal_tag("我 propose 一个 sliding window") == "proposal"
    assert _signal_tag("这个功能 not implemented，先放着") == "open"
    # a near-miss in a mixed sentence is still a near-miss
    assert _signal_tag("这个 optional 字段先不管") == "earlier"


def test_a_non_ascii_keyword_falls_back_to_substring():
    """Chinese has no word separators, so a boundary rule could never match."""
    assert _keyword_hit("还有待办事项", "待办") is True
    assert _keyword_hit("待办", "待办") is True
    assert _keyword_hit("已办结", "待办") is False


def test_paths_and_commands():
    # a fragment inside a path component is not a signal
    assert _keyword_hit("see src/optional.py", "option") is False
    assert _keyword_hit("edit config/optional.yaml", "option") is False
    # a real word in a path is
    assert _keyword_hit("edit docs/todo.md", "todo") is True
    # the same for a command line
    assert _keyword_hit("pytest --todo-mode", "todo") is True
    assert _keyword_hit("run optional_flag_tool", "option") is False


# --- 4. the effect on selection --------------------------------------------

def _asst(seq: int, content: str) -> dict:
    return {"kind": "assistant", "content": content, "seq": seq}


def test_select_assistant_does_not_promote_a_near_miss():
    """A message about an `optional` field must not crowd out the real item."""
    events = [
        _asst(1, "The optional field defaults to none."),
        _asst(2, "Still open: replay detection is not implemented."),
        _asst(3, "Progress note only."),
    ]
    picked = _select_assistant(events)
    contents = [ev["content"] for ev, _ in picked]
    assert "The optional field defaults to none." not in contents
    assert "Still open: replay detection is not implemented." in contents
    tags = {ev["content"]: tag for ev, tag in picked}
    assert tags["Progress note only."] == "latest"
    assert tags["Still open: replay detection is not implemented."] == "open"


def test_select_assistant_still_picks_a_real_proposal_first():
    events = [
        _asst(1, "I propose a sliding window."),
        _asst(2, "Still open: rotation tests."),
        _asst(3, "Pushing on."),
    ]
    picked = _select_assistant(events)
    tags = {ev["content"]: tag for ev, tag in picked}
    assert tags["I propose a sliding window."] == "proposal"
    assert tags["Still open: rotation tests."] == "open"
    assert tags["Pushing on."] == "latest"


def test_every_signal_word_is_itself_matchable():
    """A guard against a signal that can never fire.

    Every entry must match when it stands alone — a word with punctuation or
    spacing that the boundary rule rejects would be dead weight in the list.
    """
    for word in _SIGNAL_PROPOSAL + _SIGNAL_OPEN:
        assert _keyword_hit(word, word), f"{word!r} can never fire"
