import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from live_gantt.server import make_server


POLL_SECONDS = 0.04
HEARTBEAT_SECONDS = 0.12


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks" / "session-one"
        self.tasks.mkdir(parents=True)
        self.task = self.tasks / "1.json"
        self.write_task("Initial task")
        self.server = make_server(
            self.root, port=0,
            poll_seconds=POLL_SECONDS, heartbeat_seconds=HEARTBEAT_SECONDS,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": POLL_SECONDS},
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def write_task(self, subject):
        self.task.write_text(json.dumps({
            "id": "1", "subject": subject, "description": "Local fixture",
            "status": "pending", "blockedBy": [], "blocks": [],
            "metadata": {"estimate": {"low": 1, "likely": 2, "high": 3}},
        }))

    def connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_foreign_and_duplicate_hosts_are_refused_on_every_method(self):
        for method in ("GET", "POST", "HEAD"):
            with self.subTest(method=method):
                connection = self.connection()
                connection.request(method, "/api/lists", headers={"Host": "evil.example"})
                self.assertEqual(connection.getresponse().status, 403)
        connection = self.connection()
        connection.putrequest("GET", "/api/lists")
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 403)

    def test_local_hosts_json_and_missing_list(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            connection = self.connection()
            connection.request("GET", "/api/state?list=session-one", headers={"Host": host})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
            model = json.load(response)
            self.assertEqual(model["tasks"][0]["subject"], "Initial task")
            self.assertIsNotNone(model["tasks"][0]["projectedEnd"])
        for path, status in (("/api/state?list=missing", 404),
                             ("/api/state?list=../outside", 400),
                             ("/api/state", 400), ("/server.py", 404)):
            connection = self.connection()
            connection.request("GET", path)
            self.assertEqual(connection.getresponse().status, status)

    def test_stream_emits_changed_state_but_not_unchanged_state(self):
        connection = self.connection()
        connection.request("GET", "/events?list=session-one")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")

        def next_event():
            kind, data = None, None
            while True:
                line = response.readline().decode().strip()
                if line.startswith("event: "):
                    kind = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                elif not line and kind:
                    return kind, data

        initial = []
        while not any(kind == "state" for kind, _ in initial):
            initial.append(next_event())
        first = next(data for kind, data in initial if kind == "state")
        self.assertEqual(first["tasks"][0]["subject"], "Initial task")
        self.assertEqual(next_event()[0], "heartbeat")
        self.write_task("Changed task")
        for _ in range(10):
            kind, data = next_event()
            if kind == "state":
                self.assertEqual(data["tasks"][0]["subject"], "Changed task")
                break
        else:
            self.fail("No state event after the task file changed")
        response.close()

    def test_idle_start_does_not_read_sources(self):
        self.assertIsNone(self.server.monitor.worker)
        self.assertEqual(self.server.monitor.models, {})
        self.assertEqual(self.server.monitor.list_model, [])


if __name__ == "__main__":
    unittest.main()
