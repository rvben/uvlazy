# uvlazy

**Run the tool you need, at the project's locked version, without installing the
rest of the project.**

```sh
uvlazy run rumdl check .
```

uvlazy finds the declared package providing `rumdl`, reads the version constraints
from `uv.lock`, installs that package and its dependency tree into a dedicated
environment, and runs its executable. Application dependencies and unrelated
tools stay uninstalled. Python console scripts and native executables both work.

This is experimental software for macOS and Linux. It requires Python 3.11+ and
`uv` 0.6.6+ on PATH. Cargo and PyPI install the same Rust executable, which
bundles a Python engine with no third-party runtime dependencies.

## Install

Install through either registry:

```sh
cargo install uvlazy --locked
# or
uv tool install uvlazy
# or, in a Python environment
pip install uvlazy
```

Or install from this checkout (both routes require Rust):

```sh
cargo install --path . --locked
# or
uv tool install .
```

Or run directly from source without installing uvlazy:

```sh
PYTHONPATH=/path/to/uvlazy/src python3 -m uvlazy run rumdl check .
```

The executable uses `uv python find --system --no-project` to locate an
installed Python 3.11+ without syncing a project. Set `UV_PYTHON` to choose a
specific interpreter, for example `UV_PYTHON=3.12 uvlazy run rumdl check .`.
If no compatible Python is installed, run `uv python install 3.11` first.

## Use in CI

Declare the tool in your project's `pyproject.toml`, alongside your existing
application dependencies:

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["rich>=13", "Pillow>=10", "rumdl"]
```

No dependency groups or separate tool configuration are required when the
command and package names match.

Generate or update `uv.lock` during development with `uv lock`, and commit it
with your project. In CI, with uv and uvlazy available, run:

```sh
uvlazy run rumdl check .
```

There is no preceding `uv sync` step. Tool commands require an existing, current
lockfile; a missing or stale lock fails without updating it. The selected tool's
exit status, signals, stdin, stdout, stderr, arguments, and working directory
are preserved, so lint failures fail the job normally. Installation diagnostics
go to stderr; `--quiet` suppresses installation progress.

The [CI example](examples/ci/) includes a lockfile and declares Rich, Pillow, and
rumdl. Running the following installs only rumdl into the example's environment:

```sh
uvlazy run --project examples/ci rumdl check examples/ci
```

`--project` selects the dependency configuration without changing the current
working directory. Every option for uvlazy goes before the tool name; everything
after it is passed to the tool unchanged.

## Package names and dependency groups

Matching command/package names such as `rumdl` need no configuration. For a
command whose package name differs, select its declared provider explicitly:

```sh
uvlazy run --from httpie http --help
```

Or save that mapping:

```toml
[dependency-groups]
dev = ["httpie"]

[tool.uvlazy.commands]
http = "httpie"
```

Then use `uvlazy run http --help`. Providers must be declared in
`[project].dependencies`, `[dependency-groups]`, or legacy
`[tool.uv].dev-dependencies`. Unknown commands fail without installing arbitrary
packages or falling back to executables from the system PATH. The executable must
belong to the selected package.

By default, a tool declared in project dependencies uses those declarations.
Otherwise, uvlazy automatically selects groups that directly declare the tool.
Only the requested package and its dependency tree are installed, including when
its group contains other tools. Nested `{include-group = "lint"}` declarations
are supported.

To select a particular group, use:

```sh
uvlazy run --group lint rumdl check .
```

`--group` can be repeated. This is also useful when the same tool has different
requirements in different groups: choose compatible groups explicitly. uv
validates the selected combination against the project's lockfile.

## Lazy Python imports

Scripts and modules retain the original import-based behavior:

```sh
uvlazy run app.py --your-argument
uvlazy run -m your_module
uvlazy run --group dev app.py
uvlazy run --locked app.py
```

A script starts in a separate environment and installs declared packages when
execution first reaches their missing imports. Each imported package brings its
normal dependency tree. The script is never restarted, and exceptions inside
imported packages propagate normally. Existing modules and your local code take
precedence.

By default, scripts select `[project].dependencies`; `--group` adds groups.
Mappings handle differing import and distribution names:

```toml
[project]
name = "my-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["Pillow>=10"]

