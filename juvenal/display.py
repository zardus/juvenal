"""Rich Live terminal display with rolling buffer."""

from __future__ import annotations

import sys
import time
from collections import deque

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.rule import Rule
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def _elapsed(start: float) -> str:
    """Human-readable elapsed time."""
    secs = int(time.time() - start)
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    return f"{mins}m{secs}s"


class Display:
    """Terminal display for pipeline progress."""

    def __init__(self, buffer_size: int = 15, plain: bool = False):
        self._buffer_size = buffer_size
        self._live_lines: deque[str] = deque(maxlen=buffer_size)
        self._live_ctx = None
        self._live_obj = None
        self._worker_name = ""
        self._worker_start = 0.0
        self._plain = plain
        self._console = Console() if (RICH_AVAILABLE and not plain) else None
        self._use_live = RICH_AVAILABLE and not plain and sys.stdout.isatty()

    def phase_start(self, phase_id: str, attempt: int) -> None:
        """Announce phase start."""
        if self._console:
            self._console.rule(f"Phase \\[{phase_id}] attempt {attempt}", style="bold blue")
        else:
            print(f"=== Phase [{phase_id}] attempt {attempt} ===", flush=True)

    def step_start(self, step_name: str) -> None:
        """Start a new step (implement or checker)."""
        self._worker_name = step_name
        self._worker_start = time.time()
        self._live_lines.clear()
        if self._use_live:
            self._live_obj = Live(
                self._build_renderable(),
                console=self._console,
                refresh_per_second=8,
                transient=True,
            )
            self._live_obj.start()
        else:
            print(f"  > {step_name}...", flush=True)

    def step_pass(self, step_name: str) -> None:
        """Mark step as passed."""
        self._stop_live()
        elapsed = _elapsed(self._worker_start)
        if self._console:
            self._console.print(f"  [green]PASS[/green] {step_name} ({elapsed})")
        else:
            print(f"  PASS {step_name} ({elapsed})", flush=True)

    def step_fail(self, step_name: str, reason: str) -> None:
        """Mark step as failed."""
        self._stop_live()
        elapsed = _elapsed(self._worker_start)
        safe_reason = reason.replace("[", "\\[") if self._console else reason
        if self._console:
            self._console.print(f"  [red]FAIL[/red] {step_name} ({elapsed}): {safe_reason}")
        else:
            print(f"  FAIL {step_name} ({elapsed}): {reason}", flush=True)

    def pipeline_done(self, success: bool) -> None:
        """Announce pipeline completion."""
        if success:
            if self._console:
                self._console.print("\n[bold green]Pipeline completed successfully.[/bold green]")
            else:
                print("\nPipeline completed successfully.", flush=True)
        else:
            if self._console:
                self._console.print("\n[bold red]Pipeline failed.[/bold red]")
            else:
                print("\nPipeline failed.", flush=True)

    def run_summary(self, state, total_bounces: int) -> None:
        """Print a post-run summary with timing and bounce stats."""
        if self._console and RICH_AVAILABLE:
            self._run_summary_rich(state, total_bounces)
        else:
            self._run_summary_plain(state, total_bounces)

    def backoff_wait(self, delay: float) -> None:
        """Announce backoff wait."""
        if self._console:
            self._console.print(f"  [yellow]Backoff: waiting {delay:.1f}s before retry...[/yellow]")
        else:
            print(f"  Backoff: waiting {delay:.1f}s before retry...", flush=True)

    def notify_failed(self, url: str) -> None:
        """Announce a failed notification."""
        if self._console:
            self._console.print(f"  [red]Notification failed:[/red] {url}")
        else:
            print(f"  Notification failed: {url}", flush=True)

    def _run_summary_rich(self, state, total_bounces: int) -> None:
        from rich.table import Table

        table = Table(title="Run Summary")
        table.add_column("Phase", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Attempts", justify="right")
        table.add_column("Duration", justify="right")
        table.add_column("Tokens (in/out)", justify="right")

        for pid, ps in state.phases.items():
            status_style = {"completed": "green", "running": "yellow", "failed": "red", "pending": "dim"}.get(
                ps.status, "dim"
            )
            duration = ""
            if ps.started_at and ps.completed_at:
                dur = ps.completed_at - ps.started_at
                duration = f"{dur:.1f}s"
            tokens = ""
            if ps.input_tokens or ps.output_tokens:
                tokens = f"{ps.input_tokens}/{ps.output_tokens}"
            table.add_row(pid, f"[{status_style}]{ps.status}[/]", str(ps.attempt), duration, tokens)

        self._console.print(table)

        # Overall stats
        total_duration = ""
        if state.started_at and state.completed_at:
            dur = state.completed_at - state.started_at
            total_duration = f"{dur:.1f}s"
        total_inp, total_out = state.total_tokens()
        self._console.print(f"  Total time: {total_duration}")
        self._console.print(f"  Total bounces: {total_bounces}")
        if total_inp or total_out:
            self._console.print(f"  Total tokens: {total_inp} in, {total_out} out")

    def _run_summary_plain(self, state, total_bounces: int) -> None:
        print("\n--- Run Summary ---", flush=True)
        for pid, ps in state.phases.items():
            duration = ""
            if ps.started_at and ps.completed_at:
                dur = ps.completed_at - ps.started_at
                duration = f" ({dur:.1f}s)"
            tokens = ""
            if ps.input_tokens or ps.output_tokens:
                tokens = f" [tokens: {ps.input_tokens} in, {ps.output_tokens} out]"
            print(f"  {pid}: {ps.status} (attempts: {ps.attempt}){duration}{tokens}", flush=True)
        if state.started_at and state.completed_at:
            total = state.completed_at - state.started_at
            print(f"  Total time: {total:.1f}s", flush=True)
        print(f"  Total bounces: {total_bounces}", flush=True)
        total_inp, total_out = state.total_tokens()
        if total_inp or total_out:
            print(f"  Total tokens: {total_inp} in, {total_out} out", flush=True)

    def live_update(self, line: str) -> None:
        """Feed a line of output to the live display."""
        self._live_lines.append(line)
        if self._live_obj:
            self._live_obj.update(self._build_renderable())
        elif self._plain:
            print(f"    {line}", flush=True)

    def _build_renderable(self):
        """Build Rich renderable for the live display."""
        title = f"{self._worker_name} | elapsed {_elapsed(self._worker_start)} | latest {self._buffer_size} lines"
        lines = list(self._live_lines)
        if not lines:
            lines = ["(waiting for agent events...)"]
        # Pad to buffer_size to avoid flickering
        while len(lines) < self._buffer_size:
            lines.append("")
        return Group(
            Rule(title=title, style="dim"),
            Text("\n".join(lines)),
        )

    def _stop_live(self) -> None:
        """Stop the live display if active."""
        if self._live_obj:
            self._live_obj.stop()
            self._live_obj = None
