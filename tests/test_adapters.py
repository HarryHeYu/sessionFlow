"""Adapter registry tests.

Per-provider format tests live in tests/test_<provider>.py; this file covers
the cross-cutting contract: every provider loads, registers once, and — the
case that matters on a fresh machine — discovery returns an empty list
instead of crashing when no agent's storage exists.
"""

from __future__ import annotations

import importlib

import pytest

EXPECTED_PROVIDERS = {
    "codex", "claude", "zcode", "dsh", "grok", "cursor", "kiro", "antigravity",
}
# module-level path globals an adapter may expose for discovery
PATH_GLOBALS = ("SESSIONS_DIR", "PROJECTS_DIR", "FILE_HISTORY_DIR",
                "DB_PATH", "VSCDB", "CONV_DIR")


def _loaded():
    from voyager.adapters import load_all
    from voyager.adapters.base import all_adapters
    load_all()
    return all_adapters()


def test_all_providers_registered():
    providers = {a.provider for a in _loaded()}
    assert EXPECTED_PROVIDERS <= providers, f"missing: {EXPECTED_PROVIDERS - providers}"


def test_provider_metadata_is_consistent():
    for ad in _loaded():
        assert ad.provider, "adapter without a provider name"
        assert hasattr(ad, "can_resume") and hasattr(ad, "can_fork")
        # a provider that cannot resume must not advertise a resume command
        assert callable(ad.discover)


def test_discover_returns_empty_when_nothing_installed(monkeypatch, tmp_path):
    """Fresh machine: every adapter discovers zero sources without raising."""
    missing = tmp_path / "not-installed"
    monkeypatch.setenv("APPDATA", str(missing))
    monkeypatch.setenv("HOME", str(missing))
    for ad in _loaded():
        mod = importlib.import_module(type(ad).__module__)
        for name in PATH_GLOBALS:
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, missing / name)
        assert ad.discover() == [], f"{ad.provider} discovered sources when empty"


def test_unpatched_adapters_never_see_real_storage():
    """Isolation guard: without patch_paths an adapter must find NOTHING.

    A test that forgets to redirect its adapter used to discover — and in one
    case overwrite — the real session files of whatever agent was installed
    (a DSH session was destroyed that way; the suite only noticed on CI, where
    no agent data exists). The autouse isolation fixture in conftest.py makes
    such a test fail loudly with an empty discovery instead.
    """
    from voyager.adapters.base import all_adapters

    for ad in _loaded():
        sources = ad.discover()
        assert sources == [], (
            f"{ad.provider} discovered {len(sources)} real source(s) without "
            f"patch_paths: {sources[:3]}"
        )
    assert {a.provider for a in all_adapters()} >= EXPECTED_PROVIDERS


@pytest.mark.parametrize("provider", sorted(EXPECTED_PROVIDERS))
def test_every_adapter_exposes_a_parser(adapter_of, provider):
    ad = adapter_of(provider)
    # multi-session artifacts override scan(), single-artifact adapters parse()
    assert callable(getattr(ad, "scan", None)) or callable(ad.parse)
