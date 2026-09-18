# Changelog

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
