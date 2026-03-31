"""Workflow loading from YAML, directory convention, and bare .md files."""

from __future__ import annotations

import itertools
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import TemplateSyntaxError, Undefined, meta
from jinja2.sandbox import ImmutableSandboxedEnvironment

_UNRESOLVED_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\b")
_CONTROL_EXPR_RE = re.compile(
    r"{%\s*(?:if|elif)\s+(.+?)\s*%}|{%\s*for\s+(.+?)\s+in\s+(.+?)\s*%}|{{[^{}]*?\s+if\s+(.+?)\s+else\b", re.DOTALL
)
_SKIP_CONTROL_EXPR_RE = re.compile(r"(?:\w+\s+is\s+(?:not\s+)?(?:defined|undefined))")
_OPTIONAL_OUTPUT_RE = re.compile(
    r"{%\s*if\s+([A-Za-z_][A-Za-z0-9_]*)\s+is\s+(?:undefined|not\s+defined)(?:\s+or\s+\1)?\s*%}.*?{{\s*\1\b", re.DOTALL
)


class PreservePlaceholderUndefined(Undefined):
    __slots__ = ()
    __str__ = lambda self: f"{{{{{self._undefined_name or 'undefined'}}}}}"  # noqa: E731
    __getitem__ = lambda self, key: type(self)(name=f"{self._undefined_name or 'undefined'}[{key!r}]")  # noqa: E731


class TemplateRenderError(ValueError): ...


class _Sandbox(ImmutableSandboxedEnvironment):
    getattr = lambda self, obj, attr: (  # noqa: E731
        type(obj)(name=f"{obj._undefined_name or 'undefined'}.{attr}")
        if isinstance(obj, Undefined)
        else super(_Sandbox, self).getattr(obj, attr)
    )
    is_safe_attribute = lambda self, obj, attr, value: isinstance(obj, dict) and attr in {"get", "items", "keys", "values"} and super(_Sandbox, self).is_safe_attribute(obj, attr, value)  # noqa: E501,E731  # fmt: skip
    is_safe_callable = lambda self, obj: any(obj is value for value in _JINJA_ENV.globals.values()) or type(obj).__name__ == "Macro" or isinstance(getattr(obj, "__self__", None), dict) and getattr(obj, "__name__", None) in {"get", "items", "keys", "values", "<lambda>"}  # noqa: E501,E731  # fmt: skip


_SafeTemplateList, _SafeTemplateDict = type("_SafeTemplateList", (list,), {"__getitem__": lambda self, key: PreservePlaceholderUndefined(name=key._undefined_name) if isinstance(key, Undefined) else list.__getitem__(self, key)}), type("_SafeTemplateDict", (dict,), {"__getitem__": lambda self, key: PreservePlaceholderUndefined(name=key._undefined_name) if isinstance(key, Undefined) else dict.__getitem__(self, key), "get": lambda self, key, default=None: PreservePlaceholderUndefined(name=key._undefined_name) if isinstance(key, Undefined) else dict.get(self, key, default)})  # noqa: E501,E731  # fmt: skip
_safe_template_value = lambda value, name="undefined": value if value is None or isinstance(value, (bool, int, float, str, Undefined)) else _SafeTemplateDict({k: _safe_template_value(v, k if isinstance(k, str) else name) for k, v in value.items()}) if isinstance(value, dict) else _SafeTemplateList(_safe_template_value(v, name) for v in value) if isinstance(value, (list, tuple)) else PreservePlaceholderUndefined(name=name); _JINJA_ENV = _Sandbox(autoescape=False, keep_trailing_newline=True, undefined=PreservePlaceholderUndefined); template_vars = lambda text: set() if not text else meta.find_undeclared_variables(_JINJA_ENV.parse(text))  # noqa: E501,E702,E731  # fmt: skip


def apply_vars(text: str, vars: dict[str, str] | None) -> str:
    if vars is None:
        return text
    try:
        return _JINJA_ENV.from_string(text).render({k: _safe_template_value(v, k) for k, v in vars.items()})
    except Exception as exc:
        raise TemplateRenderError(str(exc)) from exc


def _sub_vars(text: str, vars: dict[str, str]) -> str:
    replaceable = template_vars(text) & set(vars)
    if not replaceable:
        return text
    substituted = "".join(
        repr(vars[value]) if token_type == "name" and value in replaceable else value
        for _, token_type, value in _JINJA_ENV.lex(text)
    )
    try:
        return apply_vars(substituted, vars) if template_vars(substituted) <= set(vars) else substituted
    except (TemplateSyntaxError, TemplateRenderError):
        return substituted


_unresolved_template_vars = lambda text, vars: template_vars(text) & set(_UNRESOLVED_TEMPLATE_VAR_RE.findall(apply_vars(text, vars)))  # noqa: E501,E731  # fmt: skip


