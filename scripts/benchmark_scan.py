"""Scalability baseline: measure scan performance at scale.

Uses SQLite set_trace_callback for SQL counting without modifying core code.
Synthetic data only - no user sessions accessed.
"""

from __future__ import annotations

import os
import sys
import time
import tracemalloc
import json
from pathlib import Path
from typing import List, Tuple, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from voyager.store import Store
from voyager.model import new_event


def generate_synthetic_sessions(count: int, repo_root: Path) -> List[Tuple[Path, dict, list]]:
    """Generate synthetic sessions across multiple providers."""
    sessions = []
    providers = ["codex", "claude", "grok", "zcode", "dsh"]
    
    for i in range(count):
        provider = providers[i % len(providers)]
        session_id = f"{provider}:bench-{i:06d}"
        
        src_dir = repo_root / "sessions" / provider
        src_dir.mkdir(parents=True, exist_ok=True)
        src_file = src_dir / f"session_{i:06d}.jsonl"
        
        session_data = {
            "id": session_id, "provider": provider,
            "native_session_id": session_id.split(":")[-1],
            "title": f"Benchmark session {i}",
            "started_at": 1700000000.0 + i, "updated_at": 1700000100.0 + i,
            "cwd": str(repo_root), "repo_root": str(repo_root),
            "message_count": 1, "tool_count": 0,
            "can_resume": True, "resume_cmd": f"{provider} resume {session_id}",
        }
        
        events = [new_event(sid=session_id, ts=1700000000.0 + i, seq=0,
                           kind="user", role="user",
                           content=f"Session {i} work item")]
        
        src_file.write_text("", encoding="utf-8")
        sessions.append((src_file, session_data, events))
    
    return sessions


class SQLCounter:
    """Track SQLite query count using set_trace_callback."""
    
    def __init__(self):
        self.count = 0
        self._original_callback = None
        
    def start_tracking(self, conn):
        """Start SQL tracing."""
        def trace_cb(command):
            if command.strip().startswith(("SELECT", "INSERT", "UPDATE", "DELETE")):
                self.count += 1
        self._original_callback = conn.set_trace_callback(trace_cb)
    
    def stop_tracking(self):
        """Stop SQL tracing."""
        if self._original_callback:
            conn.set_trace_callback(None)


def run_baseline(size: int, tmp_base: Path, sql_counter: SQLCounter) -> dict:
    """Run measurement at specified scale."""
    repo_root = tmp_base / f"scale-{size:,}"
    db_path = repo_root / "index.db"
    
    if repo_root.exists():
        import shutil
        shutil.rmtree(repo_root)
    repo_root.mkdir(parents=True)
    
    print(f"\nScale: {size:,} sessions")
    print("-" * 40)
    
    # Generate data
    start_gen = time.time()
    sessions = generate_synthetic_sessions(size, repo_root)
    gen_time = time.time() - start_gen
    
    # Run scan with instrumentation
    tracemalloc.start()
    start_scan = time.time()
    
    store = Store(db_path)
    sql_counter.start_tracking(store.con)
    
    # Simulate incremental scan by creating all sessions
    for src_file, session_data, events in sessions:
        store.replace_session(session_data, events, "test", src_file)
    
    scan_time = time.time() - start_scan
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    query_count = sql_counter.count
    
    results = {
        "size": size,
        "generation_time": gen_time,
        "scan_time": scan_time,
        "peak_memory_mb": peak_mem / 1024 / 1024,
        "query_count": query_count,
    }
    
    print(f"Generation: {gen_time:.2f}s")
    print(f"Scan: {scan_time:.2f}s")
    print(f"Queries: {query_count}")
    print(f"Peak memory: {peak_mem / 1024 / 1024:.1f} MB")
    
    store.close()
    
    # Save results
    output_file = tmp_base / f"baseline_{size}_k.json"
    output_file.write_text(json.dumps(results, indent=2))
    
    return results


if __name__ == "__main__":
    import tempfile
    
    print("=" * 60)
    print("SCALABILITY BASELINE MEASUREMENT")
    print("=" * 60)
    
    tmp_dir = Path(tempfile.gettempdir()) / "sessionflow-bench"
    
    counter = SQLCounter()
    
    results = {}
    for size in [1_000, 10_000]:
        result = run_baseline(size, tmp_dir, counter)
        results[size] = result
    
    print("\n" + "=" * 60)
    print("BASELINE SUMMARY")
    print("=" * 60)
    
    for size, metrics in sorted(results.items()):
        print(f"\n{size:,} sessions:")
        print(f"  Generation: {metrics['generation_time']:.2f}s")
        print(f"  Scan time: {metrics['scan_time']:.2f}s")
        print(f"  Queries: {metrics['query_count']}")
        print(f"  Peak memory: {metrics['peak_memory_mb']:.1f} MB")
