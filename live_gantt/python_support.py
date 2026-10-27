"""The Python versions Live Gantt supports, and interpreter selection for its entry points.

A hook runs under whichever `python3` its harness finds first. Codex's background app-server
daemon and desktop app start hooks with the login PATH, where macOS finds Apple's Python 3.9.
This module therefore stays importable on older versions; each hook calls
`require_supported_python()` before importing anything else from the package. Interactive
commands run as `python3 -B -m live_gantt.python_support live_gantt.<module> [arguments]`.
"""

import os
import subprocess
import sys

MINIMUM_PYTHON = (3, 11)
NEWER_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
SUPPORTED_CHECK = f"import sys; sys.exit(sys.version_info < {MINIMUM_PYTHON})"
PYTHON_PROBE_TIMEOUT_SECONDS = 1


def _supported_python():
    if sys.version_info >= MINIMUM_PYTHON:
        return sys.executable
    return next((candidate for candidate in NEWER_PYTHONS if supported(candidate)), None)


def require_supported_python():
    """Returns on a supported interpreter. On an older one, re-runs the current hook under the
    first interpreter in NEWER_PYTHONS that reports a supported version, with the same arguments
    and the still-unread standard input; exits successfully when there is none, since hooks never
    interrupt a session."""
    if sys.version_info >= MINIMUM_PYTHON:
        return
    candidate = _supported_python()
    if candidate is not None:
        try:
            os.execv(candidate, [candidate, "-B", os.path.abspath(sys.argv[0]), *sys.argv[1:]])
        except OSError:
            pass
    sys.exit(0)


def supported(candidate):
    """Whether the interpreter at `candidate` runs and reports at least MINIMUM_PYTHON."""
    if not os.access(candidate, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [candidate, "-c", SUPPORTED_CHECK], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            timeout=PYTHON_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main(argv=None):
    """Run a Live Gantt module under a supported Python without consuming its standard input."""
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or not arguments[0].startswith("live_gantt."):
        print("usage: python3 -B -m live_gantt.python_support live_gantt.<module> [arguments]",
              file=sys.stderr)
        return 2
    candidate = _supported_python()
    if candidate is None:
        minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
        print(f"Live Gantt requires Python {minimum} or newer; no supported interpreter was found.",
              file=sys.stderr)
        return 1
    try:
        os.execv(candidate, [candidate, "-B", "-m", *arguments])
    except OSError as error:
        print(f"Cannot start Live Gantt with {candidate}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
