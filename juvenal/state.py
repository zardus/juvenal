"""Atomic JSON state persistence for pipeline resume."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table


@dataclass
class PhaseState:
    """State for a single phase."""

    phase_id: str
    status: str = "pending"  # pending, running, completed, failed
    attempt: int = 0
    failure_context: str = ""
    logs: list[dict] = field(default_factory=list)
    started_at: float | None = None
    completed_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class PipelineState:
    """Complete pipeline state with atomic persistence."""

    state_file: Path
    phases: dict[str, PhaseState] = field(default_factory=dict)
    started_at: float | None = None
    completed_at: float | None = None

    def set_attempt(self, phase_id: str, attempt: int) -> None:
        ps = self._ensure_phase(phase_id)
        ps.attempt = attempt
        ps.status = "running"
        if ps.started_at is None:
            ps.started_at = time.time()
        self.save()

    def mark_completed(self, phase_id: str) -> None:
        ps = self._ensure_phase(phase_id)
        ps.status = "completed"
        ps.completed_at = time.time()
        self.save()

    def mark_failed(self, phase_id: str) -> None:
        ps = self._ensure_phase(phase_id)
        ps.status = "failed"
        ps.completed_at = time.time()
        self.save()

    def set_failure_context(self, phase_id: str, context: str) -> None:
        ps = self._ensure_phase(phase_id)
        ps.failure_context = context
        self.save()

    def get_failure_context(self, phase_id: str) -> str:
        ps = self.phases.get(phase_id)
        return ps.failure_context if ps else ""

    def log_step(self, phase_id: str, attempt: int, step: str, output: str) -> None:
        ps = self._ensure_phase(phase_id)
        ps.logs.append(
            {
                "attempt": attempt,
                "step": step,
                "output": output[-5000:],  # truncate to prevent state bloat
                "timestamp": time.time(),
            }
        )
        self.save()

    def add_tokens(self, phase_id: str, input_tokens: int, output_tokens: int) -> None:
        """Accumulate token usage for a phase."""
        ps = self._ensure_phase(phase_id)
        ps.input_tokens += input_tokens
        ps.output_tokens += output_tokens
        self.save()

    def total_tokens(self) -> tuple[int, int]:
        """Return (total_input_tokens, total_output_tokens) across all phases."""
        inp = sum(ps.input_tokens for ps in self.phases.values())
        out = sum(ps.output_tokens for ps in self.phases.values())
        return inp, out

    def invalidate_from(self, phase_id: str) -> None:
        """Invalidate this phase and all subsequent phases (for bounce targets)."""
        found = False
        for pid, ps in self.phases.items():
            if pid == phase_id:
                found = True
            if found:
                ps.status = "pending"
                ps.attempt = 0
                ps.failure_context = ""
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
        """Atomic save: write to tmp, fsync, rename."""
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
            for pid, pdata in data.get("phases", {}).items():
                state.phases[pid] = PhaseState(
                    phase_id=pid,
                    status=pdata.get("status", "pending"),
                    attempt=pdata.get("attempt", 0),
                    failure_context=pdata.get("failure_context", ""),
                    logs=pdata.get("logs", []),
                    started_at=pdata.get("started_at"),
                    completed_at=pdata.get("completed_at"),
                    input_tokens=pdata.get("input_tokens", 0),
                    output_tokens=pdata.get("output_tokens", 0),
                )
        return state

    def print_status(self) -> None:
        """Print a Rich-formatted status table."""
        console = Console()
        table = Table(title="Juvenal Pipeline Status")
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

    def _ensure_phase(self, phase_id: str) -> PhaseState:
        if phase_id not in self.phases:
            self.phases[phase_id] = PhaseState(phase_id=phase_id)
        return self.phases[phase_id]

    def _to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "phases": {
                pid: {
                    "status": ps.status,
                    "attempt": ps.attempt,
                    "failure_context": ps.failure_context,
                    "logs": ps.logs,
                    "started_at": ps.started_at,
                    "completed_at": ps.completed_at,
                    "input_tokens": ps.input_tokens,
                    "output_tokens": ps.output_tokens,
                }
                for pid, ps in self.phases.items()
            },
        }
