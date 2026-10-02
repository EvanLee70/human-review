"""The producers run in parallel — within what they read from each other and what they hold.

Serial, they took 8.3 min on petclinic; most of that was four steps that never wait for one
another. These tests pin the two rules that make running them at once safe: a step starts
only after the steps it NEEDS, and never alongside a step that USES the same resource
(the commit's Docker stack, the Maven build directory).
"""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("run_steps", HERE / "run-steps.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

NAMES = [s[0] for s in rs.STEPS]


def _trace(jobs: int, durations: dict[str, float] | None = None):
    """Run the real schedule with fake steps; return (start, end) per step."""
    lock = threading.Lock()
    spans: dict[str, tuple[float, float]] = {}
    t0 = time.monotonic()

    def run(name):
        start = time.monotonic() - t0
        time.sleep((durations or {}).get(name, 0.02))
        with lock:
            spans[name] = (start, time.monotonic() - t0)
        return {"step": name}

    done = rs.schedule(NAMES, jobs, run)
    assert set(done) == set(NAMES)
    return spans


def test_every_need_finishes_before_the_step_that_reads_it():
    spans = _trace(jobs=8)
    for step, needs in rs.NEEDS.items():
        for need in needs:
            assert spans[need][1] <= spans[step][0], f"{step} started before {need} ended"


def test_steps_sharing_a_resource_never_overlap():
    spans = _trace(jobs=8)
    for a in NAMES:
        for b in NAMES:
            if a < b and rs.USES.get(a, set()) & rs.USES.get(b, set()):
                (s1, e1), (s2, e2) = spans[a], spans[b]
                assert e1 <= s2 or e2 <= s1, f"{a} and {b} both held a shared resource"


def test_independent_slow_steps_do_run_at_the_same_time():
    """The point of it: Code City and the sequence suite share nothing."""
    spans = _trace(jobs=8, durations={"city": 0.3, "sequence": 0.3})
    (s1, e1), (s2, e2) = spans["city"], spans["sequence"]
    assert s1 < e2 and s2 < e1


def test_one_job_is_the_old_serial_run_in_steps_order():
    spans = _trace(jobs=1)
    order = sorted(NAMES, key=lambda n: spans[n][0])
    assert order == NAMES


def test_the_declared_names_are_real_steps():
    """A typo in NEEDS or USES would silently drop a dependency."""
    for step, needs in rs.NEEDS.items():
        assert step in NAMES and needs <= set(NAMES), step
    assert set(rs.USES) <= set(NAMES)


def test_needs_never_point_backwards_in_the_serial_order():
    """--jobs 1 runs STEPS in order, so every need must come earlier in it."""
    for step, needs in rs.NEEDS.items():
        for need in needs:
            assert NAMES.index(need) < NAMES.index(step), (need, step)


#: The steps that write into the commit's stack database: city's Playwright suite, the
#: traces' cucumber run, and the film's own clicks.
STACK_WRITERS = {"city", "traces", "video"}


def test_a_capture_never_runs_beside_a_step_that_writes_into_its_stack():
    """Eval run 5: the design-system audit shot the stack the Playwright suite had just
    written into, and three of four 'changed' screens were test data. A capture resets the
    database first (run-steps.py `to_seed`); that is only worth anything if no writer can run
    between the reset and the last screenshot — which is what sharing a lane guarantees."""
    for capture in rs.CAPTURES:
        for writer in STACK_WRITERS - {capture}:
            assert rs.USES.get(capture, set()) & rs.USES.get(writer, set()), (capture, writer)
    spans = _trace(jobs=8, durations={n: 0.05 for n in set(rs.CAPTURES) | STACK_WRITERS})
    for capture in rs.CAPTURES:
        for writer in STACK_WRITERS - {capture}:
            (s1, e1), (s2, e2) = spans[capture], spans[writer]
            assert e1 <= s2 or e2 <= s1, f"{capture} overlapped {writer}"
