"""Core non-agentic execution loop."""

from __future__ import annotations

import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from juvenal.backends import create_backend
from juvenal.checkers import parse_verdict, run_script
from juvenal.display import Display
from juvenal.notifications import build_notification_payload, send_webhook
from juvenal.state import PipelineState
from juvenal.workflow import Phase, Workflow


@dataclass
class PhaseResult:
    """Result of executing a single phase."""

    success: bool
    bounce_target: str | None = None


class PipelineExhausted(Exception):
    """Raised when the pipeline exhausts the global bounce limit."""

    def __init__(self, phase_id: str):
        self.phase_id = phase_id
        super().__init__(f"Pipeline exhausted bounce limit at phase '{phase_id}'")


class Engine:
    """Non-agentic, deterministic execution loop.

    This engine deliberately does NOT use an LLM to decide flow control.
    All decisions (retry, bounce, advance) are made programmatically.
    """

    def __init__(
        self,
        workflow: Workflow,
        resume: bool = False,
        rewind: int | None = None,
        rewind_to: str | None = None,
        start_phase: str | None = None,
        dry_run: bool = False,
        state_file: str | None = None,
        plain: bool = False,
    ):
        self.workflow = workflow
        self.backend = create_backend(workflow.backend)
        self.display = Display(plain=plain)
        self.dry_run = dry_run

        sf = state_file or ".juvenal-state.json"
        needs_state = resume or rewind is not None or rewind_to is not None
        self.state = PipelineState.load(sf) if needs_state else PipelineState(state_file=Path(sf))

        # Determine starting phase index
        if rewind_to is not None:
            self._start_idx = self._find_phase_index(rewind_to)
            self.state.invalidate_from(rewind_to)
        elif rewind is not None:
            resume_idx = self.state.get_resume_phase_index(self.workflow.phases)
            self._start_idx = max(0, resume_idx - rewind)
            self.state.invalidate_from(self.workflow.phases[self._start_idx].id)
        elif start_phase:
            self._start_idx = self._find_phase_index(start_phase)
        elif resume:
            self._start_idx = self.state.get_resume_phase_index(self.workflow.phases)
        else:
            self._start_idx = 0

    def run(self) -> int:
        """Execute the pipeline. Returns 0 on success, 1 on failure."""
        if self.dry_run:
            return self._dry_run()

        self.state.started_at = time.time()
        phases = self.workflow.phases
        phase_idx = self._start_idx
        bounces = 0

        try:
            while phase_idx < len(phases):
                phase = phases[phase_idx]

                # Check for parallel group
                pg = self._get_parallel_group(phase.id)
                if pg and phase.id == pg[0]:
                    result = self._run_parallel_group(pg)
                    if result.bounce_target:
                        bounces += 1
                        if bounces >= self.workflow.max_bounces:
                            raise PipelineExhausted(phase.id)
                        self._apply_backoff(bounces)
                        self.state.invalidate_from(result.bounce_target)
                        phase_idx = self._find_phase_index(result.bounce_target)
                        continue
                    if not result.success:
                        raise PipelineExhausted(phase.id)
                    # Skip past all phases in the group
                    last_pg_phase = pg[-1]
                    phase_idx = self._find_phase_index(last_pg_phase) + 1
                    continue

                if phase.type == "implement":
                    result = self._run_implement(phase)
                elif phase.type == "script":
                    result = self._run_script(phase, phases, phase_idx)
                elif phase.type == "check":
                    result = self._run_check(phase, phases, phase_idx)
                else:
                    raise ValueError(f"Unknown phase type: {phase.type!r}")

                if result.success:
                    self.state.mark_completed(phase.id)
                    phase_idx += 1
                elif result.bounce_target:
                    bounces += 1
                    if bounces >= self.workflow.max_bounces:
                        raise PipelineExhausted(phase.id)
                    self._apply_backoff(bounces)
                    self.state.invalidate_from(result.bounce_target)
                    phase_idx = self._find_phase_index(result.bounce_target)
                else:
                    raise PipelineExhausted(phase.id)

            self.state.completed_at = time.time()
            self.state.save()
            self.display.pipeline_done(True)
            self.display.run_summary(self.state, bounces)
            self._send_notifications(True, bounces)
            return 0

        except PipelineExhausted as e:
            self.state.mark_failed(e.phase_id)
            self.state.completed_at = time.time()
            self.state.save()
            self.display.pipeline_done(False)
            self.display.run_summary(self.state, bounces)
            self._send_notifications(False, bounces)
            return 1

    def _run_implement(self, phase: Phase) -> PhaseResult:
        """Run an implement phase once. On crash, return a bounce target."""
        failure_context = self.state.get_failure_context(phase.id)

        ps = self.state.phases.get(phase.id)
        attempt = (ps.attempt if ps and ps.attempt > 0 else 0) + 1
        self.state.set_attempt(phase.id, attempt)
        self.display.phase_start(phase.id, attempt)

        prompt = phase.render_prompt(failure_context=failure_context)
        self.display.step_start("implement")
        result = self.backend.run_agent(
            prompt,
            working_dir=self.workflow.working_dir,
            display_callback=self.display.live_update,
            timeout=phase.timeout,
            env=phase.env or None,
        )
        self.state.log_step(phase.id, attempt, "implement", result.output)
        self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

        if result.exit_code != 0:
            failure_context = f"Implementation agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
            self.display.step_fail("implement", failure_context[:500])
            # Bounce to explicit target, or back to self
            bounce_target = phase.bounce_target or phase.id
            self.state.set_failure_context(bounce_target, failure_context)
            return PhaseResult(success=False, bounce_target=bounce_target)

        self.display.step_pass("implement")
        return PhaseResult(success=True)

    def _run_script(self, phase: Phase, phases: list[Phase], phase_idx: int) -> PhaseResult:
        """Run a script phase. Exit 0 = advance. Nonzero = bounce back."""
        self.display.phase_start(phase.id, 1)
        self.display.step_start(f"script: {phase.id}")

        timeout = phase.timeout or 600
        result = run_script(phase.run, self.workflow.working_dir, timeout=timeout, env=phase.env or None)
        self.state.log_step(phase.id, 1, "script", result.output)

        if result.exit_code == 0:
            self.display.step_pass(phase.id)
            return PhaseResult(success=True)

        # Failure — resolve bounce target
        failure_context = f"Script '{phase.run}' failed (exit {result.exit_code}).\nOutput:\n{result.output[-3000:]}"
        self.display.step_fail(phase.id, failure_context[:500])

        target_id = self._resolve_bounce_target(phase, phases, phase_idx)
        if target_id:
            self.state.set_failure_context(target_id, failure_context)
            return PhaseResult(success=False, bounce_target=target_id)
        return PhaseResult(success=False)

    def _run_check(self, phase: Phase, phases: list[Phase], phase_idx: int) -> PhaseResult:
        """Run a check phase. PASS = advance. FAIL = bounce back."""
        self.display.phase_start(phase.id, 1)
        self.display.step_start(f"check: {phase.id}")

        prompt = phase.render_check_prompt()
        result = self.backend.run_agent(
            prompt,
            working_dir=self.workflow.working_dir,
            display_callback=self.display.live_update,
            timeout=phase.timeout,
            env=phase.env or None,
        )
        self.state.log_step(phase.id, 1, "check", result.output)
        self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

        if result.exit_code != 0:
            failure_context = f"Checker agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
            self.display.step_fail(phase.id, failure_context[:500])
            target_id = self._resolve_bounce_target(phase, phases, phase_idx)
            if target_id:
                self.state.set_failure_context(target_id, failure_context)
                return PhaseResult(success=False, bounce_target=target_id)
            return PhaseResult(success=False)

        passed, reason, agent_target = parse_verdict(result.output)
        if passed:
            self.display.step_pass(phase.id)
            return PhaseResult(success=True)

        # FAIL — resolve bounce target
        failure_context = f"{phase.id}: {reason}\nFull output (last 3000 chars):\n{result.output[-3000:]}"
        self.display.step_fail(phase.id, reason)

        target_id = self._resolve_bounce_target(phase, phases, phase_idx, agent_target)
        if target_id:
            self.state.set_failure_context(target_id, failure_context)
            return PhaseResult(success=False, bounce_target=target_id)
        return PhaseResult(success=False)

    def _resolve_bounce_target(
        self, phase: Phase, phases: list[Phase], phase_idx: int, agent_target: str | None = None
    ) -> str | None:
        """Resolve which phase to bounce to on failure.

        Priority:
        1. If phase has bounce_targets (agent-guided), use the agent's choice if valid,
           otherwise fall back to first in the list.
        2. If phase has bounce_target (fixed), use that.
        3. Otherwise, find the most recent implement phase.
        """
        if phase.bounce_targets:
            if agent_target and agent_target in phase.bounce_targets:
                return agent_target
            return phase.bounce_targets[0]
        if phase.bounce_target:
            return phase.bounce_target
        return self._find_last_implement(phases, phase_idx)

    def _find_last_implement(self, phases: list[Phase], before_idx: int) -> str | None:
        """Find the most recent implement phase before the given index."""
        for i in range(before_idx - 1, -1, -1):
            if phases[i].type == "implement":
                return phases[i].id
        return None

    def _run_parallel_group(self, phase_ids: list[str]) -> PhaseResult:
        """Run a group of phases in parallel."""
        phases_map = {p.id: p for p in self.workflow.phases}
        results: dict[str, PhaseResult] = {}

        with ThreadPoolExecutor(max_workers=len(phase_ids)) as pool:
            futures = {pool.submit(self._run_implement, phases_map[pid]): pid for pid in phase_ids}
            for future in as_completed(futures):
                pid = futures[future]
                result = future.result()
                results[pid] = result
                if result.success:
                    self.state.mark_completed(pid)
                if result.bounce_target:
                    # Any bounce aborts the group
                    return PhaseResult(success=False, bounce_target=result.bounce_target)

        if all(r.success for r in results.values()):
            return PhaseResult(success=True)
        return PhaseResult(success=False)

    def _get_parallel_group(self, phase_id: str) -> list[str] | None:
        """Check if a phase is the start of a parallel group."""
        for group in self.workflow.parallel_groups:
            if phase_id in group:
                return group
        return None

    def _find_phase_index(self, phase_id: str) -> int:
        """Find the index of a phase by ID."""
        for i, p in enumerate(self.workflow.phases):
            if p.id == phase_id:
                return i
        raise ValueError(f"Phase not found: {phase_id!r}")

    def _apply_backoff(self, bounces: int) -> None:
        """Apply exponential backoff delay between bounces."""
        base = self.workflow.backoff
        if base <= 0:
            return
        delay = min(base * (2 ** (bounces - 1)), self.workflow.max_backoff)
        self.display.backoff_wait(delay)
        time.sleep(delay)

    def _send_notifications(self, success: bool, bounces: int) -> None:
        """Send webhook notifications if configured."""
        if not self.workflow.notify:
            return
        duration = None
        if self.state.started_at and self.state.completed_at:
            duration = self.state.completed_at - self.state.started_at
        total_inp, total_out = self.state.total_tokens()
        phase_summaries = []
        for pid, ps in self.state.phases.items():
            phase_summaries.append(
                {
                    "id": pid,
                    "status": ps.status,
                    "attempts": ps.attempt,
                    "input_tokens": ps.input_tokens,
                    "output_tokens": ps.output_tokens,
                }
            )
        payload = build_notification_payload(
            workflow_name=self.workflow.name,
            success=success,
            total_bounces=bounces,
            duration=duration,
            total_input_tokens=total_inp,
            total_output_tokens=total_out,
            phase_summaries=phase_summaries,
        )
        for url in self.workflow.notify:
            ok = send_webhook(url, payload)
            if not ok:
                self.display.notify_failed(url)

    def _dry_run(self) -> int:
        """Print what would be done without executing."""
        from juvenal.workflow import validate_workflow

        print(f"Workflow: {self.workflow.name}")
        print(f"Backend: {self.workflow.backend}")
        print(f"Working dir: {self.workflow.working_dir}")
        print(f"Max bounces: {self.workflow.max_bounces}")
        if self.workflow.backoff > 0:
            print(f"Backoff: {self.workflow.backoff}s base, {self.workflow.max_backoff}s max")
        if self.workflow.notify:
            print(f"Notifications: {len(self.workflow.notify)} webhook(s)")
        print()

        # Validation
        errors = validate_workflow(self.workflow)
        if errors:
            print(f"Validation: {len(errors)} error(s)")
            for err in errors:
                print(f"  - {err}")
        else:
            print(f"Validation: OK ({len(self.workflow.phases)} phases)")
        print()

        # Phase type summary
        type_counts: dict[str, int] = {}
        for phase in self.workflow.phases:
            type_counts[phase.type] = type_counts.get(phase.type, 0) + 1
        print("Phase summary:")
        for ptype, count in sorted(type_counts.items()):
            print(f"  {ptype}: {count}")
        print()

        # Execution plan
        print("Execution plan:")
        for i, phase in enumerate(self.workflow.phases):
            prefix = f"  {i + 1}."
            extras = []
            if phase.timeout:
                extras.append(f"timeout={phase.timeout}s")
            if phase.env:
                extras.append(f"env={list(phase.env.keys())}")
            if phase.bounce_target:
                extras.append(f"bounce->{phase.bounce_target}")
            if phase.bounce_targets:
                extras.append(f"bounce->{phase.bounce_targets}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            if phase.type == "implement":
                prompt_preview = phase.prompt[:80].replace("\n", " ")
                print(f"{prefix} [{phase.type}] {phase.id}{extra_str}")
                print(f"     prompt: {prompt_preview}...")
            elif phase.type == "script":
                print(f"{prefix} [{phase.type}] {phase.id}: {phase.run}{extra_str}")
            elif phase.type == "check":
                target = phase.role or phase.prompt[:60].replace("\n", " ")
                print(f"{prefix} [{phase.type}] {phase.id}: {target}{extra_str}")
            print()

        if self.workflow.parallel_groups:
            print("Parallel groups:")
            for group in self.workflow.parallel_groups:
                print(f"  {group}")
        return 0


def plan_workflow(goal: str, output_path: str, backend_name: str = "codex", plain: bool = False) -> None:
    """Generate a workflow YAML from a goal description using a multi-phase pipeline."""
    import yaml as _yaml

    from juvenal.workflow import load_workflow

    # Create temp working dir with .plan/ structure
    tmp_dir = tempfile.mkdtemp(prefix="juvenal-plan-")
    tmp_path = Path(tmp_dir)
    plan_dir = tmp_path / ".plan"
    plan_dir.mkdir()
    (plan_dir / "goal.md").write_text(goal)

    try:
        # Load the canned planning workflow
        plan_yaml = Path(__file__).parent / "workflows" / "plan.yaml"
        workflow = load_workflow(plan_yaml)
        workflow.backend = backend_name
        workflow.working_dir = tmp_dir

        # Run through the engine
        engine = Engine(workflow, state_file=str(tmp_path / ".juvenal-state.json"), plain=plain)
        exit_code = engine.run()

        if exit_code != 0:
            print(f"Planning failed. Working directory preserved at: {tmp_dir}")
            raise SystemExit(1)

        # Validate and copy the produced workflow.yaml
        produced = tmp_path / "workflow.yaml"
        if not produced.exists():
            print(f"Planning failed: no workflow.yaml produced. Working directory: {tmp_dir}")
            raise SystemExit(1)

        yaml_content = produced.read_text()
        parsed = _yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict) or "phases" not in parsed:
            print(f"Planning produced invalid YAML. Working directory: {tmp_dir}")
            raise SystemExit(1)

        Path(output_path).write_text(yaml_content)
        print(f"Workflow written to {output_path}")

        # Clean up on success
        shutil.rmtree(tmp_dir)
    except SystemExit:
        raise
    except Exception:
        print(f"Planning failed. Working directory preserved at: {tmp_dir}")
        raise


def _extract_yaml(text: str) -> str:
    """Extract YAML content from LLM output, stripping markdown fences."""
    # Try ```yaml ... ``` first
    if "```yaml" in text:
        return text.split("```yaml", 1)[1].split("```", 1)[0]
    # Try ``` ... ``` (first fenced block)
    if "```" in text:
        after_fence = text.split("```", 1)[1]
        if "```" in after_fence:
            return after_fence.split("```", 1)[0]
    # No fences — return as-is, stripping leading non-YAML prose
    # YAML mappings start with a key like "name:" or "phases:"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            return "\n".join(lines[i:])
    return text
