import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from src.logger import LoggerMixin


class DownloadPool(LoggerMixin):
    """Runs downloads in worker threads.

    `submit` blocks while the queue is full, so the search does not run far ahead of the downloads
    and an interrupted run loses at most a few queued publications.
    """

    def __init__(self, workers: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download")
        self._slots = threading.BoundedSemaphore(workers * 2)
        self._pending: set[Future] = set()
        self._lock = threading.Lock()

    def submit(self, task: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._slots.acquire()
        future = self._executor.submit(task, *args, **kwargs)
        with self._lock:
            self._pending.add(future)
        future.add_done_callback(self._on_done)

    def wait(self) -> None:
        """Block until every submitted task has finished."""
        with self._lock:
            pending = list(self._pending)
        wait(pending)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _on_done(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)
        self._slots.release()
        if not future.cancelled() and (exc := future.exception()) is not None:
            # Expected errors are handled by the task itself, so this is a bug worth the full traceback.
            self.logger.error("Download task crashed", exc_info=exc)
