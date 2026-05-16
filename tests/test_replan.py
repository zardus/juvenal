"""Tests for the --replan-after / --max-replans workflow rewrite feature."""

from __future__ import annotations

import json
from pathlib import Path

from juvenal.engine import Engine
from juvenal.state import PipelineState
from juvenal.workflow import Phase, Workflow, make_command_check_prompt
from tests.conftest import MockBackend


def _make_engine(workflow, backend, tmp_path, **kwargs):
    engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
    engine.backend = backend
    return engine


def _base_workflow():
    return Workflow(
        name="orig",
        phases=[
            Phase(id="setup", type="implement", prompt="Do the setup."),
            Phase(id="setup-check", type="check", prompt=make_command_check_prompt("true")),
        ],
        backend="claude",
        max_bounces=999,
    )


def _passing_replan_yaml(name: str = "replanned") -> str:
    return f"""```yaml
name: {name}
backend: claude
working_dir: "."
max_bounces: 999
phases:
  - id: new-setup
    type: implement
    prompt: "Reworked setup that succeeds."
  - id: new-setup-check
    type: check
    prompt: |
      Confirm the work is done.
      VERDICT: PASS
```
"""


def test_replan_triggers_at_per_phase_threshold(tmp_path):
    """After replan_after bounces on the same phase, an agent replans and the new workflow runs."""
    backend = MockBackend()
    # Cycle 1: bounce 3 times (each: implement-pass + check-fail = 2 backend calls per bounce)
    for _ in range(3):
        backend.add_response(exit_code=0, output="implementing...")  # implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: still broken")  # check
    # Replan agent returns a working workflow
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    # New workflow runs to completion: 1 implement + 1 check
    backend.add_response(exit_code=0, output="new implementation done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=3)
    assert engine.run() == 0

    # Workflow was swapped
    assert engine.workflow.name == "replanned"
    assert [p.id for p in engine.workflow.phases] == ["new-setup", "new-setup-check"]

    # State reflects the swap
    assert engine.state.replan_count == 1
    assert engine.state.active_workflow_yaml is not None
    assert "new-setup" in engine.state.active_workflow_yaml
    assert len(engine.state.replan_history) == 1
    entry = engine.state.replan_history[0]
    assert entry["cycle"] == 1
    assert entry["triggered_phase"] == "setup"
    assert "orig" in entry["old_workflow_yaml"]


def test_replan_persisted_to_state_file(tmp_path):
    """The swapped workflow and history land in the state JSON for --resume."""
    backend = MockBackend()
    for _ in range(2):
        backend.add_response(exit_code=0, output="implementing...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2)
    engine.run()

    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["replan_count"] == 1
    assert "new-setup" in raw["active_workflow_yaml"]
    assert raw["replan_history"][0]["triggered_phase"] == "setup"


def test_resume_loads_replanned_workflow(tmp_path):
    """--resume uses the persisted replanned workflow, not the original workflow on disk."""
    state_path = tmp_path / "state.json"

    # First run: bounce twice → replan → new workflow runs to completion.
    backend1 = MockBackend()
    for _ in range(2):
        backend1.add_response(exit_code=0, output="implementing...")
        backend1.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend1.add_response(exit_code=0, output=_passing_replan_yaml())
    backend1.add_response(exit_code=0, output="done")
    backend1.add_response(exit_code=0, output="VERDICT: PASS")

    engine1 = Engine(_base_workflow(), state_file=str(state_path), replan_after=2)
    engine1.backend = backend1
    assert engine1.run() == 0

    raw = json.loads(state_path.read_text())
    assert raw["replan_count"] == 1
    assert "new-setup" in raw["active_workflow_yaml"]

    # Second run with --resume: pass the ORIGINAL workflow on the CLI;
    # engine should swap it out for the persisted replanned workflow at __init__ time.
    engine2 = Engine(_base_workflow(), state_file=str(state_path), resume=True)
    # Engine should now have the replanned workflow installed.
    assert engine2.workflow.name == "replanned"
    assert [p.id for p in engine2.workflow.phases] == ["new-setup", "new-setup-check"]


def test_max_replans_enforced(tmp_path):
    """With max_replans=1, the second replan trigger fails the pipeline."""
    backend = MockBackend()

    # Cycle 1: bounce 2 times -> replan #1 produces a workflow that ALSO bounces.
    for _ in range(2):
        backend.add_response(exit_code=0, output="implementing...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: still bad")
    # Replan #1: a workflow whose check also fails.
    failing_replan_yaml = """```yaml
name: replanned-1
backend: claude
working_dir: "."
max_bounces: 999
phases:
  - id: phase-a
    type: implement
    prompt: "Try again."
  - id: phase-a-check
    type: check
    prompt: "Check."
```
"""
    backend.add_response(exit_code=0, output=failing_replan_yaml)
    # New workflow bounces 2 times, triggering replan #2 which exceeds max_replans=1
    for _ in range(2):
        backend.add_response(exit_code=0, output="trying...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: still bad")
    # Subsequent calls return default PASS but they shouldn't be needed —
    # pipeline should exhaust before max_replans is exceeded.

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2, max_replans=1)
    assert engine.run() == 1
    assert engine.state.replan_count == 1  # only one replan was allowed


def test_replan_resets_per_phase_bounce_counter(tmp_path):
    """After a successful replan, prior bounce counts on old phases must not count toward the new threshold."""
    backend = MockBackend()
    for _ in range(2):
        backend.add_response(exit_code=0, output="implementing...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2)
    engine.run()

    # After the swap, _phase_bounces is cleared; the new pipeline never accumulated more.
    assert engine._phase_bounces == {}


def test_no_replan_when_flag_unset(tmp_path):
    """Without --replan-after, the engine behaves as before and exhausts max_bounces."""
    backend = MockBackend()
    # 4 bounces (1 implement + 1 check fail per bounce) on the original
    for _ in range(4):
        backend.add_response(exit_code=0, output="implementing...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: still bad")

    workflow = _base_workflow()
    workflow.max_bounces = 3
    engine = _make_engine(workflow, backend, tmp_path)  # no replan_after
    assert engine.run() == 1
    assert engine.state.replan_count == 0
    # The workflow yaml is always persisted now (so --resume works without a path arg),
    # but no replan occurred so the persisted yaml is the original workflow.
    assert engine.state.active_workflow_yaml is not None
    assert "name: orig" in engine.state.active_workflow_yaml
    assert engine.state.replan_history == []


def test_dump_workflow_round_trips(tmp_path):
    """dump_workflow + load_workflow must round-trip phase ids, prompts, and core settings."""
    from juvenal.workflow import dump_workflow, load_workflow

    wf = Workflow(
        name="rt",
        phases=[
            Phase(id="a", type="implement", prompt="alpha"),
            Phase(id="a-check", type="check", prompt="check alpha", bounce_target="a"),
        ],
        backend="codex",
        working_dir=".",
        max_bounces=42,
        vars={"X": "y"},
    )
    yaml_text = dump_workflow(wf)
    path = tmp_path / "rt.yaml"
    path.write_text(yaml_text)
    loaded = load_workflow(path)
    assert loaded.name == "rt"
    assert loaded.backend == "codex"
    assert loaded.max_bounces == 42
    assert loaded.vars == {"X": "y"}
    assert [p.id for p in loaded.phases] == ["a", "a-check"]
    assert loaded.phases[1].bounce_target == "a"


def test_dump_workflow_preserves_template_vars(tmp_path):
    """Multi-var-expanded phases must round-trip their per-phase template_vars so --resume
    doesn't silently lose the var bindings each lane was running with."""
    from juvenal.workflow import Workflow as _Workflow
    from juvenal.workflow import dump_workflow, expand_multi_vars, load_workflow

    wf = _Workflow(
        name="exp",
        phases=[Phase(id="p", type="implement", prompt="hi {{V}}")],
        backend="codex",
    )
    expanded = expand_multi_vars(wf, {"V": ["one", "two"]})
    assert any(p.template_vars for p in expanded.phases), "test setup: expansion produced no template_vars"

    yaml_text = dump_workflow(expanded)
    path = tmp_path / "exp.yaml"
    path.write_text(yaml_text)
    loaded = load_workflow(path)

    by_id = {p.id: p for p in loaded.phases}
    pre = {p.id: p for p in expanded.phases}
    assert set(by_id) == set(pre)
    for pid, p in by_id.items():
        assert p.template_vars == pre[pid].template_vars, f"template_vars lost for {pid}"


def test_lane_group_triggers_replan(tmp_path):
    """A lane phase that bounces past --replan-after surfaces the signal and the
    main engine performs a full workflow replan."""
    backend = MockBackend()
    # Lane phase bounces 2 times. Each bounce: implement (PASS) + check (FAIL).
    for _ in range(2):
        backend.add_response(exit_code=0, output="implementing...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    # Replan agent emits a fresh workflow.
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    # New workflow runs cleanly.
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    from juvenal.workflow import ParallelGroup

    workflow = Workflow(
        name="orig-laned",
        phases=[
            Phase(id="laneA-impl", type="implement", prompt="A"),
            Phase(id="laneA-check", type="check", prompt="check A", bounce_target="laneA-impl"),
        ],
        parallel_groups=[ParallelGroup(lanes=[["laneA-impl", "laneA-check"]])],
        backend="claude",
        max_bounces=999,
    )
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2, serialize=True)
    assert engine.run() == 0
    assert engine.state.replan_count == 1
    assert engine.workflow.name == "replanned"
    assert [p.id for p in engine.workflow.phases] == ["new-setup", "new-setup-check"]


def test_replan_prompt_includes_checker_transcript(tmp_path):
    """The replan prompt must include the actual failing checker output, which is logged
    under the check phase's id (not the bounce target's). This was a real bug — _tail_last_transcripts
    used to only look at the target phase's logs and miss the checker transcript entirely."""
    backend = MockBackend()
    # Two bounces, each: implement (looks-fine) + check (fails with distinctive reason).
    for i in range(2):
        backend.add_response(exit_code=0, output=f"implement attempt {i + 1}")
        backend.add_response(exit_code=0, output=f"checker output {i + 1}\nVERDICT: FAIL: distinctive-reason-{i + 1}")
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2)
    assert engine.run() == 0

    # The third backend call (index 4) is the replan agent. Its prompt must include
    # the most recent CHECKER transcript (which lives under state.phases["setup-check"]).
    replan_prompt = backend.calls[4]
    assert "checker output 2" in replan_prompt, (
        "replan prompt missing checker transcript — _tail_last_transcripts didn't search across phases"
    )
    assert "distinctive-reason-2" in replan_prompt
    # And the implementer transcript from the last attempt.
    assert "implement attempt 2" in replan_prompt


def test_subengine_inherits_replan_settings(tmp_path):
    """Static sub-workflow engines must inherit replan_after / max_replans from the parent so
    a stuck phase inside a `workflow` phase can also replan."""
    # Build a parent workflow with one `workflow` phase that points to a static sub-workflow.
    sub_yaml = tmp_path / "sub.yaml"
    sub_yaml.write_text(
        """\
name: sub
backend: claude
working_dir: "."
max_bounces: 999
phases:
  - id: sub-impl
    type: implement
    prompt: "Implement sub."
  - id: sub-check
    type: check
    prompt: "Check sub."
"""
    )
    parent = Workflow(
        name="parent",
        phases=[Phase(id="run-sub", type="workflow", workflow_file=str(sub_yaml))],
        backend="claude",
    )

    # We don't run the engine — we only need to verify the sub-engine constructor receives
    # the parent's replan settings. Patch Engine to capture the kwargs of the second
    # construction (the sub-engine).
    captured: list[dict] = []
    real_init = Engine.__init__

    def spy_init(self, workflow, **kwargs):
        captured.append(dict(kwargs))
        real_init(self, workflow, **kwargs)

    import juvenal.engine as juv_engine

    orig = juv_engine.Engine.__init__
    juv_engine.Engine.__init__ = spy_init
    try:
        engine = Engine(parent, state_file=str(tmp_path / "p.json"), replan_after=7, max_replans=2)
        engine.backend = MockBackend()
        # Trigger the sub-engine by running the workflow phase directly.
        engine._run_static_workflow(parent.phases[0], effective_max_depth=3)
    finally:
        juv_engine.Engine.__init__ = orig

    # captured[0] is the parent construction; captured[1] is the sub-engine.
    assert len(captured) >= 2
    assert captured[1].get("replan_after") == 7
    assert captured[1].get("max_replans") == 2


def test_total_tokens_sums_across_replan_history(tmp_path):
    """Tokens spent in replanned-away cycles must still count toward total_tokens(), or cost
    reporting silently under-counts the work that motivated the replan."""
    backend = MockBackend()
    # Original cycle: 2 bounces, each implement + check costs tokens.
    for _ in range(2):
        backend.add_response(exit_code=0, output="impl", input_tokens=100, output_tokens=50)
        backend.add_response(exit_code=0, output="VERDICT: FAIL: still bad", input_tokens=200, output_tokens=80)
    # Replan agent — also costs tokens.
    backend.add_response(exit_code=0, output=_passing_replan_yaml(), input_tokens=1000, output_tokens=400)
    # New workflow runs cleanly.
    backend.add_response(exit_code=0, output="new impl", input_tokens=10, output_tokens=5)
    backend.add_response(exit_code=0, output="VERDICT: PASS", input_tokens=20, output_tokens=8)

    workflow = _base_workflow()
    engine = _make_engine(workflow, backend, tmp_path, replan_after=2)
    engine.run()

    inp, out = engine.state.total_tokens()
    # Old cycle: 2*(100+200) implement+check inputs, 2*(50+80) outputs.
    # Replan call: 1000 in, 400 out (attributed to triggered_phase before record_replan).
    # New cycle: (10+20)=30 in, (5+8)=13 out.
    expected_inp = 2 * (100 + 200) + 1000 + 30
    expected_out = 2 * (50 + 80) + 400 + 13
    assert inp == expected_inp, f"expected {expected_inp} input tokens, got {inp}"
    assert out == expected_out, f"expected {expected_out} output tokens, got {out}"


def test_replan_state_record(tmp_path):
    """PipelineState.record_replan increments count and snapshots prior yaml."""
    state = PipelineState(state_file=Path(tmp_path / "state.json"))
    state.record_replan("phase-x", "old: yaml", "new: yaml")
    state.record_replan("phase-y", "new: yaml", "newer: yaml")
    assert state.replan_count == 2
    assert state.active_workflow_yaml == "newer: yaml"
    assert [entry["triggered_phase"] for entry in state.replan_history] == ["phase-x", "phase-y"]
    assert state.replan_history[0]["cycle"] == 1
    assert state.replan_history[1]["cycle"] == 2


def test_replan_clears_phase_state_for_colliding_ids(tmp_path):
    """If the replanned workflow reuses a phase id that was 'completed' in the old workflow,
    the engine must NOT skip it. State for the old phase is preserved only in replan_history."""
    backend = MockBackend()
    # Old workflow: 'setup' completes (PASS), then 'setup-check' fails twice, triggering replan.
    backend.add_response(exit_code=0, output="initial setup done")  # setup implement
    backend.add_response(exit_code=0, output="VERDICT: PASS")  # setup-check PASS (setup -> completed)
    # Now the second phase needs to bounce. But replan_after counts bounces on the *target* of
    # the bounce. We want the bounce target to be 'setup' so it bounces 2 times. Use bounce_target.
    workflow = Workflow(
        name="orig-collide",
        phases=[
            Phase(id="setup", type="implement", prompt="setup"),
            Phase(id="finish", type="check", prompt="check that's never going to pass", bounce_target="setup"),
        ],
        backend="claude",
        max_bounces=999,
    )
    # Bounce sequence: setup PASS, finish FAIL (bounce 1 to setup), setup PASS, finish FAIL (bounce 2).
    # Above we already queued setup+finish for attempt 1 (PASS, then we need FAIL).
    # Reset and queue precisely:
    backend = MockBackend()
    for _ in range(2):
        backend.add_response(exit_code=0, output="setup done")  # setup
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")  # finish

    # Replanned workflow REUSES the id 'setup' but with new semantics.
    reused_id_yaml = """```yaml
name: replanned-reused
backend: claude
working_dir: "."
max_bounces: 999
phases:
  - id: setup
    type: implement
    prompt: "The NEW setup with different semantics."
  - id: setup-check
    type: check
    prompt: "Verify the new setup."
```
"""
    backend.add_response(exit_code=0, output=reused_id_yaml)
    # New workflow must EXECUTE the reused-id 'setup' phase, not skip it.
    backend.add_response(exit_code=0, output="new setup ran")  # new setup implement
    backend.add_response(exit_code=0, output="VERDICT: PASS")  # new setup-check

    engine = _make_engine(workflow, backend, tmp_path, replan_after=2)
    assert engine.run() == 0

    # 'setup' was actually re-executed under the new workflow.
    # If it had been skipped, the backend's "new setup ran" response would be unused,
    # and "VERDICT: PASS" would land on a different phase.
    setup_logs = engine.state.phases["setup"].logs
    assert any("new setup ran" in (log.get("output") or "") for log in setup_logs), (
        f"new 'setup' phase did not execute fresh; logs={setup_logs}"
    )

    # Old phase records were snapshotted into replan_history.
    assert "old_phases" in engine.state.replan_history[0]
    old_snapshot = engine.state.replan_history[0]["old_phases"]
    assert "setup" in old_snapshot
    assert old_snapshot["setup"]["status"] == "completed"  # the OLD setup completed


def test_cli_resume_uses_persisted_workflow_without_path(tmp_path, monkeypatch, capsys):
    """`juvenal run --resume` works with no workflow path; the persisted yaml drives execution."""
    from juvenal.cli import build_parser, cmd_run

    state_path = tmp_path / "state.json"

    # Phase 1: a fresh run that bounces, replans, then completes.
    backend1 = MockBackend()
    for _ in range(2):
        backend1.add_response(exit_code=0, output="implementing...")
        backend1.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend1.add_response(exit_code=0, output=_passing_replan_yaml())
    backend1.add_response(exit_code=0, output="done")
    backend1.add_response(exit_code=0, output="VERDICT: PASS")

    engine1 = Engine(_base_workflow(), state_file=str(state_path), replan_after=2)
    engine1.backend = backend1
    assert engine1.run() == 0
    # State now has the replanned workflow persisted.
    raw = json.loads(state_path.read_text())
    assert raw["replan_count"] == 1

    # Phase 2: invoke the CLI in --resume mode with NO workflow path.
    # cmd_run should load the workflow from state.active_workflow_yaml.
    parser = build_parser()
    args = parser.parse_args(["run", "--resume", "--state-file", str(state_path)])
    args.plain = True

    # Intercept the Engine the CLI builds so we can verify it received the
    # persisted workflow (not the original).
    captured: dict = {}

    real_engine = Engine

    class _SpyEngine(real_engine):
        def __init__(self, workflow, **kwargs):
            captured["workflow_name"] = workflow.name
            captured["phase_ids"] = [p.id for p in workflow.phases]
            super().__init__(workflow, **kwargs)

        def run(self):
            # Pipeline is already complete; just return success.
            return 0

    monkeypatch.setattr("juvenal.cli.Engine", _SpyEngine, raising=False)
    # Patch in cli's import scope as well — cmd_run imports Engine at call time.
    import juvenal.engine as juv_engine

    monkeypatch.setattr(juv_engine, "Engine", _SpyEngine)

    assert cmd_run(args) == 0
    assert captured["workflow_name"] == "replanned"
    assert captured["phase_ids"] == ["new-setup", "new-setup-check"]


def test_cli_resume_warns_when_workflow_path_given(tmp_path, monkeypatch, capsys):
    """`juvenal run path.yaml --resume` warns and uses the persisted workflow, not the path."""
    from juvenal.cli import build_parser, cmd_run

    state_path = tmp_path / "state.json"

    # Pre-populate state with a persisted workflow.
    backend1 = MockBackend()
    for _ in range(2):
        backend1.add_response(exit_code=0, output="implementing...")
        backend1.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend1.add_response(exit_code=0, output=_passing_replan_yaml())
    backend1.add_response(exit_code=0, output="done")
    backend1.add_response(exit_code=0, output="VERDICT: PASS")
    engine1 = Engine(_base_workflow(), state_file=str(state_path), replan_after=2)
    engine1.backend = backend1
    engine1.run()

    # Stash a bogus workflow on disk and pass its path on --resume.
    bogus = tmp_path / "ignored.yaml"
    bogus.write_text("name: ignored\nphases:\n  - id: never-runs\n    prompt: x\n")

    captured: dict = {}

    class _SpyEngine(Engine):
        def __init__(self, workflow, **kwargs):
            captured["workflow_name"] = workflow.name
            super().__init__(workflow, **kwargs)

        def run(self):
            return 0

    import juvenal.engine as juv_engine

    monkeypatch.setattr(juv_engine, "Engine", _SpyEngine)

    parser = build_parser()
    args = parser.parse_args(["run", str(bogus), "--resume", "--state-file", str(state_path)])
    args.plain = True

    assert cmd_run(args) == 0
    out = capsys.readouterr().out
    assert "ignoring workflow path" in out
    assert captured["workflow_name"] == "replanned"


def test_cli_resume_preserves_workflow_overrides(tmp_path, monkeypatch):
    """`juvenal run --resume --max-bounces 5` must not be silently clobbered by the
    state-yaml swap inside Engine.__init__. Previously the workflow loaded by
    _cmd_run_resume had overrides applied, but Engine.__init__ reloaded the persisted
    yaml on top, discarding them."""
    from juvenal.cli import build_parser, cmd_run

    state_path = tmp_path / "state.json"

    # Seed state with a persisted workflow whose max_bounces=999.
    seed_wf = _base_workflow()
    assert seed_wf.max_bounces == 999
    seed_backend = MockBackend()
    for _ in range(2):
        seed_backend.add_response(exit_code=0, output="impl...")
        seed_backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    seed_backend.add_response(exit_code=0, output=_passing_replan_yaml())
    seed_backend.add_response(exit_code=0, output="done")
    seed_backend.add_response(exit_code=0, output="VERDICT: PASS")
    seed_engine = Engine(seed_wf, state_file=str(state_path), replan_after=2)
    seed_engine.backend = seed_backend
    assert seed_engine.run() == 0

    captured: dict = {}
    real_engine = Engine

    class _SpyEngine(real_engine):
        def __init__(self, workflow, **kwargs):
            super().__init__(workflow, **kwargs)
            captured["max_bounces"] = self.workflow.max_bounces
            captured["backend_name"] = self.workflow.backend
            captured["backoff"] = self.workflow.backoff

        def run(self):
            return 0

    import juvenal.engine as juv_engine

    monkeypatch.setattr(juv_engine, "Engine", _SpyEngine)

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--resume",
            "--state-file",
            str(state_path),
            "--max-bounces",
            "5",
            "--backend",
            "codex",
            "--backoff",
            "2.5",
        ]
    )
    args.plain = True
    assert cmd_run(args) == 0

    assert captured["max_bounces"] == 5, "CLI --max-bounces override was clobbered by state swap"
    assert captured["backend_name"] == "codex", "CLI --backend override was clobbered by state swap"
    assert captured["backoff"] == 2.5, "CLI --backoff override was clobbered by state swap"


