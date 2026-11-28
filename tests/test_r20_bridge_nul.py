"""R20/Q regression: a NUL in a bridge request must not kill the JSON-lines
loop or poison later requests.
"""
from __future__ import annotations

from voyager.api import handle_request


def test_nul_query_returns_json_error():
    resp = handle_request(
        {"id": 1, "op": "search", "params": {"query": "bad\x00query"}},
        db=":memory:")
    assert resp["id"] == 1
    assert "error" in resp
    assert "NUL" in resp["error"]


def test_next_request_after_bad_one_is_still_json():
    bad = handle_request(
        {"id": 2, "op": "search", "params": {"query": "bad\x00query"}},
        db=":memory:")
    good = handle_request(
        {"id": 3, "op": "no_such_op"}, db=":memory:")
    assert "error" in bad
    assert "error" in good
    assert good["id"] == 3
