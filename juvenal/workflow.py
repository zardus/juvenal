"""Workflow loading from YAML, directory convention, and bare .md files."""

from __future__ import annotations

import itertools
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError, meta, nodes
from jinja2.runtime import missing
from jinja2.sandbox import SandboxedEnvironment


class _PassthroughUndefined(StrictUndefined):
    """Preserve unresolved top-level variables but fail on nested lookups."""

    def __str__(self) -> str:
        if self._undefined_name is None:
            return ""
        if self._undefined_obj is missing:
            return f"{{{{{self._undefined_name}}}}}"
        return self._fail_with_undefined_error()


_JINJA_ENV = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True, undefined=_PassthroughUndefined)
_JINJA_ENV.globals.clear()
_UNDEFINED_VARS_ERROR_PREFIX = "undefined template variables require values before rendering:"
_UNKNOWN = object()


def _find_template_vars(text: str) -> set[str]:
    """Return undeclared variable names referenced by a Jinja2 template."""
    if not text:
        return set()
    return set(meta.find_undeclared_variables(_JINJA_ENV.parse(text)))


def _find_template_vars_safe(text: str) -> set[str]:
    """Best-effort variable discovery for callers that validate syntax later."""
    try:
        return _find_template_vars(text)
    except TemplateSyntaxError:
        return set()


def _vars_defined_when_true(test: nodes.Node, guaranteed_defined: frozenset[str] = frozenset()) -> frozenset[str]:
    """Variables guaranteed defined after ``test`` evaluates truthy."""
    if isinstance(test, nodes.Test) and test.name == "defined" and isinstance(test.node, nodes.Name):
        return guaranteed_defined | frozenset({test.node.name})
    if isinstance(test, nodes.Not):
        return _vars_defined_when_false(test.node, guaranteed_defined)
    if isinstance(test, nodes.And):
        left_true = _vars_defined_when_true(test.left, guaranteed_defined)
        return _vars_defined_when_true(test.right, left_true)
    if isinstance(test, nodes.Or):
        left_true = _vars_defined_when_true(test.left, guaranteed_defined)
        right_true = _vars_defined_when_true(test.right, _vars_defined_when_false(test.left, guaranteed_defined))
        return left_true & right_true
    return guaranteed_defined


def _vars_defined_when_false(test: nodes.Node, guaranteed_defined: frozenset[str] = frozenset()) -> frozenset[str]:
    """Variables guaranteed defined after ``test`` evaluates falsy."""
    if isinstance(test, nodes.Test) and test.name == "undefined" and isinstance(test.node, nodes.Name):
        return guaranteed_defined | frozenset({test.node.name})
    if isinstance(test, nodes.Not):
        return _vars_defined_when_true(test.node, guaranteed_defined)
    if isinstance(test, nodes.And):
        left_false = _vars_defined_when_false(test.left, guaranteed_defined)
        right_false = _vars_defined_when_false(test.right, _vars_defined_when_true(test.left, guaranteed_defined))
        return left_false & right_false
    if isinstance(test, nodes.Or):
        left_false = _vars_defined_when_false(test.left, guaranteed_defined)
        return _vars_defined_when_false(test.right, left_false)
    return guaranteed_defined


def _lookup_attr_or_item(value: object, key: object) -> object:
    """Mimic Jinja's attr/item lookup for simple context-driven branch pruning."""
    if isinstance(value, dict):
        return value.get(key, _UNKNOWN)
    if isinstance(key, str):
        try:
            return getattr(value, key)
        except Exception:
            pass
    try:
        return value[key]
    except Exception:
        return _UNKNOWN


