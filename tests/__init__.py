import sys

from live_gantt.python_support import MINIMUM_PYTHON

if sys.version_info < MINIMUM_PYTHON:
    minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
    raise ImportError(f"The test suite requires Python {minimum} or newer; this is Python {sys.version.split()[0]}.")
