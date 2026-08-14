import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "benchmarks" / "benchmark_agent_suite.py"
SPEC = importlib.util.spec_from_file_location("agent_suite_benchmark", SCRIPT)
BENCHMARK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BENCHMARK)


def test_parse_checkouts_rejects_duplicates_and_malformed_values(tmp_path):
    parsed = BENCHMARK._parse_checkouts(
        [f"adk={tmp_path}", f"fastapi={tmp_path / 'fastapi'}"]
    )
    assert set(parsed) == {"adk", "fastapi"}
    assert parsed["adk"] == tmp_path.resolve()

    with pytest.raises(ValueError, match="expected REPOSITORY_ID"):
        BENCHMARK._parse_checkouts(["missing-path"])
    with pytest.raises(ValueError, match="duplicate checkout"):
        BENCHMARK._parse_checkouts([f"adk={tmp_path}", f"adk={tmp_path}"])


def test_select_tasks_uses_explicit_holdout_order():
    suite = {
        "tasks": [
            {"id": "calibration"},
            {"id": "held-out-b"},
            {"id": "held-out-a"},
        ]
    }

    selected = BENCHMARK._select_tasks(
        suite, ["held-out-a", "held-out-b"]
    )

    assert [task["id"] for task in selected] == [
        "held-out-a",
        "held-out-b",
    ]


def test_repetition_summary_reports_median_and_range():
    reports = [
        {
            "aggregate": {
                "savings_percent": 20.0,
                "uncached": {"savings_percent": 10.0},
            }
        },
        {
            "aggregate": {
                "savings_percent": -5.0,
                "uncached": {"savings_percent": 0.0},
            }
        },
        {
            "aggregate": {
                "savings_percent": 10.0,
                "uncached": {"savings_percent": 5.0},
            }
        },
    ]

    assert BENCHMARK._repetition_summary(reports) == {
        "repetitions": 3,
        "total_token_savings_percent": {
            "values": [20.0, -5.0, 10.0],
            "median": 10.0,
            "min": -5.0,
            "max": 20.0,
        },
        "uncached_token_savings_percent": {
            "values": [10.0, 0.0, 5.0],
            "median": 5.0,
            "min": 0.0,
            "max": 10.0,
        },
    }


def test_completed_pair_reuses_only_two_valid_conditions(tmp_path, monkeypatch):
    task = {"id": "task-one"}
    calls = []

    def fake_parse_trace(path):
        calls.append(path.name)
        if path.read_text(encoding="utf-8") != "complete":
            raise RuntimeError("incomplete")
        return {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "uncached_input_plus_output": 8,
            "calls": {"codecanvas.logic_flow:completed": 1},
        }

    monkeypatch.setattr(BENCHMARK.paired, "_parse_trace", fake_parse_trace)
    for condition in ("baseline", "codecanvas"):
        stem = f"{condition}_{task['id']}"
        (tmp_path / f"{stem}_answer.md").write_text(
            condition, encoding="utf-8"
        )
        (tmp_path / f"{stem}_trace.jsonl").write_text(
            "complete", encoding="utf-8"
        )
        (tmp_path / f"{stem}_stderr.log").write_text("", encoding="utf-8")

    pair = BENCHMARK._completed_pair(task, tmp_path)

    assert pair is not None
    assert pair["baseline"]["total_tokens"] == 12
    assert pair["codecanvas"]["total_tokens"] == 12
    assert set(calls) == {
        "baseline_task-one_trace.jsonl",
        "codecanvas_task-one_trace.jsonl",
    }

    (tmp_path / "codecanvas_task-one_trace.jsonl").write_text(
        "partial", encoding="utf-8"
    )
    assert BENCHMARK._completed_pair(task, tmp_path) is None


def test_suite_summary_aggregates_tokens_per_repetition():
    repositories = [
        {
            "task_ids": ["a"],
            "runs": [
                {
                    "aggregate": {
                        "baseline_total_tokens": 100,
                        "codecanvas_total_tokens": 50,
                        "baseline_uncached_input_plus_output": 80,
                        "codecanvas_uncached_input_plus_output": 40,
                    }
                },
                {
                    "aggregate": {
                        "baseline_total_tokens": 200,
                        "codecanvas_total_tokens": 200,
                        "baseline_uncached_input_plus_output": 100,
                        "codecanvas_uncached_input_plus_output": 100,
                    }
                },
            ],
        },
        {
            "task_ids": ["b", "c"],
            "runs": [
                {
                    "aggregate": {
                        "baseline_total_tokens": 300,
                        "codecanvas_total_tokens": 150,
                        "baseline_uncached_input_plus_output": 120,
                        "codecanvas_uncached_input_plus_output": 60,
                    }
                },
                {
                    "aggregate": {
                        "baseline_total_tokens": 200,
                        "codecanvas_total_tokens": 100,
                        "baseline_uncached_input_plus_output": 100,
                        "codecanvas_uncached_input_plus_output": 50,
                    }
                },
            ],
        },
    ]

    summary = BENCHMARK._suite_summary(repositories, repetitions=2)

    assert summary["repositories"] == 2
    assert summary["tasks_per_repetition"] == 3
    assert summary["total_token_savings_percent"] == {
        "values": [50.0, 25.0],
        "median": 37.5,
        "min": 25.0,
        "max": 50.0,
    }
    assert summary["uncached_token_savings_percent"] == {
        "values": [50.0, 25.0],
        "median": 37.5,
        "min": 25.0,
        "max": 50.0,
    }


def test_multi_repo_manifest_and_rubrics_are_consistent():
    manifest = json.loads(
        (
            ROOT
            / "benchmarks/agent_logic_flow/multi_repo_v1.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["repetitions"] >= 3
    assert {repository["id"] for repository in manifest["repositories"]} == {
        "google-adk",
        "langgraph",
        "fastapi",
    }

    for repository in manifest["repositories"]:
        tasks = json.loads(
            (ROOT / repository["tasks"]).read_text(encoding="utf-8")
        )
        rubric = json.loads(
            (ROOT / repository["rubric"]).read_text(encoding="utf-8")
        )
        assert tasks["target_revision"] == repository["revision"]
        assert rubric["target_revision"] == repository["revision"]
        task_ids = {task["id"] for task in tasks["tasks"]}
        rubric_by_id = {task["id"]: task for task in rubric["tasks"]}
        assert set(repository["task_ids"]) <= task_ids
        assert set(repository["task_ids"]) <= rubric_by_id.keys()
        for task_id in repository["task_ids"]:
            task_rubric = rubric_by_id[task_id]
            assert sum(
                point["points"]
                for point in task_rubric["substantive_logic_points"]
            ) == rubric["scoring"]["substantive_logic_total"]
            assert sum(
                point["points"]
                for point in task_rubric["citation_format_points"]
            ) == rubric["scoring"]["citation_format_total"]
