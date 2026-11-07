"""One local daily slot; the runner's file lock arbitrates across workers."""
from datetime import timedelta
import logging
from threading import Event, Thread

from almali_library_curator.scan.runner import local_now

logger = logging.getLogger(__name__)


def next_slot(now, scan_time):
    hour, minute = map(int, scan_time.split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot >= now else slot + timedelta(days=1)


class ScanScheduler:
    def __init__(self, runner, *, clock=local_now):
        self.runner = runner
        self.clock = clock
        self.stopped = Event()
        self.thread = None
        self.settings = runner.settings_loader(runner.settings.config_dir)
        self.due_at = next_slot(clock(), self.settings.scan_time)

    def tick(self):
        settings = self.runner.settings_loader(self.runner.settings.config_dir)
        now = self.clock()
        if settings.scan_time != self.settings.scan_time:
            self.due_at = next_slot(now, settings.scan_time)
        self.settings = settings
        if not settings.scheduler_enabled or now < self.due_at:
            return None
        self.due_at = next_slot(now + timedelta(seconds=1), settings.scan_time)
        return self.runner.run("schedule")

    def start(self):
        if self.thread is not None:
            return
        self.thread = Thread(target=self._loop, name="scan-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _loop(self):
        while not self.stopped.is_set():
            try:
                self.tick()
                delay = max(0.1, min(60.0, (self.due_at - self.clock()).total_seconds()))
            except Exception as error:
                logger.warning("scan_scheduler_failed", extra={"error": type(error).__name__})
                delay = 60.0
            self.stopped.wait(delay)
