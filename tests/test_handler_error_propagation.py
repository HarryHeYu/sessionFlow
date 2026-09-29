"""Every provider handler must route a failed continuity call the same way.

The core returns its outcome as `attach_status`, and the handlers must not
collapse the possibilities:

    success    -> inject the context
    no_thread  -> the core *confirmed* there is nothing to continue here
    ambiguous  -> several threads match; nothing may be chosen
    error      -> the continuity engine failed; the outcome is unknown

`no_thread` and `error` are different claims: the first says "no work here", the
second says "we do not know".  A handler that reported a failure as `no_thread`
would suppress an injection for a thread that does exist.

These tests drive the classification through each handler with a real core status
value (mocked result object), and assert on the structured fields -- never on the
wording of a message.
"""

from __future__ import annotations

import io
import json
import sys
import types

import pytest

from voyager.integrations import antigravity_session_start as ag
from voyager.integrations import claude_session_start as cl
from voyager.integrations import cursor_session_start as cu
from voyager.integrations import hook_result as hr
from voyager.integrations import kiro_session_start as ki
from voyager.integrations import zcode_session_start as zo


def _result(attach_status, continuity_available=False, context=None):
    return types.SimpleNamespace(
        continuity_available=continuity_available,
        context=context,
        attach_status=attach_status,
        context_source="none",
        context_stale=False,
        recommended_action="pick_thread",
        thread_id=None,
        goal=None,
        repo_root=None,
    )


# --- the classifier itself -------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("auto_attached", "context"),
    ("already_attached", "context"),
    (hr.AMBIGUOUS, "ambiguous"),
    ("ERROR_NO_REPO_MATCH", "no_thread"),          # a confirmed absence
    ("ERROR_UNKNOWN_SESSION", "error"),            # an engine failure
    ("ERROR_PENDING_CONFLICT", "error"),
    ("ERROR_SESSION_IN_OTHER_THREAD", "error"),
    ("no_auto_attach", "no_thread"),
    ("pending_resolve", "no_thread"),
    ("", "no_thread"),
])
def test_classification_of_every_core_status(status, expected):
    r = _result(status, continuity_available=(expected == "context"),
                context="doc" if expected == "context" else None)
    assert hr.classify(r) == expected


def test_an_unknown_status_is_treated_as_a_failure_not_as_absence():
    """The safe direction: 'no thread' would suppress an injection."""
    assert hr.classify(_result("ERROR_SOMETHING_NEW")) == "error"


def test_classification_never_raises_on_a_broken_result():
    class Boom:
        @property
        def continuity_available(self):
            raise RuntimeError("boom")

    assert hr.classify(Boom()) == "error"
    assert hr.classify(None) == "error"


def test_error_and_ambiguous_are_distinguishable():
    r = _result(hr.AMBIGUOUS)
    out = hr.build_result(r, hr.classify(r), "E:/repo")
    assert out["status"] == "error"
    assert out["classification"] == "ambiguous"

    r2 = _result("ERROR_UNKNOWN_SESSION")
    out2 = hr.build_result(r2, hr.classify(r2), "E:/repo")
    assert out2["status"] == "error"
    assert out2["classification"] == "error"
    assert out2["classification"] != out["classification"]


def test_recommended_action_survives_every_non_context_path():
    for status in (hr.AMBIGUOUS, "ERROR_UNKNOWN_SESSION", "ERROR_NO_REPO_MATCH"):
        out = hr.build_result(_result(status), hr.classify(_result(status)), "E:/repo")
        assert out["continuity_info"]["recommended_action"] == "pick_thread"


# --- every handler routes it the same way ----------------------------------

def _patch(mod, monkeypatch, status):
    monkeypatch.setattr(mod, "startup_continuity",
                        lambda **kw: _result(status))


#: (module, handler, how to invoke it).  Claude's handler reads stdin itself, so
#: it is driven through a mocked stdin instead of an argument.
HANDLERS = [
    (zo, zo.handle_zcode_session_start, "arg"),
    (cu, cu.handle_cursor_session_start, "arg"),
    (ki, ki.handle_kiro_session_start, "arg"),
    (ag, ag.handle_antigravity_pre_invocation, "arg"),
    (cl, cl.handle_claude_session_start, "stdin"),
]


def _invoke(monkeypatch, handler, mode):
    if mode == "stdin":
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"session_id": "s", "cwd": "E:/repo"})))
        return handler(cwd="E:/repo")
    return handler(cwd="E:/repo", stdin_raw="{}")


@pytest.mark.parametrize("mod,handler,mode", HANDLERS)
@pytest.mark.parametrize("status,classification", [
    ("ERROR_UNKNOWN_SESSION", "error"),
    ("ERROR_PENDING_CONFLICT", "error"),
    (hr.AMBIGUOUS, "ambiguous"),
])
def test_other_core_errors_are_not_downgraded_to_no_thread(
        monkeypatch, mod, handler, mode, status, classification):
    _patch(mod, monkeypatch, status)
    result = _invoke(monkeypatch, handler, mode)
    assert result["status"] == "error", result
    assert result["classification"] == classification, result
    assert result["attach_status"] == status
    # never reported as a confirmed absence
    assert result["status"] != "no_thread"


@pytest.mark.parametrize("mod,handler,mode", HANDLERS)
def test_a_confirmed_absence_is_still_no_thread(monkeypatch, mod, handler, mode):
    _patch(mod, monkeypatch, "ERROR_NO_REPO_MATCH")
    result = _invoke(monkeypatch, handler, mode)
    assert result["status"] == "no_thread", result
    assert result["classification"] == "no_thread"


@pytest.mark.parametrize("mod,handler,emit", [
    (zo, zo.handle_zcode_session_start, zo.emit),
    (cu, cu.handle_cursor_session_start, cu.emit),
    (ki, ki.handle_kiro_session_start, ki.emit),
    (ag, ag.handle_antigravity_pre_invocation, ag.emit),
])
def test_a_failed_call_injects_nothing(monkeypatch, mod, handler, emit):
    _patch(mod, monkeypatch, "ERROR_UNKNOWN_SESSION")
    result = handler(cwd="E:/repo", stdin_raw="{}")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert emit(result) == 0
    assert out.getvalue() == "", "a failed continuity call must inject nothing"
