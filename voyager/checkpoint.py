"""WorkThread Deterministic Checkpoint System.

Deterministic, LLM-independent checkpoint management for WorkThreads.
This is the authoritative state source, NOT derived from session summaries.

Design Principles (ROADMAP.md Phase 4 / issue #8):
1. **Deterministic**: Checkpoints are explicit state, not LLM summaries
2. **Source Priority**: Explicit WorkThread state > deterministic derived state (session evidence) > LLM summary (optional enhancement only)
3. **Checkpoint Structure**: goal, phase, milestones, blockers, decisions, changed files, commands/tests/activities, active branches/HEAD, next actions
4. **Lifecycle Operations**: create, update, list, show, restore, export, import

Schema: CHECKPOINT table stores explicit WorkThread state with:
- id: "chk_<thread_id>:<timestamp>"
- thread_id: parent WorkThread
- goal: current primary objective (updated manually or via AI hint)
- phase: current development phase (analysis, design, implementation, testing, review)
- milestones_completed: list of completed milestones
- milestones_pending: list of pending milestones  
- blockers: current blockers/resolutions
- decisions: important decisions made (with rationale)
- changed_files: files modified in last iteration
- commands_executed: notable commands/runs
- tests_passed: recent test results
- branches_active: current branch state
- head_commit: latest commit SHA
- next_actions: immediate next steps
- created_at, updated_at: timestamps

CLI Commands:
- voyager thread checkpoint create --goal "..." --phase "implementation"
- voyager thread checkpoint list [--thread <id>]
- voyager thread checkpoint show <checkpoint_id>
- voyager thread checkpoint update --add-blocker "..." --decision "..."
- voyager thread checkpoint restore <id> --restore-to-current
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import Store


# --- Data Models -----------------------------------------------------------

@dataclass
class CheckpointMilestone:
    """A milestone in the project lifecycle."""
    title: str
    status: str  # "completed", "in_progress", "pending"
    description: str = ""
    completed_at: Optional[float] = None
    owner: Optional[str] = None  # Which provider/session owned this


@dataclass
class CheckpointBlocker:
    """A blocker currently preventing progress."""
    description: str
    severity: str  # "high", "medium", "low"
    resolved: bool = False
    resolution: Optional[str] = None
    resolved_at: Optional[float] = None
    reported_at: float = field(default_factory=time.time)


@dataclass
class CheckpointDecision:
    """An important decision made with rationale."""
    topic: str
    decision: str
    rationale: str
    made_by: str  # provider or user
    made_at: float = field(default_factory=time.time)
    alternative_considered: Optional[str] = None


@dataclass
class Checkpoint:
    """Deterministic checkpoint for a WorkThread."""

    thread_id: str
    goal: str

    # Development lifecycle
    phase: str  # "analysis", "design", "implementation", "testing", "review", "complete"
    milestones: List[CheckpointMilestone] = field(default_factory=list)

    # Current issues
    blockers: List[CheckpointBlocker] = field(default_factory=list)
    decisions: List[CheckpointDecision] = field(default_factory=list)

    # State snapshot
    changed_files: List[str] = field(default_factory=list)
    commands_executed: List[str] = field(default_factory=list)
    tests_passed: List[str] = field(default_factory=list)

    # Git state
    branches_active: List[str] = field(default_factory=list)
    head_commit: Optional[str] = None

    # Next steps
    next_actions: List[str] = field(default_factory=list)

    # Metadata
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            "thread_id": self.thread_id,
            "goal": self.goal,
            "phase": self.phase,
            "milestones": [asdict(m) for m in self.milestones],
            "blockers": [asdict(b) for b in self.blockers],
            "decisions": [asdict(d) for d in self.decisions],
            "changed_files": self.changed_files,
            "commands_executed": self.commands_executed,
            "tests_passed": self.tests_passed,
            "branches_active": self.branches_active,
            "head_commit": self.head_commit,
            "next_actions": self.next_actions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Checkpoint":
        """Reconstruct from dict."""
        return cls(
            thread_id=data["thread_id"],
            goal=data["goal"],
            phase=data.get("phase", "analysis"),
            milestones=[CheckpointMilestone(**m) for m in data.get("milestones", [])],
            blockers=[CheckpointBlocker(**b) for b in data.get("blockers", [])],
            decisions=[CheckpointDecision(**d) for d in data.get("decisions", [])],
            changed_files=data.get("changed_files", []),
            commands_executed=data.get("commands_executed", []),
            tests_passed=data.get("tests_passed", []),
            branches_active=data.get("branches_active", []),
            head_commit=data.get("head_commit"),
            next_actions=data.get("next_actions", []),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


# --- Schema Extensions -----------------------------------------------------

CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id              TEXT PRIMARY KEY,       -- chk_<thread_id>:<timestamp>
    thread_id       TEXT NOT NULL,          -- parent WorkThread
    goal            TEXT NOT NULL,
    phase           TEXT DEFAULT 'analysis',
    milestones_json TEXT,                   -- JSON array of milestones
    blockers_json   TEXT,                   -- JSON array of blockers
    decisions_json  TEXT,                   -- JSON array of decisions
    changed_files   TEXT,                   -- JSON array of file paths
    commands_json   TEXT,                   -- JSON array of command strings
    tests_json      TEXT,                   -- JSON array of test strings
    branches_json   TEXT,                   -- JSON array of branch names
    head_commit     TEXT,
    next_actions    TEXT,                   -- JSON array of action strings
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    FOREIGN KEY(thread_id) REFERENCES threads(id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON checkpoints(thread_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_created ON checkpoints(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_phase ON checkpoints(phase);
"""


