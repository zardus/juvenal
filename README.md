# Juvenal

> *Quis agit ipsos agentes?* — Who acts upon the agents?

<p align="center">
  <img src="juvenal.png" alt="Juvenal" width="200">
</p>

Juvenal is a framework for orchestrating AI coding agents through verified implementation phases. It prevents agents from cheating on success criteria, helps agents implement complex projects in phases, etc.

## The Problem

Agents such at giant problems.
This is probably only a temporary problem, but for now, an AI agent given a massive problem will fumble it.
It'll take shortcuts, lie, cheat, steal, the works.

## The Solution

There's no honor among agents!
Agent B feels no obligation to cover for some shortcut that Agent A made.
This makes an implementation-verification loop with separate agents pretty effective for catching cut corners.
When Agent B catches Agent A's shoddy work, Agent C can be spun up to implement fixes, and so on.

## How It Works

A deterministic Python runtime orchestrates AI coding agents (Claude or Codex) through alternating steps:

1. **Implementation** — an agent executes a prompt to build/modify code
2. **Verification** — separate checker agents verify the work, and can run commands when instructed to do so
3. **Bounce** — if verification fails, the pipeline bounces back (to a configurable target phase or the most recent implement phase) with failure context injected. A global bounce limit (`max_bounces`) prevents infinite loops.

The implementing agent and the checking agent are separate processes, so the implementer can't cheat by weakening tests, etc.

## Other Such Frameworks

