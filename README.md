# Juvenal

> *Quis custodiet ipsos custodes?* — Who guards the agents?

Juvenal is a framework for orchestrating AI coding agents through verified implementation phases. It prevents agents from cheating on success criteria by separating implementation from verification.

## How It Works

A non-agentic Python script orchestrates AI coding agents (Claude or Codex) through alternating steps:

1. **Implementation** — an agent executes a prompt to build/modify code
2. **Verification** — separate checkers (scripts, agents, or both) verify the work
3. **Retry** — if verification fails, the failure context is injected and the agent tries again

The implementing agent and the checking agent are separate processes, so the implementer can't cheat by weakening tests.

## Install

```bash
pip install -e ".[dev]"
```

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
max_retries: 999

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
juvenal run <workflow> [--resume] [--phase X] [--max-retries N] [--backend claude|codex] [--dry-run]
juvenal plan "goal" [-o output.yaml] [--backend claude|codex]
juvenal do "goal" [--backend claude|codex] [--max-retries N]
juvenal status [--state-file path]
juvenal init [directory] [--template name]
```

## License

MIT
