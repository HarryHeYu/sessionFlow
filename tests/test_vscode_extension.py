"""The VS Code extension is a *scaffold* — nothing here can run it.

There is no `code --extensionDevelopmentPath` in CI, so the only automated
protection against a broken extension is static consistency: a contributed
command with no handler is a dead button, and a `package.json` that points at
a file which does not exist is a broken icon. Both happened:

* `contributes.viewsContainers.activitybar[].icon` pointed at
  `media/voyager.svg` from the day the scaffold landed, but `media/` did not
  exist at all, so the activity-bar icon was blank;
* the bridge created a new output channel on every stderr chunk.

The Context Composer adds a second, subtler hazard: its webview document is
built from a JavaScript *template literal*, so a stray backtick or `${` inside
the embedded script silently truncates the document. `test_composer_inline_
script_parses` catches that by handing the embedded script to a real JS parser
(skipped when node is unavailable, like the project's other optional checks).
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "vscode-extension"


@pytest.fixture(scope="module")
def pkg() -> dict:
    return json.loads((EXT / "package.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def extension_js() -> str:
    return (EXT / "extension.js").read_text(encoding="utf-8")


def test_package_json_parses(pkg):
    assert pkg["main"] == "./extension.js"
    assert pkg["engines"]["vscode"]


def test_every_file_package_json_points_at_exists(pkg):
    """A contributed icon that isn't there is a blank button, not an error."""
    referenced = [pkg["main"]]
    for container in pkg["contributes"].get("viewsContainers", {}).get("activitybar", []):
        referenced.append(container["icon"])
    for cmd in pkg["contributes"]["commands"]:
        if "icon" in cmd:
            referenced.append(cmd["icon"])

    missing = [r for r in referenced if not (EXT / r).is_file()]
    assert not missing, "package.json references missing file(s): %s" % missing


def test_activitybar_icon_is_a_real_svg(pkg):
    icon = pkg["contributes"]["viewsContainers"]["activitybar"][0]["icon"]
    text = (EXT / icon).read_text(encoding="utf-8")
    assert "<svg" in text and "</svg>" in text
    assert 'viewBox="0 0 24 24"' in text, "activity bar icons must be 24x24"


def test_every_contributed_command_has_a_handler(pkg, extension_js):
    """A contributed command with no registerCommand is a button that throws."""
    contributed = {c["command"] for c in pkg["contributes"]["commands"]}
    registered = set(re.findall(r'registerCommand\(\s*"([^"]+)"', extension_js))
    assert contributed - registered == set(), (
        "contributed but never registered: %s" % sorted(contributed - registered))


def test_the_composer_command_is_wired(pkg, extension_js):
    assert "voyager.openComposer" in {c["command"] for c in pkg["contributes"]["commands"]}
    assert "voyager.openComposer" in extension_js
    assert "onCommand:voyager.openComposer" in pkg["activationEvents"]
    # Reachable from the view's title bar, not only the palette.
    titles = pkg["contributes"]["menus"]["view/title"]
    assert any(m["command"] == "voyager.openComposer" for m in titles)


def test_view_activation_event_is_declared(pkg):
    """The WorkThreads view must be able to activate the extension."""
    assert "onView:voyager.threads" in pkg["activationEvents"]


# --- the webview document --------------------------------------------------

@functools.lru_cache(maxsize=None)
def _composer_html(nonce: str = "TESTNONCE") -> str:
    """Render the webview document by actually running composer.js.

    Python cannot import a `.js` file (unknown source suffix), and a
    re-implementation in Python would test the re-implementation rather than
    the shipped module, so the real module is evaluated with node.  Cached:
    a process spawn costs ~1.3 s in this sandbox, and the render is pure.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = "const c=require(%s);process.stdout.write(c.html(%s));" % (
        json.dumps(str(EXT / "composer.js")), json.dumps(nonce))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_composer_html_is_self_contained():
    html = _composer_html()
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    # No network, no external assets: the CSP forbids it and nothing asks.
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html
    assert "src=" not in html, "the document must not pull in an external asset"
    assert html.count("<script") == 1 and html.count("</script>") == 1


def test_composer_nonce_reaches_both_the_csp_and_the_script_tag():
    html = _composer_html("ABC123")
    assert "script-src 'nonce-ABC123'" in html
    assert '<script nonce="ABC123">' in html
    assert "${nonce}" not in html, "unsubstituted placeholder in the document"


def test_composer_never_writes_untrusted_text_as_markup():
    """Session titles come from an agent's transcript; they go in as text."""
    source = (EXT / "composer.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source, (
        "the Composer must build nodes with textContent: session titles are "
        "untrusted and would otherwise be injected as markup")
    assert "textContent" in source


def test_composer_inline_script_parses(tmp_path):
    """The document is a template literal — a stray backtick truncates it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")

    html = _composer_html()
    body = html.split("<script nonce=", 1)[1].split(">", 1)[1]
    script = body.rsplit("</script>", 1)[0]
    target = tmp_path / "composer-inline.js"
    target.write_text(script, encoding="utf-8")
    proc = subprocess.run([node, "--check", str(target)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_extension_js_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for name in ("extension.js", "composer.js"):
        proc = subprocess.run([node, "--check", str(EXT / name)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, "%s: %s" % (name, proc.stderr)


def test_one_click_switch_command_is_wired_end_to_end(pkg, extension_js):
    """Phase 7 one-click switch: the tree items are switch targets, the
    command is contributed with an inline menu on the threads view, and the
    handler runs `voyager switch <target>` through the CLI surface (the
    P9 read-only API stays read-only — the engine is shared, not forked)."""
    commands = {c["command"]: c for c in pkg["contributes"]["commands"]}
    assert "voyager.switchThread" in commands
    menus = pkg["contributes"]["menus"]["view/item/context"]
    entry = next(m for m in menus if m["command"] == "voyager.switchThread")
    assert entry["when"] == "view == voyager.threads && viewItem == thread"
    assert entry["group"].startswith("inline")

    assert 'contextValue = "thread"' in extension_js
    assert "voyager.switchThread" in extension_js
    assert "voyager switch ${target}" in extension_js
    # the switch goes through the CLI engine — the read-only API gains no
    # handoff op: the P9 boundary is pinned by checking the op registry
    api_text = (EXT.parent / "voyager" / "api.py").read_text(encoding="utf-8")
    ops_block = api_text.split("_OPS = {", 1)[1].split("}", 1)[0]
    assert "handoff" not in ops_block
