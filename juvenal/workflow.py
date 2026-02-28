"""Workflow loading from YAML, directory convention, and bare .md files."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Checker:
    """A single verification step within a phase."""

    name: str
    type: str  # "script", "agent", "composite"
    run: str | None = None  # shell command for script/composite
    role: str | None = None  # built-in role name for agent
    prompt: str | None = None  # inline prompt for agent/composite

    def render_prompt(self, script_output: str = "") -> str:
        """Render the checker prompt, injecting script_output for composite."""
        if self.prompt:
            return self.prompt.replace("{script_output}", script_output)
        if self.role:
            return _load_role_prompt(self.role)
        return ""


@dataclass
class Phase:
    """A single phase in the workflow pipeline."""

    id: str
    prompt: str
    checkers: list[Checker] = field(default_factory=list)

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


@dataclass
class Workflow:
    """Complete workflow definition."""

    name: str
    phases: list[Phase]
    backend: str = "claude"
    working_dir: str = "."
    max_retries: int = 999
    bounce_targets: dict[str, str] = field(default_factory=dict)
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
        checkers = []
        for checker_data in phase_data.get("checkers", []):
            checker_prompt = checker_data.get("prompt")
            if not checker_prompt and checker_data.get("prompt_file"):
                prompt_path = path.parent / checker_data["prompt_file"]
                checker_prompt = prompt_path.read_text()
            checkers.append(
                Checker(
                    name=checker_data.get("name", _checker_name(checker_data)),
                    type=checker_data["type"],
                    run=checker_data.get("run"),
                    role=checker_data.get("role"),
                    prompt=checker_prompt,
                )
            )
        prompt = phase_data.get("prompt", "")
        if not prompt and phase_data.get("prompt_file"):
            prompt_path = path.parent / phase_data["prompt_file"]
            prompt = prompt_path.read_text()
        phases.append(Phase(id=phase_data["id"], prompt=prompt, checkers=checkers))

    parallel_groups = []
    for pg in data.get("parallel_groups", []):
        parallel_groups.append(pg.get("phases", []))

    return Workflow(
        name=data.get("name", path.stem),
        phases=phases,
        backend=data.get("backend", "claude"),
        working_dir=data.get("working_dir", "."),
        max_retries=data.get("max_retries", 999),
        bounce_targets=data.get("bounce_targets", {}),
        parallel_groups=parallel_groups,
    )


def _load_directory(root: Path, phases_dir: Path) -> Workflow:
    """Load workflow from directory convention."""
    phases = []
    entries = sorted(phases_dir.iterdir())

    for entry in entries:
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".md":
            # Bare .md file = single phase with default tester checker
            phase_id = entry.stem
            prompt = entry.read_text()
            checkers = [
                Checker(name="tester", type="agent", role="tester"),
            ]
            phases.append(Phase(id=phase_id, prompt=prompt, checkers=checkers))
        elif entry.is_dir():
            phase = _load_phase_dir(entry)
            if phase:
                phases.append(phase)

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
        bounce_targets=overrides.get("bounce_targets", {}),
        parallel_groups=[pg.get("phases", []) for pg in overrides.get("parallel_groups", [])],
    )


def _load_phase_dir(phase_dir: Path) -> Phase | None:
    """Load a single phase from a directory."""
    prompt_path = phase_dir / "prompt.md"
    if not prompt_path.exists():
        return None
    prompt = prompt_path.read_text()

    # Discover checkers
    checkers = []
    check_files = sorted(phase_dir.glob("check-*"))

    # Group by base name for composite detection
    sh_files: dict[str, Path] = {}
    md_files: dict[str, Path] = {}
    for f in check_files:
        base = f.stem  # e.g., "check-tests"
        if f.suffix == ".sh":
            sh_files[base] = f
        elif f.suffix == ".md":
            md_files[base] = f

    # Create checkers
    seen = set()
    for base in sorted(set(list(sh_files.keys()) + list(md_files.keys()))):
        if base in seen:
            continue
        seen.add(base)
        has_sh = base in sh_files
        has_md = base in md_files

        if has_sh and has_md:
            # Composite checker
            checkers.append(
                Checker(
                    name=base,
                    type="composite",
                    run=str(sh_files[base]),
                    prompt=md_files[base].read_text(),
                )
            )
        elif has_sh:
            # Script checker
            checkers.append(Checker(name=base, type="script", run=str(sh_files[base])))
        elif has_md:
            # Agent checker
            checkers.append(Checker(name=base, type="agent", prompt=md_files[base].read_text()))

    # If no checkers found, add a default tester
    if not checkers:
        checkers.append(Checker(name="tester", type="agent", role="tester"))

    return Phase(id=phase_dir.name, prompt=prompt, checkers=checkers)


def _load_bare_file(path: Path) -> Workflow:
    """Load a single .md file as a single-phase workflow."""
    prompt = path.read_text()
    phase = Phase(
        id=path.stem,
        prompt=prompt,
        checkers=[Checker(name="tester", type="agent", role="tester")],
    )
    return Workflow(name=path.stem, phases=[phase])


def _checker_name(checker_data: dict) -> str:
    """Generate a name for a checker from its data."""
    if checker_data.get("role"):
        return checker_data["role"]
    if checker_data.get("run"):
        cmd = checker_data["run"]
        return cmd.split()[0] if cmd else "script"
    return checker_data.get("type", "checker")


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
