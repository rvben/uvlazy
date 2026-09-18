"""Run project-locked tools and lazily installed Python programs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from uvlazy import __version__
from uvlazy.commands import run_command
from uvlazy.config import UvlazyError, find_project, normalize, read_project
from uvlazy.installer import Installer, environment_lock, run_uv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uvlazy", description="Install only the tool or Python packages you use."
    )
    parser.add_argument("--version", action="version", version=f"uvlazy {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a locked tool, Python script, or module")
    run.add_argument("--project", type=Path, help="project directory (default: discover from cwd)")
    run.add_argument("-q", "--quiet", action="store_true", help="hide installation progress")
    run.add_argument("-m", "--module", action="store_true", help="run a module, like python -m")
    run.add_argument("--from", dest="provider", help="declared package providing the command")
    run.add_argument("--group", action="append", help="select a dependency group (repeatable)")
    run.add_argument("--locked", action="store_true", help="require uv.lock (automatic for tools)")
    run.add_argument("target", help="tool command, script path, or module name")
    run.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments passed to your program")
    args = parser.parse_args(argv)
    try:
        if sys.platform == "win32":
            raise UvlazyError("This prototype currently supports macOS and Linux.")
        uv = shutil.which("uv")
        if not uv:
            raise UvlazyError("uv is required on PATH. Install it from https://docs.astral.sh/uv/.")
        mode = "command"
        if args.module:
            mode = "module"
        elif Path(args.target).is_file() or args.target.endswith(".py") or "/" in args.target:
            mode = "script"
        if mode == "script" and not Path(args.target).is_file():
            raise UvlazyError(f"Script does not exist: {args.target}")
        if args.provider and mode != "command":
            raise UvlazyError("--from is only supported when running a tool command.")
        root = args.project.resolve() if args.project else find_project(Path.cwd())
        project = read_project(root)
        groups = None if args.group is None else [normalize(group) for group in args.group]
        distribution = None
        if mode == "command":
            distribution, groups = project.command_package(args.target, groups, args.provider)
        groups = sorted(set(groups or []))
        requirements = project.selected(groups)
        locked = args.locked or mode == "command"
        if locked and not (root / "uv.lock").is_file():
            raise UvlazyError("uv.lock is required. Run `uv lock` before running this command.")
        selection = json.dumps([project.fingerprint, distribution, groups])
        fingerprint = hashlib.sha256(selection.encode()).hexdigest()[:20]
        state = root / ".uvlazy" / fingerprint
        environment = state / "venv"
        python = environment / "bin" / "python"
        with environment_lock(state):
            if not python.is_file():
                command = [
                    uv,
                    "venv",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment),
                ]
                if args.quiet:
                    command.append("--quiet")
                run_uv(command, root)
        settings = {
            "root": str(root),
            "state": str(state),
            "requirements": requirements,
            "imports": {name: dep for name, dep in project.imports.items() if dep in requirements},
            "uv": uv,
            "quiet": args.quiet,
            "python": str(python),
            "groups": groups,
            "locked": locked,
        }
        # Import the small, stdlib-only runner from this installation. The
        # managed environment needs no bootstrap packages of its own.
        bootstrap = (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from uvlazy.runtime import main; "
            "raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]))"
        )
        environment_vars = os.environ.copy()
        environment_vars.pop("PYTHONHOME", None)
        environment_vars.pop("PYTHONPATH", None)
        environment_vars["VIRTUAL_ENV"] = str(environment)
        environment_vars["PATH"] = str(python.parent) + os.pathsep + os.environ.get("PATH", "")
        if mode == "command":
            run_command(settings, distribution, args.target, args.arguments, environment_vars)
        if args.locked:
            with environment_lock(state):
                Installer(settings).resolve()
        result = subprocess.run(
            [
                str(python),
                "-c",
                bootstrap,
                str(Path(__file__).resolve().parents[1]),
                json.dumps(settings),
                mode,
                args.target,
                *args.arguments,
            ],
            env=environment_vars,
            check=False,
        )
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except (UvlazyError, OSError) as exc:
        print(f"uvlazy: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
