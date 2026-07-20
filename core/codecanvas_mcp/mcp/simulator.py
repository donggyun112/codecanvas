"""Isolated function-level state transition simulation."""
from __future__ import annotations

import argparse
import asyncio
import builtins
import contextlib
import copy
import importlib
import importlib.util
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback
from typing import Any


SUPPORTED_INVARIANTS = {
    "no_exception",
    "return_is_mapping",
    "return_has_required_keys",
    "no_unknown_return_keys",
    "state_preserves_required_keys",
}

FIXTURE_TYPES = {
    "langchain.AIMessage": ("langchain_core.messages", "AIMessage"),
    "langchain.HumanMessage": ("langchain_core.messages", "HumanMessage"),
    "langchain.SystemMessage": ("langchain_core.messages", "SystemMessage"),
    "langchain.ToolMessage": ("langchain_core.messages", "ToolMessage"),
}

SUPPORTED_SCHEMA_KEYWORDS = {
    "const", "default", "enum", "exclusiveMaximum", "exclusiveMinimum",
    "items", "maxItems", "maxLength", "maximum", "minItems", "minLength",
    "minimum", "properties", "required", "type",
}
SCHEMA_METADATA_KEYWORDS = {"description", "title"}


class _DeadlineExceeded(BaseException):
    def __init__(self, phase: str, seconds: float):
        self.phase = phase
        self.seconds = seconds
        super().__init__(f"{phase} exceeded {seconds:g} seconds.")


@contextlib.contextmanager
def _deadline(seconds: float, phase: str):
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return

    def handle_timeout(signum, frame):
        raise _DeadlineExceeded(phase, seconds)

    previous_handler = signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


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
    if isinstance(kind, list):
        samples: list[Any] = [default] if "default" in spec else []
        for member in kind:
            member_spec = dict(spec)
            member_spec["type"] = member
            samples.extend(_sample_values(member_spec))
        return _unique(samples)

    samples: list[Any] = [default] if "default" in spec else []
    if kind == "boolean":
        samples.extend([False, True])
    elif kind == "integer":
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        exclusive_minimum = spec.get("exclusiveMinimum")
        exclusive_maximum = spec.get("exclusiveMaximum")
        lower = math.ceil(minimum) if isinstance(minimum, (int, float)) else None
        upper = math.floor(maximum) if isinstance(maximum, (int, float)) else None
        if isinstance(exclusive_minimum, (int, float)):
            lower = math.floor(exclusive_minimum) + 1
        if isinstance(exclusive_maximum, (int, float)):
            upper = math.ceil(exclusive_maximum) - 1
        first = lower if lower is not None else min(0, upper) if upper is not None else 0
        samples.extend(value for value in (first, first + 1) if upper is None or value <= upper)
    elif kind == "number":
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        exclusive_minimum = spec.get("exclusiveMinimum")
        exclusive_maximum = spec.get("exclusiveMaximum")
        lower = float(exclusive_minimum if isinstance(exclusive_minimum, (int, float))
                      else minimum) if isinstance(
                          exclusive_minimum if isinstance(exclusive_minimum, (int, float))
                          else minimum, (int, float)) else None
        upper = float(exclusive_maximum if isinstance(exclusive_maximum, (int, float))
                      else maximum) if isinstance(
                          exclusive_maximum if isinstance(exclusive_maximum, (int, float))
                          else maximum, (int, float)) else None
        lower_open = isinstance(exclusive_minimum, (int, float))
        upper_open = isinstance(exclusive_maximum, (int, float))
        if lower is not None and upper is not None:
            first = (lower + upper) / 2 if lower_open or upper_open else lower
        elif lower is not None:
            first = lower + 1.0 if lower_open else lower
        elif upper is not None:
            first = upper - 1.0 if upper_open else min(0.0, upper)
        else:
            first = 0.0
        candidates = [first, first + 1.0]
        samples.extend(value for value in candidates
                       if (lower is None or value > lower or not lower_open)
                       and (upper is None or value < upper or not upper_open))
    elif kind == "string":
        minimum = max(0, spec.get("minLength", 0))
        maximum = spec.get("maxLength")
        lengths = [minimum, max(minimum, 1)]
        samples.extend("x" * length for length in lengths
                       if not isinstance(maximum, int) or length <= maximum)
    elif kind == "array":
        minimum = max(0, spec.get("minItems", 0))
        maximum = spec.get("maxItems")
        item_spec = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        item = _sample_values(item_spec)[0]
        lengths = [minimum, max(minimum, 1)]
        samples.extend([copy.deepcopy(item) for _ in range(length)] for length in lengths
                       if not isinstance(maximum, int) or length <= maximum)
    elif kind == "object":
        properties = spec.get("properties") if isinstance(spec.get("properties"), dict) else {}
        required = spec.get("required") if isinstance(spec.get("required"), list) else []
        samples.append({
            key: _sample_values(properties.get(key, {}))[0]
            for key in required
            if isinstance(key, str)
        })
    elif kind == "null":
        samples.append(None)
    else:
        samples.append(None)

    return _unique(samples)


