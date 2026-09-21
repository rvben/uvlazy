"""Read declared dependencies without importing any third-party packages."""

from __future__ import annotations

import hashlib
import keyword
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Command names that differ from their published provider distribution. These
# aliases only select an already-declared dependency; they never add one.
COMMAND_ALIASES = {"cdk": "aws-cdk-cli"}
IMPORT_ALIASES = {"aws_cdk": "aws-cdk-lib"}


class UvlazyError(RuntimeError):
    """An actionable configuration or installation failure."""


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Project:
    root: Path
    requirements: dict[str, list[str]]
    imports: dict[str, str]
    fingerprint: str
    groups: dict[str, dict[str, list[str]]]
    direct_groups: dict[str, set[str]]
    commands: dict[str, str]

    def selected(self, groups: list[str]) -> dict[str, list[str]]:
        requirements = {name: list(items) for name, items in self.requirements.items()}
        for group in groups:
            if group not in self.groups:
                raise UvlazyError(f"Unknown dependency group {group!r}.")
            for name, items in self.groups[group].items():
                requirements.setdefault(name, []).extend(items)
        return {name: list(dict.fromkeys(items)) for name, items in requirements.items()}

    def command_package(
        self, command: str, groups: list[str] | None, provider: str | None
    ) -> tuple[str, list[str]]:
        distribution = normalize(provider) if provider else self.commands.get(command)
        if distribution is None:
            raise UvlazyError(
                f"No declared package provides command {command!r}. "
                "Use --from PACKAGE or [tool.uvlazy.commands] for a command alias."
            )
        if groups is None:
            groups = (
                []
                if distribution in self.requirements
                else [group for group, names in self.direct_groups.items() if distribution in names]
            )
        groups = sorted(set(groups))
        if distribution not in self.selected(groups):
            raise UvlazyError(
                f"{distribution!r} must be declared in project dependencies or a selected group."
            )
        return distribution, groups


def parse_requirements(dependencies: object, label: str) -> dict[str, list[str]]:
    if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
        raise UvlazyError(f"{label} must be a list of requirement strings.")
    requirements: dict[str, list[str]] = {}
    for requirement in dependencies:
        # uv validates full PEP 508 syntax. Only declared distribution names
        # authorize installation; a missing command/import never names a package.
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?=\s|\[|[<>=!~;@]|$)", requirement)
        if not match:
            raise UvlazyError(f"Expected a named dependency, got {requirement!r}.")
        requirements.setdefault(normalize(match[1]), []).append(requirement)
    return requirements


def dependency_groups(data: dict, uv: dict) -> tuple[dict, dict]:
    raw = data.get("dependency-groups", {})
    if not isinstance(raw, dict):
        raise UvlazyError("[dependency-groups] must be a TOML table.")
    groups = {}
    for name, items in raw.items():
        if normalize(name) in groups:
            raise UvlazyError(f"Duplicate normalized dependency group {name!r}.")
        if not isinstance(items, list):
            raise UvlazyError(f"Dependency group {name!r} must be a list.")
        groups[normalize(name)] = list(items)
    legacy = uv.get("dev-dependencies", [])
    parse_requirements(legacy, "[tool.uv].dev-dependencies")
    if legacy:
        groups.setdefault("dev", []).extend(legacy)
    expanded: dict[str, dict[str, list[str]]] = {}
    direct: dict[str, set[str]] = {}

    def expand(name: str, stack: tuple[str, ...] = ()) -> dict[str, list[str]]:
        if name in stack:
            raise UvlazyError(f"Dependency group cycle: {' → '.join((*stack, name))}.")
        if name not in groups:
            raise UvlazyError(f"Unknown included dependency group {name!r}.")
        if name in expanded:
            return expanded[name]
        items = groups[name]
        requirements = parse_requirements(
            [item for item in items if isinstance(item, str)], f"Dependency group {name!r}"
        )
        direct[name] = set(requirements)
        for item in items:
            if isinstance(item, str):
                continue
            if (
                not isinstance(item, dict)
                or set(item) != {"include-group"}
                or not isinstance(item["include-group"], str)
            ):
                raise UvlazyError(f"Invalid item in dependency group {name!r}: {item!r}.")
            included = expand(normalize(item["include-group"]), (*stack, name))
            for distribution, specs in included.items():
                requirements.setdefault(distribution, []).extend(specs)
        expanded[name] = requirements
        return requirements

    for name in groups:
        expand(name)
    return expanded, direct


