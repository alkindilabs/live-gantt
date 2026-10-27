"""Run the local live Gantt web app without dependency installation."""

import argparse
import signal
import threading

from .endpoint import account_server, server_url
from .sources import default_root
from .codex import default_codex_root
from .usage import default_usage_root


def main():
    """Serve local Claude Code and Codex sessions until stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="bind exactly this port instead of automatic allocation")
    args = parser.parse_args()
    try:
        with account_server(default_root(), port=args.port, codex_root=default_codex_root(),
                            usage_root=default_usage_root()) as server:
            def stop(_signal, _frame):
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, stop)
            print(f"Live Gantt: {server_url(server.server_address[1])}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as error:
        parser.exit(status=1, message=f"Live Gantt could not start: {error}\n")


if __name__ == "__main__":
    main()
