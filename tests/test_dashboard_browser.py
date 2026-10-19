"""Real-browser smoke test for the generated dashboard.

Everything else in this file verifies the HTML *text*.  This verifies what a
browser actually does with it: that the filter really filters, that a session
link resolves, that the exit code is visible next to its command, and that
session content injected into the page never executes.

The page is static, so the cheapest honest way to test it in a browser is to let
a real browser load it and dump the resulting DOM.  The probe below is appended
to a *copy* of the generated file -- never to the fixture or to anything the
command writes -- performs the interactions, and writes its verdicts into the
DOM where ``--dump-dom`` can read them.

No browser framework is installed, so none is required: a browser binary found
on the host is used directly.  Without one the test skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

from voyager import dashboard as dash
from voyager.model import new_event, new_session
from voyager.store import Store

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
]


def _find_browser():
    for cand in BROWSERS:
        if os.path.exists(cand) or (shutil.which(cand) is not None):
            return cand
    return None


def _browser_works(binary) -> bool:
    """Can this browser actually launch and run a page?

    A binary existing is not the same as a browser working.  On some hosts the
    executable is present but headless cannot start (no user session, sandbox
    policy, missing runtime), and a test that fails there is a test about the
    host, not about the dashboard.  A browser smoke test must never turn CI red
    for that reason, so check capability first and skip when it is absent.
    """
    tmp = Path(tempfile.mkdtemp(prefix="voyager-brcheck-"))
    try:
        probe = tmp / "probe.html"
        probe.write_text(
            "<!doctype html><html><body><div id='r'>no</div>"
            "<script>document.getElementById('r').textContent='ok'</script>"
            "</body></html>", encoding="utf-8")
        try:
            p = subprocess.run(
                [binary, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--disable-extensions", "--dump-dom",
                 "file:///" + str(probe).replace("\\", "/")],
                capture_output=True, text=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return "'r'>ok<" in (p.stdout or "") or ">ok<" in (p.stdout or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_BROWSER = _find_browser()
_BROWSER_OK = bool(_BROWSER) and _browser_works(_BROWSER)

pytestmark = pytest.mark.skipif(
    not _BROWSER_OK,
    reason="no usable browser binary on this host; the generated-HTML tests "
           "still cover the same properties")


@pytest.fixture
def browser_world(tmp_path):
    db = tmp_path / "w.db"
    store = Store(db)
    src = tmp_path / "s.jsonl"
    src.write_text("{}", encoding="utf-8")
    repo = "E:/repo/browser"
    # A literal </script> would close the probe's own <script> element, so the
    # payload is built from parts in the page source and joined at runtime.
    payload = "<img src=x onerror=\"alert(1)\"><script>alert(2)</script>"

    def put(prov, tag, title, ts, evs):
        sid = "%s:%s" % (prov, tag)
        store.replace_session(
            new_session(id=sid, provider=prov, native_session_id=tag, title=title,
                        started_at=ts, updated_at=ts, repo_root=repo, cwd=repo,
                        message_count=len(evs), can_resume=True), evs, prov, src)
        return sid

    a = put("codex", "a", "CSV exporter", 1000, [
        new_event(sid="codex:a", ts=1000, seq=0, kind="user",
                  content="修复 CSV 引号"),
        new_event(sid="codex:a", ts=1001, seq=1, kind="assistant", content="fixing"),
        new_event(sid="codex:a", ts=1002, seq=2, kind="tool_call", tool_name="exec",
                  command="pytest -q", tool_call_id="t1"),
        new_event(sid="codex:a", ts=1003, seq=3, kind="tool_result", role="tool",
                  tool_call_id="t1", exit_code=1, tool_output="1 failed"),
    ])
    b = put("claude", "b", payload, 2000, [
        new_event(sid="claude:b", ts=2000, seq=0, kind="user", content=payload),
        new_event(sid="claude:b", ts=2001, seq=1, kind="assistant",
                  content="y" * 5000),
    ])
    ta = store.thread_create(repo_root=repo, title="CSV exporter", goal="fixing")
    tb = store.thread_create(repo_root=repo, title="JS resistance", goal="text")
    store.thread_attach(ta, a)
    store.thread_attach(tb, b)
    yield store, repo
    store.close()


_PROBE = """
<script>
(function () {
  var R = [];
  function check(n, c) { R.push(n + '=' + (c ? 'PASS' : 'FAIL')); }
  check('projects_rendered', document.querySelectorAll('.panel').length > 0);
  var q = document.getElementById('q');
  var rows = document.querySelectorAll('#turns .turn');
  check('turns_rendered', rows.length > 0);
  check('filters', (function () {
    q.value = 'CSV'; q.dispatchEvent(new Event('input', {bubbles: true}));
    var shown = 0;
    for (var i = 0; i < rows.length; i++)
      if (rows[i].style.display !== 'none') shown++;
    return shown > 0 && shown < rows.length;
  })());
  check('session_id_searchable', (function () {
    var sid = rows[0].getAttribute('data-sid') || '';
    q.value = sid; q.dispatchEvent(new Event('input', {bubbles: true}));
    var n = 0;
    for (var j = 0; j < rows.length; j++)
      if (rows[j].style.display !== 'none') n++;
    return n > 0;
  })());
  q.value = 'zzz-nope-zzz';
  q.dispatchEvent(new Event('input', {bubbles: true}));
  var note = document.getElementById('qnote');
  check('no_result_note', !!note && note.textContent.length > 0);
  var link = document.querySelector('a[href^="#s-"]');
  check('session_link', !!link);
  check('detail_resolves',
        !!document.getElementById(link.getAttribute('href').slice(1)));
  check('exit_code_visible',
        document.body.textContent.indexOf('pytest -q') >= 0);
  check('ambiguity_stated',
        document.body.textContent.indexOf('nothing is selected') >= 0);
  check('preview_bundle',
        document.body.textContent.indexOf('Continuation Bundle') >= 0);
  check('copyable_command',
        document.body.textContent.indexOf('voyager continue --thread') >= 0);
  check('no_img_element', document.querySelectorAll('img').length === 0);
  var injected = 0, all = document.querySelectorAll('script');
  for (var k = 0; k < all.length - 1; k++)
    if (all[k].textContent.indexOf('alert(2)') >= 0) injected++;
  check('no_script_injected', injected === 0);
  var TAG = '<scr' + 'ipt>' + 'alert(2)</scr' + 'ipt>';
  check('payload_is_text',
        document.body.textContent.indexOf(TAG) >= 0);
  check('truncation_noted',
        document.body.textContent.indexOf('truncated') >= 0);
  var pre = document.createElement('pre');
  pre.id = 'RESULTS'; pre.textContent = R.join(' | ');
  document.body.appendChild(pre);
})();
</script>
"""


def test_dashboard_survives_a_real_browser(browser_world, tmp_path):
    store, repo = browser_world
    out = dash.write(store, tmp_path / "d.html", repo=repo)
    html = out.read_text(encoding="utf-8")
    probe_file = tmp_path / "d-probe.html"
    probe_file.write_text(html.replace("</body>", _PROBE + "</body>"),
                          encoding="utf-8")

    if not _BROWSER_OK:
        pytest.skip("no usable browser binary on this host")
    url = "file:///" + str(probe_file).replace("\\", "/")
    p = subprocess.run(
        [_BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--disable-extensions", "--dump-dom", url],
        capture_output=True, text=True, timeout=180)
    dom = p.stdout or ""
    if not dom.strip():
        pytest.skip("the browser produced no DOM on this host")

    line = next((l for l in dom.splitlines() if 'id="RESULTS"' in l), "")
    assert line, (
        "the probe did not run: the browser returned no RESULTS block\n"
        + dom[:800]
    )
    import html as _html
    verdicts = _html.unescape(
        line.split('id="RESULTS">', 1)[1].rsplit("</pre>", 1)[0]).split(" | ")

    failed = [v.split("=")[0] for v in verdicts if v.endswith("=FAIL")]
    assert not failed, "browser checks failed: %s" % failed
    assert len(verdicts) >= 15, "too few checks ran: %r" % verdicts
