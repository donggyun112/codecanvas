"""Isolated function-level state transition simulation."""
from __future__ import annotations

import argparse
import asyncio
import builtins
import contextlib
import copy
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Any


SUPPORTED_INVARIANTS = {
    "no_exception",
    "return_is_mapping",
    "return_has_required_keys",
    "no_unknown_return_keys",
    "state_preserves_required_keys",
}


def _validate_overrides(overrides) -> tuple[list[dict], dict | None]:
    if overrides is None:
        return [], None
    if not isinstance(overrides, list):
        return [], {"error": "overrides must be a list of objects."}
    behaviors = {"return_value", "return_sequence", "raise"}
    validated = []
    for index, spec in enumerate(overrides):
        if not isinstance(spec, dict):
            return [], {"error": f"overrides[{index}] must be an object."}
        target = spec.get("target")
        if not isinstance(target, str) or not target.strip():
            return [], {"error": f"overrides[{index}].target must be a non-empty string."}
        selected = behaviors.intersection(spec)
        if len(selected) != 1:
            return [], {
                "error": f"overrides[{index}] must define exactly one of "
                         "return_value, return_sequence, or raise."
            }
        if "return_sequence" in spec and (
            not isinstance(spec["return_sequence"], list) or not spec["return_sequence"]
        ):
            return [], {
                "error": f"overrides[{index}].return_sequence must be a non-empty list."
            }
        validated.append(copy.deepcopy(spec))
    return validated, None


def _schema_parts(state_schema: dict) -> tuple[dict[str, dict], list[str]]:
    properties = state_schema.get("properties")
    if isinstance(properties, dict):
        props = {
            str(key): value if isinstance(value, dict) else {}
            for key, value in properties.items()
        }
        required = state_schema.get("required", [])
    else:
        reserved = {"properties", "required", "type", "title", "description"}
        props = {
            str(key): value if isinstance(value, dict) else {}
            for key, value in state_schema.items()
            if key not in reserved
        }
        required = state_schema.get("required", list(props))
    required_keys = [str(key) for key in required if isinstance(key, str)] \
        if isinstance(required, list) else []
    return props, required_keys


def _sample_values(spec: dict) -> list[Any]:
    if "const" in spec:
        return [spec["const"]]
    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return enum[:2]
    default = spec.get("default")
    kind = spec.get("type")
    samples: list[Any] = [default] if "default" in spec else []
    if kind == "boolean":
        samples.extend([False, True])
    elif kind == "integer":
        samples.extend([spec.get("minimum", 0), 1])
    elif kind == "number":
        samples.extend([spec.get("minimum", 0.0), 1.0])
    elif kind == "string":
        samples.extend([spec.get("minLength", 0) and "x" or "", "value"])
    elif kind == "array":
        samples.append([])
    elif kind == "object":
        samples.append({})
    elif kind == "null":
        samples.append(None)
    else:
        samples.append(None)

    unique: list[Any] = []
    for value in samples:
        if value not in unique:
            unique.append(value)
    return unique


def generate_cases(state_schema: dict, max_cases: int = 12) -> list[dict]:
    """Generate a small branch-oriented set of states from a JSON schema."""
    props, required = _schema_parts(state_schema)
    base = {
        key: _sample_values(props.get(key, {}))[0]
        for key in required
    }
    cases = [base]
    for key, spec in props.items():
        for value in _sample_values(spec):
            candidate = copy.deepcopy(base)
            candidate[key] = value
            if candidate not in cases:
                cases.append(candidate)
            if len(cases) >= max_cases:
                return cases
    return cases


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return {"type": type(value).__name__, "repr": "<max depth>"}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return {"type": type(value).__name__, "repr": repr(value)[:500]}


def _module_details(file_path: Path, project_root: Path) -> tuple[str, Path]:
    package_parts: list[str] = []
    parent = file_path.parent
    while (parent / "__init__.py").is_file():
        package_parts.insert(0, parent.name)
        parent = parent.parent
    if package_parts:
        module_parts = package_parts
        if file_path.name != "__init__.py":
            module_parts = module_parts + [file_path.stem]
        return ".".join(module_parts), parent
    return file_path.stem, project_root


def _load_target(request: dict):
    project_root = Path(request["project_root"]).resolve()
    file_path = Path(request["file_path"])
    if not file_path.is_absolute():
        file_path = project_root / file_path
    file_path = file_path.resolve()
    module_name, import_root = _module_details(file_path, project_root)
    for path in (import_root, project_root, project_root / "src"):
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
    module = importlib.import_module(module_name)
    loaded_path = Path(getattr(module, "__file__", "")).resolve()
    if loaded_path != file_path:
        raise ImportError(
            f"Imported {loaded_path} instead of target file {file_path}."
        )
    target = getattr(module, request["target_name"], None)
    if target is None or not callable(target):
        raise AttributeError(
            f"Callable {request['target_name']!r} not found in {module_name}."
        )
    return target, module