def _evaluate_node(node: nodes.Node, context: dict[str, object] | None) -> object:
    """Best-effort evaluation for simple Jinja conditions using current context."""
    if context is None:
        return _UNKNOWN
    if isinstance(node, nodes.Const):
        return node.value
    if isinstance(node, nodes.Name):
        return context.get(node.name, _UNKNOWN)
    if isinstance(node, nodes.Getattr):
        base = _evaluate_node(node.node, context)
        if base is _UNKNOWN:
            return _UNKNOWN
        return _lookup_attr_or_item(base, node.attr)
    if isinstance(node, nodes.Getitem):
        base = _evaluate_node(node.node, context)
        key = _evaluate_node(node.arg, context)
        if base is _UNKNOWN or key is _UNKNOWN:
            return _UNKNOWN
        return _lookup_attr_or_item(base, key)
    if isinstance(node, nodes.Test):
        value = _evaluate_node(node.node, context)
        if node.name == "defined":
            return value is not _UNKNOWN
        if node.name == "undefined":
            return value is _UNKNOWN
        return _UNKNOWN
    if isinstance(node, nodes.Not):
        value = _evaluate_node(node.node, context)
        if value is _UNKNOWN:
            return _UNKNOWN
        return not bool(value)
    if isinstance(node, nodes.And):
        left = _evaluate_node(node.left, context)
        if left is _UNKNOWN:
            return _UNKNOWN
        if not left:
            return False
        right = _evaluate_node(node.right, context)
        if right is _UNKNOWN:
            return _UNKNOWN
        return bool(right)
    if isinstance(node, nodes.Or):
        left = _evaluate_node(node.left, context)
        if left is _UNKNOWN:
            return _UNKNOWN
        if left:
            return True
        right = _evaluate_node(node.right, context)
        if right is _UNKNOWN:
            return _UNKNOWN
        return bool(right)
    if isinstance(node, nodes.Compare):
        left = _evaluate_node(node.expr, context)
        if left is _UNKNOWN:
            return _UNKNOWN
        current = left
        for operand in node.ops:
            right = _evaluate_node(operand.expr, context)
            if right is _UNKNOWN:
                return _UNKNOWN
            op = operand.op
            if op == "eq":
                ok = current == right
            elif op == "ne":
                ok = current != right
            elif op == "gt":
                ok = current > right
            elif op == "gteq":
                ok = current >= right
            elif op == "lt":
                ok = current < right
            elif op == "lteq":
                ok = current <= right
            elif op == "in":
                ok = current in right
            elif op == "notin":
                ok = current not in right
            else:
                return _UNKNOWN
            if not ok:
                return False
            current = right
        return True
    return _UNKNOWN


def _evaluate_truthiness(node: nodes.Node, context: dict[str, object] | None) -> bool | None:
    """Return the condition result when it can be decided from current context."""
    value = _evaluate_node(node, context)
    if value is _UNKNOWN:
        return None
    return bool(value)


