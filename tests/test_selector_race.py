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
function clickThread(i){
  const cards=byId['threads'].children.filter(c=>String(c.className).indexOf('thread')===0);
  cards[i].fire('click');
}
function lastOf(type){ const xs=sent.filter(m=>m.type===type); return xs[xs.length-1]||{}; }
function shown(){
  const t=byId['detail'].textContent;
  return { A:t.includes('THREAD-A'), B:t.includes('THREAD-B'), err:t.includes('B failed'),
           newA:t.includes('NEW-A'), staleA:t.includes('STALE-A'),
           newB:t.includes('NEW-B'), loading:t.includes('Loading thread detail') };
}

const A={id:'THREAD-A',title:'Alpha',status:'active',members:1,updated_at:1};
const B={id:'THREAD-B',title:'Beta',status:'active',members:1,updated_at:2};
const dA={thread:A,members:[],lease:{}};
const dB={thread:B,members:[],lease:{}};
const dAold={thread:Object.assign({},A,{title:'STALE-A'}),members:[],lease:{}};
const dAnew={thread:Object.assign({},A,{title:'NEW-A'}),members:[],lease:{}};
const dBnew={thread:Object.assign({},B,{title:'NEW-B'}),members:[],lease:{}};
const out={};

// Initial overview auto-selects the first thread. Capture its generation: it
// is the token of the first (A1) in-flight request.
deliver('overview',{ok:true,repo:'/r',generation:0,data:{threads:[A,B],recent_sessions:[]}});
const a1=lastOf('select');

// A1 in flight, B requested, then A2 requested. A2 returns first; A1 arrives
// late. A2 (and only A2) must remain visible.
clickThread(1); const b1=lastOf('select');
clickThread(0); const a2=lastOf('select');
deliver('detail',{ok:true,thread_id:a2.thread_id,generation:a2.generation,data:dAnew});
deliver('detail',{ok:true,thread_id:a1.thread_id,generation:a1.generation,data:dAold});
out.sameThread=shown();

// A response carrying the current generation but the wrong thread is still
// un-attributable and must not mutate the panel.
clickThread(1); const b2=lastOf('select');
deliver('detail',{ok:true,thread_id:'THREAD-A',generation:b2.generation,data:dAnew});
out.crossThreadIgnored=shown();

// Missing correlation fields are ignored too.
deliver('detail',{ok:true,data:dAold});
out.uncorrelatedIgnored=shown();

// A fresh error must not be overwritten by an old success.
clickThread(1); const b3=lastOf('select');
deliver('detail',{ok:false,thread_id:b3.thread_id,generation:b3.generation,error:'B failed'});
deliver('detail',{ok:true,thread_id:a1.thread_id,generation:a1.generation,data:dAnew});
out.afterStaleOnError=shown();

// Refresh invalidates in-flight detail requests even when the thread id stays
// the same; the ready message must carry an advanced overview generation.
clickThread(1); const b4=lastOf('select');
deliver('detail',{ok:true,thread_id:b4.thread_id,generation:b4.generation,data:dB});
const beforeRefresh=shown();
byId['refresh'].fire('click');
const ready=lastOf('ready');
const afterRefresh=shown();
deliver('detail',{ok:true,thread_id:b4.thread_id,generation:b4.generation,data:dAold});
out.refresh={ beforeB:beforeRefresh.B, generation:ready.generation,
              cleared:afterRefresh.loading, staleIgnored:!shown().staleA };

// Core errors must not render the "run scan" empty-state hint.
deliver('overview',{ok:false,error:'core unavailable',generation:ready.generation});
out.coreError={banner:byId['banner'].textContent,
               scanHint:byId['threads'].textContent.includes('Run voyager scan')};

// A disposed webview must ignore every late response.
deliver('overview',{ok:true,repo:'/r',generation:ready.generation,data:{threads:[A,B],recent_sessions:[]}});
clickThread(1); const b5=lastOf('select');
deliver('detail',{ok:true,thread_id:b5.thread_id,generation:b5.generation,data:dB});
const beforeDispose=shown();
deliver('dispose',{});
deliver('detail',{ok:true,thread_id:b5.thread_id,generation:b5.generation,data:dBnew});
out.dispose={before:beforeDispose,after:shown()};
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


def test_same_thread_old_response_cannot_beat_newer_generation(driver_verdict):
    """A1 -> B -> A2; A2 returns first and the late A1 must be discarded."""
    got = driver_verdict["sameThread"]
    assert got["newA"] is True and got["staleA"] is False, (
        "same-thread generation was not enforced: %r" % got)


def test_response_for_another_thread_is_ignored_even_with_current_generation(driver_verdict):
    got = driver_verdict["crossThreadIgnored"]
    assert got["A"] is False and got["B"] is False, (
        "a response for another thread mutated the panel: %r" % got)


def test_detail_without_correlation_fields_is_ignored(driver_verdict):
    """Both thread_id and generation are mandatory correlation tokens."""
    got = driver_verdict["uncorrelatedIgnored"]
    assert got["A"] is False and got["B"] is False, (
        "an un-correlated detail response changed the panel: %r" % got)


def test_stale_success_does_not_clear_newer_error(driver_verdict):
    """B fails, then late A success must not replace B's error with A."""
    got = driver_verdict["afterStaleOnError"]
    assert got["err"] is True and got["newA"] is False, (
        "stale A success cleared B's error: %r" % got)


def test_refresh_invalidates_in_flight_detail_and_advances_generation(driver_verdict):
    got = driver_verdict["refresh"]
    assert got["beforeB"] is True, "B detail was not shown before refresh"
    assert isinstance(got["generation"], int) and got["generation"] > 0, (
        "refresh ready message lost its generation: %r" % got)
    assert got["cleared"] is True, "refresh did not drop the stale detail"
    assert got["staleIgnored"] is True, "a pre-refresh response returned after refresh"


def test_core_error_does_not_render_empty_state_hint(driver_verdict):
    """A Core failure is not the same state as 'no WorkThreads indexed'."""
    got = driver_verdict["coreError"]
    assert "core unavailable" in got["banner"]
    assert got["scanHint"] is False, "error state rendered the 'run scan' empty hint"


def test_disposed_webview_ignores_late_responses(driver_verdict):
    got = driver_verdict["dispose"]
    assert got["before"]["B"] is True, "B detail was not shown before dispose"
    assert got["after"]["B"] is True and got["after"]["newB"] is False, (
        "a late response mutated a disposed webview: %r" % got)


def test_extension_host_echoes_generation_and_guards_disposed_panel():
    src = (EXT / "extension.js").read_text(encoding="utf-8")
    assert 'generation: msg.generation' in src, (
        "the host must echo the generation token on detail responses")
    assert 'generation: generation' in src, (
        "the host must echo the generation token on overview responses")
    assert 'if (disposed' in src, (
        "the host must stop posting to a disposed selector panel")
