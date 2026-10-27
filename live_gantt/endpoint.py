"""Allocate and discover one private, loopback chart endpoint per operating-system user."""

from contextlib import contextmanager
import errno
import fcntl
from http import HTTPStatus
from itertools import islice
import json
import os
from pathlib import Path
import platform
import socket
import stat
import subprocess
import sys
import time
import uuid

from .installation import installation_directory, service_identifier, VERSION_FILENAME
from .liveness import LSOF, PS, run_command


DEFAULT_PORT = 4791
LOOPBACK = "127.0.0.1"
LOWEST_PORT = 1
HIGHEST_PORT = 65535
OS_ASSIGNED_PORT = 0
SERVER_DIRECTORY = "server"
ENDPOINT_FILENAME = "endpoint.json"
LOCK_FILENAME = "lock"
RECORD_VERSION = 1
OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600
OTHER_PERMISSIONS = 0o077
MAX_RECORD_BYTES = 4096
MAX_PROC_BYTES = 1024 * 1024
MAX_FILE_DESCRIPTORS = 4096
INSPECTION_SECONDS = 2.0
READY_TIMEOUT_SECONDS = 2.0
STARTUP_WAIT_SECONDS = 10.0
STARTUP_POLL_SECONDS = 0.1
MAX_HEADER_BYTES = 16 * 1024
SOCKET_READ_BYTES = 4096
TCP_LISTEN_STATE = "0A"
PROC_START_TIME_INDEX = 19
PROC_ROOT = Path("/proc")
PROCESS_IDENTITY_FIELD_COUNT = 3
SERVICE_LIST_FIELD_COUNT = 3
HTTP_STATUS_FIELD_COUNT = 3
HTTP_SUCCESS_STATUS = str(HTTPStatus.OK.value).encode("ascii")
MINIMUM_PROCESS_ID = 1
PRIVATE_FILE_LINK_COUNT = 1
PROCESS_ROW_COUNT = 1


def _application_directory(directory):
    return (Path(directory) if directory is not None
            else installation_directory(platform.system(), Path.home(), os.environ))


def server_url(port):
    """Format the IPv4 loopback address used by every chart entry point."""
    return f"http://{LOOPBACK}:{port}"


def _private(info, kind):
    return (kind(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & OTHER_PERMISSIONS)


@contextmanager
def _directory(application, *, create=False):
    path = application / SERVER_DIRECTORY
    if create:
        path.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    try:
        if not _private(os.fstat(descriptor), stat.S_ISDIR):
            raise ValueError("Live Gantt server directory must be private and owned by this user")
        yield descriptor
    finally:
        os.close(descriptor)


def _open_file(directory, name, flags):
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                         OWNER_FILE_MODE, dir_fd=directory)
    info = os.fstat(descriptor)
    if not _private(info, stat.S_ISREG) or info.st_nlink != PRIVATE_FILE_LINK_COUNT:
        os.close(descriptor)
        raise ValueError("Live Gantt endpoint files must be private regular files owned by this user")
    return descriptor


def _valid_port(port):
    return type(port) is int and LOWEST_PORT <= port <= HIGHEST_PORT


