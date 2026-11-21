"""Split selected tests over workers so the SCORING.md cost is as low as LPT can make it.

Cost of one commit (SCORING.md): 0 with no workers, else max worker load + 2000 ms per
non-empty worker. Durations are whole milliseconds, round(seconds * 1000) with halves up.
"""

from __future__ import annotations

import heapq
import math
from typing import Iterable, Sequence

WORKER_COST_MS = 2_000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    """round(seconds * 1000) with halves rounded up, exactly like score.py."""
    return math.floor(seconds * 1000 + 0.5)


def cost(workers: Iterable[Sequence[int]], ms_by_test: dict[int, int]) -> int:
    """Cost in ms of a workers list, matching SCORING.md (empty workers are dropped)."""
    loads = [sum(ms_by_test[t] for t in w) for w in workers if len(w) > 0]
    if not loads:
        return 0
    return max(loads) + WORKER_COST_MS * len(loads)


def _lpt(tests: list[tuple[int, int]], k: int) -> tuple[int, list[list[int]]]:
    """Longest processing time first onto the lightest worker. Returns (cost, workers)."""
    workers: list[list[int]] = [[] for _ in range(k)]
    heap = [(0, i) for i in range(k)]  # (load, worker index); ties go to the lowest index
    for number, ms in tests:
        load, i = heapq.heappop(heap)
        workers[i].append(number)
        heapq.heappush(heap, (load + ms, i))
    workers = [w for w in workers if w]
    max_load = max(load for load, _ in heap)
    return max_load + WORKER_COST_MS * len(workers), workers


def pack(tests: Sequence[tuple[int, int]]) -> list[list[int]]:
    """tests: (number, ms). Returns the cheapest LPT packing over k = 1..8 workers."""
    if not tests:
        return []
    ordered = sorted(tests, key=lambda t: (-t[1], t[0]))
    best_cost, best = None, []
    for k in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        c, workers = _lpt(ordered, k)
        if best_cost is None or c < best_cost:
            best_cost, best = c, workers
    return best
