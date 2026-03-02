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
    - "workflow": dynamic sub-workflow (plan + execute)
    """

    id: str
    type: str = "implement"  # "implement", "check", "script", "workflow"
    prompt: str = ""  # for implement, check, and workflow
    run: str | None = None  # shell command for script
    role: str | None = None  # built-in role name for check
    bounce_target: str | None = None  # fixed phase to bounce back to on failure
    bounce_targets: list[str] = field(default_factory=list)  # agent-guided: checker picks from this list
    timeout: int | None = None  # timeout in seconds (None = no limit)
    env: dict[str, str] = field(default_factory=dict)  # environment variables for the phase
    max_depth: int | None = None  # recursion depth limit for workflow phases

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
    max_bounces: int = 999
    parallel_groups: list[list[str]] = field(default_factory=list)
    backoff: float = 0.0  # base backoff delay in seconds between bounces (0 = no backoff)
    max_backoff: float = 60.0  # maximum backoff delay cap in seconds
    notify: list[str] = field(default_factory=list)  # webhook URLs for completion/failure notifications


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
    return _load_yaml_with_includes(path, set())


def _load_yaml_with_includes(path: Path, seen: set[str]) -> Workflow:
    """Load workflow from a YAML file, resolving includes to prevent cycles."""
    resolved = str(path.resolve())
    if resolved in seen:
        raise ValueError(f"Circular include detected: {path}")
    seen.add(resolved)

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid workflow YAML in {path}: expected a mapping, got {type(data).__name__}")

    # Resolve includes first — insert included phases before this workflow's phases
    included_phases: list[Phase] = []
    included_parallel_groups: list[list[str]] = []
    for include_path_str in data.get("include", []):
        include_path = path.parent / include_path_str
        if not include_path.exists():
            raise FileNotFoundError(f"Included workflow not found: {include_path} (referenced from {path})")
        included_wf = _load_yaml_with_includes(include_path, seen)
        included_phases.extend(included_wf.phases)
        included_parallel_groups.extend(included_wf.parallel_groups)

    phases = list(included_phases)
    for phase_data in data.get("phases", []):
        prompt = phase_data.get("prompt", "")
        if not prompt and phase_data.get("prompt_file"):
            prompt_path = path.parent / phase_data["prompt_file"]
            prompt = prompt_path.read_text()
        bounce_target = phase_data.get("bounce_target")
        bounce_targets = phase_data.get("bounce_targets", [])
        if bounce_target and bounce_targets:
            raise ValueError(f"Phase '{phase_data['id']}': bounce_target and bounce_targets are mutually exclusive")
        phases.append(
            Phase(
                id=phase_data["id"],
                type=phase_data.get("type", "implement"),
                prompt=prompt,
                run=phase_data.get("run"),
                role=phase_data.get("role"),
                bounce_target=bounce_target,
                bounce_targets=bounce_targets,
                timeout=phase_data.get("timeout"),
                env=phase_data.get("env", {}),
                max_depth=phase_data.get("max_depth"),
            )
        )

    parallel_groups = list(included_parallel_groups)
    for pg in data.get("parallel_groups", []):
        parallel_groups.append(pg.get("phases", []))

    return Workflow(
        name=data.get("name", path.stem),
        phases=phases,
        backend=data.get("backend", "codex"),
        working_dir=data.get("working_dir", "."),
        max_bounces=data.get("max_bounces", data.get("max_retries", 999)),
        parallel_groups=parallel_groups,
        backoff=float(data.get("backoff", 0)),
        max_backoff=float(data.get("max_backoff", 60)),
        notify=data.get("notify", []),
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
        max_bounces=overrides.get("max_bounces", overrides.get("max_retries", 999)),
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


VALID_PHASE_TYPES = {"implement", "check", "script", "workflow"}
VALID_ROLES = {"tester", "architect", "pm", "senior-tester", "senior-engineer"}


def validate_workflow(workflow: Workflow) -> list[str]:
    """Validate a workflow definition and return a list of errors.

    Checks:
    - Phase IDs are unique
    - Phase types are valid
    - bounce_target references existing phase IDs
    - implement/check phases have a prompt, prompt_file, or role
    - script phases have a run command
    - Parallel group phase IDs reference existing phases
    - Check phases with roles reference valid built-in roles
    """
    errors: list[str] = []
    phase_ids = set()
    all_ids = {p.id for p in workflow.phases}

    for phase in workflow.phases:
        # Duplicate ID check
        if phase.id in phase_ids:
            errors.append(f"Duplicate phase ID: {phase.id!r}")
        phase_ids.add(phase.id)

        # Valid type
        if phase.type not in VALID_PHASE_TYPES:
            errors.append(f"Phase {phase.id!r}: invalid type {phase.type!r} (must be one of {VALID_PHASE_TYPES})")

        # bounce_target references existing phase
        if phase.bounce_target and phase.bounce_target not in all_ids:
            errors.append(f"Phase {phase.id!r}: bounce_target {phase.bounce_target!r} does not match any phase ID")

        # bounce_targets all reference existing phases
        for bt in phase.bounce_targets:
            if bt not in all_ids:
                errors.append(f"Phase {phase.id!r}: bounce_targets entry {bt!r} does not match any phase ID")

        # Type-specific validation
        if phase.type == "implement" and not phase.prompt:
            errors.append(f"Phase {phase.id!r}: implement phase has no prompt")
        if phase.type == "check" and not phase.prompt and not phase.role:
            errors.append(f"Phase {phase.id!r}: check phase has no prompt or role")
        if phase.type == "script" and not phase.run:
            errors.append(f"Phase {phase.id!r}: script phase has no run command")
        if phase.type == "workflow":
            if not phase.prompt:
                errors.append(f"Phase {phase.id!r}: workflow phase has no prompt")
            if phase.run:
                errors.append(f"Phase {phase.id!r}: workflow phase must not have 'run'")
            if phase.role:
                errors.append(f"Phase {phase.id!r}: workflow phase must not have 'role'")
        if phase.max_depth is not None and phase.max_depth < 1:
            errors.append(f"Phase {phase.id!r}: max_depth must be >= 1, got {phase.max_depth}")

        # Role validation
        if phase.role and phase.role not in VALID_ROLES:
            errors.append(f"Phase {phase.id!r}: unknown role {phase.role!r} (valid: {sorted(VALID_ROLES)})")

    # Parallel group validation
    for i, group in enumerate(workflow.parallel_groups):
        for pid in group:
            if pid not in all_ids:
                errors.append(f"Parallel group {i}: phase ID {pid!r} does not match any phase")

    # Backoff validation
    if workflow.backoff < 0:
        errors.append(f"backoff must be non-negative, got {workflow.backoff}")
    if workflow.max_backoff < 0:
        errors.append(f"max_backoff must be non-negative, got {workflow.max_backoff}")

    # Notify URL validation
    for url in workflow.notify:
        if not url.startswith(("http://", "https://")):
            errors.append(f"notify URL must start with http:// or https://, got {url!r}")

    return errors


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
