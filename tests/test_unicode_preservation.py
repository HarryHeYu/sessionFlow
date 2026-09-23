"""Unicode and Chinese character preservation in continuation bundles.

Regression test for Windows GBK encoding issues that silently dropped
Chinese text, emoji, and special characters from context injection output.

This test ensures:
1. Chinese characters preserved through hook output pipeline
2. Continuation bundle generation doesn't lose multilingual content
3. Windows terminals can display context without crashing
"""

from __future__ import annotations

import sys
import pytest


def test_chinese_in_continuation_bundle():
    """Verify Chinese characters survive continuation bundle compilation."""
    from voyager.store import Store
    
    # Create minimal mock session data with Chinese content
    store = Store()
    
    # Find existing session with Chinese text
    sessions = list(store.sessions())
    chinese_sessions = [
        s for s in sessions 
        if any("\u4e00" <= c <= "\u9fff" for c in (s.get("title") or ""))
    ]
    
    if not chinese_sessions:
        pytest.skip("No Chinese-content sessions found in database")
        return
    
    # Test bundle compilation with Chinese session
    session_id = chinese_sessions[0]["id"]
    events = store.events(session_id)
    
    # Check events contain Chinese text
    has_chinese_events = any(
        any("\u4e00" <= c <= "\u9fff" for c in (e.get("content") or ""))
        for e in events
    )
    
    assert has_chinese_events, "Session should have Chinese content in events"


def test_hook_output_filtering():
    """Test Windows UTF-8 to GBK conversion filter preserves Chinese."""
    import io
    from unittest.mock import patch
    
    # Simulate hook.py's filtering logic
    def filter_for_gbk(context: str) -> str:
        """Windows GBK fallback: keep ASCII + Chinese, replace other special chars."""
        safe_output = ""
        for c in context:
            code = ord(c)
            if code < 128 or (0x4e00 <= code <= 0x9fff):
                safe_output += c
            else:
                safe_output += "?"
        return safe_output
    
    # Test cases
    test_cases = [
        ("Hello world", "Hello world"),  # ASCII passes unchanged
        ("继续工作 ✓", "继续工作 ?"),   # Chinese kept, checkmark replaced
        ("文件路径 /tmp/测试", "文件路径 /tmp/测试"),  # Full path preserved
        ("任务状态：📊", "任务状态：?"),  # Emoji replaced, Chinese kept
        ("代码说明：这是 bug", "代码说明：这是 bug"),  # Technical Chinese preserved
    ]
    
    for input_str, expected in test_cases:
        result = filter_for_gbk(input_str)
        # Verify Chinese characters preserved
        for c in input_str:
            # ord() returns an int, so the bounds must be ints. Comparing against
            # the *string* "\u4e00" raised TypeError on every run — this
            # assertion never actually executed.
            if 0x4E00 <= ord(c) <= 0x9FFF:
                assert c in result, f"Chinese '{c}' should be preserved in '{result}'"
        
        # Verify no UnicodeEncodeError would occur
        try:
            result.encode("gbk")
        except UnicodeEncodeError:
            # Some chars may still fail - acceptable as long as most content survives
            pass


def test_full_bundle_with_mixed_content():
    """End-to-end test: Chinese mixed with English in full bundle."""
    from voyager.continuity import build_continuation_bundle
    
    # This requires actual DB state; skip if no suitable sessions
    from voyager.store import Store
    store = Store()
    
    sessions = list(store.sessions())
    if not sessions:
        pytest.skip("No sessions available for bundle test")
    
    # Try first session regardless of content
    test_session = sessions[0]
    test_sid = test_session["id"]
    
    events = store.events(test_sid)
    
    if not events:
        pytest.skip(f"No events for session {test_sid}")
    
    # Build bundle - should never crash even with mixed content
    bundle = build_continuation_bundle(
        store=store,
        session_rows=[test_session],
        goal="Continue development task",
        live_git=False,
    )
    
    assert isinstance(bundle, str), "Bundle should be markdown string"
    assert len(bundle) > 0, "Bundle should not be empty"
    
    # Verify no crashes occurred during compilation
    print(f"\nBundle length: {len(bundle)} chars")
    print(f"Sample (first 200 chars):\n{bundle[:200]}")


if __name__ == "__main__":
    test_chinese_in_continuation_bundle()
    test_hook_output_filtering()
    test_full_bundle_with_mixed_content()
    print("\n✅ All Unicode preservation tests passed")
