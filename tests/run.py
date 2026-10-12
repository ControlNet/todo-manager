"""Parallel test runner (standard library only).

    python3 -B -m tests.run                  # unit tests, in parallel (no Hermes needed)
    python3 -B -m tests.run --all            # unit and Hermes integration tests
    python3 -B -m tests.run --integration    # Hermes integration tests only
    python3 -B -m tests.run tests.test_prefs tests.test_cli.CliTests   # named tests, any kind

Each work unit runs as ``python3 -B -m unittest <names>`` in its own process: up to CHUNK
methods of one test class, and one method for the Hermes integration tests, which spend their
time in Hermes bridge processes and so parallelise well. Every test already uses its own temporary
workspace and HERMES_HOME; the runner points TMPDIR at a private directory, in memory
(/dev/shm) where the system has one, and removes it afterwards: syncing the tests' many small
SQLite files to disk is otherwise most of a run's time. ``python3 -B -m unittest discover -s
tests -t .`` stays the sequential reference run. Each unit's duration is kept in
.test-durations.json (not committed) so that the next run starts the slowest units first.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = "tests.integration."
FAILED_IMPORT = "unittest.loader._FailedTest."
CHUNK = 8
DURATIONS = ".test-durations.json"
LIST = ("import unittest\n"
        "def walk(s):\n"
        "    for t in s:\n"
        "        yield from walk(t) if isinstance(t, unittest.TestSuite) else [t.id()]\n"
        "print('\\n'.join(walk(unittest.defaultTestLoader.discover('tests', top_level_dir='.'))))\n")


def discover(root: Path) -> list[str]:
    """Test ids, listed by a separate interpreter so that ``root``'s own ``tests`` package is used."""
    proc = subprocess.run([sys.executable, "-B", "-c", LIST], cwd=root, capture_output=True, text=True, check=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def select(ids: list[str], *, mode: str, names: list[str]) -> list[str]:
    if names:
        return [i for i in ids if any(i == n or i.startswith(n + ".") for n in names)]
    if mode == "all":
        return list(ids)
    return [i for i in ids if i.startswith(INTEGRATION) == (mode == "integration")]


def durations(root: Path) -> dict[str, float]:
    """Seconds per work unit (by its first test id) from earlier runs."""
    try:
        return {str(k): float(v) for k, v in json.loads((root / DURATIONS).read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def keep_durations(root: Path, measured: dict[str, float]) -> None:
    try:
        (root / DURATIONS).write_text(json.dumps({**durations(root), **measured}, indent=0, sort_keys=True) + "\n",
                                      encoding="utf-8")
    except OSError:
        pass


def plan(ids: list[str], known: dict[str, float] | None = None) -> list[list[str]]:
    """Work units: integration tests one per method, first since they take longest; other tests
    in chunks of up to CHUNK methods of one class; a module that failed to import on its own.
    With ``known`` durations, the slowest units start first and units never timed before them."""
    slow: list[list[str]] = []
    classes: dict[str, list[str]] = {}
    for i in ids:
        if i.startswith(FAILED_IMPORT):
            classes.setdefault(i, []).append(i[len(FAILED_IMPORT):])
        elif i.startswith(INTEGRATION):
            slow.append([i])
        else:
            classes.setdefault(i.rsplit(".", 1)[0], []).append(i)
    units = slow + [tests[n:n + CHUNK] for tests in classes.values() for n in range(0, len(tests), CHUNK)]
    return sorted(units, key=lambda unit: -known.get(unit[0], math.inf)) if known else units


def label(unit: list[str]) -> str:
    return unit[0] if len(unit) == 1 else f"{unit[0].rsplit('.', 1)[0]} ({len(unit)} tests)"


def parse(output: str) -> tuple[int, int]:
    """(tests run, tests skipped) from a unittest summary."""
    ran = re.search(r"^Ran (\d+) tests? in ", output, re.M)
    skipped = re.search(r"^(?:OK|FAILED) \(.*?skipped=(\d+)", output, re.M)
    return int(ran.group(1)) if ran else 0, int(skipped.group(1)) if skipped else 0


def scratch() -> str:
    """The run's private temporary directory: in memory where the system has one."""
    shm = Path("/dev/shm")
    return tempfile.mkdtemp(prefix="todo-manager-tests-", dir=shm if shm.is_dir() and os.access(shm, os.W_OK) else None)


def run_unit(unit: list[str], root: Path, env: dict[str, str]) -> tuple[list[str], int, str, float]:
    start = time.perf_counter()
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", *unit], cwd=root, env=env,
                          capture_output=True, text=True)
    return unit, proc.returncode, proc.stdout + proc.stderr, time.perf_counter() - start


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    p = argparse.ArgumentParser(prog="python3 -B -m tests.run", description=__doc__.split("\n\n")[0])
    p.add_argument("names", nargs="*", help="test modules, classes or methods to run (any kind)")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--all", dest="mode", action="store_const", const="all", help="unit and integration tests")
    kind.add_argument("--integration", dest="mode", action="store_const", const="integration",
                      help="Hermes integration tests only")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 2, help="parallel processes")
    p.add_argument("--durations", type=int, default=0, metavar="N", help="list the N slowest work units")
    args = p.parse_args(argv)
    started = time.perf_counter()
    units = plan(select(discover(root), mode=args.mode or "unit", names=args.names), durations(root))
    if not units:
        print("no tests selected")
        return 1
    ran = skipped = 0
    failed: list[tuple[str, str]] = []
    timings: dict[str, float] = {}
    tmp = scratch()
    env = {**os.environ, "TMPDIR": tmp}
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for future in as_completed([pool.submit(run_unit, u, root, env) for u in units]):
                unit, code, output, seconds = future.result()
                n, s = parse(output)
                ran, skipped = ran + n, skipped + s
                if n > s:
                    timings[unit[0]] = seconds
                if code != 0 or n == 0:
                    failed.append((label(unit), output))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    keep_durations(root, timings)
    names = {u[0]: label(u) for u in units}
    for name, output in sorted(failed):
        print(f"{'=' * 70}\nFAILED UNIT: {name}\n{'-' * 70}\n{output.rstrip()}\n")
    for first, seconds in sorted(timings.items(), key=lambda kv: -kv[1])[:args.durations]:
        print(f"{seconds:8.2f}s  {names[first]}")
    elapsed = time.perf_counter() - started
    print(f"Ran {ran} test{'' if ran == 1 else 's'} in {elapsed:.1f}s ({len(units)} units, {args.jobs} processes)")
    status = f"FAILED (units={len(failed)}" + (f", skipped={skipped})" if skipped else ")") if failed else \
        "OK" + (f" (skipped={skipped})" if skipped else "")
    print(status)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