Juvenal is conceptually similar to [ralph](https://github.com/snarktank/ralph), but it works slightly better for my exact purposes and reinventing the wheel is cheap now!

## Install

```bash
pip install -e ".[dev]"
```

## Claude Code Skill

Juvenal ships as a Claude Code plugin, so you can use it directly from Claude Code with `/juvenal`.

### Install the plugin

**From the marketplace** (pending approval):
```
/plugin install juvenal
```

**From source** (works now):
```bash
claude --plugin-dir /path/to/juvenal/plugin
```

### Usage

Once installed, invoke the skill in Claude Code:

```
/juvenal add authentication to the Flask app
```

Claude will create a Juvenal workflow for your goal and run it. You can also ask for help with workflow formats or run existing workflows.

## Quick Start

```bash
# Scaffold a workflow
juvenal init my-project

# Run a workflow
juvenal run workflow.yaml

# Generate a workflow from a goal
juvenal plan "implement a REST API with tests" -o workflow.yaml

# Plan and immediately run
juvenal do "add authentication to the Flask app"
```

## Embedded API

Juvenal exposes an embedded Python API. The repository includes a toy example at `tests/mockup.py`:

```python
import subprocess, tempfile
from juvenal.api import do, goal, plan_and_do

tmpdir = tempfile.mkdtemp()
subprocess.run(["git", "init"], cwd=tmpdir, check=True)
subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmpdir, check=True)

with goal("build a toy todo CLI", working_dir=tmpdir):
    do("write example-brief.md describing the toy todo CLI", checker="pm")
    do(
        ["create sample-interactions.md with happy-path transcripts",
         "add edge-case transcripts to sample-interactions.md"],
        checkers=["security-engineer"],
    )
    do(
        ["derive acceptance-checklist.md from the prep artifacts",
         "expand the checklist with missing coverage",
         "add persisted-state scenarios to the checklist",
         "write smoke-test.sh for shell validation"],
        checkers=["tester", "senior-tester"],
    )
    plan_and_do("build the toy todo CLI in toy_app/")
```

Run it from a repo checkout: `python -m tests.mockup`

## Workflow Formats

### YAML

```yaml
name: "my-workflow"
backend: claude
max_bounces: 999
backoff: 2.0        # exponential backoff between bounces (seconds)
max_backoff: 60.0   # cap on backoff delay
notify:
  - https://example.com/webhook

phases:
  - id: implement
    prompt: "Implement the feature."
    timeout: 300
    env:
      NODE_ENV: production
    checks:
      - prompt: |
          Run `pytest tests/ -x` from the working directory and use the result to verify the implementation.
          Do not modify code while checking.
          Emit `VERDICT: FAIL: <reason>` if the command fails; otherwise emit `VERDICT: PASS`.
      - tester                          # built-in role shorthand
      - role: senior-engineer           # role as dict
      - prompt: "Check for security."   # inline prompt
```

### Directory Convention

```
my-workflow/
  phases/
    01-setup/
      prompt.md            # implementation prompt
    02-parallel/           # "parallel" in name → parallel lane group
      feature-a/           #   each subdir is a lane
        prompt.md          #     implement phase
        check.md           #     check phase (auto-bounces to implement)
      feature-b/
        prompt.md
        check.md
    03-finish/
      prompt.md
```

Commands belong in checker prompts. `.sh` files are not auto-loaded as phases.

Lanes can also use subdirectories for more complex pipelines:

```
02-parallel/
  a/
    01-implement/
      prompt.md
    02-check-review/
      prompt.md
```

Phase IDs are derived from directory names. In simple mode (lane has `prompt.md` at root): `a`, `a~check-1`, `a~check-2`. In complex mode (subdirectories): `a~01-implement`, `a~02-check-review`.

### Bare Markdown

```bash
juvenal run task.md  # single implement phase from a .md file
```

## Phase Types

| Type | Description |
|------|-------------|
| `implement` | Agent executes a prompt to build/modify code (default) |
| `check` | Separate agent verifies work, emits `VERDICT: PASS` or `VERDICT: FAIL: reason` |
| `workflow` | Dynamic sub-workflow: plans and executes a sub-pipeline from the prompt |

### Workflow Phases

A `workflow` phase dynamically generates and executes a sub-pipeline. Useful for open-ended tasks where the exact phases aren't known ahead of time:

```yaml
- id: dynamic-feature
  type: workflow
  prompt: "Build a REST API with authentication and tests."
  max_depth: 2  # recursion depth limit (default: 3)
```

## Inline Checkers

Checks are defined inline on implement phases. Each entry can be:

- **Bare string** — built-in role shorthand
- **`role: NAME`** — agent checker with built-in role
- **`prompt: TEXT`** — agent checker with inline prompt
- **`prompt_file: PATH`** — agent checker with prompt from file

Checkers can also carry `timeout` and `env`.

```yaml
- id: implement
  prompt: "Build the feature."
  checks:
    - prompt: |
        Run `pytest tests/ -x` from the working directory and verify the result.
        Emit `VERDICT: FAIL: <reason>` on failure, otherwise emit `VERDICT: PASS`.
    - tester
    - role: senior-engineer
    - prompt: "Check for security vulnerabilities."
    - prompt_file: checkers/review.md
    - prompt: "Run `npm run lint` and emit `VERDICT: PASS` only if it succeeds."
      timeout: 60
      env:
        CI: "true"
```

## Built-in Roles

Agent checkers can use built-in verification personas:

- `tester` — runs tests, checks for build errors
- `architect` — validates design, checks for circular dependencies
- `pm` — confirms requirements are met, no TODOs remain
- `senior-tester` — checks test integrity, looks for cheating
- `senior-engineer` — reviews code quality, completeness, security

Implementer roles (via `--implementer`):

- `software-engineer` — structured implementation approach

## Bounce Targets

On verification failure, the pipeline bounces back to re-implement. Two modes:

- **`bounce_target`** (fixed): always bounces to this phase
- **`bounce_targets`** (agent-guided): checker picks which phase via `VERDICT: FAIL(target-id): reason`

```yaml
- id: review
  type: check
  bounce_targets:
    - design-experiments   # agent can bounce here
    - write-paper          # or here
```

These are mutually exclusive. If neither is set, bounces to the most recent implement phase.

## Parallel Groups

### Lanes

Each lane is a mini-pipeline (e.g., implement + check) with its own internal bounce loop. All lanes run concurrently and share the global bounce budget. The group completes when every lane passes.

```yaml
parallel_groups:
  - lanes:
      - [feature-a, check-a]
      - [feature-b, check-b]
      - [feature-c, check-c]
```

Lane constraints:
- Bounce targets must stay within their lane
- No `workflow`-type phases in lanes
- No phase in multiple lanes

### Legacy Flat Format

Run implement phases concurrently with no per-phase checking. A single failure aborts the group.

```yaml
parallel_groups:
  - phases: [independent-a, independent-b]
```

## Workflow Includes

Compose workflows from reusable pieces. Included phases and parallel groups are merged in order before the current workflow's phases:

```yaml
include:
  - shared/setup.yaml
  - shared/linting.yaml
phases:
  - id: feature
    prompt: "Build the feature."
```

Nested includes are supported. Circular includes are detected.

## Exponential Backoff

Add a delay between bounces to avoid hammering APIs:

```yaml
backoff: 2.0       # base delay in seconds (doubles each bounce)
max_backoff: 60.0  # cap
```

Or via CLI: `--backoff 2.0`

## Notifications

Get webhook notifications on pipeline completion or failure:

```yaml
notify:
  - https://hooks.slack.com/services/T.../B.../xxx
```

Or via CLI: `--notify URL` (repeatable). The webhook receives a JSON payload with workflow name, status, bounces, duration, token usage, and per-phase summaries.

## Context Preservation

By default, bounces resume the agent's session so it retains the full conversation context from the previous attempt. The failure details are sent as a follow-up message rather than re-rendering the full prompt. Use `--clear-context-on-bounce` to start a fresh session on each bounce instead.

## Token Tracking

Juvenal tracks input and output token usage per phase. Token counts are shown in the run summary and included in webhook notifications. Token data is persisted in the state file for resume scenarios.

## CLI

```
juvenal run <workflow> [--resume] [--rewind N] [--rewind-to PHASE_ID] [--phase X]
                       [--max-bounces N] [--backend claude|codex] [--dry-run]
                       [--backoff SECONDS] [--notify URL] [--working-dir DIR]
                       [--state-file PATH] [--checker SPEC] [--implementer ROLE]
                       [--clear-context-on-bounce] [-D VAR=VAL] [--serialize]
juvenal plan "goal" [-o output.yaml] [--backend claude|codex]
juvenal do "goal" [--backend claude|codex] [--max-bounces N] [-D VAR=VAL] [--serialize]
juvenal status [--state-file path]
juvenal init [directory] [--template name]
juvenal validate <workflow>
```

### Key Flags

| Flag | Description |
|------|-------------|
| `--resume` | Resume from last saved state |
| `--rewind N` | Rewind N phases back from the resume point |
| `--rewind-to ID` | Rewind to a specific phase by ID |
| `--phase ID` | Start from a specific phase |
| `--dry-run` | Print execution plan without running |
| `--checker SPEC` | Inject checker on every implement phase (role or `prompt:TEXT`). Repeatable. |
| `--implementer ROLE` | Prepend implementer role prompt to every implement phase |
| `--clear-context-on-bounce` | Start fresh agent session on bounce (default: resume session) |
| `-D VAR=VAL` | Set a Jinja2 template variable. Repeatable. |
| `--backoff SECONDS` | Exponential backoff base delay between bounces |
| `--notify URL` | Webhook URL for completion/failure notifications. Repeatable. |
| `--serialize` | Disable all parallelization (run everything sequentially) |

### Resume & Rewind

```bash
# Resume from last saved state
juvenal run workflow.yaml --resume

# Rewind 2 phases back from the resume point
juvenal run workflow.yaml --rewind 2

# Rewind to a specific phase by ID
juvenal run workflow.yaml --rewind-to setup
```

`--rewind` and `--rewind-to` implicitly load existing state (no need for `--resume`) and invalidate from the target phase onward so everything from that point gets re-executed.

### Checker Injection

Inject checkers at the CLI without modifying the workflow file:

```bash
# Add a tester role checker to every implement phase
juvenal run workflow.yaml --checker tester

# Add a checker with explicit instructions
juvenal run workflow.yaml --checker "prompt:Run pytest tests/ -x and emit VERDICT based on the result."

# Add both
juvenal run workflow.yaml --checker tester --checker "prompt:Run make lint and emit VERDICT based on the result."
```

## License

MIT