def _read_record(directory):
    try:
        descriptor = _open_file(directory, ENDPOINT_FILENAME, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        contents = stream.read(MAX_RECORD_BYTES + 1)
    if len(contents) > MAX_RECORD_BYTES:
        raise ValueError("Live Gantt endpoint record is too large")
    record = json.loads(contents)
    if (not isinstance(record, dict) or type(record.get("version")) is not int
            or record["version"] != RECORD_VERSION or not _valid_port(record.get("port"))
            or type(record.get("pid")) is not int or record["pid"] < MINIMUM_PROCESS_ID
            or not isinstance(record.get("started"), str) or not record["started"]):
        raise ValueError("Invalid Live Gantt endpoint record")
    return record


def _write_record(directory, record):
    name = f".endpoint-{uuid.uuid4().hex}"
    descriptor = _open_file(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.write("\n")
        os.replace(name, ENDPOINT_FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _command(arguments, deadline):
    result = run_command(arguments, environment={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
                         deadline=deadline)
    return result.returncode, result.stdout.decode("utf-8")


def _read_proc(path):
    with path.open("rb") as stream:
        contents = stream.read(MAX_PROC_BYTES + 1)
    if len(contents) > MAX_PROC_BYTES:
        raise ValueError("Process metadata exceeds the inspection limit")
    return contents.decode("ascii")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Live Gantt endpoint verification timed out")
    return remaining


def _process_start(pid, deadline):
    _remaining(deadline)
    if platform.system() == "Linux":
        process = PROC_ROOT / str(pid)
        if process.stat().st_uid != os.getuid():
            return None
        fields = _read_proc(process / "stat").rpartition(") ")[2].split()
        started = fields[PROC_START_TIME_INDEX]
        boot = _read_proc(PROC_ROOT / "sys/kernel/random/boot_id").strip()
        return f"{boot}:{started}"
    code, output = _command([PS, "-o", "pid=,uid=,lstart=", "-p", str(pid)], deadline)
    rows = output.strip().splitlines()
    if code or len(rows) != PROCESS_ROW_COUNT:
        return None
    fields = rows[0].split(maxsplit=PROCESS_IDENTITY_FIELD_COUNT - 1)
    if len(fields) != PROCESS_IDENTITY_FIELD_COUNT or fields[:-1] != [str(pid), str(os.getuid())]:
        return None
    return fields[-1].strip() or None


def _mac_listener(pid, port, deadline):
    code, output = _command([LSOF, "-nP", "-a", "-p", str(pid),
                             f"-i4TCP@{LOOPBACK}:{port}", "-sTCP:LISTEN", "-F0puftPnT"], deadline)
    if code:
        return False
    process = {}
    files = []
    current = None
    for field in output.replace("\n", "").split("\0"):
        if not field:
            continue
        key, value = field[0], field[1:]
        if key == "p":
            if process:
                return False
            process[key] = value
        elif key == "u":
            process[key] = value
        elif key == "f":
            current = {key: value}
            files.append(current)
        elif current is not None:
            current.setdefault(key, []).append(value)
    return (process == {"p": str(pid), "u": str(os.getuid())}
            and any(item["f"].isdigit() and item.get("t") == ["IPv4"]
                    and item.get("P") == ["TCP"] and item.get("n") == [f"{LOOPBACK}:{port}"]
                    and "ST=LISTEN" in item.get("T", []) for item in files))


def _linux_listener(pid, port, deadline):
    descriptors = PROC_ROOT / str(pid) / "fd"
    with os.scandir(descriptors) as entries:
        paths = list(islice(entries, MAX_FILE_DESCRIPTORS + 1))
    if len(paths) > MAX_FILE_DESCRIPTORS:
        return False
    sockets = set()
    for entry in paths:
        _remaining(deadline)
        try:
            target = os.readlink(entry.path)
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            sockets.add(target[len("socket:["):-1])
    address = f"{int.from_bytes(socket.inet_aton(LOOPBACK), sys.byteorder):08X}:{port:04X}"
    for row in _read_proc(PROC_ROOT / "self/net/tcp").splitlines()[1:]:
        _remaining(deadline)
        try:
            _, local, _, state, _, _, _, uid, _, inode, *unused = row.split()
        except ValueError:
            continue
        if local == address and state == TCP_LISTEN_STATE and uid == str(os.getuid()) and inode in sockets:
            return True
    return False


def _owns_listener(record, deadline):
    if _process_start(record["pid"], deadline) != record["started"]:
        return False
    if platform.system() == "Linux":
        return _linux_listener(record["pid"], record["port"], deadline)
    if platform.system() == "Darwin":
        return _mac_listener(record["pid"], record["port"], deadline)
    return False


def _legacy_record(application, deadline):
    marker = (application / VERSION_FILENAME).lstat()
    if marker.st_uid != os.getuid() or not stat.S_ISREG(marker.st_mode):
        return None
    if platform.system() == "Darwin":
        code, output = _command(["/bin/launchctl", "list"], deadline)
        matches = [fields[0] for row in output.splitlines()
                   if len(fields := row.split()) == SERVICE_LIST_FIELD_COUNT
                   and fields[-1] == service_identifier("Darwin")]
        if code or len(matches) != PROCESS_ROW_COUNT or not matches[0].isdigit():
            return None
        pid = int(matches[0])
    elif platform.system() == "Linux":
        code, output = _command(["systemctl", "--user", "--no-pager", "show", service_identifier("Linux"),
                                 "--property=MainPID", "--value"], deadline)
        if code or not output.strip().isdigit():
            return None
        pid = int(output.strip())
    else:
        return None
    if pid < MINIMUM_PROCESS_ID:
        return None
    started = _process_start(pid, deadline)
    return {"port": DEFAULT_PORT, "pid": pid, "started": started} if started else None


def _record_is_locked(directory):
    descriptor = _open_file(directory, LOCK_FILENAME, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _ready(record):
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    with socket.create_connection((LOOPBACK, record["port"]), timeout=_remaining(deadline)) as connection:
        if not _owns_listener(record, deadline):
            return False
        request = f"HEAD / HTTP/1.1\r\nHost: {LOOPBACK}:{record['port']}\r\nConnection: close\r\n\r\n"
        connection.settimeout(_remaining(deadline))
        connection.sendall(request.encode("ascii"))
        headers = bytearray()
        while len(headers) < MAX_HEADER_BYTES:
            connection.settimeout(_remaining(deadline))
            block = connection.recv(min(SOCKET_READ_BYTES, MAX_HEADER_BYTES - len(headers)))
            if not block:
                return False
            headers.extend(block)
            if b"\r\n\r\n" in headers:
                status = bytes(headers).partition(b"\r\n")[0].split(b" ", HTTP_STATUS_FIELD_COUNT - 1)
                return (len(status) == HTTP_STATUS_FIELD_COUNT
                        and status[0] in {b"HTTP/1.1", b"HTTP/1.0"} and status[1] == HTTP_SUCCESS_STATUS)
        return False


def discover_server_url(*, directory=None):
    """Return a responsive current-user chart URL, or None if identity cannot be verified.

    Only owned runtime metadata and a verified listener's HEAD response are read. Missing,
    stale, unsafe or unavailable evidence never falls back to another user's chart.
    """
    application = _application_directory(directory)
    deadline = time.monotonic() + INSPECTION_SECONDS
    try:
        with _directory(application) as descriptor:
            record = _read_record(descriptor) if descriptor is not None else None
            if record is not None and not _record_is_locked(descriptor):
                return None
        legacy = record is None
        if legacy:
            record = _legacy_record(application, deadline)
        if record is None or not _owns_listener(record, deadline):
            return None
        if not _ready(record):
            return None
        deadline = time.monotonic() + INSPECTION_SECONDS
        if not _owns_listener(record, deadline):
            return None
        if legacy:
            if _legacy_record(application, deadline) != record:
                return None
        else:
            with _directory(application) as descriptor:
                if (descriptor is None or _read_record(descriptor) != record
                        or not _record_is_locked(descriptor)):
                    return None
        return server_url(record["port"])
    except (OSError, ValueError, UnicodeError, IndexError, subprocess.SubprocessError):
        return None


def wait_for_server_url(*, directory=None):
    """Wait briefly for a newly registered service to publish a verifiable endpoint."""
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    while True:
        address = discover_server_url(directory=directory)
        remaining = deadline - time.monotonic()
        if address is not None or remaining <= 0:
            return address
        time.sleep(min(STARTUP_POLL_SECONDS, remaining))


@contextmanager
def account_server(root, *, port=None, directory=None, codex_root=None, usage_root=None):
    """Bind and register this account's chart; release its listener and lock on exit.

    Automatic allocation reuses the saved port, then the default, then an operating-system
    assigned port. An explicit port binds exactly. Invalid or unsafe state raises ValueError;
    a competing instance, storage failure or unsuccessful bind raises OSError.
    """
    from .server import make_server

    if port is not None and not _valid_port(port):
        raise ValueError(f"port must be between {LOWEST_PORT} and {HIGHEST_PORT}")
    with _directory(_application_directory(directory), create=True) as descriptor:
        lock = _open_file(descriptor, LOCK_FILENAME, os.O_RDWR | os.O_CREAT)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OSError("Live Gantt is already running for this account") from error
            previous = _read_record(descriptor)
            started = _process_start(os.getpid(), time.monotonic() + INSPECTION_SECONDS)
            if not started:
                raise OSError("Cannot verify this process's ownership")
            candidates = [port] if port is not None else [DEFAULT_PORT, OS_ASSIGNED_PORT]
            if port is None and previous:
                candidates.insert(0, previous["port"])
            server = None
            for candidate in dict.fromkeys(candidates):
                try:
                    server = make_server(root, candidate, codex_root=codex_root, usage_root=usage_root)
                    break
                except OSError as error:
                    if port is not None or error.errno != errno.EADDRINUSE or candidate == OS_ASSIGNED_PORT:
                        raise
            try:
                _write_record(descriptor, {"version": RECORD_VERSION, "port": server.server_address[1],
                                           "pid": os.getpid(), "started": started})
                yield server
            finally:
                server.server_close()
        finally:
            os.close(lock)
