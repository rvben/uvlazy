"""Shared uv resolution and installation for Python imports and executables."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from uvlazy.config import UvlazyError, normalize


@contextmanager
def environment_lock(directory: Path):
    """Serialize changes across processes sharing an environment."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "install.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def project_lock(directory: Path):
    """Serialize project environment creation without creating project state."""
    import fcntl

    locks = Path(tempfile.gettempdir()) / f"uvlazy-{os.getuid()}-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:20]
    with (locks / digest).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def installer_environment() -> dict[str, str]:
    # Interpreter probes and isolated package builds must not re-enter a lazy
    # installer while the calling process holds the environment lock.
    env = os.environ.copy()
    env.pop("UVLAZY_RUNTIME", None)
    return env


def run_uv(command: list[str], root: Path, *, output: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if output else sys.stderr,
        text=True,
        check=False,
        env=installer_environment(),
    )
    if result.returncode:
        raise UvlazyError(f"uv failed (exit {result.returncode}); see its diagnostic above.")
    return result.stdout if output else ""


class Installer:
    def __init__(self, settings: dict):
        self.root = Path(settings["root"])
        self.state = Path(settings["state"])
        self.requirements = settings["requirements"]
        self.uv = settings["uv"]
        self.python = settings["python"]
        self.quiet = settings["quiet"]
        self.groups = settings["groups"]
        self.locked = settings["locked"]
        self.extra_indexes = settings.get("extra_indexes", [])
        self.only_groups = settings.get("only_groups", False)
        self.project_constraints = settings.get("project_constraints", [])

    def add_extra_indexes(self, command: list[str]):
        for index in self.extra_indexes:
            command.extend(["--extra-index-url", index])

    def resolve(self) -> Path:
        """Call with the environment lock held; never update the project lock."""
        lock_exists = (self.root / "uv.lock").is_file()
        if self.locked and not lock_exists:
            raise UvlazyError("uv.lock is required. Run `uv lock` before running this command.")
        constraints = self.state / "constraints.txt"
        if constraints.exists():
            return constraints
        if lock_exists:
            command = [
                self.uv,
                "export",
                "--locked",
                "--no-header",
                "--no-hashes",
                "--no-default-groups",
                "--no-emit-project",
                "--python",
                self.python,
            ]
            group_option = "--only-group" if self.only_groups else "--group"
            for group in self.groups:
                command.extend([group_option, group])
        else:
            # Flatten selected groups here so this also works with uv versions
            # predating `uv pip compile --group`.
            source = self.state / "requirements.in"
            source.write_text(
                "\n".join(item for items in self.requirements.values() for item in items) + "\n",
                encoding="utf-8",
            )
            command = [
                self.uv,
                "pip",
                "compile",
                str(source),
                "--python",
                self.python,
                "--no-header",
                "--no-annotate",
            ]
            if self.project_constraints:
                project_constraints = self.state / "project-constraints.txt"
                project_constraints.write_text(
                    "\n".join(self.project_constraints) + "\n", encoding="utf-8"
                )
                command.extend(["--constraints", str(project_constraints)])
            self.add_extra_indexes(command)
        if self.quiet:
            command.append("--quiet")
        content = run_uv(command, self.root, output=True)
        temporary = constraints.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(constraints)
        return constraints

    def install(self, distribution: str, trigger: str):
        """Install one declared root and its closure against the shared pins."""
        self.install_many([distribution], trigger)

    def installed_matches(self, distribution: str, constraints: Path) -> bool:
        """Return whether the environment already has the selected locked pin."""
        expected = self.pinned_requirement(distribution, constraints).split("==", 1)[1]
        code = (
            "import importlib.metadata as m, sys; "
            "\ntry: print(m.version(sys.argv[1]))"
            "\nexcept m.PackageNotFoundError: raise SystemExit(1)"
        )
        result = subprocess.run(
            [self.python, "-I", "-c", code, distribution],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            env=installer_environment(),
        )
        return result.returncode == 0 and result.stdout.strip() == expected

    def pinned_requirement(self, distribution: str, constraints: Path) -> str:
        """Return an exact requirement for a distribution in resolved constraints."""
        for line in constraints.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
            if match and normalize(match.group(1)) == normalize(distribution):
                return f"{distribution}=={match.group(2)}"
        raise UvlazyError(
            f"Resolved constraints do not contain declared package {distribution!r}. "
            "Check its environment markers for this Python interpreter."
        )

    def install_many(self, distributions: list[str], trigger: str):
        """Install declared roots and their closures against the shared pins."""
        constraints = self.resolve()
        if not self.quiet:
            packages = ", ".join(distributions)
            print(f"uvlazy: {trigger} → installing {packages}", file=sys.stderr)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", dir=self.state, encoding="utf-8", delete=False
        ) as handle:
            requested = Path(handle.name)
            handle.write(
                "\n".join(
                    requirement
                    for distribution in distributions
                    for requirement in self.requirements[distribution]
                )
                + "\n"
            )
        try:
            command = [
                self.uv,
                "pip",
                "install",
                "--python",
                self.python,
                "--requirements",
                str(requested),
                "--constraints",
                str(constraints),
            ]
            self.add_extra_indexes(command)
            if self.quiet:
                command.append("--quiet")
            run_uv(command, self.root)
        finally:
            requested.unlink(missing_ok=True)
