"""Executable out-of-order regression for the WorkThread Selector.

The other tests in this module are static source assertions. Those cannot catch
a stale response overwriting a newer selection: that is a *runtime* property of
the webview message loop. Here we actually execute the shipped inline script in
a node:vm sandbox with a minimal DOM stub, inject messages in a chosen order,
and assert on the rendered detail text.

The harness mirrors the minimum reproduction from the JYY cognitive review;
it is deliberately kept small so a regression in the message contract (a lost
thread_id, a removed generation check) turns RED immediately.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "vscode-extension"


def _render_selector_html() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = "const c=require(%s);process.stdout.write(c.html(%s));" % (
        json.dumps(str(EXT / "selector.js")), json.dumps("TESTNONCE"))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# The driver executes the inline script and prints a JSON verdict. It lives as
# a string so the test stays self-contained (no extra harness file to ship).
_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const mm = html.match(/<script nonce="[^"]*">([\s\S]*?)<\/script>/);
if (!mm) { console.log(JSON.stringify({error:'no inline script'})); process.exit(0); }

class El {
  constructor(tag){ this.tag=tag; this.children=[]; this._text=''; this.listeners={};
                    this.className=''; this.value=''; this.disabled=false; }
  set textContent(v){ this._text=String(v); this.children=[]; }
  get textContent(){ return this.children.length ? this.children.map(c=>c.textContent).join('') : this._text; }
  appendChild(c){ this.children.push(c); return c; }
  addEventListener(t,fn){ (this.listeners[t]||(this.listeners[t]=[])).push(fn); }
  fire(t){ (this.listeners[t]||[]).forEach(fn=>fn.call(this,{})); }
  get childNodes(){ return this.children; }
  get options(){ return this.children; }
  remove(){}
}
const byId={};
for (const id of ['banner','threads','detail','repo','refresh']) byId[id]=new El('div');
const sent=[]; let msgHandler=null;
const ctx={
  acquireVsCodeApi:()=>({postMessage:m=>sent.push(m)}),
  document:{ getElementById:id=>byId[id]||(byId[id]=new El('div')),
             createElement:t=>new El(t),
             createTextNode:t=>{const e=new El('#t'); e.textContent=t; return e;} },
  window:{ addEventListener:(t,fn)=>{ if(t==='message') msgHandler=fn; } },
  console
};
vm.createContext(ctx);
vm.runInContext(mm[1], ctx);
function deliver(type, extra){ msgHandler({data:Object.assign({type},extra)}); }
function shown(){ const t=byId['detail'].textContent;
  return { A:t.includes('THREAD-A'), B:t.includes('THREAD-B'), err:t.includes('B failed') }; }

const A={id:'THREAD-A',title:'Alpha',status:'active',members:1,updated_at:1};
const B={id:'THREAD-B',title:'Beta',status:'active',members:1,updated_at:2};
const dA={thread:A,members:[],lease:{}};
const dB={thread:B,members:[],lease:{}};
const out={};

deliver('overview',{ok:true,repo:'/r',data:{threads:[A,B],recent_sessions:[]}});
const cards=byId['threads'].children.filter(c=>String(c.className).indexOf('thread')===0);
cards[1].fire('click');                       // select B (newer request)
// The host now echoes the requested thread_id on every detail response.
deliver('detail',{ok:true,thread_id:'THREAD-B',data:dB});   // B returns first
deliver('detail',{ok:true,thread_id:'THREAD-A',data:dA});   // A returns late (stale)
out.afterLateA=shown();                       // want B=true, A=false

// a response without a correlation id is un-attributable and must be ignored
deliver('detail',{ok:true,data:dA});
out.uncorrelatedIgnored=shown();              // still B, never A

// error must not be overwritten by a stale success
deliver('overview',{ok:true,repo:'/r',data:{threads:[A,B],recent_sessions:[]}});
cards[1].fire('click');
deliver('detail',{ok:false,thread_id:'THREAD-B',error:'B failed'});
deliver('detail',{ok:true,thread_id:'THREAD-A',data:dA});
out.afterStaleOnError=shown();                // want err=true, A=false

// core error must not render the empty-state scan hint
deliver('overview',{ok:false,error:'core unavailable'});
out.coreError={banner:byId['banner'].textContent,
               scanHint:byId['threads'].textContent.includes('Run voyager scan')};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_verdict(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = _render_selector_html()
    html_file = tmp_path_factory.mktemp("selector-race") / "selector.html"
    html_file.write_text(html, encoding="utf-8")
    driver = tmp_path_factory.mktemp("selector-driver") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run([node, str(driver), str(html_file)],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_late_stale_detail_does_not_overwrite_newer_selection(driver_verdict):
    """A first, B second; B returns then late A -> detail must stay on B."""
    got = driver_verdict["afterLateA"]
    assert got["B"] is True and got["A"] is False, (
        "stale A overwrote newer B selection: %r" % got)


def test_detail_without_correlation_id_is_ignored(driver_verdict):
    """An un-attributable response must not mutate the current detail."""
    got = driver_verdict["uncorrelatedIgnored"]
    assert got["B"] is True and got["A"] is False, (
        "a detail response without thread_id changed the panel: %r" % got)


def test_stale_success_does_not_clear_newer_error(driver_verdict):
    """B fails, then late A success must not replace B's error with A."""
    got = driver_verdict["afterStaleOnError"]
    assert got["err"] is True and got["A"] is False, (
        "stale A success cleared B's error: %r" % got)


def test_core_error_does_not_render_empty_state_hint(driver_verdict):
    """A Core failure is not the same state as 'no WorkThreads indexed'."""
    got = driver_verdict["coreError"]
    assert "core unavailable" in got["banner"]
    assert got["scanHint"] is False, "error state rendered the 'run scan' empty hint"
