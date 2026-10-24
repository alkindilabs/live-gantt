"""Run the local live Gantt web app without dependency installation."""

import argparse
import signal
import threading

from .server import DEFAULT_PORT, make_server
from .sources import default_root
from .codex import default_codex_root
from .usage import default_usage_root

LOWEST_PORT = 1
HIGHEST_PORT = 65535


def main():
    """Serve local Claude Code and Codex sessions until stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if not LOWEST_PORT <= args.port <= HIGHEST_PORT:
        parser.error(f"port must be between {LOWEST_PORT} and {HIGHEST_PORT}")
    server = make_server(default_root(), args.port, codex_root=default_codex_root(),
                         usage_root=default_usage_root())

    def stop(_signal, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"Live Gantt: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
