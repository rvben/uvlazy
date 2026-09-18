"""Bootstrap appended to the embedded files dictionary by build.rs."""

import hashlib
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 - Cargo installs have no Python version gate
    sys.exit("uvlazy: Python 3.11+ is required; select a compatible interpreter with UV_PYTHON.")


def engine_archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, source in files.items():  # noqa: F821 - supplied by build.rs
            # Fixed timestamps make the archive and cache key reproducible.
            archive.writestr(zipfile.ZipInfo(name), source)
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    override = os.environ.get("UVLAZY_CACHE_DIR")
    if override:
        cache = Path(override)
    else:
        default = (
            Path.home() / "Library" / "Caches"
            if sys.platform == "darwin"
            else Path.home() / ".cache"
        )
        xdg = Path(os.environ.get("XDG_CACHE_HOME", str(default)))
        cache = (xdg if xdg.is_absolute() else default) / "uvlazy"
    cache = cache.resolve() / "engines"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    engine = cache / f"{digest}.zip"
    if not engine.is_file() or engine.read_bytes() != payload:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
            temporary.replace(engine)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return engine


try:
    sys.path.insert(0, str(engine_archive()))
except OSError as exc:
    sys.exit(
        f"uvlazy: cannot prepare bundled engine: {exc}. Set UVLAZY_CACHE_DIR to a writable path."
    )

sys.argv[0] = "uvlazy"
from uvlazy.cli import main  # noqa: E402

raise SystemExit(main())
