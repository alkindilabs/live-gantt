"""Report the current account's verified chart address without reading session data."""

import sys

from .endpoint import discover_server_url


def main():
    """Print a verified URL or report that this account's server could not be verified."""
    address = discover_server_url()
    if address is None:
        print("No running Live Gantt server could be verified for this account. "
              "Check its installation and service logs.", file=sys.stderr)
        return 1
    print(f"Live Gantt: {address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