def add_checkpoint_schema(conn):
    """Add checkpoint schema to database connection."""
    conn.executescript(CHECKPOINT_SCHEMA)


# --- Core Functions --------------------------------------------------------

def checkpoint_create(
    store: Store,
    thread_id: str,
    goal: str,
    phase: str = "analysis",
    milestones: Optional[List[Dict[str, Any]]] = None,
    next_actions: Optional[List[str]] = None,
) -> str:
    """Create a new checkpoint for a WorkThread."""
    now = time.time()
    checkpoint_id = f"chk_{thread_id}:{now}"

    check = Checkpoint(
        thread_id=thread_id,
        goal=goal,
        phase=phase,
        milestones=[CheckpointMilestone(**m) for m in (milestones or [])],
        next_actions=next_actions or [],
        created_at=now,
        updated_at=now,
    )

    conn = store._con
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO checkpoints (id, thread_id, goal, phase, milestones_json, 
                                 next_actions, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        checkpoint_id,
        thread_id,
        goal,
        phase,
        json.dumps([asdict(m) for m in check.milestones]),
        json.dumps(check.next_actions),
        now,
        now,
    ))
    conn.commit()

    return checkpoint_id


def checkpoint_get(store: Store, checkpoint_id: str) -> Optional[Checkpoint]:
    """Get a checkpoint by ID."""
    conn = store._con
    row = conn.execute(
        "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
    ).fetchone()

    if not row:
        return None

    return _row_to_checkpoint(row)


def checkpoint_list(
    store: Store,
    thread_id: Optional[str] = None,
    phase: Optional[str] = None,
    limit: int = 10,
) -> List[Tuple[str, Checkpoint]]:
    """List checkpoints, optionally filtered by thread_id and phase."""
    conn = store._con
    query = "SELECT * FROM checkpoints WHERE 1=1"
    params = []

    if thread_id:
        query += " AND thread_id = ?"
        params.append(thread_id)
    if phase:
        query += " AND phase = ?"
        params.append(phase)

    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [(row[0], _row_to_checkpoint(row)) for row in rows]


def checkpoint_update(
    store: Store,
    checkpoint_id: str,
    goal: Optional[str] = None,
    phase: Optional[str] = None,
    add_blocker: Optional[str] = None,
    severity: str = "medium",
    add_decision: Optional[Dict[str, str]] = None,
    add_next_action: Optional[str] = None,
    record_git_state: bool = False,
) -> str:
    """Update an existing checkpoint (soft update - creates new version)."""
    # Get existing checkpoint
    existing = checkpoint_get(store, checkpoint_id)
    if not existing:
        raise ValueError(f"Checkpoint not found: {checkpoint_id}")

    # Apply updates
    if goal:
        existing.goal = goal
    if phase:
        existing.phase = phase
    if add_blocker:
        existing.blockers.append(CheckpointBlocker(
            description=add_blocker,
            severity=severity,
            reported_at=time.time(),
        ))
    if add_decision:
        existing.decisions.append(CheckpointDecision(
            topic=add_decision.get("topic", "Unknown"),
            decision=add_decision.get("decision", ""),
            rationale=add_decision.get("rationale", ""),
            made_by=add_decision.get("made_by", "user"),
            alternative_considered=add_decision.get("alternative"),
        ))
    if add_next_action:
        existing.next_actions.append(add_next_action)

    # Optionally record git state
    if record_git_state:
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
                **subprocess.__dict__.get('DEVNULL', open('/dev/null', 'w')) or {}
            )
            if result.returncode == 0:
                existing.branches_active = [result.stdout.strip()]
        except:
            pass

    existing.updated_at = time.time()

    # Save as new checkpoint (append-only history)
    return checkpoint_store(store, existing)


