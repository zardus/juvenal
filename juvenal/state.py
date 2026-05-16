"""Atomic JSON state persistence for pipeline resume."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from rich.console import Console
from rich.table import Table


@dataclass
class PhaseState:
    """State for a single phase."""

    phase_id: str
    status: str = "pending"  # pending, running, completed, failed
    attempt: int = 0
    failure_contexts: list[dict] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    started_at: float | None = None
    completed_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    baseline_sha: str | None = None  # git HEAD before first implement run


@dataclass
class PipelineState:
    """Complete pipeline state with atomic persistence."""

    state_file: Path
    phases: dict[str, PhaseState] = field(default_factory=dict)
    workflow_phase_ids: list[str] | None = None
    started_at: float | None = None
    completed_at: float | None = None
    active_workflow_yaml: str | None = None
    replan_history: list[dict] = field(default_factory=list)
    replan_count: int = 0
    phase_bounces: dict[str, int] = field(default_factory=dict)
    _lock: RLock = field(init=False, repr=False, default_factory=RLock)

    def record_bounce(self, phase_id: str) -> int:
        """Increment the persisted per-phase bounce count and return the new value.

        Persisted so --replan-after survives ctrl-c/restart: without this, a user who
        kills the process at bounce N-1 of --replan-after=N and resumes would silently
        restart the counter at 0 and never trigger a replan.
        """
        with self._lock:
            new_count = self.phase_bounces.get(phase_id, 0) + 1
            self.phase_bounces[phase_id] = new_count
            self.save()
            return new_count

    def record_replan(self, triggered_phase: str, old_workflow_yaml: str, new_workflow_yaml: str) -> None:
        """Persist a workflow swap: snapshot the previous workflow + phase records, then clear live phases.

        The new workflow gets a clean phase state slate — without this, a reused phase id from the
        old workflow whose status is "completed" would be silently skipped by the engine even though
        the new prompt is semantically different. The wiped phase records are preserved in
        replan_history for post-mortem and resume audit.
        """
        with self._lock:
            old_phases_snapshot = {
                pid: {
                    "status": ps.status,
                    "attempt": ps.attempt,
                    "failure_contexts": list(ps.failure_contexts),
                    "logs": list(ps.logs),
                    "started_at": ps.started_at,
                    "completed_at": ps.completed_at,
                    "input_tokens": ps.input_tokens,
                    "output_tokens": ps.output_tokens,
                    "baseline_sha": ps.baseline_sha,
                }
                for pid, ps in self.phases.items()
            }
            self.replan_count += 1
            self.replan_history.append(
                {
                    "cycle": self.replan_count,
                    "triggered_phase": triggered_phase,
                    "old_workflow_yaml": old_workflow_yaml,
                    "old_phases": old_phases_snapshot,
                    "timestamp": time.time(),
                }
            )
            self.active_workflow_yaml = new_workflow_yaml
            self.phases = {}
            self.workflow_phase_ids = None
            self.phase_bounces = {}
            self.save()

    def set_attempt(self, phase_id: str, attempt: int) -> None:
        with self._lock:
            ps = self._ensure_phase(phase_id)
            ps.attempt = attempt
            ps.status = "running"
            if ps.started_at is None:
                ps.started_at = time.time()
            self.save()

    def mark_completed(self, phase_id: str) -> None:
        with self._lock:
            ps = self._ensure_phase(phase_id)
            ps.status = "completed"
            ps.completed_at = time.time()
            self.save()

    def mark_failed(self, phase_id: str) -> None:
        with self._lock:
            ps = self._ensure_phase(phase_id)
            ps.status = "failed"
            ps.completed_at = time.time()
            self.save()

    def set_failure_context(self, phase_id: str, context: str, attempt: int | None = None) -> None:
        with self._lock:
            ps = self._ensure_phase(phase_id)
            entry: dict = {
                "context": context,
                "timestamp": time.time(),
            }
            if attempt is not None:
                entry["attempt"] = attempt
            ps.failure_contexts.append(entry)
            self.save()

    def get_failure_context(self, phase_id: str) -> str:
        """Return the most recent failure context, or empty string."""
        ps = self.phases.get(phase_id)
        if ps and ps.failure_contexts:
            return ps.failure_contexts[-1]["context"]
        return ""

    def log_step(
        self, phase_id: str, attempt: int, step: str, output: str, input: str = "", transcript: str = ""
    ) -> None:
        with self._lock:
            ps = self._ensure_phase(phase_id)
            entry: dict = {
                "attempt": attempt,
                "step": step,
                "output": output,
                "timestamp": time.time(),
            }
            if input:
                entry["input"] = input
            if transcript:
                entry["transcript"] = transcript
            ps.logs.append(entry)
            self.save()

    def add_tokens(self, phase_id: str, input_tokens: int, output_tokens: int) -> None:
        """Accumulate token usage for a phase."""
        with self._lock:
            ps = self._ensure_phase(phase_id)
            ps.input_tokens += input_tokens
            ps.output_tokens += output_tokens
            self.save()

    def total_tokens(self) -> tuple[int, int]:
        """Return cumulative (input_tokens, output_tokens) across the live workflow AND
        every prior replan cycle. Without summing replan_history.old_phases too, cost
        reporting would under-count by exactly the work that motivated the replans."""
        inp = sum(ps.input_tokens for ps in self.phases.values())
        out = sum(ps.output_tokens for ps in self.phases.values())
        for entry in self.replan_history:
            old_phases = entry.get("old_phases") or {}
            for snap in old_phases.values():
                inp += snap.get("input_tokens", 0) or 0
                out += snap.get("output_tokens", 0) or 0
        return inp, out

    def invalidate_from(self, phase_id: str, scope: set[str] | None = None) -> None:
        """Invalidate this phase and all subsequent phases (for bounce targets).

        Preserves attempt count, baseline_sha (cumulative across bounces),
        failure_contexts (append-only history, new context set separately after
        invalidation by the engine loop).

        If scope is provided, only invalidate phases whose ID is in the scope set.
        This prevents lane A's bounce from clobbering lane B's state.
        """
        with self._lock:
            found = False
            for pid, ps in self.phases.items():
                if pid == phase_id:
                    found = True
                if found:
                    if scope is not None and pid not in scope:
                        continue
                    ps.status = "pending"
                    ps.started_at = None
                    ps.completed_at = None
            self.save()

    def get_resume_phase_index(self, phases: list) -> int:
        """Find the first non-completed phase index for resuming."""
        for i, phase in enumerate(phases):
            ps = self.phases.get(phase.id)
            if ps is None or ps.status != "completed":
                return i
        return len(phases)

    def save(self) -> None:
        """Atomic save: write to tmp, fsync, rename.

        Thread-safe: acquires _lock if not already held (RLock is reentrant,
        so callers that already hold the lock can call save() safely).
        """
        with self._lock:
            data = self._to_dict()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_file.with_name(f"{self.state_file.name}.tmp")
            payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_file)

    @classmethod
    def load(cls, state_file: str | Path | None) -> PipelineState:
        """Load state from file, or return empty state."""
        if state_file is None:
            state_file = Path(".juvenal-state.json")
        state_file = Path(state_file)
        state = cls(state_file=state_file)
        if state_file.exists():
            data = json.loads(state_file.read_text())
            state.started_at = data.get("started_at")
            state.completed_at = data.get("completed_at")
            state.active_workflow_yaml = data.get("active_workflow_yaml")
            state.replan_history = data.get("replan_history", []) or []
            state.replan_count = data.get("replan_count", 0) or 0
            state.phase_bounces = dict(data.get("phase_bounces") or {})
            raw_phases = data.get("phases", {})
            raw_workflow_phase_ids = data.get("workflow_phase_ids", data.get("phase_order"))
            ordered_phase_ids: list[str]
            if isinstance(raw_workflow_phase_ids, list):
                ordered_phase_ids = [pid for pid in raw_workflow_phase_ids if pid in raw_phases]
                ordered_phase_ids.extend(pid for pid in raw_phases if pid not in ordered_phase_ids)
                state.workflow_phase_ids = list(raw_workflow_phase_ids)
            else:
                ordered_phase_ids = list(raw_phases.keys())
                state.workflow_phase_ids = None

            for pid in ordered_phase_ids:
                pdata = raw_phases[pid]
                # Backwards compat: migrate scalar failure_context to list
                fc_raw = pdata.get("failure_contexts", [])
                if not fc_raw and pdata.get("failure_context"):
                    fc_raw = [{"context": pdata["failure_context"], "timestamp": 0}]
                state.phases[pid] = PhaseState(
                    phase_id=pid,
                    status=pdata.get("status", "pending"),
                    attempt=pdata.get("attempt", 0),
                    failure_contexts=fc_raw,
                    logs=pdata.get("logs", []),
                    started_at=pdata.get("started_at"),
                    completed_at=pdata.get("completed_at"),
                    input_tokens=pdata.get("input_tokens", 0),
                    output_tokens=pdata.get("output_tokens", 0),
                    baseline_sha=pdata.get("baseline_sha"),
                )
        return state

    def print_status(self) -> None:
        """Print a Rich-formatted status table."""
        console = Console()
        title = "Juvenal Pipeline Status"
        if self.replan_count:
            title += f" (replans: {self.replan_count})"
        table = Table(title=title)
        table.add_column("Phase", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Attempts", justify="right")
        table.add_column("Duration", justify="right")

        for pid, ps in self.phases.items():
            status_style = {"completed": "green", "running": "yellow", "failed": "red", "pending": "dim"}.get(
                ps.status, "dim"
            )
            duration = ""
            if ps.started_at and ps.completed_at:
                dur = ps.completed_at - ps.started_at
                duration = f"{dur:.1f}s"
            elif ps.started_at:
                dur = time.time() - ps.started_at
                duration = f"{dur:.1f}s (running)"
            table.add_row(pid, f"[{status_style}]{ps.status}[/]", str(ps.attempt), duration)

        console.print(table)

        if self.replan_history:
            console.print()
            history_table = Table(title="Replan history")
            history_table.add_column("Cycle", justify="right")
            history_table.add_column("Triggered by", style="cyan")
            history_table.add_column("Timestamp", style="dim")
            for entry in self.replan_history:
                ts = entry.get("timestamp")
                ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
                history_table.add_row(str(entry.get("cycle", "")), entry.get("triggered_phase", ""), ts_str)
            console.print(history_table)

    def _ensure_phase(self, phase_id: str) -> PhaseState:
        if phase_id not in self.phases:
            self.phases[phase_id] = PhaseState(phase_id=phase_id)
        return self.phases[phase_id]

    def _to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "workflow_phase_ids": self.workflow_phase_ids,
            "active_workflow_yaml": self.active_workflow_yaml,
            "replan_history": self.replan_history,
            "replan_count": self.replan_count,
            "phase_bounces": self.phase_bounces,
            "phases": {
                pid: {
                    "status": ps.status,
                    "attempt": ps.attempt,
                    "failure_contexts": ps.failure_contexts,
                    "logs": ps.logs,
                    "started_at": ps.started_at,
                    "completed_at": ps.completed_at,
                    "input_tokens": ps.input_tokens,
                    "output_tokens": ps.output_tokens,
                    "baseline_sha": ps.baseline_sha,
                }
                for pid, ps in self.phases.items()
            },
        }