def _resolve_override_target(target: str, target_module):
    candidates = [target]
    if not target.startswith(target_module.__name__ + "."):
        candidates.append(f"{target_module.__name__}.{target}")

    errors = []
    for candidate in candidates:
        parts = candidate.split(".")
        for split_at in range(len(parts) - 1, 0, -1):
            module_name = ".".join(parts[:split_at])
            try:
                owner = importlib.import_module(module_name)
            except (ImportError, ModuleNotFoundError) as exc:
                errors.append(str(exc))
                continue
            try:
                for part in parts[split_at:-1]:
                    owner = getattr(owner, part)
                attribute = parts[-1]
                original = getattr(owner, attribute)
                return owner, attribute, original, candidate
            except AttributeError as exc:
                errors.append(str(exc))
                break
    detail = errors[-1] if errors else "target could not be resolved"
    raise AttributeError(f"Cannot apply override {target!r}: {detail}")


def _override_exception(spec: dict) -> Exception:
    raise_spec = spec["raise"]
    if isinstance(raise_spec, str):
        type_name = raise_spec
        message = raise_spec
    elif isinstance(raise_spec, dict):
        type_name = str(raise_spec.get("type", "RuntimeError"))
        message = str(raise_spec.get("message", type_name))
    else:
        raise TypeError("override 'raise' must be a string or an object.")

    exc_type = getattr(builtins, type_name, None)
    if exc_type is None or not inspect.isclass(exc_type) or not issubclass(exc_type, Exception):
        raise ValueError(f"Unsupported override exception type: {type_name!r}")
    return exc_type(message)


def _make_override_stub(original, spec: dict, record: dict):
    sequence = copy.deepcopy(spec.get("return_sequence"))

    def behavior(args, kwargs):
        record["calls"].append({
            "args": _json_safe(args),
            "kwargs": _json_safe(kwargs),
        })
        record["called"] += 1
        if "raise" in spec:
            raise _override_exception(spec)
        if sequence is not None:
            if not sequence:
                raise RuntimeError(
                    f"Override return_sequence exhausted for {record['target']!r}."
                )
            return copy.deepcopy(sequence.pop(0))
        return copy.deepcopy(spec.get("return_value"))

    if inspect.iscoroutinefunction(original):
        async def async_stub(*args, **kwargs):
            return behavior(args, kwargs)
        return async_stub

    def sync_stub(*args, **kwargs):
        return behavior(args, kwargs)
    return sync_stub


def _apply_overrides(target_module, overrides: list[dict]) -> list[dict]:
    records = [
        {"target": spec["target"], "called": 0, "calls": []}
        for spec in overrides
    ]
    for spec, record in zip(overrides, records):
        owner, attribute, original, resolved = _resolve_override_target(
            spec["target"], target_module
        )
        record["resolved_target"] = resolved
        setattr(owner, attribute, _make_override_stub(original, spec, record))
    return records


def _invoke(target, state: dict, state_var: str):
    signature = inspect.signature(target)
    params = list(signature.parameters.values())
    state_param = signature.parameters.get(state_var)
    if state_param is None:
        candidates = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            and p.name not in {"self", "cls"}
        ]
        if len(candidates) == 1:
            state_param = candidates[0]
        else:
            raise TypeError(
                f"Could not identify state parameter {state_var!r} in {signature}."
            )

    missing = [
        p.name for p in params
        if p.name != state_param.name
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        and p.default is p.empty
    ]
    if missing:
        raise TypeError(f"Additional required parameters are unsupported: {missing}")

    if state_param.kind is state_param.POSITIONAL_ONLY:
        result = target(state)
    else:
        result = target(**{state_param.name: state})
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _violations(request: dict, result: Any, state: dict, exception: dict | None) -> list[dict]:
    invariants = request["invariants"]
    schema_keys = set(request["schema_keys"])
    required_keys = set(request["required_keys"])
    violations: list[dict] = []
    if exception is not None:
        violations.append({"invariant": "no_exception", "detail": exception["message"]})
        return violations
    if "return_is_mapping" in invariants and not isinstance(result, dict):
        violations.append({
            "invariant": "return_is_mapping",
            "detail": f"Returned {type(result).__name__}, expected a mapping.",
        })
    if isinstance(result, dict):
        if "return_has_required_keys" in invariants:
            missing = sorted(required_keys - set(result))
            if missing:
                violations.append({"invariant": "return_has_required_keys", "fields": missing})
        if "no_unknown_return_keys" in invariants and schema_keys:
            extra = sorted(set(result) - schema_keys)
            if extra:
                violations.append({"invariant": "no_unknown_return_keys", "fields": extra})
    if "state_preserves_required_keys" in invariants:
        missing = sorted(required_keys - set(state))
        if missing:
            violations.append({"invariant": "state_preserves_required_keys", "fields": missing})
    return violations


