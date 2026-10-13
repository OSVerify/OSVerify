"""One bounded priority queue; only the coordinator advances task dependencies."""

from __future__ import annotations

import heapq
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from itertools import count

from .backend import BackendCancelled
from .quota import QuotaExhausted

# Lower numbers take the next free slot. Running calls are never preempted.
FEEDBACK = 0
VALIDATION = 1
DEDUPLICATION = 2
VERIFY = 3


@dataclass(frozen=True)
class Call:
    priority: int
    args: tuple


class PriorityScheduler:
    def __init__(self, runner, workers, stop, cancel, trace):
        self.runner, self.workers, self.stop = runner, workers, stop
        self.cancel, self.trace = cancel, trace
        self.ready = []
        self.sequence = count()

    def spawn(self, task):
        self._advance(task)

    def _advance(self, task, value=None, error=None):
        try:
            call = task.throw(error) if error else task.send(value)
        except StopIteration:
            return
        heapq.heappush(self.ready, (call.priority, next(self.sequence), call, task))
        self.trace.record("job_queued", job=call.args[0], role=call.args[1], priority=call.priority)

    def run(self):
        active = {}
        # Tasks whose next call needed the backend after quota exhaustion. Calls
        # with exact cache hits still complete, so cached work is not discarded.
        parked = []
        quota_cancelled = False
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            while self.ready or active:
                if self.stop.is_set():
                    break
                while self.ready and len(active) < self.workers:
                    priority, sequence, call, task = heapq.heappop(self.ready)
                    self.trace.record(
                        "job_dispatched",
                        job=call.args[0],
                        role=call.args[1],
                        priority=priority,
                        **({"cache_only": True} if self.runner.quota_error else {}),
                    )
                    active[pool.submit(self.runner.call, *call.args)] = (sequence, call, task)
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                if self.runner.quota_error and not quota_cancelled:
                    # Stop in-flight backend calls; exact cache hits keep completing.
                    quota_cancelled = True
                    if any(not future.done() for future in active):
                        self.cancel()
                # Observe all currently completed calls before filling free slots.
                for future in sorted(done, key=lambda f: active[f][0]):
                    _, call, task = active.pop(future)
                    try:
                        value = future.result()
                    except BaseException as exc:
                        # A call that needed the backend after quota exhaustion, or
                        # was cancelled by it, waits for manual resume.
                        if isinstance(exc, QuotaExhausted) or (
                            isinstance(exc, BackendCancelled) and self.runner.quota_error
                        ):
                            parked.append((call, task))
                            continue
                        self._advance(task, error=exc)
                    else:
                        self._advance(task, value=value)
                    if self.stop.is_set():
                        break
            # No new backend call starts after quota exhaustion; stop this invocation
            # once the remaining cached work has been replayed.
            self.runner.check_quota()
        finally:
            if active or self.ready or parked:
                self.stop.set()
                if any(not future.done() for future in active):
                    self.cancel()
                for future in active:
                    future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            # Successful in-flight calls are already cached by JobRunner. Do not
            # run their continuations after stop; replay them on manual resume.
            abandoned = [(call, task, None) for _, call, task in active.values()]
            abandoned += [(call, task, None) for _, _, call, task in self.ready]
            abandoned += [(call, task, self.runner.quota_error) for call, task in parked]
            self.ready.clear()
            for call, task, quota in abandoned:
                self.trace.record(
                    "job_cancelled",
                    job=call.args[0],
                    role=call.args[1],
                    **({"reason": "quota_exhausted"} if quota else {}),
                )
                try:
                    task.throw(quota or BackendCancelled("scheduler stopped"))
                except (StopIteration, BackendCancelled, QuotaExhausted):
                    pass
                finally:
                    task.close()