def find_project(start: Path) -> Path:
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    raise UvlazyError("No pyproject.toml found. Run inside a Python project or use --project PATH.")


def read_project(root: Path) -> Project:
    root = root.resolve()
    raw = (root / "pyproject.toml").read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise UvlazyError(f"Invalid pyproject.toml: {exc}") from exc
    project = data.get("project", {})
    tool = data.get("tool", {})
    if not isinstance(project, dict) or not isinstance(tool, dict):
        raise UvlazyError("[project] and [tool] must be TOML tables.")
    uv = tool.get("uv", {})
    settings = tool.get("uvlazy", {})
    if not isinstance(uv, dict) or not isinstance(settings, dict):
        raise UvlazyError("[tool.uv] and [tool.uvlazy] must be TOML tables.")
    if uv.get("sources") or uv.get("workspace"):
        raise UvlazyError("This prototype does not yet support uv sources or workspaces.")
    if "dependencies" in project.get("dynamic", []):
        raise UvlazyError("Declare dependencies statically in [project].dependencies.")
    requirements = parse_requirements(project.get("dependencies", []), "[project].dependencies")
    groups, direct_groups = dependency_groups(data, uv)
    declared = set(requirements).union(*(set(items) for items in groups.values()))
    imports: dict[str, str] = {}
    commands: dict[str, str] = {}
    for name in sorted(declared):
        module = name.replace("-", "_")
        if module.isidentifier() and not keyword.iskeyword(module):
            imports[module] = name
        commands[name] = name
        commands[module] = name

    for command, distribution in COMMAND_ALIASES.items():
        if distribution in declared:
            commands.setdefault(command, distribution)
    for module, distribution in IMPORT_ALIASES.items():
        if distribution in declared:
            imports.setdefault(module, distribution)

    aliases = settings.get("imports", {})
    if not isinstance(aliases, dict):
        raise UvlazyError("[tool.uvlazy.imports] must map import names to declared package names.")
    for module, distribution in aliases.items():
        if not module.isidentifier() or keyword.iskeyword(module):
            raise UvlazyError(f"Import mapping {module!r} must be a top-level Python module name.")
        if not isinstance(distribution, str) or normalize(distribution) not in declared:
            raise UvlazyError(f"Import {module!r} must map to a declared dependency.")
        imports[module] = normalize(distribution)

    aliases = settings.get("commands", {})
    if not isinstance(aliases, dict):
        raise UvlazyError("[tool.uvlazy.commands] must map commands to declared package names.")
    for command, distribution in aliases.items():
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", command):
            raise UvlazyError(
                f"Command mapping {command!r} must be an executable name, not a path."
            )
        if not isinstance(distribution, str) or normalize(distribution) not in declared:
            raise UvlazyError(f"Command {command!r} must map to a declared dependency.")
        commands[command] = normalize(distribution)

    lock = root / "uv.lock"
    digest = hashlib.sha256()
    for content in (
        b"uvlazy-environment-v3",
        # Virtualenv executable shebangs contain absolute paths. Do not reuse a
        # CI cache restored under a different checkout directory.
        str(root).encode(),
        raw,
        lock.read_bytes() if lock.exists() else b"",
        (root / "uv.toml").read_bytes() if (root / "uv.toml").exists() else b"",
        str(Path(sys.executable).resolve()).encode(),
        sys.version.encode(),
    ):
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return Project(
        root, requirements, imports, digest.hexdigest()[:20], groups, direct_groups, commands
    )
