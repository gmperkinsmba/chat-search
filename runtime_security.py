"""Owner-only local runtime storage checks for SQLite paths."""
import os
import stat
from pathlib import Path


class StorageSecurityError(RuntimeError):
    """Raised when local data cannot be protected safely."""


def ensure_private_directory(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise StorageSecurityError("Local data directory must be an owned, real directory.")
            os.fchmod(descriptor, 0o700)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise StorageSecurityError("Local data directory permissions could not be secured.")
        finally:
            os.close(descriptor)
    except StorageSecurityError:
        raise
    except OSError:
        raise StorageSecurityError("Local data directory could not be secured.") from None


def _secure_file(path, create):
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise StorageSecurityError("Local database files must be owned regular files.")
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise StorageSecurityError("Local database file permissions could not be secured.")
        finally:
            os.close(descriptor)
    except StorageSecurityError:
        raise
    except OSError:
        raise StorageSecurityError("Local database file could not be secured.") from None


def prepare_database(path):
    """Secure the parent, database, and any existing SQLite sidecars."""
    path = Path(path)
    ensure_private_directory(path.parent)
    _secure_file(path, create=True)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _secure_file(sidecar, create=False)


def verify_database(path):
    """Recheck database and sidecar modes after SQLite initializes WAL."""
    path = Path(path)
    _secure_file(path, create=False)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _secure_file(sidecar, create=False)
