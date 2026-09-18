"""Install and execute one tool, without importing it or syncing the project."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from uvlazy.config import UvlazyError
from uvlazy.installer import Installer, environment_lock


def owns_executable(python: str, distribution: str, executable: Path) -> bool:
    # Inspect metadata with the target interpreter, not the launcher's packages.
    # This covers generated console scripts and native .data/scripts executables.
    code = """
import importlib.metadata as metadata
import pathlib
import sys
try:
    dist = metadata.distribution(sys.argv[1])
except metadata.PackageNotFoundError:
    raise SystemExit(1)
target = pathlib.Path(sys.argv[2]).resolve()
raise SystemExit(0 if any(
    pathlib.Path(dist.locate_file(item)).resolve() == target
    for item in (dist.files or ())
) else 1)
"""
    result = subprocess.run(
        [python, "-I", "-c", code, distribution, str(executable)],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def run_command(settings: dict, distribution: str, target: str, arguments: list[str], env: dict):
    installer = Installer(settings)
    executable = Path(installer.python).parent / target
    with environment_lock(installer.state):
        # Even a cached executable must not bypass lock validation on first use
        # of a configuration. Cached constraints belong to this fingerprint.
        installer.resolve()
        ready = installer.state / "tool-ready"
        if not ready.is_file() or not executable.is_file():
            installer.install(distribution, f"command {target}")
        if not (
            executable.is_file()
            and os.access(executable, os.X_OK)
            and owns_executable(installer.python, distribution, executable)
        ):
            raise UvlazyError(
                f"Declared package {distribution!r} does not provide executable {target!r} "
                "in this environment. Check --from, [tool.uvlazy.commands], and platform markers."
            )
        ready.touch()
    # Replace the launcher so CI receives the tool's exit status and signals
    # directly. Its arguments, working directory, stdin and streams are intact.
    os.execve(executable, [str(executable), *arguments], env)
