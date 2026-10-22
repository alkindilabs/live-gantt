"""The Python versions Live Gantt supports, and the hand-off that keeps hooks on one of them.

A hook runs under whichever `python3` its harness finds first. Codex's background app-server
daemon and desktop app start hooks with the login PATH, where macOS finds Apple's Python 3.9.
This module therefore stays importable on older versions; each hook calls
`require_supported_python()` before importing anything else from the package.
"""

import os
import subprocess
import sys

MINIMUM_PYTHON = (3, 11)
NEWER_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
SUPPORTED_CHECK = f"import sys; sys.exit(sys.version_info < {MINIMUM_PYTHON})"


def require_supported_python():
    """Returns on a supported interpreter. On an older one, re-runs the current hook under the
    first interpreter in NEWER_PYTHONS that reports a supported version, with the same arguments
    and the still-unread standard input; exits successfully when there is none, since hooks never
    interrupt a session."""
    if sys.version_info >= MINIMUM_PYTHON:
        return
    for candidate in NEWER_PYTHONS:
        if supported(candidate):
            os.execv(candidate, [candidate, "-B", os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    sys.exit(0)


def supported(candidate):
    """Whether the interpreter at `candidate` runs and reports at least MINIMUM_PYTHON."""
    if not os.access(candidate, os.X_OK):
        return False
    return subprocess.run([candidate, "-c", SUPPORTED_CHECK], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
