"""Measure cold analysis, warm search, and concurrent symbol-search latency.

Usage:
    core/.venv/bin/python benchmarks/benchmark_find_symbols.py /path/to/project
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codecanvas_mcp.graph.builder import FlowGraphBuilder
from codecanvas_mcp.mcp.queries import find_symbols


def _timed(callable_):
    started = time.perf_counter()
    result = callable_()
    return result, (time.perf_counter() - started) * 1_000


def _cold_builder(project: Path):
    temporary = tempfile.TemporaryDirectory(prefix="codecanvas-benchmark-")
    copied = Path(temporary.name) / "project"
    shutil.copytree(
        project,
        copied,
        ignore=shutil.ignore_patterns(
            ".git", ".codecanvas", ".venv", "venv", "node_modules",
            "__pycache__", "*.pyc",
        ),
    )
    builder = FlowGraphBuilder(str(copied))
    builder.call_graph.analyze_project()
    python_files = sum(1 for _ in copied.rglob("*.py"))
    return temporary, builder, python_files


def run(project: Path, query: str, iterations: int, workers: int) -> dict:
    (temporary, builder, python_files), cold_ms = _timed(
        lambda: _cold_builder(project),
    )
    try:
        symbol_count = len(builder.call_graph.all_functions())
        _, first_search_ms = _timed(
            lambda: find_symbols(builder, query, limit=20),
        )
        samples = []
        for _ in range(iterations):
            _, elapsed = _timed(
                lambda: find_symbols(builder, query, limit=20),
            )
            samples.append(elapsed)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            concurrent_results = list(pool.map(
                lambda _: find_symbols(builder, query, limit=20),
                range(iterations),
            ))
        concurrent_ms = (time.perf_counter() - started) * 1_000
        assert all("error" not in result for result in concurrent_results)

        return {
            "project": str(project),
            "python_files": python_files,
            "symbols": symbol_count,
            "cold_analysis_ms": round(cold_ms, 2),
            "first_search_ms": round(first_search_ms, 3),
            "warm_search_ms": {
                "median": round(statistics.median(samples), 3),
                "p95": round(sorted(samples)[max(0, int(len(samples) * .95) - 1)], 3),
                "max": round(max(samples), 3),
            },
            "concurrent": {
                "workers": workers,
                "calls": iterations,
                "wall_ms": round(concurrent_ms, 2),
                "calls_per_second": round(iterations / (concurrent_ms / 1000), 2),
            },
        }
    finally:
        temporary.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--query", default="resolve function")
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error(f"not a directory: {args.project}")
    print(json.dumps(
        run(args.project.resolve(), args.query, args.iterations, args.workers),
        indent=2,
    ))


if __name__ == "__main__":
    main()
