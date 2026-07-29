#!/usr/bin/env python3
"""Run paired, zero-context Codex agents for logic-flow token measurement."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = ROOT / "benchmarks/agent_logic_flow/adk_holdout_v3_tasks.json"
DEFAULT_CODECANVAS_PYTHON = ROOT / "core/.venv/bin/python"

COMMON_CONFIG = [
    'model_reasoning_effort="high"',
    'approval_policy="never"',
    'web_search="disabled"',
    "agents.enabled=false",
    "project_doc_max_bytes=0",
    "skills.include_instructions=false",
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "features.memories=false",
    "memories.use_memories=false",
    "memories.generate_memories=false",
    "apps._default.enabled=false",
    "check_for_update_on_startup=false",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _participant_prompt(task: dict[str, Any]) -> str:
    return (
        "You are participating in a blind benchmark of zero-context "
        "logic-flow understanding.\n\n"
        "The target repository is the current working directory and is "
        "read-only.\n"
        "Answer the task below using only the tools available in this session.\n"
        "Begin with no assumptions beyond the task.\n"
        "Ground every required claim in the repository and cite exact "
        "repository-relative file:line evidence.\n"
        "Do not read any benchmark rubric, prior answer, prior trace, or "
        "CodeCanvas repository file.\n"
        "Return only the task answer.\n\n"
        f"Task ({task['id']}):\n{task['prompt']}\n"
    )


def _codex_command(
    *,
    condition: str,
    project: Path,
    answer_path: Path,
    model: str,
    reasoning_effort: str,
    codecanvas_python: Path,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "-C",
        str(project),
        "-m",
        model,
        "-s",
        "read-only",
    ]
    configs = [
        value if not value.startswith("model_reasoning_effort=")
        else f'model_reasoning_effort="{reasoning_effort}"'
        for value in COMMON_CONFIG
    ]
    if condition == "baseline":
        configs.append("mcp_servers={}")
    elif condition == "codecanvas":
        configs.extend(
            [
                (
                    "mcp_servers.codecanvas.command="
                    f'"{codecanvas_python.resolve()}"'
                ),
                'mcp_servers.codecanvas.args=["-m","codecanvas_mcp.mcp.server"]',
                (
                    "mcp_servers.codecanvas.cwd="
                    f'"{(ROOT / "core").resolve()}"'
                ),
                (
                    "mcp_servers.codecanvas.default_tools_approval_mode="
                    '"approve"'
                ),
                'mcp_servers.codecanvas.enabled_tools=["logic_flow"]',
            ]
        )
    else:
        raise ValueError(f"unknown condition: {condition}")
    for config in configs:
        command.extend(["-c", config])
    command.extend(["-o", str(answer_path), "-"])
    return command


def _parse_trace(path: Path) -> dict[str, Any]:
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    completed = [event for event in events if event.get("type") == "turn.completed"]
    failures = [
        event
        for event in events
        if event.get("type") in {"turn.failed", "error"}
    ]
    if len(completed) != 1 or failures:
        raise RuntimeError(
            f"invalid trace {path}: completed={len(completed)}, "
            f"failures={len(failures)}"
        )
    usage = completed[0]["usage"]
    calls: dict[str, int] = {}
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed":
            continue
        if item.get("type") == "command_execution":
            name = "command_execution"
        elif item.get("type") == "mcp_tool_call":
            name = f"{item.get('server')}.{item.get('tool')}:{item.get('status')}"
        else:
            continue
        calls[name] = calls.get(name, 0) + 1
    return {
        **usage,
        "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        "uncached_input_plus_output": (
            usage["input_tokens"]
            - usage.get("cached_input_tokens", 0)
            + usage["output_tokens"]
        ),
        "calls": calls,
    }


def _comparison(baseline: int, codecanvas: int) -> dict[str, float]:
    ratio = codecanvas / baseline
    return {
        "ratio": round(ratio, 6),
        "savings_percent": round((1 - ratio) * 100, 2),
    }


def _run_condition(
    *,
    condition: str,
    task: dict[str, Any],
    project: Path,
    output_dir: Path,
    model: str,
    reasoning_effort: str,
    codecanvas_python: Path,
) -> dict[str, Any]:
    stem = f"{condition}_{task['id']}"
    answer_path = output_dir / f"{stem}_answer.md"
    trace_path = output_dir / f"{stem}_trace.jsonl"
    stderr_path = output_dir / f"{stem}_stderr.log"
    command = _codex_command(
        condition=condition,
        project=project,
        answer_path=answer_path,
        model=model,
        reasoning_effort=reasoning_effort,
        codecanvas_python=codecanvas_python,
    )
    with trace_path.open("w", encoding="utf-8") as stdout:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                command,
                cwd=project,
                input=_participant_prompt(task),
                text=True,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{condition}/{task['id']} exited {completed.returncode}; "
            f"see {stderr_path}"
        )
    measurement = _parse_trace(trace_path)
    measurement.update(
        {
            "answer_sha256": _sha256(answer_path),
            "trace_sha256": _sha256(trace_path),
            "stderr_sha256": _sha256(stderr_path),
        }
    )
    return measurement


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_total = sum(row["baseline"]["total_tokens"] for row in rows)
    codecanvas_total = sum(row["codecanvas"]["total_tokens"] for row in rows)
    baseline_uncached = sum(
        row["baseline"]["uncached_input_plus_output"] for row in rows
    )
    codecanvas_uncached = sum(
        row["codecanvas"]["uncached_input_plus_output"] for row in rows
    )
    return {
        "tasks": len(rows),
        "baseline_total_tokens": baseline_total,
        "codecanvas_total_tokens": codecanvas_total,
        **_comparison(baseline_total, codecanvas_total),
        "baseline_uncached_input_plus_output": baseline_uncached,
        "codecanvas_uncached_input_plus_output": codecanvas_uncached,
        "uncached": _comparison(baseline_uncached, codecanvas_uncached),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    project = args.project.resolve()
    tasks_path = args.tasks.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    suite = json.loads(tasks_path.read_text(encoding="utf-8"))
    target_revision = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if target_revision != suite["target_revision"] and not args.allow_revision_mismatch:
        raise SystemExit(
            f"target revision mismatch: {target_revision} != "
            f"{suite['target_revision']}"
        )

    rows = []
    for task in suite["tasks"]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                condition: pool.submit(
                    _run_condition,
                    condition=condition,
                    task=task,
                    project=project,
                    output_dir=output_dir,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    codecanvas_python=args.codecanvas_python,
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
        row["comparison"] = _comparison(
            row["baseline"]["total_tokens"],
            row["codecanvas"]["total_tokens"],
        )
        rows.append(row)

    report = {
        "benchmark": suite["benchmark_id"],
        "target_revision": target_revision,
        "tasks_sha256": _sha256(tasks_path),
        "runner": {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "baseline": "built-in tools only",
            "codecanvas": "built-in tools + enabled_tools=[logic_flow]",
        },
        "tasks": rows,
        "aggregate": _aggregate(rows),
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument(
        "--codecanvas-python",
        type=Path,
        default=DEFAULT_CODECANVAS_PYTHON,
    )
    parser.add_argument("--allow-revision-mismatch", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args)["aggregate"], indent=2))


if __name__ == "__main__":
    main()
