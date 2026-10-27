"""Install a stable per-user copy and its operating-system service."""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from .python_support import MINIMUM_PYTHON, supported

APPLICATION_DIRECTORY = "live-gantt"
PACKAGE_DIRECTORY = "live_gantt"
VERSION_FILENAME = "version"
INSTALLATION_SETTINGS_FILENAME = "installation.json"
SOURCE_ROOT_VARIABLE = "CLAUDE_CONFIG_DIR"
SOURCE_ROOT_VARIABLES = (SOURCE_ROOT_VARIABLE, "CODEX_HOME")
CODEX_COMMAND_VARIABLE = "LIVE_GANTT_CODEX_BIN"
OWNER_READ_WRITE = 0o600
OWNER_EXECUTABLE = 0o700
CODEX_LAUNCHER_PATH = Path(".local/bin/live-gantt-codex")
CODEX_LAUNCHER_HEADER = "#!/bin/sh\n# Managed by live-gantt: codex launcher v1\n"
LAUNCHD_TEMPLATE = Path("launchd/local.live-gantt.plist")
SYSTEMD_TEMPLATE = Path("systemd/live-gantt.service")
LAUNCHD_LABEL = "local.live-gantt"
BOOTOUT_POLL_SECONDS = 0.1
BOOTOUT_WAIT_SECONDS = 10


def _xdg_directory(environ, variable, fallback):
    candidate = Path(environ.get(variable, ""))
    return candidate if candidate.is_absolute() else fallback


def installation_directory(system, home, environ):
    """Return the stable data directory for macOS or Linux, without filesystem access."""
    home = Path(home)
    if system == "Darwin":
        return home / "Library" / "Application Support" / APPLICATION_DIRECTORY
    if system == "Linux":
        return _xdg_directory(environ, "XDG_DATA_HOME", home / ".local" / "share") / APPLICATION_DIRECTORY
    raise ValueError(f"Unsupported operating system: {system}")


def installation_settings(environ, previous):
    """Preserve installed source roots and the native Codex command unless explicitly replaced."""
    settings = {}
    for variable in SOURCE_ROOT_VARIABLES:
        source_root = environ.get(variable, previous.get(variable))
        if isinstance(source_root, str) and source_root:
            settings[variable] = str(Path(source_root).expanduser().resolve())
    executable = environ.get(CODEX_COMMAND_VARIABLE, previous.get(CODEX_COMMAND_VARIABLE))
    if isinstance(executable, str) and executable:
        settings[CODEX_COMMAND_VARIABLE] = str(Path(executable).expanduser().absolute())
    return settings


def service_identifier(system):
    """Return the service identity shared by registration and legacy endpoint discovery."""
    if system == "Darwin":
        return LAUNCHD_LABEL
    if system == "Linux":
        return SYSTEMD_TEMPLATE.name
    raise ValueError(f"Unsupported operating system: {system}")


def launchd_configuration(plugin_root, directory, python, log, settings):
    """Render the launchd template for the stable copy, Python interpreter and source root."""
    with (plugin_root / LAUNCHD_TEMPLATE).open("rb") as stream:
        config = plistlib.load(stream)
    config["Label"] = service_identifier("Darwin")
    config["ProgramArguments"][0] = str(python)
    config["WorkingDirectory"] = str(directory)
    config["StandardOutPath"] = config["StandardErrorPath"] = str(log)
    config["EnvironmentVariables"].update(settings)
    return config


def _systemd_quote(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def systemd_configuration(plugin_root, directory, python, settings):
    """Render the systemd user unit with escaped literal paths and source configuration."""
    service_environment = {**settings, "XDG_DATA_HOME": str(directory.parent)}
    environment = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in service_environment.items()
    )
    replacements = {
        "@PYTHON@": _systemd_quote(python),
        "@DIRECTORY@": _systemd_quote(directory),
        "@ENVIRONMENT@": environment,
    }
    unit = (plugin_root / SYSTEMD_TEMPLATE).read_text()
    for placeholder, value in replacements.items():
        unit = unit.replace(placeholder, value)
    return unit


