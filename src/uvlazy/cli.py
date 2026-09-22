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
from uvlazy.installer import Installer, environment_lock, project_lock, run_uv
from uvlazy.startup import prepare_startup


def delegates_to_uv(arguments: list[str]) -> bool:
    """Return whether arguments use uv syntax outside uvlazy's native surface."""
    if not arguments:
        return False
    if arguments[0] != "run":
        return arguments[0] not in {"--help", "-h", "--version", "-V"}

    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--help", "-h"}:
            return False
        if argument == "--no-project":
            return True
        if argument == "--with" or argument.startswith("--with="):
            return True
        if argument in {
            "--project",
            "--from",
            "--group",
            "--only-group",
            "--extra-index-url",
        }:
            index += 2
            continue
        if (
            argument.startswith(
                (
                    "--project=",
                    "--from=",
                    "--group=",
                    "--only-group=",
                    "--extra-index-url=",
                )
            )
            or argument in {"-q", "--quiet", "-m", "--module", "--locked", "--eager"}
            or (argument.startswith("-") and len(argument) > 1 and set(argument[1:]) <= {"q", "m"})
        ):
            index += 1
            continue
        # Once uvlazy's target is reached, all remaining options belong to it.
        return argument.startswith("-")
    return False


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if sys.platform == "win32":
            raise UvlazyError("This prototype currently supports macOS and Linux.")
        if delegates_to_uv(arguments):
            uv = shutil.which("uv")
            if not uv:
                raise UvlazyError(
                    "uv is required on PATH. Install it from https://docs.astral.sh/uv/."
                )
            os.execv(uv, [uv, *arguments])
    except (UvlazyError, OSError) as exc:
        print(f"uvlazy: {exc}", file=sys.stderr)
        return 1

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
    run.add_argument(
        "--only-group",
        action="append",
        help="select only a dependency group, excluding project dependencies (repeatable)",
    )
    run.add_argument(
        "--with",
        dest="with_requirements",
        action="append",
        default=[],
        metavar="REQUIREMENT",
        help="run with an additional requirement (repeatable)",
    )
    run.add_argument(
        "--extra-index-url",
        dest="extra_indexes",
        action="append",
        default=[],
        metavar="URL",
        help="use an additional package index for --with requirements",
    )
    run.add_argument("--locked", action="store_true", help="require uv.lock (automatic for tools)")
    run.add_argument(
        "--eager", action="store_true", help="use uv's normal project sync before running"
    )
    run.add_argument(
        "--no-project",
        action="store_true",
        help="use uv's isolated non-project execution semantics",
    )
    run.add_argument("target", help="tool command, script path, or module name")
    run.add_argument("arguments", nargs=argparse.REMAINDER, help="arguments passed to your program")
    args = parser.parse_args(arguments)
    try:
        uv = shutil.which("uv")
        if not uv:
            raise UvlazyError("uv is required on PATH. Install it from https://docs.astral.sh/uv/.")
        if args.eager:
            if args.provider:
                raise UvlazyError("--from cannot be combined with --eager.")
            forwarded = list(arguments)
            forwarded.remove("--eager")
            os.execv(uv, [uv, *forwarded])
        if args.no_project:
            os.execv(uv, [uv, *arguments])
        mode = "command"
        if args.module:
            mode = "module"
        elif args.target in {"python", "python3"}:
            mode = "interpreter"
        elif Path(args.target).is_file() or args.target.endswith(".py") or "/" in args.target:
            mode = "script"
        if mode == "script" and not Path(args.target).is_file():
            raise UvlazyError(f"Script does not exist: {args.target}")
        if args.provider and mode != "command":
            raise UvlazyError("--from is only supported when running a tool command.")
        if args.extra_indexes and not args.with_requirements:
            raise UvlazyError("--extra-index-url requires --with.")
        root = args.project.resolve() if args.project else find_project(Path.cwd())
        project = read_project(root)
        only_groups = [normalize(group) for group in (args.only_group or [])]
        include_project = not only_groups
        explicit_groups = [normalize(group) for group in (args.group or [])] + only_groups
        groups = explicit_groups or None
        distribution = None
        if mode == "command":
            distribution, groups = project.command_package(
                args.target,
                groups,
                args.provider,
                include_project=include_project,
            )
        groups = sorted(set(groups or []))
        requirements = project.selected(groups, include_project=include_project)
        locked = args.locked or mode == "command"
        if locked and not (root / "uv.lock").is_file():
            raise UvlazyError("uv.lock is required. Run `uv lock` before running this command.")
        selection = json.dumps(
            [
                project.fingerprint,
                distribution,
                groups,
                bool(only_groups),
                __version__,
            ]
        )
        fingerprint = hashlib.sha256(selection.encode()).hexdigest()[:20]
        environment = root / ".venv"
        state = environment / ".uvlazy" / fingerprint
        python = environment / "bin" / "python"
        with project_lock(root):
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
            "extra_indexes": args.extra_indexes,
            "only_groups": bool(only_groups),
            "project_constraints": project.constraints,
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
        environment_vars["UVLAZY_RUNTIME"] = prepare_startup(settings)
        if mode == "command":
            run_command(settings, distribution, args.target, args.arguments, environment_vars)
        if args.locked or (root / "uv.lock").is_file():
            with environment_lock(state):
                Installer(settings).resolve()
        if mode == "interpreter":
            os.execve(str(python), [str(python), *args.arguments], environment_vars)
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