def test_phase_bounces_persisted_across_restart(tmp_path):
    """A phase that bounced N-1 times before a process crash must resume at N-1 (not 0)
    so the next bounce hits --replan-after=N. Without persistence the user would have to
    bounce the full N again in a single session, which is surprising for long-running pipelines."""
    backend1 = MockBackend()
    # Bounce 2 times in session 1 (threshold is 3, so no replan yet).
    for _ in range(2):
        backend1.add_response(exit_code=0, output="impl...")
        backend1.add_response(exit_code=0, output="VERDICT: FAIL: still bad")
    # Backend exhausted after 4 responses, so the 3rd bounce attempt would fail —
    # but we kill the engine before that by capping max_bounces to 2.
    workflow1 = _base_workflow()
    workflow1.max_bounces = 2  # forces failure exit after 2 bounces (no replan yet)
    engine1 = _make_engine(workflow1, backend1, tmp_path, replan_after=3)
    assert engine1.run() == 1
    assert engine1.state.phase_bounces.get("setup") == 2

    # Reload from disk — phase_bounces must survive.
    state_after = PipelineState.load(str(tmp_path / "state.json"))
    assert state_after.phase_bounces.get("setup") == 2

    # Session 2 (--resume): one more bounce on the same phase should hit replan_after=3 and replan.
    backend2 = MockBackend()
    backend2.add_response(exit_code=0, output="impl...")  # bounce #3
    backend2.add_response(exit_code=0, output="VERDICT: FAIL: still bad")
    backend2.add_response(exit_code=0, output=_passing_replan_yaml())
    backend2.add_response(exit_code=0, output="done")
    backend2.add_response(exit_code=0, output="VERDICT: PASS")
    workflow2 = _base_workflow()
    workflow2.max_bounces = 999
    engine2 = Engine(workflow2, state_file=str(tmp_path / "state.json"), resume=True, replan_after=3)
    engine2.backend = backend2
    assert engine2.run() == 0
    assert engine2.state.replan_count == 1, "replan should fire on the very first bounce after resume"


