import importlib.util
import hashlib
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).parent.parent
    / "benchmarks"
    / "benchmark_agent_logic_flow.py"
)
SPEC = importlib.util.spec_from_file_location("agent_logic_flow_benchmark", SCRIPT)
BENCHMARK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BENCHMARK)


def test_treatment_command_exposes_only_logic_flow(tmp_path):
    command = BENCHMARK._codex_command(
        condition="codecanvas",
        project=tmp_path,
        answer_path=tmp_path / "answer.md",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        codecanvas_python=tmp_path / "python",
    )

    joined = " ".join(command)
    assert 'default_tools_approval_mode="approve"' in joined
    assert 'enabled_tools=["logic_flow"]' in joined
    assert "mcp_servers={}" not in joined


def test_baseline_command_disables_mcp(tmp_path):
    command = BENCHMARK._codex_command(
        condition="baseline",
        project=tmp_path,
        answer_path=tmp_path / "answer.md",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        codecanvas_python=tmp_path / "python",
    )

    assert "mcp_servers={}" in command
    assert not any("enabled_tools" in item for item in command)


def test_aggregate_reports_total_and_uncached_comparisons():
    rows = [
        {
            "baseline": {
                "total_tokens": 100,
                "uncached_input_plus_output": 80,
            },
            "codecanvas": {
                "total_tokens": 50,
                "uncached_input_plus_output": 60,
            },
        },
        {
            "baseline": {
                "total_tokens": 300,
                "uncached_input_plus_output": 120,
            },
            "codecanvas": {
                "total_tokens": 150,
                "uncached_input_plus_output": 90,
            },
        },
    ]

    assert BENCHMARK._aggregate(rows) == {
        "tasks": 2,
        "baseline_total_tokens": 400,
        "codecanvas_total_tokens": 200,
        "ratio": 0.5,
        "savings_percent": 50.0,
        "baseline_uncached_input_plus_output": 200,
        "codecanvas_uncached_input_plus_output": 150,
        "uncached": {
            "ratio": 0.75,
            "savings_percent": 25.0,
        },
    }


def test_published_holdout_result_is_internally_consistent():
    root = Path(__file__).parent.parent
    result = json.loads(
        (root / "benchmarks/results/adk_holdout_v3.json").read_text(
            encoding="utf-8"
        )
    )
    tasks = result["tasks"]

    assert sum(row["baseline"]["total_tokens"] for row in tasks) == 1_363_087
    assert sum(row["codecanvas"]["total_tokens"] for row in tasks) == 646_436
    assert result["aggregate"]["savings_percent"] == 52.58
    assert result["aggregate"]["uncached_savings_percent"] == 14.39
    assert result["aggregate"]["baseline_mean_score"] == 100.0
    assert result["aggregate"]["codecanvas_mean_score"] == 99.5

    artifacts = {
        "tasks_sha256": root
        / "benchmarks/agent_logic_flow/adk_holdout_v3_tasks.json",
        "rubric_sha256": root
        / "benchmarks/agent_logic_flow/adk_holdout_v3_rubric.json",
        "grades_sha256": root
        / "benchmarks/results/adk_holdout_v3_grades.json",
    }
    for key, path in artifacts.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            result["frozen_inputs"][key]
        )
