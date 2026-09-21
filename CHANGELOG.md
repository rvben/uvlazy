# Changelog

## 0.1.2

- Enable lazy imports in managed Python tools and their Python subprocesses,
  including applications launched by native tools such as `cdk ls`. Preserve
  dependency selection and lock constraints without syncing the whole project.
- Recognize `import aws_cdk` as the declared `aws-cdk-lib` dependency, installing
  the SDK only when the application imports it.
- Keep interpreter probes and package builds outside the lazy import hook to
  avoid recursive installation.

## 0.1.1

- Recognize `cdk` as a command provided by the declared `aws-cdk-cli` dependency,
  selecting only that package and its dependencies without a manual alias.
- Report failed Python discovery with the uv executable and exit status, while
  preserving the underlying diagnostic. Configuration, mise trust, and cache
  failures no longer incorrectly report that Python is missing.

## 0.1.0

Initial experimental release for macOS and Linux.

- Run declared tools at their project's locked versions while installing only
  the selected tool and its dependencies. No dependency groups are required.
- Install declared Python packages when scripts first reach their missing imports.
- Preserve tool arguments, working directory, stdin, exit status, and signals.
- Reuse isolated environments and serialize concurrent installations.
- Install the same executable through Cargo or PyPI. The Rust launcher bundles
  the Python engine; uv and Python 3.11+ are required at runtime.

Lockfile exports constrain versions and sources but do not enforce artifact
hashes. Project installation, uv sources/workspaces, and Windows are not yet
supported. See the README for the full boundaries of this release.