def _control_template_vars(text: str, defined_vars: dict[str, object]) -> set[str]:
    found, guarded = set(), set(re.findall(r"{%\s*if\s+(\w+)\s+is\s+(?:undefined|not\s+defined)\s*%}", text := "".join('""' if t == "string" else v for _, t, v in _JINJA_ENV.lex(text) if t not in {"data", "comment_begin", "comment", "comment_end", "raw_begin", "raw_end"})))  # noqa: E501  # fmt: skip
    for if_expr, loop_var, loop_expr, cond_expr in _CONTROL_EXPR_RE.findall(text):
        expr = re.sub(r"\((\w+\s+is\s+(?:not\s+)?(?:defined|undefined))\)", r"\1", (if_expr or loop_expr or cond_expr).strip())  # noqa: E501  # fmt: skip
        guard = re.fullmatch(r"(\w+)\s+is\s+(?:(?:not\s+)?undefined\s+or|defined\s+and)\s+(.+)", expr)
        expr = re.sub(r"^(\w+)\s+is\s+defined\s+or\s+(.+)$", lambda m: "" if m.group(1) in defined_vars else m.group(2), re.sub(r"^(\w+)\s+(and|or)\s+(.+)$", lambda m: "" if m.group(1) in defined_vars and ((defined_vars[m.group(1)] and m.group(2) == "or") or (not defined_vars[m.group(1)] and m.group(2) == "and")) else m.group(3) if m.group(1) in defined_vars else m.group(0), re.sub(r"^(?:[Ff]alse\s+and|[Tt]rue\s+or)\b.*$", "", re.sub(r"^(?:[Tt]rue\s+and|[Ff]alse\s+or)\s+(.+)$", r"\1", "" if guard and guard.group(1) not in defined_vars else guard.group(2) if guard else expr))))  # noqa: E501  # fmt: skip
        if expr and not _SKIP_CONTROL_EXPR_RE.fullmatch(expr):
            found.update(template_vars(f"{{{{ {expr} }}}}") - set(re.findall(r"\w+", loop_var)) - set(re.findall(r"(\w+)\s*\|\s*(?:default|d)\b", expr)))  # noqa: E501  # fmt: skip
    return found - guarded


_optional_output_vars = lambda text: {m.group(1) for m in _OPTIONAL_OUTPUT_RE.finditer(text)}  # noqa: E731


@dataclass
class Phase:
    """A single phase in the workflow pipeline.

    Every phase is exactly one thing:
    - "implement": agentic implementation (default)
    - "check": agentic checker (parses VERDICT)
    - "script": non-agentic shell command
    - "workflow": sub-workflow (dynamic via prompt, or static via workflow_file/workflow_dir)
    """

    id: str
    type: str = "implement"  # "implement", "check", "script", "workflow"
    prompt: str = ""  # for implement, check, and workflow (dynamic)
    run: str | None = None  # shell command for script
    role: str | None = None  # built-in role name for check
    bounce_target: str | None = None  # fixed phase to bounce back to on failure
    bounce_targets: list[str] = field(default_factory=list)  # agent-guided: checker picks from this list
    timeout: int | None = None  # timeout in seconds (None = no limit)
    env: dict[str, str] = field(default_factory=dict)  # environment variables for the phase
    interactive: bool = False  # launch interactive TUI session (claude only)
    max_depth: int | None = None  # recursion depth limit for workflow phases
    workflow_file: str | None = None  # path to static sub-workflow YAML (resolved at load time)
    workflow_dir: str | None = None  # path to static sub-workflow directory (resolved at load time)

    def render_prompt(self, failure_context: str = "", vars: dict[str, str] | None = None) -> str:
        """Render the implementation prompt, injecting failure context on retry."""
        text = self.prompt
        if vars is not None:
            text = apply_vars(text, vars)
        if failure_context:
            text += (
                "\n\nIMPORTANT: A previous attempt failed verification.\n"
                f"Failure details:\n\n{failure_context}\n\n"
                "Fix these issues in your implementation.\n"
            )
        return text

    def render_check_prompt(self, vars: dict[str, str] | None = None) -> str:
        """Render the checker prompt for check phases."""
        if self.prompt:
            text = self.prompt
            if vars is not None:
                text = apply_vars(text, vars)
            return text
        if self.role:
            return _load_role_prompt(self.role)
        return ""


