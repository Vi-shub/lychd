"""Execute an argv-safe pytest selection inside the repository check image."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """Preserve Make's public selectors without evaluating shell source."""
    if not Path("/opt/lychd-test-image").is_file() or not any(
        path.is_file() for path in (Path("/run/.containerenv"), Path("/.dockerenv"))
    ):
        message = "Core pytest requires the observed OCI test image."
        raise SystemExit(message)
    env = os.environ
    args = [sys.executable, "-m", "pytest", "-n", env.get("N", "0"), "--dist", "loadscope"]
    if env.get("VERBOSE") == "1":
        args.extend(("-s", "--tb=long", "-o", "log_cli=true", "--log-cli-level=DEBUG"))
    else:
        args.append("-qq")
    if env.get("K"):
        args.extend(("-k", env["K"]))
    args.extend(("-m", env.get("M") or "not container"))
    targets = shlex.split(env.get("PYTEST_TARGETS", "tests"))
    extra = shlex.split(env.get("ARGS", ""))
    if extra and targets == ["tests"]:
        targets = []
    basetemp = env.get("PYTEST_BASETEMP")
    if not basetemp:
        root = Path(env.get("PYTEST_BASE_ROOT", ".cache/pytest"))
        root.mkdir(parents=True, exist_ok=True)
        basetemp = tempfile.mkdtemp(prefix="run.", dir=root)
    else:
        Path(basetemp).parent.mkdir(parents=True, exist_ok=True)
    args.extend((*extra, "--basetemp", basetemp, *targets))
    os.execv(sys.executable, args)  # noqa: S606 - Fixed interpreter replaces this argv-only harness.


if __name__ == "__main__":
    main()
