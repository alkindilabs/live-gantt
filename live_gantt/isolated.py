"""Share private native-runtime leases between launchers and read-only observers."""

from contextlib import contextmanager
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from .liveness import PS


RUNTIME_DIRECTORY = "codex-runtimes"
RUNTIME_LIMIT = 64
_LEASE_VERSION = 1
_LEASE_BYTES = 8192
_LEASE_NAME = re.compile(r"[0-9a-f]{32}\.json\Z")
_OWNER_DIRECTORY_MODE = 0o700
_OWNER_FILE_MODE = 0o600
_OTHER_PERMISSIONS = 0o077
_PROCESS_SECONDS = 1.0
_PROCESS_OUTPUT_BYTES = 16 * 1024


def runtime_registry_root(usage_root):
    if usage_root is None:
        from .usage import default_usage_root
        usage_root = default_usage_root()
    return Path(usage_root).parent / RUNTIME_DIRECTORY


def _private(info, kind):
    return kind(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & _OTHER_PERMISSIONS


def _process_starts(pids):
    if not pids:
        return {}
    if any(type(pid) is not int or pid <= 0 for pid in pids):
        raise ValueError("Invalid native process")
    result = subprocess.run([PS, "-o", "pid=,uid=,lstart=", "-p", ",".join(map(str, sorted(pids)))],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, "LC_ALL": "C"}, text=True, timeout=_PROCESS_SECONDS, check=False)
    if result.returncode or len(result.stdout) > _PROCESS_OUTPUT_BYTES:
        raise ValueError("Unverifiable native processes")
    bindings = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            raise ValueError("Invalid native process identity")
        pid = int(fields[0])
        if pid not in pids or pid in bindings:
            raise ValueError("Unexpected native process identity")
        if fields[1] == str(os.getuid()):
            bindings[pid] = fields[2].strip()
    return bindings


def _process_start(pid):
    started = _process_starts({pid}).get(pid)
    if started is None:
        raise ValueError("Unverifiable native process")
    return started


def _private_endpoint(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Noncanonical native socket")
    endpoint, directory = path.lstat(), path.parent.lstat()
    if not _private(endpoint, stat.S_ISSOCK) or not _private(directory, stat.S_ISDIR):
        raise ValueError("Untrusted native socket")
    return tuple((info.st_dev, info.st_ino) for info in (endpoint, directory))


@contextmanager
def _registry_directory(root, *, create=False):
    root = Path(root)
    if create:
        root.mkdir(mode=_OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not _private(os.fstat(descriptor), stat.S_ISDIR):
            raise ValueError("Untrusted runtime registry")
        yield descriptor
    finally:
        os.close(descriptor)


def _runtime_endpoints(registry_root, codex_root):
    candidates = []
    endpoints = []
    try:
        with _registry_directory(registry_root) as directory:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in islice(entries, RUNTIME_LIMIT + 1)]
            if len(names) > RUNTIME_LIMIT:
                return []
            for name in sorted(names):
                if not _LEASE_NAME.fullmatch(name):
                    continue
                try:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not _private(info, stat.S_ISREG) or info.st_nlink != 1:
                            continue
                        payload = stream.read(_LEASE_BYTES + 1)
                    if len(payload) > _LEASE_BYTES:
                        continue
                    lease = json.loads(payload)
                    if (not isinstance(lease, dict) or type(lease.get("version")) is not int
                            or lease["version"] != _LEASE_VERSION
                            or lease.get("codexRoot") != str(Path(codex_root).resolve())
                            or not isinstance(lease.get("socket"), str)
                            or not isinstance(lease.get("started"), str)
                            or type(lease.get("pid")) is not int or lease["pid"] <= 0):
                        continue
                    endpoint = Path(lease["socket"])
                    identity = _private_endpoint(endpoint)
                    candidates.append((endpoint, identity, lease["pid"], lease["started"]))
                except (OSError, ValueError, UnicodeError, RecursionError, subprocess.SubprocessError):
                    continue
        bindings = _process_starts({pid for _, _, pid, _ in candidates})
        for endpoint, identity, pid, started in candidates:
            try:
                if (bindings.get(pid) == started and _private_endpoint(endpoint) == identity
                        and endpoint not in endpoints):
                    endpoints.append(endpoint)
            except (OSError, ValueError):
                continue
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return endpoints


@contextmanager
def _register_runtime(registry_root, codex_root, socket_path, pid):
    """Publish one verified runtime until exit, removing only this lease's inode."""
    socket_path = Path(socket_path).resolve(strict=True)
    _private_endpoint(socket_path)
    lease = {"version": _LEASE_VERSION, "codexRoot": str(Path(codex_root).resolve()),
             "socket": str(socket_path), "pid": pid, "started": _process_start(pid)}
    with _registry_directory(registry_root, create=True) as directory:
        with os.scandir(directory) as entries:
            if len(list(islice(entries, RUNTIME_LIMIT))) >= RUNTIME_LIMIT:
                raise ValueError("Runtime registry is full")
        name = uuid.uuid4().hex + ".json"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             _OWNER_FILE_MODE, dir_fd=directory)
        identity = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(lease, stream, separators=(",", ":"))
            yield Path(registry_root) / name
        finally:
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