def _find_vars_requiring_values(
    ast: nodes.Template,
    missing_vars: set[str],
    *,
    allow_passthrough: bool,
    context: dict[str, object] | None = None,
) -> set[str]:
    """Return missing vars that are used in a way that still requires a value."""

    required: set[str] = set()

    def _walk(
        node: nodes.Node, parent: nodes.Node | None = None, guaranteed_defined: frozenset[str] = frozenset()
    ) -> None:
        if isinstance(node, nodes.Name) and node.ctx == "load" and node.name in missing_vars:
            if node.name in guaranteed_defined:
                return
            if isinstance(parent, nodes.Test) and parent.node is node and parent.name in {"defined", "undefined"}:
                return
            if isinstance(parent, nodes.Filter) and parent.node is node and parent.name == "default":
                return
            if allow_passthrough and isinstance(parent, nodes.Output) and node in parent.nodes:
                return
            required.add(node.name)
            return

        if isinstance(node, nodes.If):
            _walk(node.test, node, guaranteed_defined)
            true_defined = _vars_defined_when_true(node.test, guaranteed_defined)
            false_defined = _vars_defined_when_false(node.test, guaranteed_defined)
            branch_truth = _evaluate_truthiness(node.test, context)
            if branch_truth is True:
                for child in node.body:
                    _walk(child, node, true_defined)
                return
            if branch_truth is False:
                for elif_node in node.elif_:
                    _walk(elif_node.test, elif_node, false_defined)
                    elif_true_defined = _vars_defined_when_true(elif_node.test, false_defined)
                    elif_truth = _evaluate_truthiness(elif_node.test, context)
                    if elif_truth is True:
                        for child in elif_node.body:
                            _walk(child, elif_node, elif_true_defined)
                        return
                    if elif_truth is False:
                        false_defined = _vars_defined_when_false(elif_node.test, false_defined)
                        continue
                    for child in elif_node.body:
                        _walk(child, elif_node, elif_true_defined)
                    false_defined = _vars_defined_when_false(elif_node.test, false_defined)
                    for later_elif in node.elif_[node.elif_.index(elif_node) + 1 :]:
                        _walk(later_elif.test, later_elif, false_defined)
                        later_true_defined = _vars_defined_when_true(later_elif.test, false_defined)
                        for child in later_elif.body:
                            _walk(child, later_elif, later_true_defined)
                        false_defined = _vars_defined_when_false(later_elif.test, false_defined)
                    for child in node.else_:
                        _walk(child, node, false_defined)
                    return
                for child in node.else_:
                    _walk(child, node, false_defined)
                return
            for child in node.body:
                _walk(child, node, true_defined)
            for elif_node in node.elif_:
                _walk(elif_node.test, elif_node, false_defined)
                elif_true_defined = _vars_defined_when_true(elif_node.test, false_defined)
                for child in elif_node.body:
                    _walk(child, elif_node, elif_true_defined)
                false_defined = _vars_defined_when_false(elif_node.test, false_defined)
            for child in node.else_:
                _walk(child, node, false_defined)
            return

        if isinstance(node, nodes.CondExpr):
            _walk(node.test, node, guaranteed_defined)
            true_defined = _vars_defined_when_true(node.test, guaranteed_defined)
            false_defined = _vars_defined_when_false(node.test, guaranteed_defined)
            branch_truth = _evaluate_truthiness(node.test, context)
            if branch_truth is True:
                _walk(node.expr1, node, true_defined)
                return
            if branch_truth is False:
                if node.expr2 is not None:
                    _walk(node.expr2, node, false_defined)
                return
            _walk(node.expr1, node, true_defined)
            if node.expr2 is not None:
                _walk(node.expr2, node, false_defined)
            return

        if isinstance(node, nodes.And):
            _walk(node.left, node, guaranteed_defined)
            _walk(node.right, node, _vars_defined_when_true(node.left, guaranteed_defined))
            return

        if isinstance(node, nodes.Or):
            _walk(node.left, node, guaranteed_defined)
            _walk(node.right, node, _vars_defined_when_false(node.left, guaranteed_defined))
            return

        for child in node.iter_child_nodes():
            _walk(child, node, guaranteed_defined)

    _walk(ast)
    return required


def apply_vars(text: str, vars: dict[str, str] | None) -> str:
    """Render text with Jinja2 using vars as the template context."""
    if not text:
        return text
    context = vars or {}
    ast = _JINJA_ENV.parse(text)
    missing_vars = set(meta.find_undeclared_variables(ast)) - set(context.keys())
    required_vars = _find_vars_requiring_values(ast, missing_vars, allow_passthrough=True, context=context)
    if required_vars:
        missing_list = ", ".join(sorted(required_vars))
        raise UndefinedError(f"{_UNDEFINED_VARS_ERROR_PREFIX} {missing_list}")
    return _JINJA_ENV.from_string(text).render(context)


def _describe_template_render_error(phase_id: str, field_name: str, exc: Exception) -> str:
    """Format a Jinja2 render failure for validation and dry-run output."""
    return f"Jinja2 render error in {field_name} for phase '{phase_id}': {type(exc).__name__}: {exc}"


