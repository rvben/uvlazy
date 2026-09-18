"""A fallback import finder; application code is never restarted or retried."""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import os
import runpy
import sys
from pathlib import Path

from uvlazy.config import UvlazyError
from uvlazy.installer import Installer, environment_lock


class LazyFinder:
    def __init__(self, settings: dict):
        self.installer = Installer(settings)
        self.imports = settings["imports"]
        self.attempted: set[str] = set()
        self.installing = False

    def find_spec(self, fullname, path=None, target=None):
        if path is not None or fullname not in self.imports or self.installing:
            return None
        distribution = self.imports[fullname]
        if distribution in self.attempted:
            return None
        self.installing = True
        try:
            with environment_lock(self.installer.state):
                # Another process may have installed it while we were waiting.
                importlib.invalidate_caches()
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec is not None:
                    return spec
                self.installer.install(distribution, f"import {fullname}")
                self.attempted.add(distribution)
                importlib.invalidate_caches()
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec is None:
                    raise ModuleNotFoundError(
                        f"{distribution!r} does not provide {fullname!r} in this environment. "
                        "Check [tool.uvlazy.imports] and dependency environment markers.",
                        name=fullname,
                    )
                return spec
        finally:
            self.installing = False


def main(settings_json: str, mode: str, target: str, arguments: list[str]) -> int:
    settings = json.loads(settings_json)
    if sys.prefix == sys.base_prefix:
        raise UvlazyError("The lazy import hook must run inside its managed virtual environment.")
    finder = LazyFinder(settings)
    sys.meta_path.append(finder)
    sys.argv = [target, *arguments]
    # Replace the launcher's import path with ordinary Python script/-m behavior.
    sys.path[:2] = [str(Path(target).resolve().parent) if mode == "script" else os.getcwd()]
    try:
        if mode == "module":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(target, run_name="__main__")
    except UvlazyError as exc:
        print(f"uvlazy: {exc}", file=sys.stderr)
        return 1
    finally:
        sys.meta_path.remove(finder)
    return 0
