"""Workflow loading from YAML, directory convention, and bare .md files."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Phase:
    """A single phase in the workflow pipeline.

    Every phase is exactly one thing:
    - "implement": agentic implementation (default)
    - "check": agentic checker (parses VERDICT)
    - "script": non-agentic shell command
    """

    id: str
    type: str = "implement"  # "implement", "check", "script"
    prompt: str = ""  # for implement and check
    run: str | None = None  # shell command for script
    role: str | None = None  # built-in role name for check
    bounce_target: str | None = None  # phase to bounce back to on failure

    def render_prompt(self, failure_context: str = "") -> str:
        """Render the implementation prompt, injecting failure context on retry."""
        text = self.prompt
        if failure_context:
            text += (
                "\n\nIMPORTANT: A previous attempt failed verification.\n"
                f"Failure details:\n\n{failure_context}\n\n"
                "Fix these issues in your implementation.\n"
            )
        return text

    def render_check_prompt(self) -> str:
        """Render the checker prompt for check phases."""
        if self.prompt:
            return self.prompt
        if self.role:
            return _load_role_prompt(self.role)
        return ""


@dataclass
class Workflow:
    """Complete workflow definition."""

    name: str
    phases: list[Phase]
    backend: str = "codex"
    working_dir: str = "."
    max_retries: int = 999
    parallel_groups: list[list[str]] = field(default_factory=list)


def load_workflow(path: str | Path) -> Workflow:
    """Load a workflow from a YAML file, directory, or bare .md file.

    Dispatch rules:
    - .yaml/.yml file -> YAML workflow
    - directory with phases/ subdir -> directory convention
    - directory with .md files -> bare .md convention
    - single .md file -> single-phase workflow with default checker
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Workflow path does not exist: {p}")

    if p.is_file():
        if p.suffix in (".yaml", ".yml"):
            return _load_yaml(p)
        if p.suffix == ".md":
            return _load_bare_file(p)
        raise ValueError(f"Unsupported file type: {p.suffix}")

    if p.is_dir():
        # Check for workflow.yaml first
        yaml_path = p / "workflow.yaml"
        if yaml_path.exists():
            return _load_yaml(yaml_path)
        yml_path = p / "workflow.yml"
        if yml_path.exists():
            return _load_yaml(yml_path)
        # Check for phases/ directory
        phases_dir = p / "phases"
        if phases_dir.is_dir():
            return _load_directory(p, phases_dir)
        # Treat directory itself as phases dir if it contains .md or subdirs
        return _load_directory(p, p)

    raise ValueError(f"Cannot load workflow from: {p}")


def _load_yaml(path: Path) -> Workflow:
    """Load workflow from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid workflow YAML in {path}: expected a mapping, got {type(data).__name__}")

    phases = []
    for phase_data in data.get("phases", []):
        prompt = phase_data.get("prompt", "")
        if not prompt and phase_data.get("prompt_file"):
            prompt_path = path.parent / phase_data["prompt_file"]
            prompt = prompt_path.read_text()
        phases.append(
            Phase(
                id=phase_data["id"],
                type=phase_data.get("type", "implement"),
                prompt=prompt,
                run=phase_data.get("run"),
                role=phase_data.get("role"),
                bounce_target=phase_data.get("bounce_target"),
            )
        )

    parallel_groups = []
    for pg in data.get("parallel_groups", []):
        parallel_groups.append(pg.get("phases", []))

    return Workflow(
        name=data.get("name", path.stem),
        phases=phases,
        backend=data.get("backend", "codex"),
        working_dir=data.get("working_dir", "."),
        max_retries=data.get("max_retries", 999),
        parallel_groups=parallel_groups,
    )


def _load_directory(root: Path, phases_dir: Path) -> Workflow:
    """Load workflow from directory convention.

    Detection:
    - Subdirectory with prompt.md and NO check- prefix -> implement
    - Subdirectory with prompt.md and check- prefix -> check
    - .sh file at top level -> script
    - Bare .md file at top level -> implement + auto check phase
    """
    phases = []
    entries = sorted(phases_dir.iterdir())

    for entry in entries:
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            # Script phase
            phase_id = entry.stem
            phases.append(Phase(id=phase_id, type="script", run=str(entry)))
        elif entry.is_file() and entry.suffix == ".md":
            # Bare .md file = implement phase + auto check phase
            phase_id = entry.stem
            prompt = entry.read_text()
            phases.append(Phase(id=phase_id, type="implement", prompt=prompt))
            phases.append(Phase(id=f"{phase_id}-check", type="check", role="tester"))
        elif entry.is_dir():
            loaded = _load_phase_dir(entry)
            if loaded:
                phases.extend(loaded)

    # Merge overrides from workflow.yaml if present
    overrides = {}
    for name in ("workflow.yaml", "workflow.yml"):
        override_path = root / name
        if override_path.exists():
            with open(override_path) as f:
                overrides = yaml.safe_load(f) or {}
            break

    return Workflow(
        name=overrides.get("name", root.name),
        phases=phases,
        backend=overrides.get("backend", "claude"),
        working_dir=overrides.get("working_dir", "."),
        max_retries=overrides.get("max_retries", 999),
        parallel_groups=[pg.get("phases", []) for pg in overrides.get("parallel_groups", [])],
    )


def _load_phase_dir(phase_dir: Path) -> list[Phase] | None:
    """Load phase(s) from a directory.

    - check- prefix -> check phase
    - no prefix -> implement phase
    """
    prompt_path = phase_dir / "prompt.md"
    if not prompt_path.exists():
        return None
    prompt = prompt_path.read_text()

    if phase_dir.name.startswith("check-") or "-check-" in phase_dir.name:
        # Check phase
        return [Phase(id=phase_dir.name, type="check", prompt=prompt)]
    else:
        # Implement phase
        return [Phase(id=phase_dir.name, type="implement", prompt=prompt)]


def _load_bare_file(path: Path) -> Workflow:
    """Load a single .md file as a two-phase workflow: implement + check."""
    prompt = path.read_text()
    phases = [
        Phase(id=path.stem, type="implement", prompt=prompt),
        Phase(id=f"{path.stem}-check", type="check", role="tester"),
    ]
    return Workflow(name=path.stem, phases=phases)


def _load_role_prompt(role: str) -> str:
    """Load a built-in role prompt from the prompts directory."""
    role_file = f"checker-{role}.md"
    prompts_dir = Path(__file__).parent / "prompts"
    role_path = prompts_dir / role_file
    if role_path.exists():
        return role_path.read_text()
    raise FileNotFoundError(f"Built-in role prompt not found: {role_file}")


def scaffold_workflow(directory: str, template: str = "default") -> None:
    """Scaffold a workflow directory from a template."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    template_dir = Path(__file__).parent / "templates" / template
    if not template_dir.exists():
        raise FileNotFoundError(f"Template not found: {template}")

    for src in template_dir.rglob("*"):
        if src.is_file():
            rel = src.relative_to(template_dir)
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
