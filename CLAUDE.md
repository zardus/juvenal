# Juvenal

> *Quis custodiet ipsos custodes?* — Who guards the agents?

A framework for orchestrating AI coding agents (Claude, Codex) through verified implementation phases. The core idea: the implementing agent and the checking agent are separate processes, so the implementer can't cheat by weakening tests.

## Quick Reference

```bash
# Install
pip install -e ".[dev]"

# Lint and format
ruff check juvenal/ tests/
ruff format juvenal/ tests/

# Unit tests (excludes E2E and skill tests)
pytest tests/ -x --ignore=tests/test_e2e_claude.py --ignore=tests/test_e2e_codex.py --ignore=tests/test_skill.py

# E2E tests (require API keys and CLI tools installed)
pytest tests/test_e2e_claude.py -x -v
pytest tests/test_e2e_codex.py -x -v
pytest tests/test_skill.py -x -v
```

## Architecture

The system uses a **non-agentic, deterministic execution loop**. All control flow decisions (retry, bounce, advance) are made programmatically — no LLM decides flow control.

### Core Modules

| Module | Purpose |
|--------|---------|
| `engine.py` | Main orchestration loop (`Engine.run()`). Executes phases sequentially or in parallel groups (flat or lane-based). `BounceCounter` for thread-safe global bounce tracking in lanes. Global bounce counter (`max_bounces`) limits total bounces across all phases. Supports `--resume`, `--rewind N`, and `--rewind-to PHASE_ID` for resuming/rewinding pipeline state. |
| `workflow.py` | Workflow loading and `Phase`/`Workflow`/`ParallelGroup` dataclasses. Supports YAML, directory convention (including `parallel` directories for lane groups), and bare `.md` formats. `apply_vars()` handles `{{VAR}}` template substitution. In directory convention, extra `.md` files in a phase dir become check phases; command execution belongs inside agentic checker prompts rather than `.sh` phase discovery. |
| `backends.py` | Abstract `Backend` base class with `ClaudeBackend` and `CodexBackend`. Manages subprocess invocation and JSON stream parsing. `run_interactive()` for terminal passthrough (Claude only). |
| `state.py` | Atomic JSON state persistence (`PipelineState`). Thread-safe (RLock). Writes to `.tmp`, fsyncs, then atomic renames. Supports resume, rewind, and scoped invalidation (for lane bounces). |
| `checkers.py` | Verdict parsing (`VERDICT: PASS` / `VERDICT: FAIL: reason`). |
| `display.py` | Rich TUI with rolling 15-line buffer. Thread-safe (Lock). Falls back to plain text with `--plain` or parallel mode. `pause()`/`resume()` for interactive terminal passthrough. |
| `cli.py` | CLI entry point. Commands: `run`, `plan`, `do`, `status`, `init`, `validate`. Run flags: `--resume`, `--rewind N`, `--rewind-to PHASE_ID`, `--phase`, `--backoff`, `--notify`, `-D VAR=VAL`, `--serialize`. `plan`/`do` support `--interactive`/`-i` for human-in-the-loop planning. `status` exits 0 if pipeline fully completed, 1 otherwise. |
| `notifications.py` | Webhook notification support (`build_notification_payload`, `send_webhook`). |

### Execution Flow

1. `cli.py` parses args, dispatches to command handler
2. `workflow.py` loads and validates the workflow definition
3. `engine.py` iterates phases: implement → check → advance or bounce
4. `backends.py` spawns agent subprocesses, streams JSON events
5. `checkers.py` parses verdicts from agent output
6. `state.py` persists progress after each phase for resumability

### Phase Types

- **implement** — agent executes a prompt to build/modify code. Supports `interactive: true` for terminal passthrough (Claude only, enabled with `--interactive`)
- **check** — separate agent verifies work, emits `VERDICT: PASS` or `VERDICT: FAIL: reason`
- **workflow** — sub-workflow: dynamic (LLM plans from `prompt`) or static (`workflow_file` / `workflow_dir`). Recursion depth capped by `max_depth`. Parent vars propagate to sub-workflows.

### Template Variables

Prompts and check `run` commands support `{{VAR}}` placeholders. Variables are set via:
- **YAML `vars:` block** — workflow-level defaults
- **CLI `-D VAR=VAL`** — overrides YAML defaults (repeatable)
- **Includes** — included workflow vars are base defaults; including workflow overrides

Unrecognized `{{VAR}}` placeholders pass through unchanged. Applied at render time via `apply_vars()` in `workflow.py`.

Multi-value `-D VAR=VAL1 -D VAR=VAL2` duplicates phases referencing `{{VAR}}` into parallel lanes (cartesian product for multiple vars). `expand_multi_vars()` in `workflow.py` handles this.

## Code Conventions

- **Python 3.10+** with `from __future__ import annotations` for forward references
- **Type hints** throughout, modern union syntax (`X | None`)
- **Dataclasses** for all data structures (`Phase`, `Workflow`, `PhaseState`, `AgentResult`, etc.)
- **Snake case** for functions/variables, **PascalCase** for classes, **kebab-case** for phase IDs
- **Private methods** prefixed with `_`
- **Import order**: stdlib → third-party → local (enforced by ruff `I` rule)
- **Line length**: 120 characters (ruff enforced)
- **Ruff rules**: E (errors), F (pyflakes), W (warnings), I (import sorting)

## Testing

- Tests live in `tests/` using pytest
- `conftest.py` provides shared fixtures: `MockBackend`, `tmp_workflow`, `sample_yaml`, `bare_md`, `simple_workflow`
- `MockBackend` simulates agent responses — use it instead of hitting real APIs
- E2E tests (`test_e2e_claude.py`, `test_e2e_codex.py`) require API keys and are skipped in PRs (run on push to main only)
- `test_skill.py` tests the Claude Code skill integration
- CI runs: lint → unit → e2e (on push only)

## Versioning

When bumping the version, update it in both `pyproject.toml` and `.claude-plugin/plugin.json`.

## Dependencies

**Runtime**: `pyyaml>=6.0` (workflow parsing), `rich>=13.0` (terminal UI)
**Dev**: `pytest>=8.0`, `ruff>=0.4`
**External CLIs** (not pip-managed): `claude` (Anthropic CLI), `npx @openai/codex@latest` (OpenAI Codex)

## Project Layout

```
juvenal/
├── __init__.py          # Version (__version__ = "0.6.0")
├── backends.py          # Backend ABC + Claude/Codex implementations
├── checkers.py          # Verdict parsing helpers
├── cli.py               # CLI argument parsing and dispatch
├── display.py           # Rich TUI rendering
├── engine.py            # Core execution loop
├── notifications.py     # Webhook notifications
├── state.py             # Atomic state persistence
├── workflow.py          # Workflow/Phase models and loading
├── prompts/             # Built-in checker role prompts (.md)
├── templates/           # Workflow scaffolding templates
└── workflows/           # Built-in workflows (plan.yaml)
tests/
├── conftest.py          # Shared fixtures (MockBackend, etc.)
├── test_cli.py          # CLI argument parsing tests
├── test_engine.py       # Engine execution tests (largest test file)
├── test_state.py        # State persistence tests
├── test_workflow.py     # Workflow loading tests
├── test_e2e_claude.py   # E2E with Claude (needs ANTHROPIC_API_KEY)
├── test_e2e_codex.py    # E2E with Codex (needs OPENAI_API_KEY)
├── test_round2.py       # Includes, cost tracking, backoff, notifications tests
├── test_skill.py        # Claude Code skill tests
└── test_validation.py   # Workflow validation tests
skills/juvenal/SKILL.md  # Claude Code skill definition
```