@dataclass
class ParallelGroup:
    """A group of phases to run in parallel.

    Two formats:
    - Legacy flat: phases = ["a", "b"] — run implement phases concurrently, no per-phase checking
    - Lane: lanes = [["a", "check_a"], ["b", "check_b"]] — each lane is a mini-pipeline with internal bounce
    """

    phases: list[str] = field(default_factory=list)
    lanes: list[list[str]] = field(default_factory=list)

    def is_lane_group(self) -> bool:
        """True if this group uses lanes (not legacy flat format)."""
        return len(self.lanes) > 0

    def all_phase_ids(self) -> list[str]:
        """All phase IDs referenced by this group."""
        if self.is_lane_group():
            return [pid for lane in self.lanes for pid in lane]
        return list(self.phases)

    def first_phase_id(self) -> str:
        """First phase ID in this group (for main loop skip logic)."""
        if self.is_lane_group():
            return self.lanes[0][0]
        return self.phases[0]

    def last_phase_id(self) -> str:
        """Last phase ID in this group (for main loop skip logic)."""
        if self.is_lane_group():
            return self.lanes[-1][-1]
        return self.phases[-1]


@dataclass
class Workflow:
    """Complete workflow definition."""

    name: str
    phases: list[Phase]
    backend: str = "codex"
    working_dir: str = "."
    max_bounces: int = 999
    parallel_groups: list[ParallelGroup] = field(default_factory=list)
    backoff: float = 0.0  # base backoff delay in seconds between bounces (0 = no backoff)
    max_backoff: float = 60.0  # maximum backoff delay cap in seconds
    notify: list[str] = field(default_factory=list)  # webhook URLs for completion/failure notifications
    vars: dict[str, str] = field(default_factory=dict)  # template variables for {{VAR}} substitution