def _read_settings(directory):
    try:
        settings = json.loads((directory / INSTALLATION_SETTINGS_FILENAME).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(settings, dict):
        raise ValueError("Installed configuration must be an object")
    return settings


def _stop_service(system, identifier):
    if system != "Darwin":
        subprocess.run(["systemctl", "--user", "stop", identifier], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    service = f"gui/{os.getuid()}/{identifier}"
    subprocess.run(["launchctl", "bootout", service], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_until_launchd_forgets(service)


def _wait_until_launchd_forgets(service):
    """Wait, at most BOOTOUT_WAIT_SECONDS, until launchd no longer lists `service`.

    `launchctl bootout` returns before launchd has removed the service, and bootstrapping the
    same label meanwhile fails with error 5 (Input/output error)."""
    deadline = time.monotonic() + BOOTOUT_WAIT_SECONDS
    while time.monotonic() < deadline:
        listed = subprocess.run(["launchctl", "print", service], check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if listed.returncode != 0:
            return
        time.sleep(BOOTOUT_POLL_SECONDS)


def _service_target(system, home, environ):
    identifier = service_identifier(system)
    if system == "Darwin":
        return identifier, home / "Library" / "LaunchAgents" / f"{identifier}.plist"
    config_root = _xdg_directory(environ, "XDG_CONFIG_HOME", home / ".config")
    return identifier, config_root / "systemd" / "user" / identifier


def service_python(which, executable):
    """The interpreter the service runs: `python3` as found on PATH when it is a supported
    version, kept unresolved so that a package manager moving its versioned binary (Homebrew's
    Cellar) does not strand the service; else the interpreter running the installer, which its
    entry points have already checked. Codex's daemon finds macOS's Python 3.9 first on PATH."""
    found = which("python3")
    return Path(found) if found and supported(found) else Path(executable)


def _owns_codex_launcher(target):
    if target.is_symlink() or not target.is_file():
        return False
    header = CODEX_LAUNCHER_HEADER.encode("utf-8")
    with target.open("rb") as stream:
        return stream.read(len(header)) == header


def _validate_codex_launcher(target):
    if target.is_symlink() or target.exists():
        if not _owns_codex_launcher(target):
            raise FileExistsError(f"Refusing to replace existing command: {target}")


def _write_codex_launcher(target, directory, python):
    target.parent.mkdir(parents=True, exist_ok=True)
    command = (
        f"PYTHONPATH={shlex.quote(str(directory))} "
        f"exec {shlex.quote(str(python))} -B -P -m live_gantt.codex_launcher \"$@\"\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", prefix=f".{target.name}-",
                                     dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
        stream.write(CODEX_LAUNCHER_HEADER + command)
    try:
        staged.chmod(OWNER_EXECUTABLE)
        _validate_codex_launcher(target)
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def install(plugin_root):
    """Copy this plugin's package into stable storage and restart its per-user service."""
    system = platform.system()
    home = Path.home()
    directory = installation_directory(system, home, os.environ)
    manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    version = manifest["version"]
    settings = installation_settings(os.environ, _read_settings(directory))
    identifier, target = _service_target(system, home, os.environ)
    python = service_python(shutil.which, sys.executable)
    codex_launcher = home / CODEX_LAUNCHER_PATH
    _validate_codex_launcher(codex_launcher)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=directory) as staging:
        staged_package = Path(staging) / PACKAGE_DIRECTORY
        shutil.copytree(
            plugin_root / PACKAGE_DIRECTORY, staged_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        _stop_service(system, identifier)
        current_package = directory / PACKAGE_DIRECTORY
        if current_package.exists():
            current_package.rename(Path(staging) / "previous-package")
        staged_package.rename(current_package)
        target.parent.mkdir(parents=True, exist_ok=True)
        if system == "Darwin":
            log = home / "Library" / "Logs" / f"{APPLICATION_DIRECTORY}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            config = launchd_configuration(plugin_root, directory, python, log, settings)
            target.write_bytes(plistlib.dumps(config))
        else:
            target.write_text(systemd_configuration(plugin_root, directory, python, settings))
        target.chmod(OWNER_READ_WRITE)
        settings_path = directory / INSTALLATION_SETTINGS_FILENAME
        settings_path.write_text(json.dumps(settings) + "\n")
        settings_path.chmod(OWNER_READ_WRITE)
        _write_codex_launcher(codex_launcher, directory, python)
        if system == "Darwin":
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
        else:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", identifier], check=True)
        (directory / VERSION_FILENAME).write_text(version + "\n")


def uninstall(plugin_root):
    """Stop the service and remove its generated configuration and installed package."""
    system = platform.system()
    home = Path.home()
    directory = installation_directory(system, home, os.environ)
    identifier, target = _service_target(system, home, os.environ)
    if system == "Linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", identifier], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        _stop_service(system, identifier)
    target.unlink(missing_ok=True)
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    package = directory / PACKAGE_DIRECTORY
    if package.exists():
        shutil.rmtree(package)
    for name in (VERSION_FILENAME, INSTALLATION_SETTINGS_FILENAME):
        (directory / name).unlink(missing_ok=True)
    codex_launcher = home / CODEX_LAUNCHER_PATH
    if _owns_codex_launcher(codex_launcher):
        codex_launcher.unlink()
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


def main():
    """Install or uninstall the local service from this source tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    args = parser.parse_args()
    if sys.version_info < MINIMUM_PYTHON:
        parser.error("Python {}.{} or later is required".format(*MINIMUM_PYTHON))
    plugin_root = Path(__file__).resolve().parents[1]
    try:
        if args.action == "install":
            install(plugin_root)
            from .endpoint import wait_for_server_url

            address = wait_for_server_url()
            if address is None:
                raise OSError("service registered, but no running server could be verified for this account; "
                              "check its service logs")
            print(f"Live Gantt installed: {address}")
        else:
            uninstall(plugin_root)
            print("Live Gantt uninstalled. Claude Code files and service logs are retained.")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(status=1, message=f"Live Gantt {args.action} failed: {error}\n")


if __name__ == "__main__":
    main()
