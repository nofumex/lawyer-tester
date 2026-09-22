"""Small bounded, lossy queue for noncritical network cleanup."""
import logging
import queue
import threading
import time
from contextlib import contextmanager


@contextmanager
def latency(section, **context):
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = (time.monotonic() - started) * 1000
        if elapsed > 200:
            logging.warning('latency section=%s duration_ms=%.1f %s', section, elapsed,
                            ' '.join(f'{k}={v}' for k, v in context.items()))


class BestEffort:
    def __init__(self, workers=2, capacity=1000):
        self.queue = queue.Queue(capacity)
        for i in range(workers):
            threading.Thread(target=self._run, name=f'cleanup-{i}', daemon=True).start()

    def submit(self, fn, *args, **kwargs):
        try:
            self.queue.put_nowait((fn, args, kwargs))
        except queue.Full:
            logging.warning('Cleanup queue full; dropping best-effort operation')

    def _run(self):
        while True:
            fn, args, kwargs = self.queue.get()
            try:
                with latency('cleanup'):
                    fn(*args, **kwargs)
            except Exception:
                logging.debug('Best-effort cleanup failed', exc_info=True)
            finally:
                self.queue.task_done()


cleanup = BestEffort()
