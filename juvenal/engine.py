"""Core workflow execution loop."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from jinja2 import TemplateSyntaxError

from juvenal.backends import Backend, create_backend
from juvenal.checkers import NO_VERDICT_REASON, parse_verdict
from juvenal.display import Display
from juvenal.notifications import build_notification_payload, send_webhook
from juvenal.state import PhaseState, PipelineState
from juvenal.workflow import ParallelGroup, Phase, Workflow

_BASH_CODE_BLOCK_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)
_sleep = time.sleep


@dataclass
class PhaseResult:
    """Result of executing a single phase."""

    success: bool
    bounce_target: str | None = None
    failure_context: str = ""


@dataclass
class PlanResult:
    """Result of planning a workflow from a goal description."""

    success: bool
    workflow_yaml_path: str | None = None
    temp_dir: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class BounceCounter:
    """Thread-safe global bounce counter for lane execution."""

    def __init__(self, max_bounces: int):
        self._max = max_bounces
        self._count = 0
        self._lock = Lock()

    def try_increment(self) -> bool:
        """Try to increment. Returns False if budget exhausted."""
        with self._lock:
            if self._count >= self._max:
                return False
            self._count += 1
            return True

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


class PipelineExhausted(Exception):
    """Raised when the pipeline exhausts the global bounce limit."""

    def __init__(self, phase_id: str):
        self.phase_id = phase_id
        super().__init__(f"Pipeline exhausted bounce limit at phase '{phase_id}'")


class ResumeWorkflowMismatchError(ValueError):
    """Raised when resume state was created from a different expanded workflow."""


class Engine:
    """Deterministic execution loop.

    Agents handle phase work, while flow control remains programmatic:
    retry, bounce, and advance decisions are all made by the engine.
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
        _depth: int = 0,
        _max_depth: int = 3,
        clear_context_on_bounce: bool = False,
        serialize: bool = False,
        interactive: bool = False,
        backend_instance: Backend | None = None,
        replan_after: int | None = None,
        max_replans: int | None = None,
    ):
        self.workflow = workflow
        self.backend = backend_instance if backend_instance is not None else create_backend(workflow.backend)
        self.display = Display(plain=plain)
        self._depth = _depth
        self._max_depth = _max_depth
        self.dry_run = dry_run
        self.preserve_context_on_bounce = not clear_context_on_bounce
        self.serialize = serialize
        self.interactive = interactive
        self.replan_after = replan_after
        self.max_replans = max_replans
        self._session_ids: dict[str, str] = {}  # phase_id -> last session_id
        self._bounce_targets: set[str] = set()  # phases that should resume on next run
        self._phase_bounces: dict[str, int] = {}  # phase_id -> bounces in current replan cycle
        self._engine_lock = Lock()  # protects _session_ids and _bounce_targets in parallel

        sf = state_file or ".juvenal-state.json"
        needs_state = resume or rewind is not None or rewind_to is not None
        self.state = PipelineState.load(sf) if needs_state else PipelineState(state_file=Path(sf))

        # If resuming and the state has a replanned workflow, that workflow is authoritative —
        # it represents whatever the last replan agent committed to. Swap it in before any
        # phase-id validation runs.
        if needs_state and self.state.active_workflow_yaml:
            self.workflow = self._load_workflow_from_yaml_text(self.state.active_workflow_yaml)

        if needs_state:
            self._validate_resume_state_matches_workflow()

        # Ensure state phases are ordered to match workflow (fixes invalidate_from
        # when parallel execution creates entries in non-deterministic order)
        self._align_state_phases()

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

        # If start lands inside a parallel group, snap to group's first phase
        self._start_idx = self._snap_to_group_start(self._start_idx)

        # Make the active workflow yaml authoritative: every subsequent state.save()
        # (triggered by phase events) will include it on disk. We don't force a save
        # here so that constructing an Engine that then crashes before any phase
        # progress leaves the state file untouched, matching pre-replan behavior.
        from juvenal.workflow import dump_workflow

        self.state.active_workflow_yaml = dump_workflow(self.workflow)

    def run(self) -> int:
        """Execute the pipeline. Returns 0 on success, 1 on failure."""
        if self.dry_run:
            return self._dry_run()

        errors = self._collect_validation_errors()
        if errors:
            self._print_validation_errors(errors)
            self.display.pipeline_done(False)
            return 1

        self.state.started_at = time.time()
        phases = self.workflow.phases
        phase_idx = self._start_idx
        bounces = 0

        try:
            while phase_idx < len(phases):
                phase = phases[phase_idx]

                # Check for parallel group
                pg = self._get_parallel_group(phase.id)
                if pg and phase.id == pg.first_phase_id():
                    if pg.is_lane_group():
                        # Lane groups handle bouncing internally; they bubble a
                        # bounce_target up only when a phase inside crossed the
                        # per-phase replan threshold.
                        result, lane_bounces = self._run_lane_group(pg, bounces)
                        bounces += lane_bounces
                        if not result.success:
                            if result.bounce_target and self._should_replan(result.bounce_target):
                                if self._try_replan(result.bounce_target, result.failure_context):
                                    phases = self.workflow.phases
                                    phase_idx = self._post_replan_start_index()
                                    bounces = 0
                                    continue
                            raise PipelineExhausted(phase.id)
                    else:
                        result = self._run_parallel_group(pg)
                        if result.bounce_target:
                            bounces += 1
                            self._phase_bounces[result.bounce_target] = (
                                self._phase_bounces.get(result.bounce_target, 0) + 1
                            )
                            if self._should_replan(result.bounce_target):
                                if self._try_replan(result.bounce_target, result.failure_context):
                                    phases = self.workflow.phases
                                    phase_idx = self._post_replan_start_index()
                                    bounces = 0
                                    continue
                                raise PipelineExhausted(phase.id)
                            if bounces >= self.workflow.max_bounces:
                                raise PipelineExhausted(phase.id)
                            self._apply_backoff(bounces)
                            self.state.invalidate_from(result.bounce_target)
                            if result.failure_context:
                                target_attempt = self.state._ensure_phase(result.bounce_target).attempt
                                self.state.set_failure_context(
                                    result.bounce_target, result.failure_context, attempt=target_attempt
                                )
                            self._bounce_targets.add(result.bounce_target)
                            phase_idx = self._snap_to_group_start(self._find_phase_index(result.bounce_target))
                            continue
                        if not result.success:
                            raise PipelineExhausted(phase.id)
                    # Skip past all phases in the group
                    last_pg_phase = pg.last_phase_id()
                    phase_idx = self._find_phase_index(last_pg_phase) + 1
                    continue

                if phase.type == "implement":
                    result = self._run_implement(phase)
                elif phase.type == "check":
                    result = self._run_check(phase, phases, phase_idx)
                elif phase.type == "workflow":
                    result = self._run_workflow(phase)
                else:
                    raise ValueError(f"Unknown phase type: {phase.type!r}")

                if result.success:
                    self.state.mark_completed(phase.id)
                    phase_idx += 1
                elif result.bounce_target:
                    bounces += 1
                    self._phase_bounces[result.bounce_target] = self._phase_bounces.get(result.bounce_target, 0) + 1
                    if self._should_replan(result.bounce_target):
                        if self._try_replan(result.bounce_target, result.failure_context):
                            phases = self.workflow.phases
                            phase_idx = self._post_replan_start_index()
                            bounces = 0
                            continue
                        raise PipelineExhausted(phase.id)
                    if bounces >= self.workflow.max_bounces:
                        raise PipelineExhausted(phase.id)
                    self._apply_backoff(bounces)
                    self.state.invalidate_from(result.bounce_target)
                    if result.failure_context:
                        target_attempt = self.state._ensure_phase(result.bounce_target).attempt
                        self.state.set_failure_context(
                            result.bounce_target, result.failure_context, attempt=target_attempt
                        )
                    self._bounce_targets.add(result.bounce_target)
                    phase_idx = self._snap_to_group_start(self._find_phase_index(result.bounce_target))
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

        except KeyboardInterrupt:
            self.backend.kill_active()
            self.state.save()
            print("\nInterrupted. State saved. Resume with --resume.")
            return 130

    def _should_replan(self, triggered_phase: str) -> bool:
        """True if the per-phase bounce count has reached --replan-after."""
        if self.replan_after is None:
            return False
        return self._phase_bounces.get(triggered_phase, 0) >= self.replan_after

    def _post_replan_start_index(self) -> int:
        """After a workflow swap, find the first phase the engine should run.

        Mirrors the snap-to-group-start logic in __init__ so a new workflow whose
        first incomplete phase happens to sit inside a parallel group enters at
        the group's first phase instead of mid-group.
        """
        idx = self.state.get_resume_phase_index(self.workflow.phases)
        return self._snap_to_group_start(idx)

    _REPLAN_MAX_ATTEMPTS = 3

    def _try_replan(self, triggered_phase: str, last_failure_context: str) -> bool:
        """Invoke the replan agent and swap the workflow on success.

        Returns True if a new workflow was successfully validated and installed.
        Returns False if the agent failed to produce a valid workflow within
        _REPLAN_MAX_ATTEMPTS, or if --max-replans has been exhausted.
        """
        from juvenal.workflow import dump_workflow, validate_workflow

        if self.max_replans is not None and self.state.replan_count >= self.max_replans:
            self.display.step_fail(
                "replan",
                f"--max-replans={self.max_replans} exhausted after {self.state.replan_count} replan(s)",
            )
            return False

        current_yaml = dump_workflow(self.workflow)
        prompt = self._build_replan_prompt(triggered_phase, current_yaml, last_failure_context)

        last_error = ""
        retry_prompt = prompt
        for _attempt in range(self._REPLAN_MAX_ATTEMPTS):
            self.display.step_start(f"replan: cycle #{self.state.replan_count + 1}")
            result = self.backend.run_agent(
                retry_prompt,
                working_dir=self.workflow.working_dir,
                display_callback=self.display.live_update,
                timeout=None,
            )
            self.state.add_tokens(triggered_phase, result.input_tokens, result.output_tokens)

            if result.exit_code != 0:
                last_error = f"replan agent crashed (exit {result.exit_code})"
                retry_prompt = prompt + f"\n\nThe previous attempt failed: {last_error}. Try again."
                continue

            yaml_text = _extract_yaml(result.output).strip()
            if not yaml_text:
                last_error = "no YAML found in replan agent output"
                retry_prompt = prompt + f"\n\nThe previous attempt failed: {last_error}. Try again."
                continue

            try:
                new_workflow = self._load_workflow_from_yaml_text(yaml_text)
            except Exception as exc:
                last_error = f"replan yaml failed to load: {exc}"
                retry_prompt = prompt + f"\n\nThe previous attempt failed: {last_error}. Try again."
                continue

            errors = validate_workflow(new_workflow)
            if errors:
                last_error = "replan workflow validation failed: " + "; ".join(errors)
                retry_prompt = prompt + f"\n\nThe previous attempt failed: {last_error}. Try again."
                continue

            # Success — install the new workflow. record_replan snapshots the old
            # phase records into replan_history and clears the live phase state, so
            # the new workflow starts with a clean slate even if it reuses phase ids.
            # _align_state_phases then materializes fresh PhaseState entries for the
            # new workflow's phase ids and reseats state.workflow_phase_ids.
            new_workflow.working_dir = self.workflow.working_dir
            new_workflow.backend = new_workflow.backend or self.workflow.backend
            old_yaml = current_yaml
            self.workflow = new_workflow
            self._phase_bounces.clear()
            self._bounce_targets.clear()
            self._session_ids.clear()
            self.state.record_replan(triggered_phase, old_yaml, yaml_text)
            self._align_state_phases()
            self.display.step_pass(f"replan: cycle #{self.state.replan_count}")
            return True

        self.display.step_fail("replan", last_error[:500])
        return False

    def _build_replan_prompt(self, triggered_phase: str, current_yaml: str, last_failure_context: str) -> str:
        """Render the replan prompt template with bounce history baked in."""
        template = self._read_replan_template()
        bounce_history = self._format_bounce_history()
        last_impl, last_check = self._tail_last_transcripts(triggered_phase)
        replacements = {
            "{{BOUNCE_COUNT}}": str(self._phase_bounces.get(triggered_phase, 0)),
            "{{REPLAN_CYCLE}}": str(self.state.replan_count + 1),
            "{{STUCK_PHASE_ID}}": triggered_phase,
            "{{CURRENT_WORKFLOW_YAML}}": current_yaml,
            "{{BOUNCE_HISTORY}}": bounce_history or "(no per-phase bounce records)",
            "{{LAST_IMPLEMENT_OUTPUT}}": last_impl or "(no recent implement output captured)",
            "{{LAST_CHECK_OUTPUT}}": last_check or last_failure_context or "(no recent check output captured)",
        }
        out = template
        for key, val in replacements.items():
            out = out.replace(key, val)
        return out

    @staticmethod
    def _read_replan_template() -> str:
        return (Path(__file__).parent / "prompts" / "replan.md").read_text()

    def _format_bounce_history(self) -> str:
        """Summarize attempts and most recent failure reasons for every phase with attempts."""
        lines: list[str] = []
        for pid, ps in self.state.phases.items():
            if ps.attempt == 0 and not ps.failure_contexts:
                continue
            last_fc = ps.failure_contexts[-1]["context"] if ps.failure_contexts else ""
            last_fc_snippet = last_fc[:600].replace("\n", " ")
            lines.append(f"- `{pid}`: status={ps.status}, attempts={ps.attempt}. Last failure: {last_fc_snippet}")
        return "\n".join(lines)

    def _tail_last_transcripts(self, phase_id: str, limit: int = 4000) -> tuple[str, str]:
        """Return (most recent implement-step output, most recent check-step output).

        Checker outputs are logged under the check phase's id, not the bounce target's,
        so we walk all phase logs ordered by timestamp to find the latest of each kind.
        Falls back to empty strings if nothing relevant is recorded.
        """
        impl_entry: dict | None = None
        check_entry: dict | None = None
        for ps in self.state.phases.values():
            for entry in ps.logs:
                step = entry.get("step", "")
                ts = entry.get("timestamp", 0) or 0
                if step.startswith("implement") or step.startswith("interactive"):
                    if impl_entry is None or ts >= (impl_entry.get("timestamp", 0) or 0):
                        impl_entry = entry
                elif step.startswith("check"):
                    if check_entry is None or ts >= (check_entry.get("timestamp", 0) or 0):
                        check_entry = entry
        last_impl = (impl_entry or {}).get("output", "") or ""
        last_check = (check_entry or {}).get("output", "") or ""
        return last_impl[-limit:], last_check[-limit:]

    @staticmethod
    def _load_workflow_from_yaml_text(yaml_text: str) -> Workflow:
        """Parse a workflow yaml string by writing to a temp file and loading."""
        from juvenal.workflow import load_workflow

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_text)
            tmp_path = f.name
        try:
            return load_workflow(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _run_implement(self, phase: Phase) -> PhaseResult:
        """Run an implement phase once. On crash, return a bounce target."""
        failure_context = self.state.get_failure_context(phase.id)

        ps = self.state._ensure_phase(phase.id)
        if ps.baseline_sha is None:
            ps.baseline_sha = self._get_git_head()
            self.state.save()

        attempt = (ps.attempt if ps.attempt > 0 else 0) + 1
        self.state.set_attempt(phase.id, attempt)
        self.display.phase_start(phase.id, attempt)

        # Interactive mode: agent-driven Q&A loop
        if phase.interactive and self.interactive:
            return self._run_interactive_loop(phase, attempt, failure_context)

        # When preserve_context_on_bounce is enabled and we're bouncing back to this
        # phase, resume the previous session instead of starting fresh. The failure
        # context is sent as the resume message (not the full prompt).
        should_resume = (
            self.preserve_context_on_bounce and phase.id in self._bounce_targets and phase.id in self._session_ids
        )
        self._bounce_targets.discard(phase.id)

        self.display.step_start("implement")
        if should_resume:
            resume_prompt = (
                "A previous attempt failed verification.\n"
                f"Failure details:\n\n{failure_context}\n\n"
                "Fix these issues in your implementation.\n"
            )
            result = self.backend.resume_agent(
                self._session_ids[phase.id],
                resume_prompt,
                working_dir=self.workflow.working_dir,
                display_callback=self.display.live_update,
                timeout=phase.timeout,
                env=phase.env or None,
            )
        else:
            try:
                prompt = phase.render_prompt(failure_context=failure_context, vars=self.workflow.vars)
            except Exception as exc:
                return self._template_render_failure(phase.id, "prompt", "implement", exc)
            result = self.backend.run_agent(
                prompt,
                working_dir=self.workflow.working_dir,
                display_callback=self.display.live_update,
                timeout=phase.timeout,
                env=phase.env or None,
            )
        logged_input = resume_prompt if should_resume else prompt
        self.state.log_step(
            phase.id, attempt, "implement", result.output, input=logged_input, transcript=result.transcript
        )
        self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

        # Track session ID for potential future resume
        if result.session_id:
            self._session_ids[phase.id] = result.session_id

        if result.exit_code != 0:
            failure_context = f"Implementation agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
            self.display.step_fail("implement", failure_context[:500])
            # Bounce to explicit target, or back to self
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        self.display.step_pass("implement")
        return PhaseResult(success=True)

    _INTERACTIVE_SENTINEL = "PLAN_COMPLETE"
    _INTERACTIVE_PREAMBLE = (
        "You are in interactive mode. The user is available to discuss the plan "
        "with you. If you encounter a genuinely important ambiguity that could "
        "lead the implementation in the wrong direction, ask the user. For "
        "routine decisions, use your judgment. Ask questions ONE AT A TIME — "
        "present a single question, wait for the answer, then move on to the "
        "next. Do not batch multiple questions together. When a decision is "
        "made, update the plan text to reflect it directly at the relevant "
        "location. The plan should be self-contained.\n\n"
        "When you have resolved all ambiguities and finished updating the plan, "
        "emit PLAN_COMPLETE on its own line as the very last thing you output.\n\n"
    )

    def _run_interactive_loop(self, phase: Phase, attempt: int, failure_context: str) -> PhaseResult:
        """Run an agent-driven Q&A loop. Agent asks questions, user answers, agent updates plan."""
        try:
            prompt = phase.render_prompt(failure_context=failure_context, vars=self.workflow.vars)
        except Exception as exc:
            return self._template_render_failure(phase.id, "prompt", "interactive", exc)
        prompt = self._INTERACTIVE_PREAMBLE + prompt

        self.display.step_start("interactive")
        result = self.backend.run_agent(
            prompt,
            working_dir=self.workflow.working_dir,
            display_callback=self.display.live_update,
            timeout=phase.timeout,
            env=phase.env or None,
        )
        self.state.log_step(phase.id, attempt, "interactive", result.output, input=prompt, transcript=result.transcript)
        self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

        if result.exit_code != 0:
            failure_context = f"Interactive agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
            self.display.step_fail("interactive", failure_context[:500])
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        session_id = result.session_id
        if session_id:
            self._session_ids[phase.id] = session_id

        # Q&A loop: agent asks questions, user answers, until agent emits PLAN_COMPLETE
        while self._INTERACTIVE_SENTINEL not in result.output:
            # Show the agent's question and prompt for user input
            self.display._stop_live()
            print(f"\n{result.output}\n", flush=True)
            try:
                user_input = input(">>> ")
            except (EOFError, KeyboardInterrupt):
                print(flush=True)
                self.display.step_pass("interactive (user exited)")
                return PhaseResult(success=True)

            if not session_id:
                self.display.step_fail("interactive", "No session ID for resume")
                return PhaseResult(success=False)

            self.display.step_start("interactive")
            result = self.backend.resume_agent(
                session_id,
                user_input,
                working_dir=self.workflow.working_dir,
                display_callback=self.display.live_update,
                timeout=phase.timeout,
                env=phase.env or None,
            )
            self.state.log_step(
                phase.id, attempt, "interactive-resume", result.output, input=user_input, transcript=result.transcript
            )
            self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

            if result.exit_code != 0:
                failure_context = f"Interactive agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
                self.display.step_fail("interactive", failure_context[:500])
                bounce_target = phase.bounce_target or phase.id
                return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        self.display.step_pass("interactive")
        return PhaseResult(success=True)

    _MAX_NO_VERDICT_RESUMES = 2
    _RESUME_PROMPT = (
        "Your previous response did not include a VERDICT line. Please review the work\n"
        "you just examined and emit exactly one of:\n"
        "- VERDICT: PASS\n"
        "- VERDICT: FAIL: <reason>"
    )

    def _run_check(self, phase: Phase, phases: list[Phase], phase_idx: int) -> PhaseResult:
        """Run a check phase. PASS = advance. FAIL = bounce back."""
        ps = self.state.phases.get(phase.id)
        attempt = (ps.attempt if ps and ps.attempt > 0 else 0) + 1
        self.state.set_attempt(phase.id, attempt)
        self.display.phase_start(phase.id, attempt)
        self.display.step_start(f"check: {phase.id}")

        try:
            prompt = phase.render_check_prompt(vars=self.workflow.vars)
        except Exception as exc:
            return self._template_render_failure(phase.id, "checker prompt", phase.id, exc)

        # Inject the parent implement phase's directions so the checker knows what to verify
        try:
            parent_prompt = self._get_parent_prompt(phase, phases, phase_idx)
        except Exception as exc:
            return self._template_render_failure(phase.id, "parent implement prompt", phase.id, exc)
        if parent_prompt:
            prompt = (
                f"You are a CHECKER. You must NOT write any code or implement anything. "
                f"Another agent has already attempted the task below. "
                f"Your ONLY job is to VERIFY their work.\n\n"
                f"## Implementation Task Given to the Implementer — THIS IS NOT YOUR TASK, YOU WILL VERIFY IT\n\n"
                f"{parent_prompt}\n\n"
                f"---\n\n"
                f"## Your Checker Instructions\n\n"
                f"{prompt}"
            )

        # Inject baseline SHA so the checker can see ALL changes, not just the latest commit
        baseline_sha = self._get_baseline_sha(phase, phases, phase_idx)
        if baseline_sha:
            prompt += (
                f"\n\nIMPORTANT: The implementation started from commit {baseline_sha}. "
                f"Use `git diff {baseline_sha}..HEAD` to see ALL changes made by the implementor, "
                "not just the latest commit. This ensures you review the complete scope of work."
            )

        result = self.backend.run_agent(
            prompt,
            working_dir=self.workflow.working_dir,
            display_callback=self.display.live_update,
            timeout=phase.timeout,
            env=phase.env or None,
        )
        self.state.log_step(phase.id, attempt, "check", result.output, input=prompt, transcript=result.transcript)
        self.state.add_tokens(phase.id, result.input_tokens, result.output_tokens)

        if result.exit_code != 0:
            failure_context = f"Checker agent crashed (exit {result.exit_code}).\n{result.output[-3000:]}"
            self.display.step_fail(phase.id, failure_context[:500])
            target_id = self._resolve_bounce_target(phase, phases, phase_idx)
            if target_id:
                return PhaseResult(success=False, bounce_target=target_id, failure_context=failure_context)
            return PhaseResult(success=False)

        passed, reason, agent_target = parse_verdict(result.output)

        # If no verdict was emitted, try resuming the session to get one
        if not passed and reason == NO_VERDICT_REASON and result.session_id:
            for _ in range(self._MAX_NO_VERDICT_RESUMES):
                resume_result = self.backend.resume_agent(
                    result.session_id,
                    self._RESUME_PROMPT,
                    working_dir=self.workflow.working_dir,
                    display_callback=self.display.live_update,
                    timeout=phase.timeout,
                    env=phase.env or None,
                )
                self.state.log_step(
                    phase.id,
                    attempt,
                    "check-resume",
                    resume_result.output,
                    input=self._RESUME_PROMPT,
                    transcript=resume_result.transcript,
                )
                self.state.add_tokens(phase.id, resume_result.input_tokens, resume_result.output_tokens)

                if resume_result.exit_code != 0:
                    break

                passed, reason, agent_target = parse_verdict(resume_result.output)
                if reason != NO_VERDICT_REASON:
                    break

        if passed:
            self.display.step_pass(phase.id)
            return PhaseResult(success=True)

        # FAIL — resolve bounce target
        failure_context = f"{phase.id}: {reason}\nFull output (last 3000 chars):\n{result.output[-3000:]}"
        self.display.step_fail(phase.id, reason)

        target_id = self._resolve_bounce_target(phase, phases, phase_idx, agent_target)
        if target_id:
            return PhaseResult(success=False, bounce_target=target_id, failure_context=failure_context)
        return PhaseResult(success=False)

    def _run_workflow(self, phase: Phase) -> PhaseResult:
        """Run a workflow phase: static (workflow_file/workflow_dir) or dynamic (prompt-planned)."""
        effective_max_depth = phase.max_depth if phase.max_depth is not None else self._max_depth

        # Check recursion depth
        if self._depth >= effective_max_depth:
            failure_context = (
                f"Workflow phase '{phase.id}' exceeded max recursion depth ({self._depth} >= {effective_max_depth})"
            )
            self.display.step_fail(phase.id, failure_context)
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        ps = self.state._ensure_phase(phase.id)
        if ps.baseline_sha is None:
            ps.baseline_sha = self._get_git_head()
            self.state.save()

        if phase.workflow_file or phase.workflow_dir:
            return self._run_static_workflow(phase, effective_max_depth)
        return self._run_dynamic_workflow(phase, effective_max_depth)

    def _inherit_subworkflow_vars(self, phase: Phase, sub_workflow: Workflow) -> None:
        """Merge parent workflow context into a sub-workflow before validation/execution."""
        merged_vars = dict(sub_workflow.vars)
        merged_vars.update(self.workflow.vars)
        merged_vars.update(phase.template_vars)
        sub_workflow.vars = merged_vars

    def _run_static_workflow(self, phase: Phase, effective_max_depth: int) -> PhaseResult:
        """Run a static sub-workflow from workflow_file or workflow_dir."""
        from juvenal.workflow import load_workflow

        ps = self.state.phases.get(phase.id)
        attempt = (ps.attempt if ps and ps.attempt > 0 else 0) + 1
        self.state.set_attempt(phase.id, attempt)
        self.display.phase_start(phase.id, attempt)
        self.display.step_start(f"workflow: {phase.id}")

        wf_path = phase.workflow_file or phase.workflow_dir
        sub_workflow = load_workflow(wf_path)
        sub_workflow.working_dir = self.workflow.working_dir
        self._inherit_subworkflow_vars(phase, sub_workflow)

        # State file alongside parent's, named by phase ID
        parent_state = self.state.state_file
        sub_state = str(parent_state.parent / f".juvenal-state-{phase.id}.json")

        sub_engine = Engine(
            sub_workflow,
            backend_instance=self.backend,
            state_file=sub_state,
            _depth=self._depth + 1,
            _max_depth=effective_max_depth,
            clear_context_on_bounce=not self.preserve_context_on_bounce,
            serialize=self.serialize,
            interactive=self.interactive,
            replan_after=self.replan_after,
            max_replans=self.max_replans,
        )
        sub_engine.display = self.display

        exit_code = sub_engine.run()

        # Aggregate tokens
        sub_inp, sub_out = sub_engine.state.total_tokens()
        self.state.add_tokens(phase.id, sub_inp, sub_out)

        if exit_code != 0:
            failure_context = f"Sub-workflow execution failed for phase '{phase.id}'"
            self.display.step_fail(phase.id, failure_context)
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        self.display.step_pass(phase.id)
        return PhaseResult(success=True)

    def _run_dynamic_workflow(self, phase: Phase, effective_max_depth: int) -> PhaseResult:
        """Run a dynamic sub-workflow: plan via LLM, then execute."""
        failure_context = self.state.get_failure_context(phase.id)
        ps = self.state.phases.get(phase.id)
        attempt = (ps.attempt if ps and ps.attempt > 0 else 0) + 1
        self.state.set_attempt(phase.id, attempt)
        self.display.phase_start(phase.id, attempt)

        # Step 1: Plan the sub-workflow
        self.display.step_start(f"workflow-plan: {phase.id}")
        try:
            prompt = phase.render_prompt(failure_context=failure_context, vars=self.workflow.vars)
        except Exception as exc:
            return self._template_render_failure(phase.id, "prompt", f"workflow-plan: {phase.id}", exc)
        plan_result = _plan_workflow_internal(
            goal=prompt,
            backend_instance=self.backend,
            display=self.display,
            working_dir=self.workflow.working_dir,
            serialize=self.serialize,
            depth=self._depth + 1,
            max_depth=effective_max_depth,
            interactive=self.interactive,
            replan_after=self.replan_after,
            max_replans=self.max_replans,
        )
        self.state.add_tokens(phase.id, plan_result.input_tokens, plan_result.output_tokens)

        if not plan_result.success:
            failure_context = f"Sub-workflow planning failed: {plan_result.error}"
            self.display.step_fail(phase.id, failure_context[:500])
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        # Step 2: Load and execute the sub-workflow
        self.display.step_start(f"workflow-exec: {phase.id}")
        from juvenal.workflow import load_workflow

        sub_workflow = load_workflow(plan_result.workflow_yaml_path)
        sub_workflow.working_dir = self.workflow.working_dir
        self._inherit_subworkflow_vars(phase, sub_workflow)

        sub_engine = Engine(
            sub_workflow,
            backend_instance=self.backend,
            state_file=str(Path(plan_result.temp_dir) / ".juvenal-state.json"),
            _depth=self._depth + 1,
            _max_depth=effective_max_depth,
            clear_context_on_bounce=not self.preserve_context_on_bounce,
            serialize=self.serialize,
            interactive=self.interactive,
            replan_after=self.replan_after,
            max_replans=self.max_replans,
        )
        sub_engine.display = self.display

        exit_code = sub_engine.run()

        # Aggregate tokens from sub-workflow execution
        sub_inp, sub_out = sub_engine.state.total_tokens()
        self.state.add_tokens(phase.id, sub_inp, sub_out)

        if exit_code != 0:
            failure_context = f"Sub-workflow execution failed for phase '{phase.id}'"
            self.display.step_fail(phase.id, failure_context)
            bounce_target = phase.bounce_target or phase.id
            return PhaseResult(success=False, bounce_target=bounce_target, failure_context=failure_context)

        # Success — clean up temp dir
        self.display.step_pass(phase.id)
        if plan_result.temp_dir:
            shutil.rmtree(plan_result.temp_dir, ignore_errors=True)
        return PhaseResult(success=True)

    @staticmethod
    def _describe_template_render_error(phase_id: str, field_name: str, exc: Exception) -> str:
        """Format a Jinja2 rendering failure for user-facing output."""
        if isinstance(exc, TemplateSyntaxError):
            return f"Invalid Jinja2 {field_name} in phase '{phase_id}': {exc.message} (line {exc.lineno})"
        return f"Jinja2 render error in {field_name} for phase '{phase_id}': {type(exc).__name__}: {exc}"

    def _template_render_failure(self, phase_id: str, field_name: str, step_name: str, exc: Exception) -> PhaseResult:
        """Convert a Jinja2 render failure into a clean phase failure."""
        failure_context = self._describe_template_render_error(phase_id, field_name, exc)
        self.display.step_fail(step_name, failure_context[:500])
        return PhaseResult(success=False)

    def _resolve_bounce_target(
        self, phase: Phase, phases: list[Phase], phase_idx: int, agent_target: str | None = None
    ) -> str | None:
        """Resolve which phase to bounce to on failure.

        Priority:
        1. If phase has bounce_targets (agent-guided), use the agent's choice if valid,
           otherwise fall back to first in the list.
        2. If phase has bounce_target (fixed), use that.
        3. Otherwise, find the most recent implement/workflow phase.
        """
        if phase.bounce_targets:
            if agent_target and agent_target in phase.bounce_targets:
                return agent_target
            return phase.bounce_targets[0]
        if phase.bounce_target:
            return phase.bounce_target
        return self._find_last_rework_phase(phases, phase_idx)

    def _find_last_rework_phase(self, phases: list[Phase], before_idx: int) -> str | None:
        """Find the most recent implement/workflow phase before the given index."""
        for i in range(before_idx - 1, -1, -1):
            if phases[i].type in {"implement", "workflow"}:
                return phases[i].id
        return None

    def _run_parallel_group(self, pg: ParallelGroup) -> PhaseResult:
        """Run a legacy flat group of implement phases in parallel."""
        phase_ids = pg.phases
        phases_map = {p.id: p for p in self.workflow.phases}

        # Skip already-completed phases (e.g., on resume after partial completion)
        incomplete_ids = [pid for pid in phase_ids if not self._is_completed(pid)]
        if not incomplete_ids:
            return PhaseResult(success=True)

        results: dict[str, PhaseResult] = {}

        if self.serialize:
            for pid in incomplete_ids:
                result = self._run_implement(phases_map[pid])
                results[pid] = result
                if result.success:
                    self.state.mark_completed(pid)
                if result.bounce_target:
                    return PhaseResult(
                        success=False, bounce_target=result.bounce_target, failure_context=result.failure_context
                    )
        else:
            self.display.set_parallel_mode(True)
            try:
                with ThreadPoolExecutor(max_workers=len(incomplete_ids)) as pool:
                    futures = {pool.submit(self._run_implement, phases_map[pid]): pid for pid in incomplete_ids}
                    for future in as_completed(futures):
                        pid = futures[future]
                        result = future.result()
                        results[pid] = result
                        if result.success:
                            self.state.mark_completed(pid)
                        if result.bounce_target:
                            return PhaseResult(
                                success=False,
                                bounce_target=result.bounce_target,
                                failure_context=result.failure_context,
                            )
            finally:
                self.display.set_parallel_mode(False)

        if all(r.success for r in results.values()):
            return PhaseResult(success=True)
        return PhaseResult(success=False)

    def _run_lane_group(self, pg: ParallelGroup, bounces_so_far: int) -> tuple[PhaseResult, int]:
        """Run a lane group: each lane is a mini-pipeline running concurrently.

        Returns (result, bounces_consumed). Lanes share a global bounce budget.
        """
        bounce_counter = BounceCounter(self.workflow.max_bounces - bounces_so_far)

        # Filter to lanes that have at least one incomplete phase
        incomplete_lanes = [lane for lane in pg.lanes if any(not self._is_completed(pid) for pid in lane)]
        if not incomplete_lanes:
            return PhaseResult(success=True), 0

        results: dict[int, PhaseResult] = {}

        if self.serialize:
            for i, lane in enumerate(incomplete_lanes):
                results[i] = self._run_lane(lane, bounce_counter)
        else:
            self.display.set_parallel_mode(True)
            try:
                with ThreadPoolExecutor(max_workers=len(incomplete_lanes)) as pool:
                    futures = {
                        pool.submit(self._run_lane, lane, bounce_counter): i for i, lane in enumerate(incomplete_lanes)
                    }
                    for future in as_completed(futures):
                        lane_idx = futures[future]
                        results[lane_idx] = future.result()
            finally:
                self.display.set_parallel_mode(False)

        consumed = bounce_counter.count
        if all(r.success for r in results.values()):
            return PhaseResult(success=True), consumed
        # Surface the first lane that requested a replan, so the main loop can act on it.
        for r in results.values():
            if not r.success and r.bounce_target and self._should_replan(r.bounce_target):
                return PhaseResult(
                    success=False,
                    bounce_target=r.bounce_target,
                    failure_context=r.failure_context,
                ), consumed
        return PhaseResult(success=False), consumed

    def _run_lane(self, lane_phase_ids: list[str], bounce_counter: BounceCounter) -> PhaseResult:
        """Run a single lane: sequential implement/check loop with internal bounce.

        On bounce, the per-phase replan threshold is checked under the engine lock so
        only the first lane to cross the line gets to surface the replan signal. The
        lane returns a PhaseResult with the bounce_target set, which _run_lane_group
        propagates back to the main engine loop for a full workflow replan.
        """
        phases_map = {p.id: p for p in self.workflow.phases}
        lane_phases = [phases_map[pid] for pid in lane_phase_ids]
        lane_scope = set(lane_phase_ids)
        phase_idx = 0

        # Skip already-completed phases at the start (e.g., on resume)
        while phase_idx < len(lane_phases) and self._is_completed(lane_phases[phase_idx].id):
            phase_idx += 1

        while phase_idx < len(lane_phases):
            phase = lane_phases[phase_idx]

            if phase.type == "implement":
                result = self._run_implement(phase)
            elif phase.type == "workflow":
                result = self._run_workflow(phase)
            elif phase.type == "check":
                result = self._run_check(phase, lane_phases, phase_idx)
            else:
                raise ValueError(f"Unsupported phase type in lane: {phase.type!r}")

            if result.success:
                self.state.mark_completed(phase.id)
                phase_idx += 1
            elif result.bounce_target:
                with self._engine_lock:
                    self._phase_bounces[result.bounce_target] = self._phase_bounces.get(result.bounce_target, 0) + 1
                    hit_replan = self._should_replan(result.bounce_target)
                if hit_replan:
                    return PhaseResult(
                        success=False,
                        bounce_target=result.bounce_target,
                        failure_context=result.failure_context,
                    )
                if not bounce_counter.try_increment():
                    return PhaseResult(success=False)
                self._apply_backoff(bounce_counter.count)
                self.state.invalidate_from(result.bounce_target, scope=lane_scope)
                if result.failure_context:
                    target_attempt = self.state._ensure_phase(result.bounce_target).attempt
                    self.state.set_failure_context(result.bounce_target, result.failure_context, attempt=target_attempt)
                with self._engine_lock:
                    self._bounce_targets.add(result.bounce_target)
                # Track session ID is already done in _run_implement
                # Find bounce target index within the lane
                try:
                    phase_idx = lane_phase_ids.index(result.bounce_target)
                except ValueError:
                    return PhaseResult(success=False)
            else:
                return PhaseResult(success=False)

        return PhaseResult(success=True)

    def _get_parallel_group(self, phase_id: str) -> ParallelGroup | None:
        """Check if a phase is the start of a parallel group."""
        for group in self.workflow.parallel_groups:
            if phase_id in group.all_phase_ids():
                return group
        return None

    def _is_completed(self, phase_id: str) -> bool:
        """Check if a phase is already completed in state."""
        ps = self.state.phases.get(phase_id)
        return ps is not None and ps.status == "completed"

    def _snap_to_group_start(self, phase_idx: int) -> int:
        """If phase_idx points inside a parallel group, snap to the group's first phase."""
        if phase_idx >= len(self.workflow.phases):
            return phase_idx
        phase_id = self.workflow.phases[phase_idx].id
        for group in self.workflow.parallel_groups:
            if phase_id in group.all_phase_ids() and phase_id != group.first_phase_id():
                return self._find_phase_index(group.first_phase_id())
        return phase_idx

    def _align_state_phases(self) -> None:
        """Ensure state phases exist for all workflow phases in workflow order.

        Fixes invalidate_from() when parallel execution creates dict entries
        in non-deterministic order.
        """
        ordered: dict[str, PhaseState] = {}
        for phase in self.workflow.phases:
            if phase.id in self.state.phases:
                ordered[phase.id] = self.state.phases[phase.id]
            else:
                ordered[phase.id] = PhaseState(phase_id=phase.id)
        for pid, ps in self.state.phases.items():
            if pid not in ordered:
                ordered[pid] = ps
        self.state.phases = ordered
        self.state.workflow_phase_ids = [phase.id for phase in self.workflow.phases]

    def _validate_resume_state_matches_workflow(self) -> None:
        """Refuse resume/rewind when the saved state came from a different phase list."""
        if not self.state.phases:
            return

        workflow_phase_ids = [phase.id for phase in self.workflow.phases]
        saved_phase_ids = self.state.workflow_phase_ids or list(self.state.phases.keys())

        # Legacy state files did not record phase order, and the JSON serializer
        # sorted dict keys alphabetically. For those files, only membership is
        # trustworthy; exact ordering is not.
        if self.state.workflow_phase_ids is None:
            if all(pid in workflow_phase_ids for pid in saved_phase_ids):
                return
        elif saved_phase_ids == workflow_phase_ids or saved_phase_ids == workflow_phase_ids[: len(saved_phase_ids)]:
            return

        only_in_state = [pid for pid in saved_phase_ids if pid not in workflow_phase_ids]
        only_in_workflow = [pid for pid in workflow_phase_ids if pid not in saved_phase_ids]

        details: list[str] = []
        if self.state.workflow_phase_ids is not None:
            mismatch_idx = next(
                (
                    i
                    for i, (saved_id, workflow_id) in enumerate(zip(saved_phase_ids, workflow_phase_ids))
                    if saved_id != workflow_id
                ),
                min(len(saved_phase_ids), len(workflow_phase_ids)),
            )
            if mismatch_idx < len(saved_phase_ids) or mismatch_idx < len(workflow_phase_ids):
                saved_id = saved_phase_ids[mismatch_idx] if mismatch_idx < len(saved_phase_ids) else "<end>"
                workflow_id = workflow_phase_ids[mismatch_idx] if mismatch_idx < len(workflow_phase_ids) else "<end>"
                details.append(
                    f"first mismatch at position {mismatch_idx + 1}: "
                    f"state has {saved_id!r}, workflow has {workflow_id!r}"
                )
        if only_in_state:
            details.append(f"phases only in saved state: {', '.join(only_in_state[:5])}")
        if only_in_workflow:
            details.append(f"phases only in current workflow: {', '.join(only_in_workflow[:5])}")

        detail_text = "; ".join(details)
        raise ResumeWorkflowMismatchError(
            "resume state was created for a different expanded workflow"
            + (f" ({detail_text})" if detail_text else "")
            + ". This usually means the original run used shape-changing options such as "
            "--checker, --standard-checkers, --implementer, multi-value -D vars, or the workflow file changed. "
            "Resume with the same options/workflow, or start with a new state file."
        )

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
        _sleep(delay)

    def _get_git_head(self) -> str | None:
        """Get the current git HEAD SHA, or None if not in a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workflow.working_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _get_parent_prompt(self, phase: Phase, phases: list[Phase], phase_idx: int) -> str | None:
        """Get the prompt from the parent implement/workflow phase for a checker phase."""
        target_id = self._resolve_bounce_target(phase, phases, phase_idx)
        if target_id:
            for p in phases:
                if p.id == target_id and p.type in {"implement", "workflow"}:
                    return p.render_prompt(vars=self.workflow.vars) or None
        return None

    def _get_baseline_sha(self, phase: Phase, phases: list[Phase], phase_idx: int) -> str | None:
        """Get the baseline SHA for a checker phase's implement/workflow target."""
        target_id = self._resolve_bounce_target(phase, phases, phase_idx)
        if target_id:
            target_ps = self.state.phases.get(target_id)
            if target_ps and target_ps.baseline_sha:
                return target_ps.baseline_sha
        return None

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
            replan_count=self.state.replan_count,
        )
        for url in self.workflow.notify:
            ok = send_webhook(url, payload)
            if not ok:
                self.display.notify_failed(url)

    def _collect_validation_errors(self) -> list[str]:
        """Validate the workflow."""
        from juvenal.workflow import validate_workflow

        return validate_workflow(self.workflow)

    @staticmethod
    def _print_validation_errors(errors: list[str]) -> None:
        """Print validation errors in the standard CLI format."""
        print(f"Validation: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")

    def _dry_run(self) -> int:
        """Print what would be done without executing."""
        print(f"Workflow: {self.workflow.name}")
        print(f"Backend: {self.workflow.backend}")
        print(f"Working dir: {self.workflow.working_dir}")
        print(f"Max bounces: {self.workflow.max_bounces}")
        if self.workflow.backoff > 0:
            print(f"Backoff: {self.workflow.backoff}s base, {self.workflow.max_backoff}s max")
        if self.workflow.notify:
            print(f"Notifications: {len(self.workflow.notify)} webhook(s)")
        if self.workflow.vars:
            print(f"Variables: {self.workflow.vars}")
        print()

        # Validation
        errors = self._collect_validation_errors()
        has_errors = bool(errors)
        if errors:
            self._print_validation_errors(errors)
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
            if phase.max_depth is not None:
                extras.append(f"max_depth={phase.max_depth}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            if phase.type == "implement":
                prompt_preview = phase.prompt if has_errors else phase.render_prompt(vars=self.workflow.vars)
                prompt_preview = prompt_preview[:80].replace("\n", " ")
                print(f"{prefix} [{phase.type}] {phase.id}{extra_str}")
                print(f"     prompt: {prompt_preview}...")
            elif phase.type == "check":
                target = phase.role or _preview_check_target(phase.prompt)
                print(f"{prefix} [{phase.type}] {phase.id}: {target}{extra_str}")
            elif phase.type == "workflow":
                print(f"{prefix} [{phase.type}] {phase.id}{extra_str}")
                if phase.workflow_file:
                    print(f"     workflow_file: {phase.workflow_file}")
                elif phase.workflow_dir:
                    print(f"     workflow_dir: {phase.workflow_dir}")
                else:
                    prompt_preview = phase.prompt if has_errors else phase.render_prompt(vars=self.workflow.vars)
                    prompt_preview = prompt_preview[:80].replace("\n", " ")
                    print(f"     prompt: {prompt_preview}...")
            print()

        if self.workflow.parallel_groups:
            print("Parallel groups:")
            for gi, group in enumerate(self.workflow.parallel_groups):
                if group.is_lane_group():
                    print(f"  Group {gi + 1} ({len(group.lanes)} lanes):")
                    for li, lane in enumerate(group.lanes):
                        print(f"    Lane {li + 1}: {' → '.join(lane)}")
                else:
                    print(f"  Group {gi + 1} (flat): {', '.join(group.phases)}")
        return 1 if has_errors else 0


def _preview_check_target(prompt: str) -> str:
    """Summarize a check prompt for dry-run output."""
    match = _BASH_CODE_BLOCK_RE.search(prompt)
    if match:
        command = " ".join(line.strip() for line in match.group(1).strip().splitlines() if line.strip())
        if command:
            return command
    return prompt[:60].replace("\n", " ")


def _plan_workflow_internal(
    goal: str,
    backend_instance: Backend | None = None,
    display: Display | None = None,
    working_dir: str | None = None,
    backend_name: str = "codex",
    plain: bool = False,
    serialize: bool = False,
    depth: int = 0,
    max_depth: int = 3,
    interactive: bool = False,
    project_dir: str | None = None,
    resume: bool = False,
    linear: bool = True,
    replan_after: int | None = None,
    max_replans: int | None = None,
) -> PlanResult:
    """Internal planning logic: generate a sub-workflow YAML from a goal.

    Returns a PlanResult with the path to the generated YAML and temp dir.
    When project_dir is set, plan artifacts go into project_dir/.plan/ and agents
    run in the project directory (so they can read the codebase). When not set
    (dynamic sub-workflows), uses a temp dir.
    Callers are responsible for cleanup of temp_dir on success.

    When ``linear`` is True (default) the programmatic validation gate enforces a
    strictly linear workflow shape; when False the gate only applies basic schema
    validation, allowing the planner to emit non-linear structures.
    """
    import yaml as _yaml

    from juvenal.workflow import load_workflow

    if project_dir:
        work_dir = project_dir
        plan_dir = Path(project_dir) / ".plan"
        plan_dir.mkdir(exist_ok=True)
        tmp_dir = None
    else:
        tmp_dir = tempfile.mkdtemp(prefix="juvenal-plan-")
        work_dir = tmp_dir
        plan_dir = Path(tmp_dir) / ".plan"
        plan_dir.mkdir()

    if not resume:
        (plan_dir / "goal.md").write_text(goal)

    try:
        from juvenal.plan_validation import validate_planned_workflow
        from juvenal.workflow import validate_workflow

        plan_yaml = Path(__file__).parent / "workflows" / "plan.yaml"
        workflow = load_workflow(plan_yaml)
        if backend_instance is None:
            workflow.backend = backend_name
        workflow.vars["GOAL"] = goal
        workflow.vars["LINEAR"] = "true" if linear else "false"
        workflow.vars["PYTHON"] = sys.executable
        workflow.working_dir = work_dir

        state_path = str(plan_dir / ".juvenal-state.json")
        structure_path = plan_dir / "workflow-structure.yaml"
        produced = Path(work_dir) / "workflow.yaml"
        plan_inp = 0
        plan_out = 0

        # Programmatic validation gate. The planner's own `planned-workflow-validate`
        # check phase is LLM-driven and has been observed to PASS while the generated
        # workflow was unrunnable (e.g. unescaped `{{ secrets.X }}` becoming undefined
        # Jinja2 vars). After the planning engine finishes, mechanically re-run the
        # validator; on failure, rewind state to `write-workflow` with failure context
        # and re-run the engine so the planner can retry. Mirrors the pattern in
        # juvenal.api._finalize_staged_planning.
        max_validation_retries = 3
        last_errors: list[str] = []
        for validation_attempt in range(max_validation_retries + 1):
            engine = Engine(
                workflow,
                backend_instance=backend_instance,
                resume=resume or validation_attempt > 0,
                state_file=state_path,
                plain=plain,
                serialize=serialize,
                _depth=depth,
                _max_depth=max_depth,
                clear_context_on_bounce=True,
                interactive=interactive,
                replan_after=replan_after,
                max_replans=max_replans,
            )
            # Share display if provided, otherwise use defaults
            if display is not None:
                engine.display = display

            exit_code = engine.run()

            # Aggregate tokens from planning engine
            plan_inp, plan_out = engine.state.total_tokens()

            if exit_code != 0:
                return PlanResult(
                    success=False,
                    temp_dir=tmp_dir,
                    error="Planning engine returned non-zero",
                    input_tokens=plan_inp,
                    output_tokens=plan_out,
                )

            if not produced.exists():
                return PlanResult(
                    success=False,
                    temp_dir=tmp_dir,
                    error="No workflow.yaml produced",
                    input_tokens=plan_inp,
                    output_tokens=plan_out,
                )

            yaml_content = produced.read_text()
            parsed = _yaml.safe_load(yaml_content)
            if not isinstance(parsed, dict) or "phases" not in parsed:
                return PlanResult(
                    success=False,
                    temp_dir=tmp_dir,
                    error="Produced invalid YAML",
                    input_tokens=plan_inp,
                    output_tokens=plan_out,
                )

            try:
                if structure_path.exists():
                    validation_errors = validate_planned_workflow(structure_path, produced, linear_only=linear)
                else:
                    # No structure file from this planner — still gate on the core
                    # workflow validator (template vars, loadability, etc.) so an
                    # unrunnable workflow can't slip through.
                    loaded = load_workflow(produced)
                    validation_errors = validate_workflow(loaded)
            except Exception as exc:
                validation_errors = [f"Planned workflow load failed: {exc}"]

            if not validation_errors:
                return PlanResult(
                    success=True,
                    workflow_yaml_path=str(produced),
                    temp_dir=tmp_dir,
                    input_tokens=plan_inp,
                    output_tokens=plan_out,
                )

            last_errors = validation_errors
            if validation_attempt >= max_validation_retries:
                break

            # Rewind the planner state to write-workflow and attach the failure
            # context so the next engine run can see what went wrong.
            rewind_state = PipelineState.load(Path(state_path))
            rewind_state.invalidate_from("write-workflow")
            existing = rewind_state.phases.get("write-workflow")
            failure_msg = (
                "Programmatic validation of workflow.yaml failed after the planner reported success. "
                "Fix these errors and regenerate workflow.yaml:\n  - " + "\n  - ".join(last_errors)
            )
            rewind_state.set_failure_context(
                "write-workflow",
                failure_msg,
                attempt=existing.attempt if existing is not None and existing.attempt > 0 else None,
            )

        return PlanResult(
            success=False,
            temp_dir=tmp_dir,
            error="Planned workflow validation failed: " + "; ".join(last_errors),
            input_tokens=plan_inp,
            output_tokens=plan_out,
        )
    except Exception as e:
        return PlanResult(success=False, temp_dir=tmp_dir, error=str(e))


def plan_workflow(
    goal: str,
    output_path: str,
    backend_name: str = "codex",
    plain: bool = False,
    interactive: bool = False,
    resume: bool = False,
    linear: bool = True,
    replan_after: int | None = None,
    max_replans: int | None = None,
) -> None:
    """Generate a workflow YAML from a goal description using a multi-phase pipeline."""
    import os

    result = _plan_workflow_internal(
        goal=goal,
        backend_name=backend_name,
        plain=plain,
        interactive=interactive,
        project_dir=os.getcwd(),
        resume=resume,
        linear=linear,
        replan_after=replan_after,
        max_replans=max_replans,
    )

    if not result.success:
        error_loc = result.temp_dir or ".plan/"
        print(f"Planning failed: {result.error}. Working directory preserved at: {error_loc}")
        raise SystemExit(1)

    # Copy the produced workflow to the output path
    Path(output_path).write_text(Path(result.workflow_yaml_path).read_text())
    print(f"Workflow written to {output_path}")

    # Clean up temp dir on success (project_dir/.plan/ is kept)
    if result.temp_dir:
        shutil.rmtree(result.temp_dir)


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