[tool.uvlazy.imports]
PIL = "Pillow"
```

`from PIL import Image` triggers Pillow's installation. An undeclared missing
import raises `ModuleNotFoundError` normally. Top-level imports count as use,
even when no functions from the package are subsequently called. Import
probes such as `importlib.util.find_spec()` can also trigger installation.

The [conditional example](examples/conditional/) has `plain`, `pretty`, and
`image` branches. From this checkout:

```sh
uvlazy run --project examples/conditional examples/conditional/demo.py plain
uvlazy run --project examples/conditional examples/conditional/demo.py pretty
uvlazy run --project examples/conditional examples/conditional/demo.py image
```

Unlike tool commands, scripts may run without a lockfile. At the first missing
import, `uv pip compile` resolves all selected requirements and caches the pins.
That resolution can fetch metadata or build artifacts for unused dependencies,
and an unsatisfiable unused dependency can prevent it. With `uv.lock`, the pins
come from `uv export --locked`. Add `--locked` to require and validate the lock
before any application code runs, even on paths that import nothing.

## Environments and caching

Environments and constraints live in `.uvlazy/<fingerprint>/`. Each tool package
and group selection has its own environment, separate from Python script runs.
Subsequent runs reuse installed packages. Concurrent runs serialize environment
creation and installation. A warm tool environment can run offline.

Changes to `pyproject.toml`, `uv.lock`, local `uv.toml`, the launcher interpreter,
selected groups, or checkout path select a fresh environment. This avoids
reusing virtualenv scripts whose absolute paths point to another checkout.
Existing `.venv` and `uv.lock` files are left alone.

For CI, cache uv's download cache to reuse package artifacts across jobs.
`.uvlazy` environments contain absolute paths and should only be reused at the
same checkout path with a compatible interpreter. Add `.uvlazy/` to the project's
`.gitignore`. Delete it while no uvlazy processes are running to start fresh.

The native executable also caches its bundled engine under
`$XDG_CACHE_HOME/uvlazy` (default: `~/Library/Caches/uvlazy` on macOS or
`~/.cache/uvlazy` on Linux). `UVLAZY_CACHE_DIR` overrides that location. The
archive is keyed by its contents and recreated locally when needed.

## Current boundaries

- Tool runs install the tool's normal dependency tree. They do not inject a lazy
  import hook into the executable or automatically install undeclared plugins.
  Tools that need the application installed, such as many pytest workflows,
  still need a complete project environment.
- Lock export supplies version/source constraints without enforcing lockfile
  artifact hashes. This is not a complete replacement for uv's locked sync.
- The project itself is not installed. Editable installs and uv sources/workspaces
  remain unsupported; uv sources/workspaces are rejected explicitly.
- Project optional-dependency groups cannot be selected yet. Extras within a
  selected requirement and environment markers are passed through to uv.
- For Python scripts, metadata queries, plugin discovery, namespace-package
  mappings, and newly installed `.pth` hooks have no special lazy handling.
  New Python subprocesses do not inherit the import hook.
- Set `UV_PYTHON` to an installed interpreter compatible with the project and
  selected groups. The default selects an installed Python 3.11+; it does not
  infer the project's Python requirement. Windows is not supported yet.

## Development

The integration suite generates local wheels and exercises real uv with network
access disabled, including command execution and selective installation.

```sh
cargo build --locked
UVLAZY_TEST_BINARY=target/debug/uvlazy PYTHONPATH=src \
  python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m unittest discover -s tests -v
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
ruff check src tests scripts examples
ruff format --check src tests scripts examples
```

`Cargo.toml` is the version source for both registries. maturin uses
[`bindings = "bin"`](https://www.maturin.rs/bindings.html#bin) to put the native
executable into platform wheels. Cargo builds embed the Python sources without
requiring Python at build time; running the installed command needs no checkout
or separately installed `uvlazy` Python package.

Verify both source packages before a release:

```sh
cargo package --locked
maturin build --release --sdist --locked --out dist
```

CI installs and tests both the Cargo executable and a wheel rebuilt from the
source distribution, with offline local-wheel fixtures. It covers Linux and
macOS, Python 3.11 and 3.14, and the minimum and latest uv versions.

The release workflow builds Linux and macOS wheels for x86_64 and aarch64, plus
a source distribution. A matching `v<version>` tag publishes to crates.io and
PyPI in independent jobs using `CARGO_REGISTRY_TOKEN` and `PYPI_API_TOKEN`
repository secrets. Manual runs default to `dry_run: true`; publishing requires
a matching version tag. After both registries succeed, the workflow creates a
GitHub release with the packages and SHA-256 checksums.

The implementation uses uv's [lockfile export](https://docs.astral.sh/uv/concepts/projects/export/)
and [constraints](https://docs.astral.sh/uv/pip/compile/), and Python's
[import finder API](https://docs.python.org/3/library/importlib.html).