def _unique(values: list[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
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


def _schema_generation_notes(state_schema: dict) -> dict:
    encountered: set[str] = set()

    def visit(schema: dict) -> None:
        for key, value in schema.items():
            encountered.add(str(key))
            if key == "properties" and isinstance(value, dict):
                for property_schema in value.values():
                    if isinstance(property_schema, dict):
                        visit(property_schema)
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                visit(value)
            elif key in {"allOf", "anyOf", "oneOf"} and isinstance(value, list):
                for branch in value:
                    if isinstance(branch, dict):
                        visit(branch)

    visit(state_schema)
    ignored = encountered - SUPPORTED_SCHEMA_KEYWORDS - SCHEMA_METADATA_KEYWORDS
    return {
        "strategy": "required-field baseline plus one-property variations",
        "supported_keywords_used": sorted(encountered & SUPPORTED_SCHEMA_KEYWORDS),
        "ignored_keywords": sorted(ignored),
    }


def _hydrate_fixtures(value: Any) -> Any:
    if isinstance(value, list):
        return [_hydrate_fixtures(item) for item in value]
    if not isinstance(value, dict):
        return value

    fixture_type = value.get("$type")
    if fixture_type is None:
        return {key: _hydrate_fixtures(item) for key, item in value.items()}
    if fixture_type not in FIXTURE_TYPES:
        raise ValueError(
            f"Unsupported fixture type {fixture_type!r}. "
            f"Supported types: {sorted(FIXTURE_TYPES)}"
        )
    module_name, class_name = FIXTURE_TYPES[fixture_type]
    module = importlib.import_module(module_name)
    fixture_class = getattr(module, class_name)
    kwargs = {
        key: _hydrate_fixtures(item)
        for key, item in value.items()
        if key != "$type"
    }
    return fixture_class(**kwargs)


def _redact_text(text: str) -> str:
    home = str(Path.home())
    if home:
        text = text.replace(home, "<HOME>")
    for key, value in os.environ.items():
        upper_key = key.upper()
        if (
            value
            and len(value) >= 4
            and any(marker in upper_key for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ):
            text = text.replace(value, "<redacted>")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", text)
    return re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return {"type": type(value).__name__, "repr": "<max depth>"}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    fixture_name = next((
        name for name, (module_name, class_name) in FIXTURE_TYPES.items()
        if value.__class__.__module__.startswith(module_name)
        and value.__class__.__name__ == class_name
    ), None)
    if fixture_name is not None:
        if hasattr(value, "model_dump"):
            fields = value.model_dump()
        else:
            fields = vars(value)
        return {"$type": fixture_name, **_json_safe(fields, depth + 1)}
    return {
        "type": type(value).__name__,
        "repr": _redact_text(repr(value)[:500]),
    }


def _module_details(file_path: Path, project_root: Path) -> tuple[str, Path, bool]:
    package_parts: list[str] = []
    parent = file_path.parent
    while (parent / "__init__.py").is_file():
        package_parts.insert(0, parent.name)
        parent = parent.parent
    if package_parts:
        module_parts = package_parts
        if file_path.name != "__init__.py":
            module_parts = module_parts + [file_path.stem]
        return ".".join(module_parts), parent, True
    return file_path.stem, file_path.parent, False


def _load_target(request: dict):
    project_root = Path(request["project_root"]).resolve()
    file_path = Path(request["file_path"])
    if not file_path.is_absolute():
        file_path = project_root / file_path
    file_path = file_path.resolve()
    module_name, import_root, is_package_module = _module_details(file_path, project_root)
    for path in (project_root / "src", project_root, import_root):
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
    if is_package_module:
        module = importlib.import_module(module_name)
    else:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create an import spec for {file_path}.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
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
            return _hydrate_fixtures(copy.deepcopy(sequence.pop(0)))
        return _hydrate_fixtures(copy.deepcopy(spec.get("return_value")))

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
    if exception is not None and "no_exception" in invariants:
        violations.append({"invariant": "no_exception", "detail": exception["message"]})
    if exception is not None:
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
    timings: dict[str, float] = {}
    override_records = [
        {"target": spec["target"], "called": 0, "calls": []}
        for spec in request["overrides"]
    ]
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            import_started = time.perf_counter()
            with _deadline(request["import_timeout_seconds"], "import"):
                target, target_module = _load_target(request)
                state = _hydrate_fixtures(state)
                override_records = _apply_overrides(target_module, request["overrides"])
            timings["import_seconds"] = round(time.perf_counter() - import_started, 6)

            execution_started = time.perf_counter()
            try:
                with _deadline(request["timeout_seconds"], "execution"):
                    result = _invoke(target, state, request["state_var"])
            finally:
                timings["execution_seconds"] = round(
                    time.perf_counter() - execution_started, 6
                )
    except _DeadlineExceeded as exc:
        exception = {
            "type": "TimeoutError",
            "message": str(exc),
            "phase": exc.phase,
        }
    except Exception as exc:
        exception = {
            "type": type(exc).__name__,
            "message": _redact_text(str(exc)),
            "traceback": _redact_text(traceback.format_exc(limit=8)),
        }
    if exception is not None and exception.get("phase") in {"import", "execution"}:
        violations = [{
            "invariant": "timeout",
            "phase": exception["phase"],
            "detail": exception["message"],
        }]
    else:
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
        "timings": timings,
    }
    if exception is not None:
        output["exception"] = exception
    if stdout.getvalue():
        output["stdout"] = _redact_text(stdout.getvalue()[-2000:])
    if stderr.getvalue():
        output["stderr"] = _redact_text(stderr.getvalue()[-2000:])
    return output


def _worker() -> int:
    try:
        request = json.load(sys.stdin)
        response = _run_case(request)
    except Exception as exc:
        response = {
            "passed": False,
            "violations": [{"invariant": "worker", "detail": _redact_text(str(exc))}],
            "exception": {
                "type": type(exc).__name__,
                "message": _redact_text(str(exc)),
            },
        }
    json.dump(response, sys.stdout)
    return 0


def _result_summary(results: list[dict]) -> dict:
    failed_cases = [result["case"] for result in results if not result.get("passed", False)]
    failure_kinds = sorted({
        violation.get("invariant", "unknown")
        for result in results
        for violation in result.get("violations", [])
    })
    unused_overrides = sorted({
        target
        for result in results
        for target in result.get("unused_overrides", [])
    })
    import_times = [
        result.get("timings", {}).get("import_seconds")
        for result in results
        if result.get("timings", {}).get("import_seconds") is not None
    ]
    execution_times = [
        result.get("timings", {}).get("execution_seconds")
        for result in results
        if result.get("timings", {}).get("execution_seconds") is not None
    ]
    return {
        "status": "failed" if failed_cases else "passed",
        "failed_cases": failed_cases,
        "failure_kinds": failure_kinds,
        "unused_overrides": unused_overrides,
        "max_import_seconds": max(import_times, default=None),
        "max_execution_seconds": max(execution_times, default=None),
    }


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
    import_timeout_seconds: float,
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
    if invariants is not None and (
        not isinstance(invariants, list)
        or not all(isinstance(invariant, str) for invariant in invariants)
    ):
        return {"error": "invariants must be a list of strings."}
    selected_invariants = ["no_exception"] if invariants is None else invariants
    unknown = sorted(set(selected_invariants) - SUPPORTED_INVARIANTS)
    if unknown:
        return {
            "error": f"Unsupported invariants: {unknown}",
            "supported_invariants": sorted(SUPPORTED_INVARIANTS),
        }
    try:
        timeout_seconds = min(30.0, max(0.1, float(timeout_seconds)))
        import_timeout_seconds = min(60.0, max(0.1, float(import_timeout_seconds)))
        max_cases = min(50, max(1, int(max_cases)))
    except (TypeError, ValueError):
        return {
            "error": "timeout_seconds and import_timeout_seconds must be numbers; "
                     "max_cases must be an integer."
        }
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
            "timeout_seconds": timeout_seconds,
            "import_timeout_seconds": import_timeout_seconds,
        }
        try:
            completed = subprocess.run(
                [sys.executable, worker_path, "--worker"],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=import_timeout_seconds + timeout_seconds + 1.0,
                env=os.environ.copy(),
            )
            if completed.returncode != 0:
                result = {
                    "passed": False,
                    "violations": [{
                        "invariant": "worker",
                        "detail": _redact_text(
                            completed.stderr[-1000:] or f"exit code {completed.returncode}"
                        ),
                    }],
                }
            else:
                result = json.loads(completed.stdout)
        except subprocess.TimeoutExpired:
            result = {
                "passed": False,
                "violations": [{
                    "invariant": "timeout",
                    "phase": "worker",
                    "detail": "Worker exceeded the combined import and execution deadline.",
                }],
            }
        except (OSError, json.JSONDecodeError) as exc:
            result = {
                "passed": False,
                "violations": [{"invariant": "worker", "detail": _redact_text(str(exc))}],
            }
        result["case"] = index
        result["input_state"] = case
        results.append(result)

    failed = sum(not result.get("passed", False) for result in results)
    output = {
        "generated_cases": cases is None,
        "invariants": selected_invariants,
        "case_count": len(results),
        "passed": len(results) - failed,
        "failed": failed,
        "summary": _result_summary(results),
        "results": results,
    }
    if cases is None:
        output["generated_case_notes"] = _schema_generation_notes(state_schema)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return _worker()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
