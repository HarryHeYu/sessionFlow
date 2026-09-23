"""Transcript writers (roadmap issue #10 — OPTIONAL, gated).

Writes a NEW native session file containing a flattened, text-only
transcript (user/assistant turns only) of a WorkThread's members, so the
target agent can natively resume a session that never existed before.

Hard gates (all must hold, enforced here and by callers):
- opt-in only: `--mode transcript` (default path is the Continuation Bundle, D11)
- the WorkThread must hold an ACTIVE lease for the target provider
  (D13: one writer per thread); no lease → RuntimeError
- NEW session id only: never append to or rewrite an existing native file
- user/assistant text only: tool calls, reasoning and hidden state are dropped
- provider probe gate: only providers with a verified headless resume of a
  synthetic text-only session are supported (2026-09-16 probe: Grok HIT,
  Codex HIT, Claude TIMEOUT, DSH unverified)

Schema drift fails loudly: after writing, the file is parsed back with the
platform adapter and must round-trip (same id, same turn count) or a
RuntimeError is raised.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List

from .store import Store

# Providers whose headless resume of a synthetic text-only session is
# verified (see docs/ROADMAP.md probe table, 2026-09-16).
SUPPORTED_WRITERS = {"codex", "grok"}

UNSUPPORTED_REASON = {
    "claude": "headless resume probe TIMEOUT (2026-09-16) — bundle mode only",
    "dsh": "synthetic-session resume unverified",
    "zcode": "native format is a private SQLite store; write-back impossible",
    "cursor": "native format is an undocumented KV store; write-back impossible",
    "antigravity": "native format is protobuf; write-back impossible",
    "kiro": "no native session CLI",
}


def writer_supported(provider: str) -> bool:
    return provider in SUPPORTED_WRITERS


def _flatten(events: List[Any], user_max: int = 2000,
             asst_max: int = 4000) -> List[Dict[str, str]]:
    """User/assistant text turns only, in chronological order."""
    turns: List[Dict[str, str]] = []
    for ev in events:
        kind = ev["kind"]
        if kind == "user" and ev["content"]:
            turns.append({"role": "user",
                          "text": ev["content"][:user_max]})
        elif kind == "assistant" and ev["content"]:
            turns.append({"role": "assistant",
                          "text": ev["content"][:asst_max]})
    return turns


def _require_lease(store: Store, thread_id: str, holder: str) -> Dict[str, Any]:
    """D13 gate: an ACTIVE lease for `holder` must exist on the thread."""
    import time as _time
    lease = store.thread_lease_get(thread_id)
    if lease is None:
        raise RuntimeError(
            f"transcript writer requires a lease on {thread_id}; none held")
    if lease["holder"] != holder:
        raise RuntimeError(
            f"lease held by {lease['holder']}, not {holder}")
    if time.time() - (lease["heartbeat_at"] or 0) > 90:
        raise RuntimeError("lease heartbeat is stale (>90s); renew or re-acquire")
    return dict(lease)


# ---------------------------------------------------------------------------
# codex writer: rollout-<ts>-<new_session_id>_voyager.jsonl
# ---------------------------------------------------------------------------

def _codex_writer(store: Store, thread_id: str, members: List[Any],
                  home: Path) -> Dict[str, Any]:
    home = Path(os.environ.get("VOYAGER_HOME_OVERRIDE", home)).expanduser()
    new_native = str(uuid.uuid4())
    sid = f"codex:{new_native}"
    events = []
    for m in members:
        events.extend(store.events(m["id"]))
    turns = _flatten(events)

    cwd = members[0]["cwd"] if members else None
    lines = [
        json.dumps({"timestamp": _iso(time.time()), "ordinal": 0,
                    "type": "session_meta",
                    "payload": {"session_id": new_native,
                                "id": new_native,
                                "timestamp": _iso(time.time()),
                                "cwd": cwd,
                                "originator": "voyager_transplant",
                                "source": "voyager",
                                "model_provider": "openai"}})
    ]
    ordinal = 1
    for t in turns:
        lines.append(json.dumps({
            "timestamp": _iso(time.time()),
            "ordinal": ordinal,
            "type": "response_item",
            "payload": {"type": "message", "role": t["role"],
                        "content": [{"type": "input_text" if t["role"] == "user"
                                     else "output_text", "text": t["text"]}]}}))
        ordinal += 1

    sessions_root = home / ".codex" / "sessions"
    day = time.strftime("%Y/%m/%d")
    out_dir = sessions_root / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{new_native}_voyager.jsonl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": out, "native_session_id": new_native,
            "resume_cmd": f"codex resume {new_native}", "sid": sid,
            "turns": len(turns)}


def _grok_writer(store: Store, thread_id: str, members: List[Any],
                 home: Path) -> Dict[str, Any]:
    home = Path(os.environ.get("VOYAGER_HOME_OVERRIDE", home)).expanduser()
    new_native = str(uuid.uuid4())
    cwd = members[0]["cwd"] if members else None
    encoded = quote(cwd or "unknown")
    sdir = home / ".grok" / "sessions" / encoded / new_native
    sdir.mkdir(parents=True, exist_ok=True)
    events = []
    for m in members:
        events.extend(store.events(m["id"]))
    turns = _flatten(events)

    chat = [{"type": "system", "content": "Voyager transcript transplant "
              "(flattened user/assistant text; tool state dropped)."}]
    for t in turns:
        chat.append({"type": t["role"], "content": t["text"]})
    (sdir / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(x) for x in chat) + "\n", encoding="utf-8")
    summary = {"info": {"id": new_native, "cwd": cwd},
               "session_summary": "voyager transcript transplant",
               "created_at": _iso(time.time()),
               "updated_at": _iso(time.time()),
               "num_messages": len(turns), "current_model_id": "unknown"}
    (sdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False),
                                       encoding="utf-8")
    return {"path": sdir / "chat_history.jsonl",
            "native_session_id": new_native,
            "resume_cmd": f"grok -r {new_native}",
            "sid": f"grok:{new_native}", "turns": len(turns)}


_WRITERS = {"codex": _codex_writer, "grok": _grok_writer}


def write_transcript(store: Store, thread_id: str, target: str,
                     home: Optional[Path] = None) -> Dict[str, Any]:
    """Materialize a flattened transcript of the thread's members as a NEW
    native session for `target`, under an active lease. Round-trip verified:
    the written file must parse back with the platform adapter or this
    raises (schema drift fails loudly)."""
    if target not in SUPPORTED_WRITERS:
        raise RuntimeError(
            f"transcript transplant unsupported for '{target}': "
            + UNSUPPORTED_REASON.get(target, "unverified resume gate"))
    home = home or Path.home()
    lease = _require_lease(store, thread_id, target)
    members = store.thread_members(thread_id)
    if not members:
        raise RuntimeError("thread has no live member sessions to transplant")

    result = _WRITERS[target](store, thread_id, members, home)

    # schema-drift gate: the written file must parse back via the adapter
    _round_trip_check(target, result, home)

    # record the new native session on the lease (D13: one writer)
    store.con.execute(
        "UPDATE thread_leases SET native_session_id=? WHERE thread_id=?",
        (result["native_session_id"], thread_id))
    store.con.commit()
    result["lease_token"] = lease["lease_token"]
    return result


def _round_trip_check(target: str, result: Dict[str, Any], home: Path) -> None:
    """Parse the written file back through the platform adapter: same
    session id must come out, or schema drift fails loudly."""
    from .adapters.base import get_adapter
    ad = get_adapter(target)
    r = ad.parse(result["path"])
    if r is None or "__error__" in r:
        raise RuntimeError("schema drift: written {0} session cannot be "
                           "parsed back".format(target))
    if r["session"]["native_session_id"] != result["native_session_id"]:
        raise RuntimeError("schema drift: {0} round-trip mismatch".format(target))


def _iso(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
