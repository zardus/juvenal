"""Core non-agentic execution loop."""

from __future__ import annotations

import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from juvenal.backends import create_backend
from juvenal.checkers import run_checker
from juvenal.display import Display
from juvenal.state import PipelineState
from juvenal.workflow import Phase, Workflow


@dataclass
class PhaseResult:
    """Result of executing a single phase."""

    success: bool
    bounce_target: str | None = None


class PipelineExhausted(Exception):
    """Raised when a phase exhausts all retries with no bounce target."""

    def __init__(self, phase_id: str):
        self.phase_id = phase_id
        super().__init__(f"Phase '{phase_id}' exhausted all retries")


class Engine:
    """Non-agentic, deterministic execution loop.

    This engine deliberately does NOT use an LLM to decide flow control.
    All decisions (retry, bounce, advance) are made programmatically.
    """

    def __init__(
        self,
        workflow: Workflow,
        resume: bool = False,
        start_phase: str | None = None,
        dry_run: bool = False,
        state_file: str | None = None,
    ):
        self.workflow = workflow
        self.backend = create_backend(workflow.backend)
        self.display = Display()
        self.dry_run = dry_run

        sf = state_file or ".juvenal-state.json"
        self.state = PipelineState.load(sf) if resume else PipelineState(state_file=Path(sf))

        # Determine starting phase index
        if start_phase:
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

        try:
            while phase_idx < len(phases):
                phase = phases[phase_idx]

                # Check for parallel group
                pg = self._get_parallel_group(phase.id)
                if pg and phase.id == pg[0]:
                    result = self._run_parallel_group(pg)
                    if result.bounce_target:
                        phase_idx = self._find_phase_index(result.bounce_target)
                        continue
                    if not result.success:
                        raise PipelineExhausted(phase.id)
                    # Skip past all phases in the group
                    last_pg_phase = pg[-1]
                    phase_idx = self._find_phase_index(last_pg_phase) + 1
                    continue

                result = self._run_phase(phase)

                if result.success:
                    self.state.mark_completed(phase.id)
                    phase_idx += 1
                elif result.bounce_target:
                    self.state.invalidate_from(result.bounce_target)
                    phase_idx = self._find_phase_index(result.bounce_target)
                else:
                    raise PipelineExhausted(phase.id)

            self.state.completed_at = time.time()
            self.state.save()
            self.display.pipeline_done(True)
            return 0

        except PipelineExhausted as e:
            self.state.mark_failed(e.phase_id)
            self.state.completed_at = time.time()
            self.state.save()
            self.display.pipeline_done(False)
            return 1

    def _run_phase(self, phase: Phase) -> PhaseResult:
        """Run a single phase: implement + check loop."""
        failure_context = self.state.get_failure_context(phase.id)

        for attempt in range(1, self.workflow.max_retries + 1):
            self.state.set_attempt(phase.id, attempt)
            self.display.phase_start(phase.id, attempt)

            # Step 1: Implementation
            prompt = phase.render_prompt(failure_context=failure_context)
            self.display.step_start("implement")
            result = self.backend.run_agent(
                prompt,
                working_dir=self.workflow.working_dir,
                display_callback=self.display.live_update,
            )
            self.state.log_step(phase.id, attempt, "implement", result.output)

            if result.exit_code != 0:
                failure_context = f"Implementation agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
                self.display.step_fail("implement", failure_context[:500])
                continue

            self.display.step_pass("implement")

            # Step 2: Run all checkers sequentially
            all_passed = True
            for checker in phase.checkers:
                self.display.step_start(f"check: {checker.name}")
                check_result = run_checker(
                    checker,
                    self.backend,
                    self.workflow.working_dir,
                    display_callback=self.display.live_update,
                )
                self.state.log_step(phase.id, attempt, checker.name, check_result.output)

                if check_result.passed:
                    self.display.step_pass(checker.name)
                else:
                    self.display.step_fail(checker.name, check_result.reason)
                    failure_context = (
                        f"{checker.name}: {check_result.reason}\n"
                        f"Full output (last 3000 chars):\n{check_result.output[-3000:]}"
                    )
                    all_passed = False
                    break

            if all_passed:
                return PhaseResult(success=True)

        # Exhausted retries
        bounce_target = self.workflow.bounce_targets.get(phase.id)
        if bounce_target:
            self.state.set_failure_context(phase.id, failure_context)
            return PhaseResult(success=False, bounce_target=bounce_target)
        return PhaseResult(success=False)

    def _run_parallel_group(self, phase_ids: list[str]) -> PhaseResult:
        """Run a group of phases in parallel."""
        phases_map = {p.id: p for p in self.workflow.phases}
        results: dict[str, PhaseResult] = {}

        with ThreadPoolExecutor(max_workers=len(phase_ids)) as pool:
            futures = {pool.submit(self._run_phase, phases_map[pid]): pid for pid in phase_ids}
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

    def _dry_run(self) -> int:
        """Print what would be done without executing."""
        print(f"Workflow: {self.workflow.name}")
        print(f"Backend: {self.workflow.backend}")
        print(f"Working dir: {self.workflow.working_dir}")
        print(f"Max retries: {self.workflow.max_retries}")
        print()
        for i, phase in enumerate(self.workflow.phases):
            print(f"Phase {i + 1}: {phase.id}")
            print(f"  Prompt: {phase.prompt[:100]}...")
            for checker in phase.checkers:
                print(f"  Checker: {checker.name} ({checker.type})")
                if checker.run:
                    print(f"    Run: {checker.run}")
                if checker.role:
                    print(f"    Role: {checker.role}")
            print()
        if self.workflow.bounce_targets:
            print("Bounce targets:")
            for src, dst in self.workflow.bounce_targets.items():
                print(f"  {src} -> {dst}")
        if self.workflow.parallel_groups:
            print("Parallel groups:")
            for group in self.workflow.parallel_groups:
                print(f"  {group}")
        return 0


def plan_workflow(goal: str, output_path: str, backend_name: str = "claude") -> None:
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
        engine = Engine(workflow, state_file=str(tmp_path / ".juvenal-state.json"))
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
