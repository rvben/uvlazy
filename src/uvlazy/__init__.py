"""Project-locked tools and Python dependencies, installed on demand by uv."""

import tomllib
from importlib.resources import files
from pathlib import Path

try:
    __version__ = files(__package__).joinpath("_version").read_text(encoding="utf-8").strip()
except FileNotFoundError:
    # Direct source execution uses the same version as the native packages.
    manifest = Path(__file__).resolve().parents[2] / "Cargo.toml"
    __version__ = tomllib.loads(manifest.read_text(encoding="utf-8"))["package"]["version"]