def test_phase_bounces_cleared_by_replan(tmp_path):
    """After a successful replan, state.phase_bounces is wiped so the new workflow's per-phase
    counters start at zero, matching state.phases being cleared at the same point."""
    backend = MockBackend()
    for _ in range(2):
        backend.add_response(exit_code=0, output="impl...")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend.add_response(exit_code=0, output=_passing_replan_yaml())
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    engine = _make_engine(_base_workflow(), backend, tmp_path, replan_after=2)
    assert engine.run() == 0
    assert engine.state.phase_bounces == {}


def test_replan_swaps_backend_when_changed(tmp_path, monkeypatch):
    """If the replan agent emits a workflow with a different `backend:`, the engine must
    rebuild self.backend; otherwise subsequent phases still hit the old CLI."""
    backend = MockBackend()
    for _ in range(2):
        backend.add_response(exit_code=0, output="impl")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope")
    backend.add_response(
        exit_code=0,
        output="""```yaml
name: replanned-codex
backend: codex
working_dir: "."
max_bounces: 999
phases:
  - id: x
    type: implement
    prompt: hi
  - id: x-check
    type: check
    prompt: verify
```
""",
    )
    backend.add_response(exit_code=0, output="done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    # Stub create_backend so the swap doesn't spawn a real codex CLI; we only need to
    # confirm the swap was *attempted* with the new backend name.
    constructed: list[str] = []

    def fake_create_backend(name: str):
        constructed.append(name)
        return backend  # reuse the MockBackend so subsequent phases keep replaying the queue

    import juvenal.engine as juv_engine

    monkeypatch.setattr(juv_engine, "create_backend", fake_create_backend)

    engine = _make_engine(_base_workflow(), backend, tmp_path, replan_after=2)
    engine.run()

    assert engine.workflow.backend == "codex"
    assert "codex" in constructed, f"backend was not rebuilt after replan; create_backend calls: {constructed}"


def test_cli_resume_falls_through_for_legacy_state_files(tmp_path, capsys):
    """Pre-replan state files have no active_workflow_yaml; --resume must NOT short-circuit and
    must instead fall through to the standard cmd_run path so CLI injections still apply."""
    from juvenal.cli import build_parser, cmd_run

    state_path = tmp_path / "state.json"
    # Legacy-shape state file: no active_workflow_yaml field.
    state_path.write_text(json.dumps({"phases": {}, "workflow_phase_ids": []}))

    # Without a workflow path, the standard path will error (workflow is required).
    parser = build_parser()
    args = parser.parse_args(["run", "--resume", "--state-file", str(state_path)])
    args.plain = True

    assert cmd_run(args) == 1
    out = capsys.readouterr().out
    # Comes from the standard cmd_run error, not the resume-helper error.
    assert "workflow path is required" in out