def load_workflow(path: str | Path) -> Workflow:
    """Load a workflow from a YAML file, directory, or bare .md file.

    Dispatch rules:
    - .yaml/.yml file -> YAML workflow
    - directory with phases/ subdir -> directory convention
    - directory with .md files -> bare .md convention
    - single .md file -> single-phase workflow (no checker unless --checker is used)
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
    included_parallel_groups: list[ParallelGroup] = []
    included_vars: dict[str, str] = {}
    for include_path_str in data.get("include", []):
        include_path = path.parent / include_path_str
        if not include_path.exists():
            raise FileNotFoundError(f"Included workflow not found: {include_path} (referenced from {path})")
        included_wf = _load_yaml_with_includes(include_path, seen)
        included_phases.extend(included_wf.phases)
        included_parallel_groups.extend(included_wf.parallel_groups)
        included_vars.update(included_wf.vars)

    _VALID_PHASE_KEYS = {
        "id",
        "type",
        "prompt",
        "prompt_file",
        "run",
        "role",
        "bounce_target",
        "bounce_targets",
        "timeout",
        "env",
        "interactive",
        "max_depth",
        "checks",
        "workflow_file",
        "workflow_dir",
    }

    phases = list(included_phases)
    for i, phase_data in enumerate(data.get("phases", [])):
        if "id" not in phase_data:
            raise ValueError(f"Phase {i} in {path}: missing required 'id' field")
        unknown = set(phase_data.keys()) - _VALID_PHASE_KEYS
        if unknown:
            import warnings

            warnings.warn(f"Phase '{phase_data['id']}': unknown keys {unknown} (typo?)", stacklevel=2)
        prompt = phase_data.get("prompt", "")
        if not prompt and phase_data.get("prompt_file"):
            prompt_path = path.parent / phase_data["prompt_file"]
            prompt = prompt_path.read_text()
        bounce_target = phase_data.get("bounce_target")
        bounce_targets = phase_data.get("bounce_targets", [])
        if bounce_target and bounce_targets:
            raise ValueError(f"Phase '{phase_data['id']}': bounce_target and bounce_targets are mutually exclusive")
        # Resolve workflow_file/workflow_dir relative to the YAML file
        wf_file = phase_data.get("workflow_file")
        wf_dir = phase_data.get("workflow_dir")
        if wf_file and wf_dir:
            raise ValueError(f"Phase '{phase_data['id']}': workflow_file and workflow_dir are mutually exclusive")
        if wf_file:
            wf_file = str((path.parent / wf_file).resolve())
        if wf_dir:
            wf_dir = str((path.parent / wf_dir).resolve())

        phase = Phase(
            id=phase_data["id"],
            type=phase_data.get("type", "implement"),
            prompt=prompt,
            run=phase_data.get("run"),
            role=phase_data.get("role"),
            bounce_target=bounce_target,
            bounce_targets=bounce_targets,
            timeout=phase_data.get("timeout"),
            env=phase_data.get("env", {}),
            interactive=phase_data.get("interactive", False),
            max_depth=phase_data.get("max_depth"),
            workflow_file=wf_file,
            workflow_dir=wf_dir,
        )
        phases.append(phase)

        if "checks" in phase_data:
            phases.extend(_expand_checkers(phase.id, phase_data["checks"], path.parent))

    parallel_groups = list(included_parallel_groups)
    for pg in data.get("parallel_groups", []):
        if "lanes" in pg:
            parallel_groups.append(ParallelGroup(lanes=pg["lanes"]))
        else:
            parallel_groups.append(ParallelGroup(phases=pg.get("phases", [])))

    # Merge vars: included defaults, then this workflow's vars override
    merged_vars = dict(included_vars)
    merged_vars.update(data.get("vars", {}))

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
        vars=merged_vars,
    )


def _load_directory(root: Path, phases_dir: Path) -> Workflow:
    """Load workflow from directory convention.

    Detection:
    - Subdirectory with prompt.md and NO check- prefix -> implement
    - Subdirectory with prompt.md and check- prefix -> check
    - Subdirectory with "parallel" in name -> parallel lane group
    - .sh file at top level -> script
    - Bare .md file at top level -> implement + auto check phase
    """
    phases = []
    parallel_groups: list[ParallelGroup] = []
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
        elif entry.is_dir():
            if "parallel" in entry.name:
                par_phases, par_group = _load_parallel_dir(entry)
                phases.extend(par_phases)
                parallel_groups.append(par_group)
            else:
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

    override_groups = [
        ParallelGroup(lanes=pg["lanes"]) if "lanes" in pg else ParallelGroup(phases=pg.get("phases", []))
        for pg in overrides.get("parallel_groups", [])
    ]

    return Workflow(
        name=overrides.get("name", root.name),
        phases=phases,
        backend=overrides.get("backend", "claude"),
        working_dir=overrides.get("working_dir", "."),
        max_bounces=overrides.get("max_bounces", overrides.get("max_retries", 999)),
        parallel_groups=parallel_groups + override_groups,
        vars=overrides.get("vars", {}),
    )


def _load_phase_dir(phase_dir: Path) -> list[Phase] | None:
    """Load phase(s) from a directory.

    - check- prefix or -check- in name -> check phase (prompt.md only)
    - no prefix -> implement phase, plus:
        - additional .md files (besides prompt.md) -> check phases
        - .sh files -> script phases
      Check/script phases auto-get bounce_target set to the implement phase.
    """
    prompt_path = phase_dir / "prompt.md"
    if not prompt_path.exists():
        return None
    prompt = prompt_path.read_text()

    if phase_dir.name.startswith("check-") or "-check-" in phase_dir.name:
        # Check phase
        return [Phase(id=phase_dir.name, type="check", prompt=prompt)]

    # Implement phase — also pick up sibling check/script files
    phase_id = phase_dir.name
    phases = [Phase(id=phase_id, type="implement", prompt=prompt)]

    check_n = 0
    script_n = 0
    for entry in sorted(phase_dir.iterdir()):
        if entry.name == "prompt.md" or entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            script_n += 1
            phases.append(
                Phase(id=f"{phase_id}~script-{script_n}", type="script", run=str(entry), bounce_target=phase_id)
            )
        elif entry.is_file() and entry.suffix == ".md":
            check_n += 1
            phases.append(
                Phase(id=f"{phase_id}~check-{check_n}", type="check", prompt=entry.read_text(), bounce_target=phase_id)
            )

    return phases


def _load_parallel_dir(parallel_dir: Path) -> tuple[list[Phase], ParallelGroup]:
    """Load a parallel lane group from a directory.

    Each subdirectory is a lane. Within each lane, phases are loaded
    using standard conventions. Check/script phases auto-get bounce_target
    set to the lane's first implement phase.
    """
    lanes: list[list[str]] = []
    all_phases: list[Phase] = []

    for lane_dir in sorted(parallel_dir.iterdir()):
        if not lane_dir.is_dir():
            continue
        if lane_dir.name.startswith(".") or lane_dir.name.startswith("_"):
            continue

        lane_phases = _load_lane_dir(lane_dir)
        if not lane_phases:
            continue

        # Auto-set bounce targets for check/script phases
        first_implement = next((p.id for p in lane_phases if p.type == "implement"), None)
        for p in lane_phases:
            if p.type in ("check", "script") and not p.bounce_target and first_implement:
                p.bounce_target = first_implement

        lanes.append([p.id for p in lane_phases])
        all_phases.extend(lane_phases)

    return all_phases, ParallelGroup(lanes=lanes)


def _load_lane_dir(lane_dir: Path) -> list[Phase]:
    """Load phases from a lane directory.

    Simple mode (prompt.md at root):
        prompt.md       -> implement phase (id = lane dir name)
        check*.md       -> check phases (id = {lane}~check-N)
        *.sh            -> script phases (id = {lane}~script-N)

    Complex mode (subdirectories):
        Each entry is loaded using standard directory conventions,
        with phase IDs prefixed by the lane name.
    """
    lane_name = lane_dir.name
    phases: list[Phase] = []

    # Simple mode: prompt.md at root of the lane dir
    root_prompt = lane_dir / "prompt.md"
    if root_prompt.exists():
        phases.append(Phase(id=lane_name, type="implement", prompt=root_prompt.read_text()))

        check_n = 0
        script_n = 0
        for entry in sorted(lane_dir.iterdir()):
            if entry.name == "prompt.md" or entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".sh":
                script_n += 1
                phases.append(Phase(id=f"{lane_name}~script-{script_n}", type="script", run=str(entry)))
            elif entry.is_file() and entry.suffix == ".md":
                check_n += 1
                phases.append(Phase(id=f"{lane_name}~check-{check_n}", type="check", prompt=entry.read_text()))
        return phases

    # Complex mode: subdirectories as phases
    for entry in sorted(lane_dir.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            phases.append(Phase(id=f"{lane_name}~{entry.stem}", type="script", run=str(entry)))
        elif entry.is_file() and entry.suffix == ".md":
            phases.append(Phase(id=f"{lane_name}~{entry.stem}", type="implement", prompt=entry.read_text()))
        elif entry.is_dir():
            prompt_path = entry / "prompt.md"
            if not prompt_path.exists():
                continue
            prompt = prompt_path.read_text()
            phase_id = f"{lane_name}~{entry.name}"
            if entry.name.startswith("check-") or "-check-" in entry.name:
                phases.append(Phase(id=phase_id, type="check", prompt=prompt))
            else:
                phases.append(Phase(id=phase_id, type="implement", prompt=prompt))

    return phases


def _load_bare_file(path: Path) -> Workflow:
    """Load a single .md file as a single implement phase workflow."""
    prompt = path.read_text()
    phases = [
        Phase(id=path.stem, type="implement", prompt=prompt),
    ]
    return Workflow(name=path.stem, phases=phases)


def _expand_checkers(
    parent_id: str,
    checkers: list,
    base_path: Path | None = None,
    check_offset: int = 0,
    script_offset: int = 0,
) -> list[Phase]:
    """Expand inline checkers on an implement phase into synthetic check/script phases.

    Each entry can be:
    - bare string -> role shorthand (must be in VALID_ROLES)
    - dict with "role" -> check phase with built-in role
    - dict with "prompt" or "prompt_file" -> check phase with inline/file prompt
    - dict with "run" -> script phase
    Dicts may also carry "timeout" and "env".

    check_offset/script_offset let counters continue from existing inline checkers.
    """
    result: list[Phase] = []
    check_n = check_offset
    script_n = script_offset

    for i, entry in enumerate(checkers):
        if isinstance(entry, str):
            # Bare string = role shorthand
            if entry not in VALID_ROLES:
                raise ValueError(
                    f"Phase {parent_id!r}: checkers entry {i}: unknown role {entry!r} (valid: {sorted(VALID_ROLES)})"
                )
            check_n += 1
            result.append(
                Phase(
                    id=f"{parent_id}~check-{check_n}",
                    type="check",
                    role=entry,
                    bounce_target=parent_id,
                )
            )
        elif isinstance(entry, dict):
            timeout = entry.get("timeout")
            env = entry.get("env", {})

            if "run" in entry:
                script_n += 1
                result.append(
                    Phase(
                        id=f"{parent_id}~script-{script_n}",
                        type="script",
                        run=entry["run"],
                        bounce_target=parent_id,
                        timeout=timeout,
                        env=env,
                    )
                )
            elif "role" in entry:
                role = entry["role"]
                if role not in VALID_ROLES:
                    raise ValueError(
                        f"Phase {parent_id!r}: checkers entry {i}: unknown role {role!r} (valid: {sorted(VALID_ROLES)})"
                    )
                check_n += 1
                result.append(
                    Phase(
                        id=f"{parent_id}~check-{check_n}",
                        type="check",
                        role=role,
                        bounce_target=parent_id,
                        timeout=timeout,
                        env=env,
                    )
                )
            elif "prompt" in entry or "prompt_file" in entry:
                prompt = entry.get("prompt", "")
                if not prompt and entry.get("prompt_file"):
                    prompt = (base_path / entry["prompt_file"]).read_text()
                check_n += 1
                result.append(
                    Phase(
                        id=f"{parent_id}~check-{check_n}",
                        type="check",
                        prompt=prompt,
                        bounce_target=parent_id,
                        timeout=timeout,
                        env=env,
                    )
                )
            else:
                raise ValueError(
                    f"Phase {parent_id!r}: checkers entry {i}: must have 'role', 'prompt', 'prompt_file', or 'run'"
                )
        else:
            raise ValueError(
                f"Phase {parent_id!r}: checkers entry {i}: expected string or dict, got {type(entry).__name__}"
            )

    return result


def _load_role_prompt(role: str) -> str:
    """Load a built-in role prompt from the prompts directory."""
    role_file = f"checker-{role}.md"
    prompts_dir = Path(__file__).parent / "prompts"
    role_path = prompts_dir / role_file
    if role_path.exists():
        return role_path.read_text()
    raise FileNotFoundError(f"Built-in role prompt not found: {role_file}")


VALID_PHASE_TYPES = {"implement", "check", "script", "workflow"}
VALID_ROLES = {"tester", "architect", "pm", "senior-tester", "senior-engineer", "security-engineer"}
VALID_IMPLEMENTER_ROLES = {"software-engineer"}


def parse_checker_string(spec: str) -> dict | str:
    """Parse a --checker CLI value into the format _expand_checkers expects.

    - Bare string matching VALID_ROLES -> role shorthand (str)
    - "run:CMD" -> {"run": CMD}
    - "prompt:TEXT" -> {"prompt": TEXT}
    - Anything else -> ValueError
    """
    if spec in VALID_ROLES:
        return spec
    if spec.startswith("run:"):
        return {"run": spec[4:]}
    if spec.startswith("prompt:"):
        return {"prompt": spec[7:]}
    raise ValueError(
        f"Invalid --checker spec {spec!r}: must be a valid role ({sorted(VALID_ROLES)}), 'run:CMD', or 'prompt:TEXT'"
    )


def inject_checkers(workflow: Workflow, checker_specs: list[str]) -> Workflow:
    """Inject CLI --checker specs onto every implement phase in the workflow.

    For each implement phase, synthetic check/script phases are inserted after
    the phase (and after any existing inline checkers). Returns a new Workflow
    with the expanded phase list.
    """
    if not checker_specs:
        return workflow

    parsed = [parse_checker_string(s) for s in checker_specs]

    new_phases: list[Phase] = []
    for phase in workflow.phases:
        new_phases.append(phase)
        if phase.type != "implement":
            continue

        # Count existing inline checkers for this parent to get offsets
        parent_id = phase.id
        existing_checks = 0
        existing_scripts = 0
        for p in workflow.phases:
            if p.id.startswith(f"{parent_id}~check-"):
                existing_checks += 1
            elif p.id.startswith(f"{parent_id}~script-"):
                existing_scripts += 1

        # Find insertion point: after last existing child of this parent
        # (children will be appended naturally since we iterate in order)
        # We just need to expand at the end — but we need to defer insertion
        # until after all existing children are appended.
        # Store expansion info for deferred insertion.
        # Actually, since we iterate phases in order and children follow parent,
        # we use a deferred approach below.

    # Rebuild with deferred expansion: process phases, when we see an implement
    # phase, collect it and its children, then append expansions.
    new_phases = []
    i = 0
    phases = workflow.phases
    while i < len(phases):
        phase = phases[i]
        new_phases.append(phase)
        i += 1

        if phase.type != "implement":
            continue

        parent_id = phase.id
        prefix = f"{parent_id}~"

        # Collect existing children (they immediately follow the parent)
        existing_checks = 0
        existing_scripts = 0
        while i < len(phases) and phases[i].id.startswith(prefix):
            child = phases[i]
            new_phases.append(child)
            if child.id.startswith(f"{parent_id}~check-"):
                existing_checks += 1
            elif child.id.startswith(f"{parent_id}~script-"):
                existing_scripts += 1
            i += 1

        # Expand CLI checkers with proper offsets
        expanded = _expand_checkers(parent_id, parsed, check_offset=existing_checks, script_offset=existing_scripts)
        new_phases.extend(expanded)

    return Workflow(
        name=workflow.name,
        phases=new_phases,
        backend=workflow.backend,
        working_dir=workflow.working_dir,
        max_bounces=workflow.max_bounces,
        parallel_groups=workflow.parallel_groups,
        backoff=workflow.backoff,
        max_backoff=workflow.max_backoff,
        notify=list(workflow.notify),
        vars=dict(workflow.vars),
    )


def _load_implementer_prompt(role: str) -> str:
    """Load a built-in implementer role prompt from the prompts directory."""
    role_file = f"implementer-{role}.md"
    prompts_dir = Path(__file__).parent / "prompts"
    role_path = prompts_dir / role_file
    if role_path.exists():
        return role_path.read_text()
    raise FileNotFoundError(f"Built-in implementer prompt not found: {role_file}")


def inject_implementer(workflow: Workflow, role: str) -> Workflow:
    """Prepend an implementer role prompt to every implement phase in the workflow.

    Returns a new Workflow with modified prompts.
    """
    if role not in VALID_IMPLEMENTER_ROLES:
        raise ValueError(f"Invalid --implementer role {role!r}: must be one of {sorted(VALID_IMPLEMENTER_ROLES)}")

    preamble = _load_implementer_prompt(role)
    new_phases = []
    for phase in workflow.phases:
        if phase.type == "implement":
            phase = Phase(
                id=phase.id,
                type=phase.type,
                prompt=preamble + phase.prompt,
                run=phase.run,
                role=phase.role,
                bounce_target=phase.bounce_target,
                bounce_targets=list(phase.bounce_targets),
                timeout=phase.timeout,
                env=dict(phase.env),
                interactive=phase.interactive,
                max_depth=phase.max_depth,
                workflow_file=phase.workflow_file,
                workflow_dir=phase.workflow_dir,
            )
        new_phases.append(phase)

    return Workflow(
        name=workflow.name,
        phases=new_phases,
        backend=workflow.backend,
        working_dir=workflow.working_dir,
        max_bounces=workflow.max_bounces,
        parallel_groups=workflow.parallel_groups,
        backoff=workflow.backoff,
        max_backoff=workflow.max_backoff,
        notify=list(workflow.notify),
        vars=dict(workflow.vars),
    )


def expand_multi_vars(workflow: Workflow, multi_vars: dict[str, list[str]]) -> Workflow:
    """Expand phases that reference multi-value vars into parallel duplicates.

    For each phase whose prompt or run command references a multi-value var,
    create N copies (one per value, or cartesian product for multiple vars).
    If the phase has child phases (checkers/scripts with IDs like parent~check-1),
    duplicate the entire group as a lane group. Otherwise, create a flat parallel group.
    """
    if not multi_vars:
        return workflow

    # Build set of phase IDs already in parallel groups (skip these)
    parallel_phase_ids: set[str] = set()
    for pg in workflow.parallel_groups:
        parallel_phase_ids.update(pg.all_phase_ids())

    # Group phases: parent + children (IDs starting with parent~)
    groups: list[list[Phase]] = []
    i = 0
    phases = workflow.phases
    while i < len(phases):
        phase = phases[i]
        group = [phase]
        prefix = f"{phase.id}~"
        j = i + 1
        while j < len(phases) and phases[j].id.startswith(prefix):
            group.append(phases[j])
            j += 1
        groups.append(group)
        i = j

    new_phases: list[Phase] = []
    new_parallel_groups = list(workflow.parallel_groups)

    for group in groups:
        parent = group[0]

        # Skip if already in a parallel group
        if parent.id in parallel_phase_ids:
            new_phases.extend(group)
            continue

        # Find which multi-value vars this group references
        try:
            used_vars = {v for phase in group for text in (phase.prompt, phase.run or "") for v in template_vars(text)}
        except TemplateSyntaxError:
            new_phases.extend(group)
            continue
        referenced = [k for k in multi_vars if k in used_vars]

        if not referenced:
            new_phases.extend(group)
            continue

        # Generate all value combinations (cartesian product)
        value_lists = [[(k, v) for v in multi_vars[k]] for k in referenced]
        combinations = list(itertools.product(*value_lists))

        lanes: list[list[str]] = []
        for combo in combinations:
            combo_vars = dict(combo)
            suffix = "~".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in combo)
            group_old_ids = {p.id for p in group}
            lane_ids: list[str] = []

            for phase in group:
                new_id = f"{phase.id}~{suffix}"

                # Update bounce_target if it points within the same group
                new_bounce = phase.bounce_target
                if new_bounce in group_old_ids:
                    new_bounce = f"{new_bounce}~{suffix}"
                new_bounce_targets = [f"{bt}~{suffix}" if bt in group_old_ids else bt for bt in phase.bounce_targets]

                new_phase = Phase(
                    id=new_id,
                    type=phase.type,
                    prompt=(prompt := _sub_vars(phase.prompt, workflow.vars | combo_vars) if phase.prompt else ""),
                    run=(run := _sub_vars(phase.run, workflow.vars | combo_vars) if phase.run else phase.run),
                    role=phase.role,
                    bounce_target=new_bounce,
                    bounce_targets=new_bounce_targets,
                    timeout=phase.timeout,
                    env=dict(phase.env),
                    interactive=phase.interactive,
                    max_depth=phase.max_depth,
                    workflow_file=phase.workflow_file,
                    workflow_dir=phase.workflow_dir,
                )
                if phase is parent and not f"{prompt}{run or ''}".strip(): break  # noqa: E701  # fmt: skip
                new_phases.append(new_phase)
                lane_ids.append(new_id)

            lane_ids and lanes.append(lane_ids)

        new_parallel_groups.append(ParallelGroup(lanes=lanes))

    return Workflow(
        name=workflow.name,
        phases=new_phases,
        backend=workflow.backend,
        working_dir=workflow.working_dir,
        max_bounces=workflow.max_bounces,
        parallel_groups=new_parallel_groups,
        backoff=workflow.backoff,
        max_backoff=workflow.max_backoff,
        notify=list(workflow.notify),
        vars=dict(workflow.vars),
    )


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

    defined_vars = set(workflow.vars)
    try:
        recursive_vars = json.dumps(workflow.vars, default=repr) is None
    except (TypeError, ValueError) as exc:
        recursive_vars = str(exc) == "Circular reference detected"
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
            has_source = bool(phase.prompt) or bool(phase.workflow_file) or bool(phase.workflow_dir)
            if not has_source:
                errors.append(f"Phase {phase.id!r}: workflow phase needs prompt, workflow_file, or workflow_dir")
            if phase.workflow_file and phase.workflow_dir:
                errors.append(f"Phase {phase.id!r}: workflow_file and workflow_dir are mutually exclusive")
            if phase.run:
                errors.append(f"Phase {phase.id!r}: workflow phase must not have 'run'")
            if phase.role:
                errors.append(f"Phase {phase.id!r}: workflow phase must not have 'role'")
        if phase.type != "workflow" and (phase.workflow_file or phase.workflow_dir):
            errors.append(f"Phase {phase.id!r}: workflow_file/workflow_dir only allowed on workflow phases")
        if phase.max_depth is not None and phase.max_depth < 1:
            errors.append(f"Phase {phase.id!r}: max_depth must be >= 1, got {phase.max_depth}")

        # Interactive validation
        if phase.interactive and phase.type != "implement":
            errors.append(f"Phase {phase.id!r}: interactive is only valid on implement phases")

        # Role validation
        if phase.role and phase.role not in VALID_ROLES:
            errors.append(f"Phase {phase.id!r}: unknown role {phase.role!r} (valid: {sorted(VALID_ROLES)})")

    # Parallel group validation
    for i, group in enumerate(workflow.parallel_groups):
        for pid in group.all_phase_ids():
            if pid not in all_ids:
                errors.append(f"Parallel group {i}: phase ID {pid!r} does not match any phase")

        if group.is_lane_group():
            # Lane-specific validation
            for li, lane in enumerate(group.lanes):
                if not lane:
                    errors.append(f"Parallel group {i}, lane {li}: lane is empty")
                for pid in lane:
                    if pid in all_ids:
                        phase = next(p for p in workflow.phases if p.id == pid)
                        if phase.type == "workflow":
                            errors.append(
                                f"Parallel group {i}, lane {li}: workflow-type phase {pid!r} not allowed in lanes"
                            )

            # Check for phases appearing in multiple lanes
            seen_ids: set[str] = set()
            for li, lane in enumerate(group.lanes):
                for pid in lane:
                    if pid in seen_ids:
                        errors.append(f"Parallel group {i}: phase {pid!r} appears in multiple lanes")
                    seen_ids.add(pid)

            # Check bounce targets stay within their lane
            for li, lane in enumerate(group.lanes):
                lane_set = set(lane)
                for pid in lane:
                    if pid not in all_ids:
                        continue
                    phase = next(p for p in workflow.phases if p.id == pid)
                    if phase.bounce_target and phase.bounce_target not in lane_set:
                        errors.append(
                            f"Parallel group {i}, lane {li}: phase {pid!r} bounce_target "
                            f"{phase.bounce_target!r} is outside its lane"
                        )

    # Backoff validation
    if workflow.backoff < 0:
        errors.append(f"backoff must be non-negative, got {workflow.backoff}")
    if workflow.max_backoff < 0:
        errors.append(f"max_backoff must be non-negative, got {workflow.max_backoff}")

    # Notify URL validation
    for url in workflow.notify:
        if not url.startswith(("http://", "https://")):
            errors.append(f"notify URL must start with http:// or https://, got {url!r}")

    # Template validation: syntax must parse, render must succeed, and unresolved placeholders must be defined.
    for phase in workflow.phases:
        if recursive_vars and (phase.prompt or phase.run):
            errors.append("Phase %r: template render failed: template variables contain recursive data" % phase.id)
            continue
        try:
            missing = set()
            for text in filter(None, (phase.prompt, phase.run)):
                template_vars(text)
                missing.update(_control_template_vars(text, workflow.vars) - defined_vars)
                missing.update(_unresolved_template_vars(text, workflow.vars) - _optional_output_vars(text))
        except TemplateSyntaxError as exc:
            errors.append(f"Phase {phase.id!r}: invalid template syntax on line {exc.lineno}: {exc.message}")
            continue
        except TemplateRenderError as exc:
            errors.append(f"Phase {phase.id!r}: template render failed: {exc}")
            continue
        for var_name in sorted(missing):
            errors.append(f"Phase {phase.id!r}: template variable {{{{{var_name}}}}} has no value defined")

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
