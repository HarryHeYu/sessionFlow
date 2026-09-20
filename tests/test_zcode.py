"""ZCode adapter regression test.

Fixture: tests/fixtures/zcode/seed.sql builds a synthetic db.sqlite whose DDL
mirrors the real ZCode CLI schema (session / message / part / model_usage /
tool_usage, constraints included). Also asserts the tool_usage exit-code
enrichment and the model_usage aggregation the adapter depends on.
"""

from __future__ import annotations


def test_zcode_scan(adapter_of, patch_paths, zcode_fixture):
    # Use env var override for discovery-based scan (new architecture)
    import os
    import sys
    
    # Temporarily set env var before importing adapter
    original_env = os.environ.get('VOYAGER_ZCODE_DB')
    os.environ['VOYAGER_ZCODE_DB'] = str(zcode_fixture)
    
    try:
        # Clear cached modules to force fresh import
        modules_to_remove = [k for k in list(sys.modules.keys()) if 'voyager' in k]
        for mod in modules_to_remove:
            del sys.modules[mod]
        
        ad = adapter_of("zcode")
        bundles = ad.scan(lambda p, f: True)
        
        assert len(bundles) == 1
        s, evs = bundles[0]["session"], bundles[0]["events"]
        assert s["id"] == "zcode:sess_z1"
        assert s["title"] == "zcode title"
        assert s["cwd"] == "E:/proj/demo"
        assert s["model"] == "p:m"
        kinds = [e["kind"] for e in evs]
        assert kinds.count("user") == 1 and kinds.count("reasoning") == 1
        assert kinds.count("tool_call") == 1
        tc = next(e for e in evs if e["kind"] == "tool_call")
        assert tc["exit_code"] == 0     # enriched from tool_usage
        assert s["metadata"]["summary"] == {"additions": 12, "deletions": 3, "files": 4}
        assert s["metadata"]["usage_totals"]["by_model"]["anthropic:claude-sonnet"] == {
            "input": 200, "output": 60, "reasoning": 0,
            "cache_read": 20, "cache_write": 0,
        }
    finally:
        # Restore original env
        if original_env:
            os.environ['VOYAGER_ZCODE_DB'] = original_env
        elif 'VOYAGER_ZCODE_DB' in os.environ:
            del os.environ['VOYAGER_ZCODE_DB']
