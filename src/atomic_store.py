"""Durable replacement and a reentrant cross-process lock for private state."""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading

_GATE = threading.RLock()
_LOCAL = threading.local()


@contextmanager
def state_lock(root):
    path = Path(root) / 'data/state-write.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = os.path.normcase(str(path.parent.resolve() / path.name))
    with _GATE:
        held = getattr(_LOCAL, 'held', {})
        if lock_key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if not path.stat().st_size:
                    stream.write(b'0'); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held[lock_key] = True
            _LOCAL.held = held
            try:
                yield
            finally:
                held.pop(lock_key)
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def locked(root_fn):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with state_lock(root_fn()):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def write_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))
