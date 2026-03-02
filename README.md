# Juvenal

> *Quis custodiet ipsos agentes?* — Who guards the agents?

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

A non-agentic Python script orchestrates AI coding agents (Claude or Codex) through alternating steps:

1. **Implementation** — an agent executes a prompt to build/modify code
2. **Verification** — separate checkers (scripts, agents, or both) verify the work
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

## Workflow Formats

### YAML

```yaml
name: "my-workflow"
backend: claude
max_bounces: 999

phases:
  - id: implement
    prompt: "Implement the feature."
    checkers:
      - type: script
        run: "pytest tests/ -x"
      - type: agent
        role: tester
```

### Directory Convention

```
my-workflow/
  phases/
    01-setup/
      prompt.md            # implementation prompt
      check-build.sh       # script checker (exit 0 = pass)
      check-quality.md     # agent checker
    02-implement/
      prompt.md
      check-tests.sh       # paired with .md = composite
      check-tests.md       # gets {script_output} injected
```

### Bare Markdown

```
phases/
  01-setup.md              # single phase, default tester checker
```

## Checker Types

| Type | Description |
|------|-------------|
| `script` | Shell command; exit 0 = PASS, nonzero = FAIL |
| `agent` | AI agent that emits `VERDICT: PASS` or `VERDICT: FAIL: reason` |
| `composite` | Script runs first, output fed to agent via `{script_output}` |

## Built-in Roles

Agent checkers can use built-in verification personas:

- `tester` — runs tests, checks for build errors
- `architect` — validates design, checks for circular dependencies
- `pm` — confirms requirements are met, no TODOs remain
- `senior-tester` — checks test integrity, looks for cheating
- `senior-engineer` — reviews code quality, completeness, security

## CLI

```
juvenal run <workflow> [--resume] [--rewind N] [--rewind-to PHASE_ID] [--phase X]
                       [--max-bounces N] [--backend claude|codex] [--dry-run]
                       [--backoff SECONDS] [--notify WEBHOOK_URL]
                       [--working-dir DIR] [--state-file PATH]
juvenal plan "goal" [-o output.yaml] [--backend claude|codex]
juvenal do "goal" [--backend claude|codex] [--max-bounces N]
juvenal status [--state-file path]
juvenal init [directory] [--template name]
juvenal validate <workflow>
```

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

## License

MIT
