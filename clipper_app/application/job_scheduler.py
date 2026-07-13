from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledWork:
    callback: Callable[[], None]
    compute_heavy: bool = False


class BoundedDaemonScheduler:
    """Two fixed daemon-worker lanes with bounded pending work."""

    def __init__(
        self,
        *,
        interactive_workers: int = 1,
        interactive_pending: int = 16,
        batch_workers: int = 2,
        batch_pending: int = 8,
    ) -> None:
        self._condition = threading.Condition()
        self._interactive: deque[ScheduledWork] = deque()
        self._batch_regular: deque[ScheduledWork] = deque()
        self._batch_compute: deque[ScheduledWork] = deque()
        self._pending_limits = {
            "interactive": max(0, interactive_pending),
            "batch": max(0, batch_pending),
        }
        self._pending_counts = {"interactive": 0, "batch": 0}
        self._compute_active = False
        self._workers: list[threading.Thread] = []
        self._start_workers("interactive", interactive_workers)
        self._start_workers("batch", batch_workers)

    def submit(
        self,
        lane: str,
        callback: Callable[[], None],
        *,
        compute_heavy: bool = False,
    ) -> bool:
        work = ScheduledWork(callback=callback, compute_heavy=compute_heavy)
        with self._condition:
            if self._pending_counts[lane] >= self._pending_limits[lane]:
                return False
            if lane == "interactive":
                self._interactive.append(work)
            elif compute_heavy:
                self._batch_compute.append(work)
            else:
                self._batch_regular.append(work)
            self._pending_counts[lane] += 1
            self._condition.notify_all()
        return True

    def _start_workers(self, lane: str, count: int) -> None:
        for index in range(max(0, count)):
            worker = threading.Thread(
                target=self._worker,
                args=(lane,),
                name=f"clipper-control-{lane}-{index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _next_work(self, lane: str) -> ScheduledWork:
        with self._condition:
            while True:
                work: ScheduledWork | None = None
                if lane == "interactive" and self._interactive:
                    work = self._interactive.popleft()
                elif lane == "batch":
                    # Non-compute export work may always occupy the second worker.
                    if self._batch_regular:
                        work = self._batch_regular.popleft()
                    elif self._batch_compute and not self._compute_active:
                        work = self._batch_compute.popleft()
                        self._compute_active = True
                if work is not None:
                    self._pending_counts[lane] -= 1
                    return work
                self._condition.wait()

    def _worker(self, lane: str) -> None:
        while True:
            work = self._next_work(lane)
            try:
                work.callback()
            except Exception:
                # The service callback persists its own failure state. Keep the
                # fixed worker alive if an unexpected persistence error escapes.
                pass
            finally:
                if work.compute_heavy:
                    with self._condition:
                        self._compute_active = False
                        self._condition.notify_all()
                del work
