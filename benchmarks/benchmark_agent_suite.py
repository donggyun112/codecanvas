#!/usr/bin/env python3
"""Run a repeated, paired CodeCanvas benchmark across Python repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    from benchmarks import benchmark_agent_logic_flow as paired
except ModuleNotFoundError:  # Direct execution from benchmarks/.
    import benchmark_agent_logic_flow as paired


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "benchmarks/agent_logic_flow/multi_repo_v1.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checkouts(values: list[str]) -> dict[str, Path]:
    checkouts: dict[str, Path] = {}
    for value in values:
        repository_id, separator, raw_path = value.partition("=")
        if not separator or not repository_id or not raw_path:
            raise ValueError(
                f"invalid --checkout {value!r}; expected REPOSITORY_ID=/path"
            )
        if repository_id in checkouts:
            raise ValueError(f"duplicate checkout for {repository_id!r}")
        checkouts[repository_id] = Path(raw_path).expanduser().resolve()
    return checkouts


def _resolve_artifact(path: str) -> Path:
    artifact = (ROOT / path).resolve()
    if ROOT not in artifact.parents:
        raise ValueError(f"benchmark artifact escapes repository root: {path}")
    if not artifact.is_file():
        raise FileNotFoundError(artifact)
    return artifact


def _revision(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _select_tasks(suite: dict[str, Any], task_ids: list[str]) -> list[dict[str, Any]]:
    by_id = {task["id"]: task for task in suite["tasks"]}
    missing = [task_id for task_id in task_ids if task_id not in by_id]
    if missing:
        raise ValueError(f"task ids absent from task file: {missing}")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task_ids must be unique")
    return [by_id[task_id] for task_id in task_ids]


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "values": values,
        "median": round(statistics.median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def _repetition_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    total_savings = [
        report["aggregate"]["savings_percent"] for report in reports
    ]
    uncached_savings = [
        report["aggregate"]["uncached"]["savings_percent"] for report in reports
    ]
    return {
        "repetitions": len(reports),
        "total_token_savings_percent": _distribution(total_savings),
        "uncached_token_savings_percent": _distribution(uncached_savings),
    }


def _completed_condition(
    *,
    condition: str,
    task: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any] | None:
    stem = f"{condition}_{task['id']}"
    answer_path = output_dir / f"{stem}_answer.md"
    trace_path = output_dir / f"{stem}_trace.jsonl"
    stderr_path = output_dir / f"{stem}_stderr.log"
    if not answer_path.is_file() or not trace_path.is_file():
        return None
    try:
        measurement = paired._parse_trace(trace_path)
    except (KeyError, RuntimeError, json.JSONDecodeError):
        return None
    if condition == "codecanvas" and not any(
        name.startswith("codecanvas.logic_flow:")
        and name.endswith(":completed")
        and count > 0
        for name, count in measurement["calls"].items()
    ):
        return None
    measurement.update(
        {
            "answer_sha256": _sha256(answer_path),
            "trace_sha256": _sha256(trace_path),
            "stderr_sha256": (
                _sha256(stderr_path) if stderr_path.is_file() else None
            ),
        }
    )
    return measurement


def _completed_pair(
    task: dict[str, Any], output_dir: Path
) -> dict[str, Any] | None:
    measurements = {
        condition: _completed_condition(
            condition=condition,
            task=task,
            output_dir=output_dir,
        )
        for condition in ("baseline", "codecanvas")
    }
    if any(measurement is None for measurement in measurements.values()):
        return None
    return {
        "id": task["id"],
        **measurements,
    }


def _run_repository(
    *,
    repository: dict[str, Any],
    checkout: Path,
    output_dir: Path,
    repetitions: int,
    model: str,
    reasoning_effort: str,
    codecanvas_python: Path,
) -> dict[str, Any]:
    actual_revision = _revision(checkout)
    expected_revision = repository["revision"]
    if actual_revision != expected_revision:
        raise ValueError(
            f"{repository['id']} revision mismatch: "
            f"{actual_revision} != {expected_revision}"
        )

    project = (checkout / repository.get("project_subdir", ".")).resolve()
    if not project.is_dir():
        raise FileNotFoundError(project)

    tasks_path = _resolve_artifact(repository["tasks"])
    rubric_path = _resolve_artifact(repository["rubric"])
    task_suite = json.loads(tasks_path.read_text(encoding="utf-8"))
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    if (
        task_suite["target_revision"] != expected_revision
        or rubric["target_revision"] != expected_revision
    ):
        raise ValueError(
            f"{repository['id']} artifact revision does not match suite revision"
        )
    if task_suite["benchmark_id"] != rubric["benchmark_id"]:
        raise ValueError(
            f"{repository['id']} task and rubric benchmark ids differ"
        )
    tasks = _select_tasks(task_suite, repository["task_ids"])
    rubric_ids = {task["id"] for task in rubric["tasks"]}
    missing_rubrics = [
        task["id"] for task in tasks if task["id"] not in rubric_ids
    ]
    if missing_rubrics:
        raise ValueError(
            f"{repository['id']} tasks lack rubric entries: {missing_rubrics}"
        )

    reports: list[dict[str, Any]] = []
    for repetition in range(1, repetitions + 1):
        repetition_dir = output_dir / repository["id"] / f"run-{repetition:02d}"
        repetition_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for task in tasks:
            row = _completed_pair(task, repetition_dir)
            if row is None:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = {
                        condition: pool.submit(
                            paired._run_condition,
                            condition=condition,
                            task=task,
                            project=project,
                            output_dir=repetition_dir,
                            model=model,
                            reasoning_effort=reasoning_effort,
                            codecanvas_python=codecanvas_python,
                        )
                        for condition in ("baseline", "codecanvas")
                    }
                    row = {
                        "id": task["id"],
                        **{
                            condition: future.result()
                            for condition, future in futures.items()
                        },
                    }
            row["comparison"] = paired._comparison(
                row["baseline"]["total_tokens"],
                row["codecanvas"]["total_tokens"],
            )
            rows.append(row)

        report = {
            "repetition": repetition,
            "tasks": rows,
            "aggregate": paired._aggregate(rows),
        }
        (repetition_dir / "report.json").write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        reports.append(report)

    return {
        "id": repository["id"],
        "repository": repository["repository"],
        "revision": expected_revision,
        "project_subdir": repository.get("project_subdir", "."),
        "tasks_sha256": _sha256(tasks_path),
        "rubric_sha256": _sha256(rubric_path),
        "task_ids": repository["task_ids"],
        "runs": reports,
        "summary": _repetition_summary(reports),
    }


def _suite_summary(
    repositories: list[dict[str, Any]], repetitions: int
) -> dict[str, Any]:
    run_savings: list[float] = []
    uncached_run_savings: list[float] = []
    for index in range(repetitions):
        baseline_total = 0
        codecanvas_total = 0
        baseline_uncached = 0
        codecanvas_uncached = 0
        for repository in repositories:
            aggregate = repository["runs"][index]["aggregate"]
            baseline_total += aggregate["baseline_total_tokens"]
            codecanvas_total += aggregate["codecanvas_total_tokens"]
            baseline_uncached += aggregate[
                "baseline_uncached_input_plus_output"
            ]
            codecanvas_uncached += aggregate[
                "codecanvas_uncached_input_plus_output"
            ]
        run_savings.append(
            paired._comparison(baseline_total, codecanvas_total)[
                "savings_percent"
            ]
        )
        uncached_run_savings.append(
            paired._comparison(baseline_uncached, codecanvas_uncached)[
                "savings_percent"
            ]
        )
    return {
        "repositories": len(repositories),
        "tasks_per_repetition": sum(
            len(repository["task_ids"]) for repository in repositories
        ),
        "repetitions": repetitions,
        "total_token_savings_percent": _distribution(run_savings),
        "uncached_token_savings_percent": _distribution(
            uncached_run_savings
        ),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    suite_path = args.suite.resolve()
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    repetitions = (
        args.repetitions
        if args.repetitions is not None
        else suite["repetitions"]
    )
    if repetitions < 1:
        raise ValueError("repetitions must be at least one")

    checkouts = _parse_checkouts(args.checkout)
    required = {repository["id"] for repository in suite["repositories"]}
    missing = sorted(required - checkouts.keys())
    extra = sorted(checkouts.keys() - required)
    if missing or extra:
        raise ValueError(
            f"checkout ids do not match suite; missing={missing}, extra={extra}"
        )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    repositories = [
        _run_repository(
            repository=repository,
            checkout=checkouts[repository["id"]],
            output_dir=output_dir,
            repetitions=repetitions,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            codecanvas_python=args.codecanvas_python,
        )
        for repository in suite["repositories"]
    ]
    report = {
        "benchmark": suite["benchmark_id"],
        "suite_sha256": _sha256(suite_path),
        "profile": suite["profile"],
        "runner": {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "baseline": "built-in tools only",
            "codecanvas": "built-in tools + enabled_tools=[logic_flow]",
        },
        "repositories": repositories,
        "summary": _suite_summary(repositories, repetitions),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout",
        action="append",
        default=[],
        metavar="ID=/path",
        help="Checkout root for one repository in the suite; repeat per repo.",
    )
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument(
        "--codecanvas-python",
        type=Path,
        default=paired.DEFAULT_CODECANVAS_PYTHON,
    )
    args = parser.parse_args()
    print(json.dumps(run(args)["summary"], indent=2))


if __name__ == "__main__":
    main()