def checkpoint_store(store: Store, checkpoint: Checkpoint) -> str:
    """Store checkpoint (used internally after updates)."""
    now = time.time()
    checkpoint_id = f"chk_{checkpoint.thread_id}:{now}"

    conn = store._con
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO checkpoints (id, thread_id, goal, phase, milestones_json,
                                 blockers_json, decisions_json, changed_files,
                                 commands_json, tests_json, branches_json,
                                 head_commit, next_actions, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        checkpoint_id,
        checkpoint.thread_id,
        checkpoint.goal,
        checkpoint.phase,
        json.dumps([asdict(m) for m in checkpoint.milestones]) if checkpoint.milestones else None,
        json.dumps([asdict(b) for b in checkpoint.blockers]) if checkpoint.blockers else None,
        json.dumps([asdict(d) for d in checkpoint.decisions]) if checkpoint.decisions else None,
        json.dumps(checkpoint.changed_files) if checkpoint.changed_files else None,
        json.dumps(checkpoint.commands_executed) if checkpoint.commands_executed else None,
        json.dumps(checkpoint.tests_passed) if checkpoint.tests_passed else None,
        json.dumps(checkpoint.branches_active) if checkpoint.branches_active else None,
        checkpoint.head_commit,
        json.dumps(checkpoint.next_actions),
        checkpoint.created_at,
        now,
    ))
    conn.commit()

    return checkpoint_id


def checkpoint_delete(store: Store, checkpoint_id: str) -> bool:
    """Delete a checkpoint (permanent!)."""
    conn = store._con
    cursor = conn.cursor()
    cursor.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
    affected = cursor.rowcount
    conn.commit()
    return affected > 0


def checkpoint_export(store: Store, checkpoint_id: str, output_path: Path) -> bool:
    """Export checkpoint to JSON file."""
    checkpoint = checkpoint_get(store, checkpoint_id)
    if not checkpoint:
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(checkpoint.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def checkpoint_import(store: Store, input_path: Path, target_thread_id: Optional[str] = None) -> str:
    """Import checkpoint from JSON file, creating new checkpoint."""
    data = json.loads(input_path.read_text(encoding="utf-8"))

    checkpoint = Checkpoint.from_dict(data)
    if target_thread_id:
        checkpoint.thread_id = target_thread_id

    return checkpoint_store(store, checkpoint)


def _row_to_checkpoint(row) -> Checkpoint:
    """Convert DB row to Checkpoint object."""
    return Checkpoint(
        thread_id=row[1],
        goal=row[2],
        phase=row[3],
        milestones=[CheckpointMilestone(**m) for m in json.loads(row[4])] if row[4] else [],
        blockers=[CheckpointBlocker(**b) for b in json.loads(row[5])] if row[5] else [],
        decisions=[CheckpointDecision(**d) for d in json.loads(row[6])] if row[6] else [],
        changed_files=json.loads(row[7]) if row[7] else [],
        commands_executed=json.loads(row[8]) if row[8] else [],
        tests_passed=json.loads(row[9]) if row[9] else [],
        branches_active=json.loads(row[10]) if row[10] else [],
        head_commit=row[11],
        next_actions=json.loads(row[12]) if row[12] else [],
        created_at=row[13],
        updated_at=row[14],
    )


# --- Integration with Store -----------------------------------------------

def init_checkpoint_schema(store: Store):
    """Initialize checkpoint schema if not present."""
    add_checkpoint_schema(store._con)


# --- Utility Functions ---------------------------------------------------

def get_latest_checkpoint(store: Store, thread_id: str) -> Optional[Checkpoint]:
    """Get the most recent checkpoint for a thread."""
    checkpoints = checkpoint_list(store, thread_id=thread_id, limit=1)
    return checkpoints[0][1] if checkpoints else None


def checkpoint_summary(checkpoint: Checkpoint) -> str:
    """Generate human-readable summary of a checkpoint."""
    lines = [
        f"Checkpoint for thread: {checkpoint.thread_id}",
        f"Goal: {checkpoint.goal}",
        f"Phase: {checkpoint.phase.upper()}",
        "",
        f"Milestones: {len(checkpoint.milestones)} total",
    ]

    completed = [m for m in checkpoint.milestones if m.status == "completed"]
    in_progress = [m for m in checkpoint.milestones if m.status == "in_progress"]
    pending = [m for m in checkpoint.milestones if m.status == "pending"]

    lines.extend([
        f"  ✓ Completed: {len(completed)}",
        f"  → In Progress: {len(in_progress)}",
        f"  ◦ Pending: {len(pending)}",
    ])

    if checkpoint.blockers:
        unresolved = [b for b in checkpoint.blockers if not b.resolved]
        lines.append(f"\nActive Blockers: {len(unresolved)}")
        for b in unresolved:
            lines.append(f"  [{b.severity.upper()}] {b.description}")

    if checkpoint.decisions:
        lines.append(f"\nDecisions Made: {len(checkpoint.decisions)}")

    if checkpoint.next_actions:
        lines.append(f"\nNext Actions:")
        for i, action in enumerate(checkpoint.next_actions[:5], 1):
            lines.append(f"  {i}. {action}")
        if len(checkpoint.next_actions) > 5:
            lines.append(f"  ... and {len(checkpoint.next_actions) - 5} more")

    if checkpoint.branches_active:
        lines.append(f"\nActive Branches: {', '.join(checkpoint.branches_active)}")

    return "\n".join(lines)
