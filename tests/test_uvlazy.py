"""Offline integration tests against real uv and locally generated wheels."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import zipfile
from pathlib import Path

from uvlazy.config import UvlazyError, read_project

SOURCE = Path(__file__).resolve().parents[1] / "src"
UV = shutil.which("uv")
UVLAZY = (
    [str(Path(os.environ["UVLAZY_TEST_BINARY"]).resolve())]
    if os.environ.get("UVLAZY_TEST_BINARY")
    else [sys.executable, "-m", "uvlazy"]
)


@unittest.skipUnless(os.environ.get("UVLAZY_TEST_BINARY"), "requires a built native executable")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="uvlazy-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = os.environ.copy()
        self.env.update(
            {
                "UV_PYTHON": sys.executable,
                "UV_PYTHON_DOWNLOADS": "never",
                "UV_OFFLINE": "1",
                "UV_CACHE_DIR": str(self.root / "uv-cache"),
                "UVLAZY_CACHE_DIR": str(self.root / "engine cache λ"),
            }
        )

    def invoke(self, *arguments):
        return subprocess.run(
            [*UVLAZY, *arguments],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_version_needs_no_uv_or_python_and_matches_cargo(self):
        self.env["PATH"] = str(self.root)
        manifest = tomllib.loads((SOURCE.parent / "Cargo.toml").read_text())
        for option in ["--version", "-V"]:
            result = self.invoke(option)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"uvlazy {manifest['package']['version']}\n")

    def test_missing_uv_is_actionable(self):
        self.env["PATH"] = str(self.root)
        result = self.invoke("run", "tool")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install uv and put it on PATH", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unavailable_requested_python_fails_without_installing(self):
        self.env["UV_PYTHON"] = str(self.root / "missing-python")
        result = self.invoke("run", "tool")
        self.assertEqual(result.returncode, 1)
        # uv may render a path relative to its working directory.
        self.assertIn("missing-python", result.stderr)
        self.assertIn("Python discovery failed", result.stderr)
        self.assertFalse((self.root / ".uvlazy").exists())

    def test_uv_discovery_errors_preserve_the_real_cause(self):
        shim = self.root / "uv"
        self.env["PATH"] = str(self.root)
        for diagnostic, status in [
            ("mise ERROR Config files are not trusted. Trust them with `mise trust`.", 1),
            ("error: failed to open uv cache: Permission denied", 2),
        ]:
            with self.subTest(diagnostic=diagnostic):
                shim.write_text(f"#!/bin/sh\nprintf '%s\\n' '{diagnostic}' >&2\nexit {status}\n")
                shim.chmod(0o755)
                result = self.invoke("run", "tool")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(diagnostic, result.stderr)
                self.assertIn(str(shim), result.stderr)
                self.assertIn("Python discovery", result.stderr)
                self.assertNotIn("Python 3.11+ is required", result.stderr)
                self.assertNotIn("uv python install", result.stderr)
                self.assertFalse((self.root / ".uvlazy").exists())
                self.assertFalse(Path(self.env["UVLAZY_CACHE_DIR"]).exists())

    def test_bundle_is_isolated_reusable_and_repairs_corrupt_cache(self):
        (self.root / "argparse.py").write_text("raise RuntimeError('shadowed stdlib')\n")
        self.env["PYTHONPATH"] = str(self.root)
        self.env["PYTHONHOME"] = str(self.root / "invalid-home")
        result = self.invoke("run", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: uvlazy run", result.stdout)
        archives = list(Path(self.env["UVLAZY_CACHE_DIR"]).glob("engines/*.zip"))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        initial = archive.read_bytes()
        mtime = archive.stat().st_mtime_ns
        warm = self.invoke("run", "--help")
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertEqual(archive.stat().st_mtime_ns, mtime)
        archive.write_bytes(b"corrupt")
        repaired = self.invoke("run", "--help")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(archive.read_bytes(), initial)

    def test_unwritable_cache_is_actionable(self):
        blocker = self.root / "file-not-directory"
        blocker.write_text("existing file")
        self.env["UVLAZY_CACHE_DIR"] = str(blocker)
        result = self.invoke("run", "--help")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Set UVLAZY_CACHE_DIR to a writable path", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


def wheel(
    directory,
    name,
    version="1.0",
    *,
    module=None,
    code="VALUE = 42\n",
    requires=(),
    entries=None,
    scripts=None,
    extra_files=None,
):
    normalized = name.replace("-", "_")
    info = f"{normalized}-{version}.dist-info"
    files = {
        f"{module or normalized}/__init__.py": code,
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires)
            + "\n"
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: uvlazy-tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if entries:
        files[f"{info}/entry_points.txt"] = (
            "[console_scripts]\n"
            + "\n".join(f"{command} = {entry}" for command, entry in entries.items())
            + "\n"
        )
    for command, script in (scripts or {}).items():
        files[f"{normalized}-{version}.data/scripts/{command}"] = script
    files.update(extra_files or {})
    record = []
    for path, content in files.items():
        payload = content.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(payload)}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    with zipfile.ZipFile(directory / f"{normalized}-{version}-py3-none-any.whl", "w") as archive:
        for path, content in files.items():
            entry = zipfile.ZipInfo(path)
            entry.external_attr = (0o100755 if ".data/scripts/" in path else 0o100644) << 16
            archive.writestr(entry, content)


@unittest.skipUnless(UV, "uv must be installed")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="uvlazy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        wheel(self.wheels, "first", code="from shared import VALUE\n", requires=["shared>=1"])
        wheel(self.wheels, "second", requires=["shared<2"])
        wheel(self.wheels, "shared", "1.0", code="VALUE = 1\n")
        wheel(self.wheels, "shared", "2.0", code="VALUE = 2\n")
        wheel(self.wheels, "unused")
        wheel(self.wheels, "different-name", module="alias")
        wheel(self.wheels, "broken", code="import missing_inside_broken\n")
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("UV_") and key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
        }
        self.env.update(
            {
                "PYTHONPATH": str(SOURCE),
                "UV_CACHE_DIR": str(self.root / "cache"),
                "UVLAZY_CACHE_DIR": str(self.root / "engine-cache"),
                "UV_PYTHON": sys.executable,
                "UV_NO_INDEX": "1",
                "UV_FIND_LINKS": str(self.wheels),
                "UV_PYTHON_DOWNLOADS": "never",
                "UV_OFFLINE": "1",
            }
        )
        self.project(["first", "second", "unused", "different-name", "broken"])

    def project(self, dependencies, extra=""):
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "fixture"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
            f"dependencies = {json.dumps(dependencies)}\n" + extra,
            encoding="utf-8",
        )

    def script(self, content):
        path = self.root / "app.py"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def run_script(self, content, *arguments):
        self.script(content)
        return self.invoke("run", "app.py", *arguments)

    def invoke(self, *arguments):
        return subprocess.run(
            [*UVLAZY, *arguments],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def lock(self):
        result = subprocess.run(
            [UV, "lock"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assert_success(result)
        return (self.root / "uv.lock").read_bytes()

    def tool_project(self, extra="", *, group="lint", version="1.0"):
        wheel(
            self.wheels,
            "lint-tool",
            version,
            requires=["shared>=1"],
            entries={"lint-tool": "lint_tool:main", "lint": "lint_tool:main"},
            code=textwrap.dedent("""
                def main():
                    import importlib.metadata as metadata
                    import json
                    import os
                    import signal
                    import sys
                    from shared import VALUE
                    if "--signal" in sys.argv:
                        os.kill(os.getpid(), signal.SIGTERM)
                    print(json.dumps({
                        "args": sys.argv[1:], "cwd": os.getcwd(), "shared": VALUE,
                        "version": metadata.version("lint-tool"),
                        "installed": sorted(
                            dist.metadata["Name"] for dist in metadata.distributions()
                        ),
                        "stdin": sys.stdin.read() if "--stdin" in sys.argv else None,
                    }))
                    return 7 if "--fail" in sys.argv else 0
            """),
        )
        if group:
            self.project(
                ["unused", "second"], f'[dependency-groups]\n{group} = ["lint-tool"]\n' + extra
            )
        else:
            self.project(["unused", "second", "lint-tool"], extra)
        return self.lock()

    def test_command_installs_only_locked_tool_and_closure(self):
        before = self.tool_project()
        # A new release must not change the CI result without a lock update.
        wheel(self.wheels, "lint-tool", "2.0", code="raise AssertionError('wrong release')\n")
        # An unrelated locked package need not even be downloadable in this job.
        (self.wheels / "unused-1.0-py3-none-any.whl").unlink()
        result = self.invoke("run", "lint-tool", "check", ".", "two words", "--flag")
        self.assert_success(result)
        report = json.loads(result.stdout)
        self.assertEqual(report["args"], ["check", ".", "two words", "--flag"])
        self.assertEqual(Path(report["cwd"]), self.root.resolve())
        self.assertEqual(report["version"], "1.0")
        self.assertEqual(report["shared"], 1)
        self.assertEqual(report["installed"], ["lint-tool", "shared"])
        self.assertEqual((self.root / "uv.lock").read_bytes(), before)
        self.assertFalse((self.root / ".venv").exists())

    def test_command_warm_run_reuses_environment_offline(self):
        self.tool_project()
        self.assert_success(self.invoke("run", "lint-tool"))
        shutil.rmtree(self.wheels)
        shutil.rmtree(self.root / "cache")
        result = self.invoke("run", "lint-tool")
        self.assert_success(result)
        self.assertEqual(result.stderr, "")

    def test_command_alias_and_from_use_declared_provider(self):
        self.tool_project('[tool.uvlazy.commands]\nlint = "lint-tool"\n')
        self.assert_success(self.invoke("run", "lint"))
        self.assert_success(self.invoke("run", "--from", "lint-tool", "lint"))
        unknown = self.invoke("run", "--from", "not-declared", "lint")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("must be declared", unknown.stderr)

    def test_command_group_selection_and_nested_groups(self):
        self.tool_project('ci = [{include-group = "lint"}, "unused"]\n')
        result = self.invoke("run", "--group", "ci", "lint-tool")
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout)["installed"], ["lint-tool", "shared"])
        missing = self.invoke("run", "--group", "missing", "lint-tool")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Unknown dependency group", missing.stderr)

    def test_command_in_project_dependencies(self):
        self.tool_project(group=None)
        result = self.invoke("run", "lint-tool")
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout)["installed"], ["lint-tool", "shared"])

    def test_cdk_selects_cli_without_installing_sdk_or_unrelated_dependencies(self):
        wheel(
            self.wheels,
            "aws-cdk-cli",
            entries={"cdk": "aws_cdk_cli:main"},
            code=textwrap.dedent("""
                def main():
                    import importlib.metadata as metadata
                    import json
                    print(json.dumps(sorted(
                        dist.metadata["Name"] for dist in metadata.distributions()
                    )))
                    return 0
            """),
        )
        wheel(self.wheels, "aws-cdk-lib", requires=["shared"])
        for grouped in [False, True]:
            with self.subTest(grouped=grouped):
                if grouped:
                    self.project(
                        ["unused"],
                        '[dependency-groups]\ndev = ["aws-cdk-cli", "aws-cdk-lib", "second"]\n',
                    )
                else:
                    self.project(["aws-cdk-cli", "aws-cdk-lib", "second", "unused"])
                before = self.lock()
                result = self.invoke("run", "cdk")
                self.assert_success(result)
                self.assertEqual(json.loads(result.stdout), ["aws-cdk-cli"])
                self.assertEqual((self.root / "uv.lock").read_bytes(), before)
                self.assertFalse((self.root / ".venv").exists())

    def test_cdk_does_not_install_an_undeclared_cli_provider(self):
        wheel(self.wheels, "aws-cdk-lib")
        self.project(["aws-cdk-lib"])
        self.lock()
        result = self.invoke("run", "cdk")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No declared package", result.stderr)
        self.assertFalse((self.root / ".uvlazy").exists())

    def test_command_in_legacy_dev_dependencies(self):
        self.tool_project()
        self.project(["second", "unused"], '[tool.uv]\ndev-dependencies = ["lint-tool"]\n')
        self.lock()
        self.assert_success(self.invoke("run", "lint-tool"))

    def test_command_requires_lock_and_rejects_stale_lock(self):
        self.tool_project()
        (self.root / "uv.lock").unlink()
        missing = self.invoke("run", "lint-tool")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("uv.lock is required", missing.stderr)
        self.assertFalse((self.root / ".uvlazy").exists())
        before = self.lock()
        self.project(["second"], '[dependency-groups]\nlint = ["lint-tool", "unused"]\n')
        stale = self.invoke("run", "lint-tool")
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(stale.stdout, "")
        self.assertEqual((self.root / "uv.lock").read_bytes(), before)

    def test_command_passes_exit_status_stdin_and_signals(self):
        self.tool_project()
        failed = self.invoke("run", "lint-tool", "--fail")
        self.assertEqual(failed.returncode, 7)
        signaled = self.invoke("run", "lint-tool", "--signal")
        self.assertEqual(signaled.returncode, -15)
        result = subprocess.run(
            [*UVLAZY, "run", "lint-tool", "--stdin"],
            cwd=self.root,
            env=self.env,
            input="input from CI\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout)["stdin"], "input from CI\n")

    def test_command_never_falls_back_to_path_or_another_packages_executable(self):
        self.tool_project()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ["undeclared", "wrong-command"]:
            executable = bin_path / name
            executable.write_text("#!/bin/sh\nprintf 'wrong executable'\n")
            executable.chmod(0o755)
        self.env["PATH"] = str(bin_path) + os.pathsep + self.env["PATH"]
        undeclared = self.invoke("run", "undeclared")
        self.assertNotEqual(undeclared.returncode, 0)
        self.assertEqual(undeclared.stdout, "")
        wrong = self.invoke("run", "--from", "lint-tool", "wrong-command")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("does not provide executable", wrong.stderr)
        self.assertEqual(wrong.stdout, "")
        # The virtualenv's existing Python executable isn't owned by lint-tool.
        wrong_owner = self.invoke("run", "--from", "lint-tool", "python", "-V")
        self.assertNotEqual(wrong_owner.returncode, 0)
        self.assertEqual(wrong_owner.stdout, "")

    def test_command_runs_non_python_wheel_script(self):
        wheel(
            self.wheels,
            "native-tool",
            scripts={"native-tool": "#!/bin/sh\nprintf '%s\\n' \"$@\"\n"},
        )
        self.project(["unused"], '[dependency-groups]\nlint = ["native-tool"]\n')
        self.lock()
        result = self.invoke("run", "native-tool", "check", ".")
        self.assert_success(result)
        self.assertEqual(result.stdout, "check\n.\n")

    def test_script_group_can_be_selected_without_lock(self):
        self.project([], '[dependency-groups]\nlint = ["first", "second"]\n')
        self.script("import first; print(first.VALUE)")
        result = self.invoke("run", "--group", "lint", "app.py")
        self.assert_success(result)
        self.assertEqual(result.stdout, "1\n")

    def test_locked_script_validates_before_running_dependency_free_code(self):
        self.project([])
        self.lock()
        self.project(["unused"])
        self.script("print('must not run')")
        result = self.invoke("run", "--locked", "app.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_no_import_means_no_resolution_or_install(self):
        # Even an unavailable declared package must not cause network access or
        # failure on a path that never reaches a third-party import.
        self.project(["unavailable-package"])
        result = self.run_script("print('plain')")
        self.assert_success(result)
        self.assertEqual(result.stdout, "plain\n")
        self.assertEqual(list(self.root.glob(".uvlazy/*/constraints.txt")), [])
        self.assertEqual(list(self.root.glob(".uvlazy/*/venv/lib/*/site-packages/*.dist-info")), [])

    def test_first_import_installs_closure_and_pins_full_graph(self):
        result = self.run_script("""
            import importlib.metadata as metadata
            from pathlib import Path
            Path("side-effect").write_text("once")
            try:
                metadata.version("first")
            except metadata.PackageNotFoundError:
                pass
            else:
                raise AssertionError("installed too soon")
            import first
            assert first.VALUE == 1, "full graph must pin shared<2 before second is imported"
            import second
            assert metadata.version("shared") == "1.0"
            try:
                metadata.version("unused")
            except metadata.PackageNotFoundError:
                print("unused stayed absent")
            else:
                raise AssertionError("unused dependency was installed")
        """)
        self.assert_success(result)
        self.assertEqual(result.stdout, "unused stayed absent\n")
        self.assertEqual((self.root / "side-effect").read_text(), "once")
        self.assertIn("installing first", result.stderr)
        self.assertIn("installing second", result.stderr)

    def test_warm_run_reuses_installed_packages(self):
        first = self.run_script("import first; print(first.VALUE)")
        self.assert_success(first)
        second = self.invoke("run", "app.py")
        self.assert_success(second)
        self.assertEqual(second.stdout, "1\n")
        self.assertEqual(second.stderr, "")

    def test_explicit_alias_and_dynamic_import(self):
        self.project(
            ["different-name", "unused"], '[tool.uvlazy.imports]\nalias = "different-name"\n'
        )
        result = self.run_script("import importlib; print(importlib.import_module('alias').VALUE)")
        self.assert_success(result)
        self.assertEqual(result.stdout, "42\n")

    def test_unknown_import_does_not_install(self):
        result = self.run_script("import typo_that_is_not_declared")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'typo_that_is_not_declared'", result.stderr)
        self.assertNotIn("installing", result.stderr)

    def test_broken_package_is_not_retried_or_masked(self):
        result = self.run_script("""
            with open("effects", "a") as handle:
                handle.write("once\\n")
            import broken
        """)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'missing_inside_broken'", result.stderr)
        self.assertEqual((self.root / "effects").read_text(), "once\n")
        self.assertEqual(result.stderr.count("installing broken"), 1)

    def test_local_module_wins(self):
        (self.root / "unused.py").write_text("VALUE = 'local'\n")
        result = self.run_script("import unused; print(unused.VALUE)")
        self.assert_success(result)
        self.assertEqual(result.stdout, "local\n")
        self.assertNotIn("installing", result.stderr)

    def test_script_path_does_not_keep_launchers_current_directory(self):
        folder = self.root / "scripts"
        folder.mkdir()
        (self.root / "unused.py").write_text("VALUE = 'wrong'\n")
        (folder / "app.py").write_text("import unused; print(unused.VALUE)\n")
        result = self.invoke("run", "scripts/app.py")
        self.assert_success(result)
        self.assertEqual(result.stdout, "42\n")

    def test_quiet_keeps_program_output_and_hides_progress(self):
        self.script("import first; print(first.VALUE)")
        result = self.invoke("run", "--quiet", "app.py")
        self.assert_success(result)
        self.assertEqual(result.stdout, "1\n")
        self.assertEqual(result.stderr, "")

    def test_arguments_exit_status_and_main_module(self):
        result = self.run_script(
            """
            import __main__
            import sys
            assert __main__.__file__.endswith("app.py")
            print(sys.argv[1:])
            raise SystemExit(7)
        """,
            "--flag",
            "two words",
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "['--flag', 'two words']\n")

    def test_module_mode_can_install_target(self):
        wheel(
            self.wheels,
            "entry",
            code="",
            extra_files={
                "entry/__main__.py": "import first; print(first.VALUE)\n",
            },
        )
        self.project(["entry", "first", "second"])
        result = self.invoke("run", "-m", "entry")
        self.assert_success(result)
        self.assertEqual(result.stdout, "1\n")

    def test_inactive_environment_marker(self):
        self.project(['unused; python_version < "2"'])
        result = self.run_script("""
            try:
                import unused
            except ModuleNotFoundError:
                print("inactive")
        """)
        self.assert_success(result)
        self.assertEqual(result.stdout, "inactive\n")
        self.assertEqual(list(self.root.glob(".uvlazy/*/venv/lib/*/site-packages/unused*")), [])

    def test_resolution_failure_stops_before_application_continues(self):
        self.project(["unavailable"])
        result = self.run_script("import unavailable; print('should not run')")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("uv failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_uv_lock_is_preserved_and_stale_lock_is_rejected(self):
        self.project(["first", "second", "unused"])
        locked = subprocess.run(
            [UV, "lock"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assert_success(locked)
        before = (self.root / "uv.lock").read_bytes()
        result = self.run_script("import first; print(first.VALUE)")
        self.assert_success(result)
        self.assertEqual(result.stdout, "1\n")
        self.assertEqual((self.root / "uv.lock").read_bytes(), before)
        self.project(["first", "second", "unused", "different-name"])
        stale = self.invoke("run", "app.py")
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual((self.root / "uv.lock").read_bytes(), before)

    def test_concurrent_runs_share_one_environment(self):
        self.script("import first; print(first.VALUE)")
        processes = [
            subprocess.Popen(
                [*UVLAZY, "run", "app.py"],
                cwd=self.root,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual(stdout, "1\n")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def read(self, text):
        (self.root / "pyproject.toml").write_text(text)
        return read_project(self.root)

    def test_preserves_extras_markers_and_multiple_constraints(self):
        requirements = ['Some_Package[extra]>=1; python_version >= "3"', "some-package<3"]
        project = self.read(f"[project]\ndependencies = {json.dumps(requirements)}")
        self.assertEqual(project.requirements, {"some-package": requirements})
        self.assertEqual(project.imports, {"some_package": "some-package"})

    def test_alias_cannot_add_undeclared_dependency(self):
        with self.assertRaisesRegex(UvlazyError, "declared"):
            self.read('[tool.uvlazy.imports]\na = "not-declared"')

    def test_uv_sources_are_rejected_instead_of_ignored(self):
        with self.assertRaisesRegex(UvlazyError, "sources"):
            self.read('[tool.uv.sources]\na = {path = "../a"}')

    def test_dependency_changes_select_fresh_environment(self):
        before = self.read('[project]\ndependencies = ["first==1"]')
        after = self.read('[project]\ndependencies = ["first==2"]')
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_relocated_checkout_does_not_reuse_absolute_venv_paths(self):
        before = self.read('[project]\ndependencies = ["first"]')
        relocated = self.root / "other-checkout"
        relocated.mkdir()
        shutil.copyfile(self.root / "pyproject.toml", relocated / "pyproject.toml")
        self.assertNotEqual(before.fingerprint, read_project(relocated).fingerprint)

    def test_group_cycles_are_rejected(self):
        with self.assertRaisesRegex(UvlazyError, "cycle"):
            self.read(
                '[dependency-groups]\na = [{include-group = "b"}]\nb = [{include-group = "a"}]'
            )

    def test_unknown_included_group_is_rejected(self):
        with self.assertRaisesRegex(UvlazyError, "Unknown included"):
            self.read('[dependency-groups]\na = [{include-group = "missing"}]')

    def test_command_alias_cannot_add_undeclared_package(self):
        with self.assertRaisesRegex(UvlazyError, "declared"):
            self.read('[tool.uvlazy.commands]\nlint = "undeclared"')

    def test_cdk_alias_can_be_overridden_explicitly(self):
        project = self.read(
            '[project]\ndependencies = ["aws-cdk-cli", "custom-cdk"]\n'
            '[tool.uvlazy.commands]\ncdk = "custom-cdk"\n'
        )
        self.assertEqual(project.command_package("cdk", None, None), ("custom-cdk", []))
        self.assertEqual(project.command_package("cdk", None, "aws-cdk-cli"), ("aws-cdk-cli", []))

    def test_group_command_automatically_selects_direct_group(self):
        project = self.read("""
            [dependency-groups]
            ci = [{include-group = "lint"}, "unused"]
            lint = ["lint-tool"]
        """)
        self.assertEqual(project.command_package("lint-tool", None, None), ("lint-tool", ["lint"]))


if __name__ == "__main__":
    unittest.main()