@dataclass
class Phase:
    """A single phase in the workflow pipeline.

    Every phase is exactly one thing:
    - "implement": agentic implementation (default)
    - "check": agentic checker (parses VERDICT)
    - "workflow": sub-workflow (dynamic via prompt, or static via workflow_file/workflow_dir)
    """

    id: str
    type: str = "implement"  # "implement", "check", "workflow"
    prompt: str = ""  # for implement, check, and workflow (dynamic)
    role: str | None = None  # built-in role name for check
    bounce_target: str | None = None  # fixed phase to bounce back to on failure
    bounce_targets: list[str] = field(default_factory=list)  # agent-guided: checker picks from this list
    timeout: int | None = None  # timeout in seconds (None = no limit)
    env: dict[str, str] = field(default_factory=dict)  # environment variables for the phase
    interactive: bool = False  # launch interactive TUI session (claude only)
    max_depth: int | None = None  # recursion depth limit for workflow phases
    workflow_file: str | None = None  # path to static sub-workflow YAML (resolved at load time)
    workflow_dir: str | None = None  # path to static sub-workflow directory (resolved at load time)
    template_vars: dict[str, str] = field(default_factory=dict)  # per-phase Jinja2 variables from expansion

    def _render_text(self, text: str, vars: dict[str, str] | None = None) -> str:
        context = dict(vars or {})
        context.update(self.template_vars)
        return apply_vars(text, context)

    def render_prompt(self, failure_context: str = "", vars: dict[str, str] | None = None) -> str:
        """Render the implementation prompt, injecting failure context on retry."""
        text = self._render_text(self.prompt, vars)
        if failure_context:
            text += (
                "\n\nIMPORTANT: A previous attempt failed verification.\n"
                f"Failure details:\n\n{failure_context}\n\n"
                "Fix these issues in your implementation.\n"
            )
        return text

    def render_check_prompt(self, vars: dict[str, str] | None = None) -> str:
        """Render the checker prompt for check phases."""
        parts: list[str] = []
        if self.prompt:
            parts.append(self._render_text(self.prompt, vars))
        if self.role:
            parts.append(_load_role_prompt(self.role))
        return "\n\n".join(part for part in parts if part)


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
    vars: dict[str, str] = field(default_factory=dict)  # Jinja2 template variables


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
        if "run" in phase_data:
            raise ValueError(
                f"Phase '{phase_data['id']}': 'run' is no longer supported; use an agentic check prompt instead"
            )
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
        phase_type = phase_data.get("type", "implement")
        if phase_type == "script":
            raise ValueError(
                f"Phase '{phase_data['id']}': type 'script' is no longer supported; use type 'check' with a prompt"
            )

        phase = Phase(
            id=phase_data["id"],
            type=phase_type,
            prompt=prompt,
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
    - Bare .md file at top level -> implement + auto check phase
    """
    phases = []
    parallel_groups: list[ParallelGroup] = []
    entries = sorted(phases_dir.iterdir())

    for entry in entries:
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            raise ValueError(
                f"Run-based .sh phases are no longer supported: {entry}. "
                "Create an explicit check prompt that tells the agent which command to run."
            )
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
      Check phases auto-get bounce_target set to the implement phase.
    """
    prompt_path = phase_dir / "prompt.md"
    if not prompt_path.exists():
        return None
    prompt = prompt_path.read_text()

    if phase_dir.name.startswith("check-") or "-check-" in phase_dir.name:
        # Check phase
        return [Phase(id=phase_dir.name, type="check", prompt=prompt)]

    # Implement phase — also pick up sibling markdown check files
    phase_id = phase_dir.name
    phases = [Phase(id=phase_id, type="implement", prompt=prompt)]

    check_n = 0
    for entry in sorted(phase_dir.iterdir()):
        if entry.name == "prompt.md" or entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            raise ValueError(
                f"Run-based .sh checkers are no longer supported: {entry}. "
                "Replace the script file with a markdown checker prompt."
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
    using standard conventions. Check phases auto-get bounce_target
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

        # Auto-set bounce targets for check phases
        first_implement = next((p.id for p in lane_phases if p.type == "implement"), None)
        for p in lane_phases:
            if p.type == "check" and not p.bounce_target and first_implement:
                p.bounce_target = first_implement

        lanes.append([p.id for p in lane_phases])
        all_phases.extend(lane_phases)

    return all_phases, ParallelGroup(lanes=lanes)


def _load_lane_dir(lane_dir: Path) -> list[Phase]:
    """Load phases from a lane directory.

    Simple mode (prompt.md at root):
        prompt.md       -> implement phase (id = lane dir name)
        check*.md       -> check phases (id = {lane}~check-N)

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
        for entry in sorted(lane_dir.iterdir()):
            if entry.name == "prompt.md" or entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".sh":
                raise ValueError(
                    f"Run-based .sh checkers are no longer supported: {entry}. "
                    "Replace the script file with a markdown checker prompt."
                )
            elif entry.is_file() and entry.suffix == ".md":
                check_n += 1
                phases.append(Phase(id=f"{lane_name}~check-{check_n}", type="check", prompt=entry.read_text()))
        return phases

    # Complex mode: subdirectories as phases
    for entry in sorted(lane_dir.iterdir()):
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".sh":
            raise ValueError(
                f"Run-based .sh phases are no longer supported: {entry}. "
                "Replace the script file with a markdown checker prompt."
            )
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
    template_vars: dict[str, str] | None = None,
) -> list[Phase]:
    """Expand inline checkers on an implement phase into synthetic check phases.

    Each entry can be:
    - bare string -> role shorthand (must be in VALID_ROLES)
    - dict with "role" -> check phase with built-in role
    - dict with "prompt" or "prompt_file" -> check phase with inline/file prompt
    Dicts may also carry "timeout" and "env".

    check_offset lets counters continue from existing inline checkers.
    """
    result: list[Phase] = []
    check_n = check_offset

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
                    template_vars=dict(template_vars or {}),
                )
            )
        elif isinstance(entry, dict):
            timeout = entry.get("timeout")
            env = entry.get("env", {})

            if "run" in entry:
                raise ValueError(
                    f"Phase {parent_id!r}: checkers entry {i}: 'run' is no longer supported; "
                    "use 'prompt' or 'prompt_file' and tell the checker which command to run"
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
                        template_vars=dict(template_vars or {}),
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
                        template_vars=dict(template_vars or {}),
                    )
                )
            else:
                raise ValueError(
                    f"Phase {parent_id!r}: checkers entry {i}: must have 'role', 'prompt', or 'prompt_file'"
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


def make_command_check_prompt(command: str) -> str:
    """Render an agentic checker prompt that validates work by running a command."""
    return (
        "Run the following command from the working directory and use the result to verify the implementation:\n\n"
        f"```bash\n{command}\n```\n\n"
        "Do not change code while verifying. If the command fails, or if it reveals a defect, "
        "emit `VERDICT: FAIL: <reason>`. If it succeeds and the work looks correct for this check, "
        "emit `VERDICT: PASS`."
    )


VALID_PHASE_TYPES = {"implement", "check", "workflow"}
VALID_ROLES = {
    "tester",
    "architect",
    "pm",
    "senior-tester",
    "senior-engineer",
    "security-engineer",
    "technical-writer",
    "llm-writing",
    "professor",
    "grant-reviewer",
}
VALID_IMPLEMENTER_ROLES = {"software-engineer", "professor-writer"}


def parse_checker_string(spec: str) -> dict | str:
    """Parse a --checker CLI value into the format _expand_checkers expects.

    - Bare string matching VALID_ROLES -> role shorthand (str)
    - "prompt:TEXT" -> {"prompt": TEXT}
    - Anything else -> ValueError
    """
    if spec in VALID_ROLES:
        return spec
    if spec.startswith("run:"):
        raise ValueError(f"Invalid --checker spec {spec!r}: 'run:' is no longer supported; use 'prompt:TEXT' instead")
    if spec.startswith("prompt:"):
        return {"prompt": spec[7:]}
    raise ValueError(f"Invalid --checker spec {spec!r}: must be a valid role ({sorted(VALID_ROLES)}) or 'prompt:TEXT'")


def inject_checkers(workflow: Workflow, checker_specs: list[str]) -> Workflow:
    """Inject CLI --checker specs onto every implement phase in the workflow.

    For each implement phase, synthetic check phases are inserted after
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
        for p in workflow.phases:
            if p.id.startswith(f"{parent_id}~check-"):
                existing_checks += 1

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
        while i < len(phases) and phases[i].id.startswith(prefix):
            child = phases[i]
            new_phases.append(child)
            if child.id.startswith(f"{parent_id}~check-"):
                existing_checks += 1
            i += 1

        # Expand CLI checkers with proper offsets
        expanded = _expand_checkers(parent_id, parsed, check_offset=existing_checks, template_vars=phase.template_vars)
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
                role=phase.role,
                bounce_target=phase.bounce_target,
                bounce_targets=list(phase.bounce_targets),
                timeout=phase.timeout,
                env=dict(phase.env),
                interactive=phase.interactive,
                max_depth=phase.max_depth,
                workflow_file=phase.workflow_file,
                workflow_dir=phase.workflow_dir,
                template_vars=dict(phase.template_vars),
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

    For each phase whose prompt references a multi-value var,
    create N copies (one per value, or cartesian product for multiple vars).
    If the phase has child phases (checkers with IDs like parent~check-1),
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
        all_text = parent.prompt
        for child in group[1:]:
            all_text += child.prompt
        used_vars = _find_template_vars_safe(all_text)
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
            suffix = "~".join(f"{k}={v}" for k, v in combo)
            group_old_ids = {p.id for p in group}
            lane_ids: list[str] = []

            for phase in group:
                new_id = f"{phase.id}~{suffix}"

                # Update bounce_target if it points within the same group
                new_bounce = phase.bounce_target
                if new_bounce in group_old_ids:
                    new_bounce = f"{new_bounce}~{suffix}"
                new_bounce_targets = [f"{bt}~{suffix}" if bt in group_old_ids else bt for bt in phase.bounce_targets]
                new_template_vars = dict(phase.template_vars)
                new_template_vars.update(combo_vars)

                new_phase = Phase(
                    id=new_id,
                    type=phase.type,
                    prompt=phase.prompt,
                    role=phase.role,
                    bounce_target=new_bounce,
                    bounce_targets=new_bounce_targets,
                    timeout=phase.timeout,
                    env=dict(phase.env),
                    interactive=phase.interactive,
                    max_depth=phase.max_depth,
                    workflow_file=phase.workflow_file,
                    workflow_dir=phase.workflow_dir,
                    template_vars=new_template_vars,
                )
                new_phases.append(new_phase)
                lane_ids.append(new_id)

            lanes.append(lane_ids)

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
    - implement phases have a prompt
    - check phases have a prompt or role
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
        if phase.type == "workflow":
            has_source = bool(phase.prompt) or bool(phase.workflow_file) or bool(phase.workflow_dir)
            if not has_source:
                errors.append(f"Phase {phase.id!r}: workflow phase needs prompt, workflow_file, or workflow_dir")
            if phase.workflow_file and phase.workflow_dir:
                errors.append(f"Phase {phase.id!r}: workflow_file and workflow_dir are mutually exclusive")
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

    # Template validation: referenced Jinja2 variables must have values
    for phase in workflow.phases:
        render_context = dict(workflow.vars)
        render_context.update(phase.template_vars)
        defined_vars = set(render_context.keys())
        undefined: set[str] = set()
        phase_has_template_errors = False
        for field_name, text in (("prompt", phase.prompt),):
            if not text:
                continue
            render_field_name = "checker prompt" if phase.type == "check" else field_name
            try:
                ast = _JINJA_ENV.parse(text)
            except TemplateSyntaxError as exc:
                errors.append(f"Phase {phase.id!r}: invalid Jinja2 {field_name}: {exc.message} (line {exc.lineno})")
                phase_has_template_errors = True
                continue
            missing_vars = set(meta.find_undeclared_variables(ast)) - defined_vars
            field_undefined = _find_vars_requiring_values(
                ast, missing_vars, allow_passthrough=False, context=render_context
            )
            if field_undefined:
                try:
                    rendered_text = phase._render_text(text, vars=workflow.vars)
                except Exception as exc:
                    if isinstance(exc, UndefinedError) and str(exc).startswith(_UNDEFINED_VARS_ERROR_PREFIX):
                        rendered_text = None
                    else:
                        errors.append(_describe_template_render_error(phase.id, render_field_name, exc))
                        phase_has_template_errors = True
                        continue
                else:
                    # Drop vars that only appear in branches eliminated by the current render context.
                    field_undefined &= _find_template_vars_safe(rendered_text)
            undefined.update(field_undefined)
        for var_name in sorted(undefined):
            errors.append(f"Phase {phase.id!r}: template variable {{{{{var_name}}}}} has no value defined")
        if undefined or phase_has_template_errors:
            continue

        try:
            if phase.type == "implement":
                phase.render_prompt(vars=workflow.vars)
            elif phase.type == "check" and phase.prompt:
                phase._render_text(phase.prompt, vars=workflow.vars)
            elif phase.type == "workflow" and phase.prompt:
                phase.render_prompt(vars=workflow.vars)
        except Exception as exc:
            field_name = "prompt"
            if phase.type == "check":
                field_name = "checker prompt"
            errors.append(_describe_template_render_error(phase.id, field_name, exc))

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
