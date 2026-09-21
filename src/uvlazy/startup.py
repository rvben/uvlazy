"""Activate lazy imports in Python descendants of a managed command."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from uvlazy.config import UvlazyError
from uvlazy.installer import environment_lock, installer_environment


def prepare_startup(settings: dict) -> str:
    state = Path(settings["state"])
    runtime_content = json.dumps(settings, sort_keys=True)
    digest = hashlib.sha256(runtime_content.encode()).hexdigest()
    runtime = state / "runtimes" / f"{digest}.json"
    engine = str(Path(__file__).resolve().parents[1])
    # A .pth hook belongs only to this virtualenv. Activation is inherited by
    # ordinary Python, shell, and native subprocesses without changing their
    # commands, replacing sitecustomize, or adding the engine to application paths.
    bootstrap = (
        "import os, sys\n"
        f"sys.path.insert(0, {engine!a})\n"
        "try:\n"
        "    from uvlazy.runtime import activate\n"
        "    activate(os.environ['UVLAZY_RUNTIME'])\n"
        "finally:\n"
        "    sys.path.pop(0)\n"
    )
    hook = (
        f"import os; exec({bootstrap!a}) "
        f"if os.path.dirname(os.environ.get('UVLAZY_RUNTIME', '')) == {str(runtime.parent)!a} "
        "else None\n"
    )
    with environment_lock(state):
        result = subprocess.run(
            [
                settings["python"],
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            env=installer_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise UvlazyError(f"Could not locate the managed Python site-packages: {result.stderr}")
        site_packages = Path(result.stdout.strip())
        if not site_packages.is_relative_to(Path(settings["python"]).parent.parent):
            raise UvlazyError(
                "Python site-packages must be inside its managed virtual environment."
            )
        # Settings are immutable per configuration so concurrent quiet/locked
        # runs cannot overwrite the behavior inherited by each other's children.
        runtime.parent.mkdir(exist_ok=True)
        # Replace atomically so existing children never see a partially written file.
        for path, content in [
            (runtime, runtime_content),
            (site_packages / "_uvlazy.pth", hook),
        ]:
            if path.is_file() and path.read_text(encoding="utf-8") == content:
                continue
            temporary = path.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
    return str(runtime)
