"""
Cooperative engine shutdown: stop file, interruptible waits, PID lifecycle.

The UI writes engine.stop; the runner polls this between work chunks and releases
the SocketLock (port 19888) immediately so Stop Monitoring does not hang.
"""
from __future__ import annotations

import os
import socket
import sys
import time
import logging

from config.settings import config

logger = logging.getLogger(__name__)

LOCK_PORT = 19888


def stop_file_path() -> str:
    return config.PATHS["STOP_FILE"]


def pid_file_path() -> str:
    return config.PATHS["PID_FILE"]


def request_stop() -> None:
    with open(stop_file_path(), "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))


def is_stop_requested() -> bool:
    return os.path.exists(stop_file_path())


def clear_stop_signal() -> None:
    try:
        if os.path.exists(stop_file_path()):
            os.remove(stop_file_path())
    except OSError:
        pass


def write_pid() -> None:
    try:
        with open(pid_file_path(), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.error(f"[SHUTDOWN] Failed to write PID file: {e}")


def remove_pid() -> None:
    try:
        if os.path.exists(pid_file_path()):
            os.remove(pid_file_path())
    except OSError:
        pass


def read_pid() -> int | None:
    if not os.path.exists(pid_file_path()):
        return None
    try:
        with open(pid_file_path(), encoding="utf-8") as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def is_port_locked() -> bool:
    """True if the runner SocketLock port is in use (engine running)."""
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test_sock.settimeout(0.5)
        test_sock.bind(("127.0.0.1", LOCK_PORT))
        return False
    except OSError:
        return True
    finally:
        try:
            test_sock.close()
        except Exception:
            pass


def is_process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_engine_running() -> tuple[bool, int | None]:
    """
    Engine is running if SocketLock port is bound.
    PID is read from engine.pid when port is locked (authoritative runner PID).
    """
    if not is_port_locked():
        remove_pid()
        return False, None
    return True, read_pid()


def interruptible_sleep(seconds: float, step: float = 0.25) -> bool:
    """
    Sleep in small chunks. Returns True if stop was requested during the wait.
    """
    if seconds <= 0:
        return is_stop_requested()
    elapsed = 0.0
    while elapsed < seconds:
        if is_stop_requested():
            return True
        chunk = min(step, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
    return is_stop_requested()


def terminate_process(pid: int | None) -> bool:
    """Last resort: kill runner process by PID."""
    if pid is None or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import subprocess
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
            return r.returncode == 0
        os.kill(pid, 9)
        return True
    except OSError:
        return False
