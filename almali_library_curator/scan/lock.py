"""An open file description owns the scan slot, across threads/processes."""
import fcntl


class ScanLock:
    def __init__(self, config_dir):
        self.path = config_dir / "scan.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.file = handle
        return True

    def release(self):
        if self.file is not None:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