def _run_case(request: dict) -> dict:
    state = copy.deepcopy(request["state"])
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = None
    exception = None
    override_records = [
        {"target": spec["target"], "called": 0, "calls": []}
        for spec in request["overrides"]
    ]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            target, target_module = _load_target(request)
            override_records = _apply_overrides(target_module, request["overrides"])
            result = _invoke(target, state, request["state_var"])
    except Exception as exc:
        exception = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }
    violations = _violations(request, result, state, exception)
    output = {
        "return_value": _json_safe(result),
        "mutated_state": _json_safe(state),
        "violations": violations,
        "passed": not violations,
        "overrides": override_records,
        "unused_overrides": [
            record["target"] for record in override_records
            if record["called"] == 0
        ],
    }
    if exception is not None:
        output["exception"] = exception
    if stdout.getvalue():
        output["stdout"] = stdout.getvalue()[-2000:]
    if stderr.getvalue():
        output["stderr"] = stderr.getvalue()[-2000:]
    return output


def _worker() -> int:
    try:
        request = json.load(sys.stdin)
        response = _run_case(request)
    except Exception as exc:
        response = {
            "passed": False,
            "violations": [{"invariant": "worker", "detail": str(exc)}],
            "exception": {"type": type(exc).__name__, "message": str(exc)},
        }
    json.dump(response, sys.stdout)
    return 0


def simulate(
    *,
    project_root: str,
    file_path: str,
    target_name: str,
    state_schema: dict,
    cases: list[dict] | None,
    invariants: list[str] | None,
    overrides: list[dict] | None,
    state_var: str,
    timeout_seconds: float,
    max_cases: int,
) -> dict:
    """Execute state cases in isolated child processes and collect evidence."""
    if not isinstance(state_schema, dict):
        return {"error": "state_schema must be a dict."}
    if cases is not None and (
        not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases)
    ):
        return {"error": "cases must be a list of state dictionaries."}
    validated_overrides, override_error = _validate_overrides(overrides)
    if override_error is not None:
        return override_error
    selected_invariants = invariants or ["no_exception"]
    unknown = sorted(set(selected_invariants) - SUPPORTED_INVARIANTS)
    if unknown:
        return {
            "error": f"Unsupported invariants: {unknown}",
            "supported_invariants": sorted(SUPPORTED_INVARIANTS),
        }
    timeout_seconds = min(30.0, max(0.1, float(timeout_seconds)))
    max_cases = min(50, max(1, int(max_cases)))
    selected_cases = copy.deepcopy(cases) if cases is not None else generate_cases(
        state_schema, max_cases=max_cases
    )
    if not selected_cases:
        return {"error": "No simulation cases were provided or generated."}
    selected_cases = selected_cases[:max_cases]
    props, required = _schema_parts(state_schema)

    results = []
    worker_path = str(Path(__file__).resolve())
    for index, case in enumerate(selected_cases):
        request = {
            "project_root": str(Path(project_root).resolve()),
            "file_path": file_path,
            "target_name": target_name,
            "state": case,
            "state_var": state_var,
            "schema_keys": sorted(props),
            "required_keys": sorted(set(required)),
            "invariants": selected_invariants,
            "overrides": validated_overrides,
        }
        try:
            completed = subprocess.run(
                [sys.executable, worker_path, "--worker"],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=timeout_seconds,
                env=os.environ.copy(),
            )
            if completed.returncode != 0:
                result = {
                    "passed": False,
                    "violations": [{
                        "invariant": "worker",
                        "detail": completed.stderr[-1000:] or f"exit code {completed.returncode}",
                    }],
                }
            else:
                result = json.loads(completed.stdout)
        except subprocess.TimeoutExpired:
            result = {
                "passed": False,
                "violations": [{
                    "invariant": "timeout",
                    "detail": f"Exceeded {timeout_seconds:g} seconds.",
                }],
            }
        except (OSError, json.JSONDecodeError) as exc:
            result = {
                "passed": False,
                "violations": [{"invariant": "worker", "detail": str(exc)}],
            }
        result["case"] = index
        result["input_state"] = case
        results.append(result)

    failed = sum(not result.get("passed", False) for result in results)
    return {
        "generated_cases": cases is None,
        "invariants": selected_invariants,
        "case_count": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return _worker()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
