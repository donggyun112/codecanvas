"""Agent-facing query functions over the analysis engine.

Each function takes an analyzed FlowGraphBuilder and returns a compact,
JSON-serializable dict. No FlowGraph IR, no coordinates.
"""
from __future__ import annotations

import difflib
import base64
import hashlib
import json
import os
import re

from rapidfuzz import fuzz, process, utils

from codecanvas_mcp.mcp.answers import capped
from codecanvas_mcp.parser.call_graph import REVIEW_SIGNAL_POINTS, db_access_kind


def _location(func) -> str:
    return f"{func.file_path}:{func.line_start}"


def _risk_level(score: float) -> str:
    if score >= 10:
        return "critical"
    if score >= 6:
        return "high"
    if score >= 3:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _risk_scale() -> dict:
    return {
        "kind": "static heuristic",
        "weights": dict(REVIEW_SIGNAL_POINTS),
        "levels": {
            "0": "none",
            "1-2": "low",
            "3-5": "medium",
            "6-9": "high",
            "10+": "critical",
        },
        "aggregation": (
            "Function risk sums direct static review-signal weights. "
            "Entry point risk sums the changed functions reachable from that surface."
        ),
    }


def _entrypoint_row(e) -> dict:
    kind = getattr(e, "kind", "api") or "api"
    handler_file = getattr(e, "handler_file", "") or ""
    handler_line = getattr(e, "handler_line", 0) or 0
    row = {
        "kind": kind,
        "entrypoint_id": e.endpoint_id,
        "surface": e.label or e.path,
        "handler": getattr(e, "handler_name", "") or "",
        "location": f"{handler_file}:{handler_line}" if handler_file else "",
        "via": e.affected_functions,
        "call_depth": e.max_depth,
        "risk": e.aggregate_risk,
        "risk_level": _risk_level(e.aggregate_risk),
    }
    if kind == "api":
        row["method"] = e.method
        row["path"] = e.path
    elif e.path:
        row["module"] = e.path
    return row


def _legacy_endpoint_row(e) -> dict:
    return {
        "method": e.method,
        "path": e.path,
        "via": e.affected_functions,
        "call_depth": e.max_depth,
        "risk": e.aggregate_risk,
        "risk_level": _risk_level(e.aggregate_risk),
    }


def _is_test_path(fp: str) -> bool:
    """True if a file path looks like test code (dir segment or filename)."""
    parts = (fp or "").replace("\\", "/").split("/")
    if any(seg in ("tests", "test") for seg in parts):
        return True
    base = parts[-1] if parts else ""
    return base.startswith("test_") or base.endswith("_test.py")


def _rank_key(cg, f) -> tuple:
    """Ranking key, higher is better: (non_test, concrete, fan_in)."""
    non_test = not _is_test_path(f.file_path or "")
    concrete = not (f.is_protocol or f.is_abstract)
    fan_in = len(cg.get_callers(f.qualified_name))
    return (non_test, concrete, fan_in)


def _rank_and_select(cg, ref: str, cands: list):
    """Rank ambiguous candidates; auto-select a dominant one or return a list.

    Dominance: the top candidate wins outright on the categorical key
    (non_test, concrete); on a categorical tie it must also dominate
    fan-in by a clear margin (>= 2x and >= +2) to auto-select. Otherwise
    a ranked, best-first candidate list is returned for the agent.
    """
    keyed = sorted(
        ((_rank_key(cg, f), f) for f in cands),
        key=lambda kf: kf[0],
        reverse=True,
    )
    (top_key, top), (second_key, _second) = keyed[0], keyed[1]
    top_cat, second_cat = top_key[:2], second_key[:2]
    if top_cat > second_cat or (
        top_cat == second_cat
        and top_key[2] >= 2 * second_key[2]
        and top_key[2] - second_key[2] >= 2
    ):
        return top, None
    return None, {
        "error": f"Ambiguous '{ref}' ({len(cands)} matches); pick one by qualified_name.",
        "candidates": [
            {
                "qualified_name": f.qualified_name,
                "location": _location(f),
                "kind": "method" if f.class_name else "function",
                "is_interface": bool(f.is_protocol or f.is_abstract),
                "callers": key[2],
            }
            for key, f in keyed[:10]
        ],
    }


def _miss_suggestions(cg, funcs, ref: str) -> dict:
    """Error payload for an unresolved ref.

    Suggest qualified names whose own (tail) name matches best — exact tail
    hits first, else a fuzzy match on the tail — so the agent gets a
    copy-pasteable target instead of a bare simple name.
    """
    tail = ref.rsplit(".", 1)[-1]
    hits = [f for f in funcs if f.name == tail]
    if not hits:
        close = set(difflib.get_close_matches(tail, {f.name for f in funcs}, n=5))
        hits = [f for f in funcs if f.name in close]
    hits.sort(key=lambda f: _rank_key(cg, f), reverse=True)
    return {
        "error": f"No function matching '{ref}'.",
        "suggestions": [f.qualified_name for f in hits[:5]],
    }


def _gapped_suffix_match(qname: str, ref: str) -> bool:
    """True if ``ref``'s dotted segments occur in order within ``qname``, tail-anchored.

    Matches a scope-skipping reference like ``Class.nested`` against
    ``module.Class.method.nested`` (the enclosing ``method`` omitted). The final
    segment must coincide (the function's own name) and every ``ref`` segment
    must appear in order, so ordering and the tail are enforced — this keeps the
    looser match from firing on unrelated functions.
    """
    q = qname.split(".")
    r = ref.split(".")
    if len(r) < 2 or r[-1] != q[-1]:
        return False
    i = 0
    for seg in q:
        if i < len(r) and seg == r[i]:
            i += 1
    return i == len(r)


def resolve_function(builder, ref: str):
    """Resolve a function reference to a FunctionDef.

    Accepts a qualified name, a bare name (if unique), a ``file:line``, or a
    scope-skipping suffix such as ``Class.nested`` (enclosing method omitted).
    Returns (func, None) or (None, {"error", "suggestions"}).
    """
    cg = builder.call_graph
    funcs = cg.all_functions()

    # 1. Exact qualified name.
    exact = cg.get_function(ref)
    if exact is not None:
        return exact, None

    # 2. file:line form.
    if ":" in ref:
        path_part, _, line_part = ref.rpartition(":")
        if line_part.isdigit():
            line = int(line_part)
            matches = []
            for f in funcs:
                fp = f.file_path or ""
                same = fp.endswith(path_part) or path_part.endswith(os.path.basename(fp))
                end = f.line_end or f.line_start
                if same and f.line_start <= line <= end:
                    matches.append(f)
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return _rank_and_select(cg, ref, matches)

    # 3. Bare name or dot-boundary suffix (Class.method / module.Class.method).
    cands = [f for f in funcs
             if f.qualified_name == ref or f.qualified_name.endswith("." + ref)]
    if len(cands) == 1:
        return cands[0], None
    if len(cands) > 1:
        return _rank_and_select(cg, ref, cands)

    # 3b. Gapped dot-boundary subsequence — fallback for a reference that skips
    #     an enclosing scope, e.g. `Class.nested` omitting the method between.
    #     Only for dotted refs; a bare miss gains nothing here.
    if len(ref.split(".")) >= 2:
        gapped = [f for f in funcs if _gapped_suffix_match(f.qualified_name, ref)]
        if len(gapped) == 1:
            return gapped[0], None
        if len(gapped) > 1:
            return _rank_and_select(cg, ref, gapped)

    # 4. Miss -> suggest qualified names whose own (tail) name matches best.
    return None, _miss_suggestions(cg, funcs, ref)


def list_entrypoints(builder, filter=None, kind=None,
                     include_tests=False) -> dict:
    """List discovered entrypoints (APIs + scripts + functions).

    Optional narrowing, applied BEFORE the output cap so a target in a
    large project is not hidden by truncation:
    - ``kind``: keep only entrypoints of this kind (e.g. "api", "script").
    - ``filter``: case-insensitive substring matched against the method,
      path, handler, id, and tags.
    - ``include_tests``: by default entrypoints whose handler lives under a
      test path (``tests/`` dir, ``test_*.py`` / ``*_test.py``) are hidden,
      since test-app fixtures are not real service routes. Set True to keep
      them.
    """
    eps = builder.get_entrypoints()

    hidden_tests = 0
    if not include_tests:
        kept = [e for e in eps if not _is_test_path(e.handler_file or "")]
        hidden_tests = len(eps) - len(kept)
        eps = kept

    if kind:
        eps = [e for e in eps if e.kind == kind]
    if filter:
        needle = filter.lower()
        eps = [
            e for e in eps
            if needle in (
                f"{e.method} {e.path} {e.handler_name} {e.id} "
                f"{' '.join(e.tags or [])}"
            ).lower()
        ]

    rows = [
        {
            "id": e.id,
            "kind": e.kind,
            "method": e.method,
            "path": e.path,
            "handler": e.handler_name,
            "location": f"{e.handler_file}:{e.handler_line}",
            "tags": e.tags,
        }
        for e in eps
    ]
    rows, cap_note = capped(rows)
    out = {"count": len(eps), "entrypoints": rows}
    notes = []
    if hidden_tests:
        notes.append(
            f"{hidden_tests} test-fixture entrypoint(s) hidden; "
            f"pass include_tests=True to show them."
        )
    if cap_note:
        notes.append(cap_note)
        inventory = _export_package_inventory(eps)
        if inventory:
            notes.append(inventory)
    if notes:
        out["note"] = " ".join(notes)
    return out


def _export_package_inventory(eps) -> str | None:
    """Name the distributions behind a truncated list.

    A large monorepo has far more public surface than the output cap, and no
    ordering makes every package's headline symbol visible at once. Saying which
    packages exist turns truncation into something the caller can navigate.
    """
    counts: dict[str, int] = {}
    for entry in eps:
        if entry.kind != "export":
            continue
        package = (entry.metadata or {}).get("package")
        if package:
            counts[package] = counts.get(package, 0) + 1
    if not counts:
        return None

    listed = ", ".join(f"{name} ({n})" for name, n in sorted(counts.items()))
    return (f"Exports span {len(counts)} package(s): {listed}. "
            f"Narrow with filter=<package or symbol>.")


def _symbol_words(value: str) -> list[str]:
    """Split Python/qualified identifiers into search aliases."""
    return [
        token.lower()
        for token in re.findall(
            r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+",
            value.replace("_", " ").replace(".", " "),
        )
    ]


def _symbol_aliases(func) -> dict[str, str]:
    name_words = _symbol_words(func.name)
    qualified_words = _symbol_words(func.qualified_name)
    aliases = {
        "name": func.name,
        "qualified_name": func.qualified_name,
        "name_words": " ".join(name_words),
        "qualified_words": " ".join(qualified_words),
        "acronym": "".join(word[0] for word in name_words if word),
        "word_prefixes": " ".join(word[:4] for word in name_words),
    }
    if func.class_name:
        aliases["scope_and_name"] = " ".join(
            _symbol_words(func.class_name) + name_words
        )
    if func.docstring:
        aliases["docstring"] = func.docstring
    return {reason: alias for reason, alias in aliases.items() if alias}


def _symbol_role(func) -> tuple[str, list[str]]:
    """Describe intent without pretending static heuristics are certainty."""
    evidence = []
    decorators = " ".join(func.decorators).lower()
    normalized_path = (func.file_path or "").replace("\\", "/").lower()
    if any(segment in normalized_path for segment in (
        "/references/", "/vendor/", "/site-packages/",
    )):
        evidence.append("external_source_path")
        return "external", evidence
    if func.is_protocol or func.is_abstract:
        evidence.append("abstract_or_protocol")
        return "contract", evidence
    if any(marker in decorators for marker in ("route", ".get", ".post", ".put",
                                                ".patch", ".delete", "tool",
                                                "command")):
        evidence.append("entrypoint_decorator")
        return "entrypoint", evidence
    if any(marker in decorators for marker in ("wraps", "decorator")):
        evidence.append("wrapper_decorator")
        return "wrapper", evidence
    if len(func.calls) == 1 and len(func.logic_steps) <= 1:
        evidence.append("single_call_delegation")
        return "wrapper", evidence
    if func.class_name:
        evidence.append("concrete_class_method")
        return "implementation", evidence
    if func.name.startswith("_"):
        evidence.append("non_public_name")
        return "internal", evidence
    return "function", evidence


# Related wordings that should reach each other in a concept search. Kept
# deliberately tight: every extra member widens what "semantic" returns, and a
# loose group floods results with near-misses. Matching stays lexical — this is
# vocabulary expansion, not embeddings, and the response says so.
_CONCEPT_GROUPS = (
    {"auth", "authentication", "authorization", "login", "signin",
     "credential", "credentials", "identity", "permission"},
    {"concurrency", "concurrent", "parallel", "simultaneous", "async",
     "asynchronous", "thread", "threading", "worker"},
    {"limit", "limiter", "throttle", "throttling", "ratelimit", "quota",
     "cap", "budget"},
    {"cache", "caching", "memo", "memoize", "lru"},
    {"delete", "remove", "destroy", "erase", "purge", "drop"},
    {"create", "insert", "add", "register", "provision"},
    {"update", "modify", "change", "edit", "patch", "mutate"},
    {"fetch", "read", "load", "retrieve", "query", "lookup"},
    {"save", "persist", "write", "commit", "flush"},
    {"validate", "verify", "check", "ensure", "assert"},
    {"error", "exception", "failure", "fault"},
    {"config", "configuration", "settings", "options"},
    {"user", "account", "member", "profile"},
    {"token", "jwt", "apikey", "secret"},
    {"retry", "backoff", "resilience"},
    {"send", "deliver", "delivery", "dispatch", "emit", "publish"},
    {"message", "msg", "notification", "event", "payload"},
    {"log", "logging", "logger", "trace", "telemetry"},
    {"schedule", "scheduler", "cron", "timer", "periodic", "interval"},
    {"serialize", "encode", "marshal", "dump"},
    {"deserialize", "decode", "unmarshal", "parse"},
)
_CONCEPT_SUFFIXES = ("ization", "ations", "ation", "ence", "ance", "ency",
                     "ancy", "ings", "ing", "ers", "er", "ies", "ed", "es",
                     "s", "y")


def _stem(token: str) -> str:
    """Crude suffix stripper so `limiter`/`limit` and `requests`/`request` meet."""
    for suffix in _CONCEPT_SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _concept_index() -> dict[str, frozenset[str]]:
    index: dict[str, set[str]] = {}
    for group in _CONCEPT_GROUPS:
        expanded = set(group) | {_stem(word) for word in group}
        for word in expanded:
            index.setdefault(word, set()).update(expanded)
    return {word: frozenset(related) for word, related in index.items()}


_CONCEPT_INDEX = _concept_index()


def _expand_token(token: str) -> frozenset[str]:
    forms = {token, _stem(token)}
    for form in tuple(forms):
        forms |= _CONCEPT_INDEX.get(form, frozenset())
    return frozenset(forms)


def _concept_coverage(query: str, alias: str) -> tuple[float, list[str]]:
    """Fraction of the query's content words the alias accounts for.

    Coverage is what stops a one-word-of-four overlap from scoring like a full
    match: ``token_set_ratio`` saturates at 100 whenever the query's tokens are
    a subset, which is why partial hits used to flood the results.
    """
    query_tokens = [
        token for token in _symbol_words(query)
        if token not in _SEARCH_STOPWORDS
    ]
    if not query_tokens:
        return 0.0, []
    alias_terms = set()
    for token in _symbol_words(alias):
        alias_terms.add(token)
        alias_terms.add(_stem(token))
    matched = [
        token for token in query_tokens if _expand_token(token) & alias_terms
    ]
    return len(matched) / len(query_tokens), matched


def _name_query_coverage(query: str, alias: str) -> float:
    """Coverage for fuzzy identifier matching, including per-token typos."""
    query_tokens = [
        token for token in _symbol_words(query)
        if token not in _SEARCH_STOPWORDS
    ]
    alias_tokens = [
        token for token in _symbol_words(alias)
        if token not in _SEARCH_STOPWORDS
    ]
    if not query_tokens or not alias_tokens:
        return 0.0
    alias_terms = set(alias_tokens) | {_stem(token) for token in alias_tokens}
    matched = sum(
        1
        for query_token in query_tokens
        if (
            _expand_token(query_token) & alias_terms
            or any(
                fuzz.ratio(query_token, alias_token) >= 80
                or (
                    min(len(query_token), len(alias_token)) >= 4
                    and (
                        alias_token.startswith(query_token)
                        or query_token.startswith(alias_token)
                    )
                )
                for alias_token in alias_tokens
            )
        )
    )
    return matched / len(query_tokens)


def _concept_document(func) -> str:
    """Searchable prose for a symbol: what it is called plus what it says."""
    parts = [" ".join(_symbol_words(func.qualified_name))]
    if func.class_name:
        parts.append(" ".join(_symbol_words(func.class_name)))
    if func.docstring:
        parts.append(func.docstring)
    return " ".join(part for part in parts if part)


def _match_evidence(query: str, alias: str, field: str, strategy: str) -> dict:
    normalized_query = utils.default_process(query) or ""
    normalized_alias = utils.default_process(alias) or ""
    query_tokens = _symbol_words(query)
    coverage, matched_tokens = _concept_coverage(query, alias)
    spans = [
        {"start": block.b, "end": block.b + block.size,
         "text": normalized_alias[block.b:block.b + block.size]}
        for block in difflib.SequenceMatcher(
            None, normalized_query, normalized_alias
        ).get_matching_blocks()
        if block.size
    ]
    return {
        "field": field,
        "strategy": strategy,
        "text": alias[:240],
        "query_tokens": query_tokens,
        "matched_tokens": matched_tokens,
        "coverage": round(coverage, 3),
        "character_spans": spans,
    }


def _cursor_fingerprint(query: str, kind, path, include_tests: bool,
                        search_mode: str, min_score: float) -> str:
    raw = json.dumps(
        [query, kind, path, include_tests, search_mode, min_score],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _encode_symbol_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"v": 1, "offset": offset, "q": fingerprint},
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_symbol_cursor(cursor: str | None, fingerprint: str) -> tuple[int, str | None]:
    if not cursor:
        return 0, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("v") != 1 or payload.get("q") != fingerprint:
            raise ValueError
        offset = int(payload["offset"])
        if offset < 0:
            raise ValueError
        return offset, None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 0, "cursor is invalid or belongs to a different search."


_SEARCH_STOPWORDS = {"a", "an", "and", "for", "in", "of", "or", "the", "to"}


def _has_common_query_token(query: str, alias: str) -> bool:
    query_tokens = [
        token for token in _symbol_words(query) if token not in _SEARCH_STOPWORDS
    ]
    alias_tokens = [
        token for token in _symbol_words(alias) if token not in _SEARCH_STOPWORDS
    ]
    if set(query_tokens).intersection(alias_tokens):
        return True
    return any(
        fuzz.ratio(query_token, alias_token) >= 80
        for query_token in query_tokens
        for alias_token in alias_tokens
    )


def _render_symbol_row(row: dict, needle: str) -> dict:
    rendered = dict(row)
    func = rendered.pop("_func")
    alias = rendered.pop("_match_alias")
    field = rendered.pop("_match_field")
    strategy = rendered.pop("_match_strategy")
    role, role_evidence = _symbol_role(func)
    rendered["match"] = _match_evidence(needle, alias, field, strategy)
    rendered["role"] = role
    rendered["role_evidence"] = role_evidence
    return rendered


def find_symbols(builder, query: str, kind=None, path=None,
                 include_tests=False, limit: int = 20, cursor: str | None = None,
                 search_mode: str = "hybrid", min_score: float = 0.68) -> dict:
    """Find symbols by identifier and, optionally, docstring meaning."""
    needle = (query or "").strip()
    if not needle:
        return {"error": "query must not be empty."}
    if search_mode not in {"name", "semantic", "hybrid"}:
        return {"error": "search_mode must be name, semantic, or hybrid."}
    limit = max(1, min(int(limit), 100))
    min_score = max(0.0, min(float(min_score), 1.0))
    fingerprint = _cursor_fingerprint(
        needle, kind, path, include_tests, search_mode, min_score,
    )
    offset, cursor_error = _decode_symbol_cursor(cursor, fingerprint)
    if cursor_error:
        return {"error": cursor_error}
    funcs = []
    for func in builder.call_graph.all_functions():
        symbol_kind = (
            "class" if func.definition_type == "class"
            else "method" if func.class_name else "function"
        )
        if kind and symbol_kind != kind:
            continue
        if path and path.lower() not in (func.file_path or "").lower():
            continue
        if not include_tests and _is_test_path(func.file_path or ""):
            continue
        funcs.append((func, symbol_kind))

    choices = {}
    aliases = {}
    acronym_query = not any(char.isspace() for char in needle) and len(needle) <= 8
    for index, (func, _symbol_kind) in enumerate(funcs):
        if search_mode == "semantic":
            # Concept matching owns this mode entirely; identifier aliases
            # would only reintroduce plain name matching under another label.
            continue
        for reason, alias in _symbol_aliases(func).items():
            if reason == "docstring":
                # Scored by the concept pass below, never by token_set_ratio:
                # that scorer saturates on partial overlap.
                continue
            if reason == "acronym" and not acronym_query:
                continue
            choices[(index, reason)] = alias
            aliases[(index, reason)] = alias

    def _external_penalty(func, score: float) -> float:
        normalized_path = (func.file_path or "").replace("\\", "/").lower()
        if any(segment in normalized_path for segment in (
            "/references/", "/vendor/", "/site-packages/",
        )):
            return score * 0.75
        return score

    best: dict[int, tuple[float, str, str, str]] = {}
    for scorer_name, scorer in (
        ("weighted", fuzz.WRatio),
        ("token_sort", fuzz.token_sort_ratio),
    ):
        matches = process.extract(
            needle,
            choices,
            scorer=scorer,
            processor=utils.default_process,
            score_cutoff=50,
            limit=None,
        )
        for _alias, score, (index, reason) in matches:
            normalized_alias = utils.default_process(aliases[(index, reason)]) or ""
            normalized_query = utils.default_process(needle) or ""
            query_tokens = set(_symbol_words(needle))
            alias_tokens = set(_symbol_words(aliases[(index, reason)]))
            too_short = len(normalized_alias) < len(normalized_query) * 0.4
            func_name = utils.default_process(funcs[index][0].name) or ""
            name_tokens = set(_symbol_words(funcs[index][0].name))
            name_too_short = len(func_name) < len(normalized_query) * 0.4
            if (
                (too_short and not query_tokens.intersection(alias_tokens))
                or (name_too_short and not query_tokens.intersection(name_tokens))
            ):
                continue
            if not _has_common_query_token(needle, aliases[(index, reason)]):
                continue
            coverage = _name_query_coverage(needle, aliases[(index, reason)])
            if coverage <= 0:
                continue
            # WRatio can score a one-token subset near 0.9. Preserve typo
            # tolerance while making incomplete multi-token matches pay for
            # the query words they do not account for.
            score *= 0.5 + 0.5 * coverage
            score = _external_penalty(funcs[index][0], score)
            if index not in best or score > best[index][0]:
                best[index] = (
                    score, reason, scorer_name, aliases[(index, reason)],
                )

    if search_mode in {"semantic", "hybrid"}:
        # Concept pass: how much of the query the symbol's own words plus its
        # docstring account for, weighted by coverage so a partial overlap can
        # never outrank a full one. Vocabulary-expanded lexical matching, not
        # embeddings — `matched_tokens` shows exactly what carried the hit.
        for index, (func, _symbol_kind) in enumerate(funcs):
            document = _concept_document(func)
            coverage, _matched = _concept_coverage(needle, document)
            if coverage <= 0:
                continue
            similarity = fuzz.token_set_ratio(
                needle, document, processor=utils.default_process,
            ) / 100.0
            score = _external_penalty(
                func, 100.0 * coverage * (0.8 + 0.2 * similarity) * 0.92,
            )
            if index not in best or score > best[index][0]:
                best[index] = (score, "concept", "coverage", document)

    rows = []
    normalized_needle = utils.default_process(needle)
    for index, (score, field, strategy, alias) in best.items():
        func, symbol_kind = funcs[index]
        if utils.default_process(func.name) == normalized_needle:
            field, strategy, alias = "name", "exact", func.name
        rows.append({
            "_func": func,
            "_match_alias": alias,
            "_match_field": field,
            "_match_strategy": strategy,
            "qualified_name": func.qualified_name,
            "name": func.name,
            "kind": symbol_kind,
            "signature": f"{func.name}({', '.join(func.params)})",
            "location": _location(func),
            "score": round(score / 100.0, 3),
            "matched_by": strategy if strategy == "exact" else f"{field}:{strategy}",
        })
    rows.sort(key=lambda row: (
        row["matched_by"] != "exact",
        -row["score"],
        len(row["qualified_name"]),
        row["qualified_name"],
    ))
    exact_rows = [row for row in rows if row["matched_by"] == "exact"]
    eligible = exact_rows or [row for row in rows if row["score"] >= min_score]
    # With nothing over the bar, near-misses are more useful than an empty
    # answer: coverage weighting deliberately pushes partial matches down, so
    # without a lower floor a half-right query would return silence.
    suggestion_floor = 0.5 if eligible else 0.2
    suggestion_rows = [
        row for row in rows
        if row not in eligible and row["score"] >= suggestion_floor
    ][:5]
    total = len(eligible)
    page = [
        _render_symbol_row(row, needle)
        for row in eligible[offset:offset + limit]
    ]
    suggestions = [
        _render_symbol_row(row, needle)
        for row in suggestion_rows
    ]
    next_offset = offset + len(page)
    has_more = next_offset < total
    return {
        "query": query,
        "count": total,
        "symbols": page,
        "has_more": has_more,
        "next_cursor": (
            _encode_symbol_cursor(next_offset, fingerprint) if has_more else None
        ),
        "search_mode": search_mode,
        "match_method": (
            "concept-expanded lexical coverage over identifiers and docstrings"
            if search_mode in {"semantic", "hybrid"}
            else "identifier fuzzy match"
        ),
        "min_score": min_score,
        "suggestions": suggestions,
    }


def who_calls(builder, function: str, depth: int = 1, filter=None) -> dict:
    """Callers of a function (ground-truth reverse edges).

    ``depth`` controls how many hops of the reverse call tree to walk:
    - ``depth=1`` (default): direct callers only.
    - ``depth=N``: transitive callers up to N hops. Each row carries its
      ``depth`` (hops from the target) and ``callee`` (the function it calls
      on the traced path). The walk is breadth-first and dedups by qualified
      name, so cycles/recursion terminate and no caller is listed twice.

    ``filter`` is a case-insensitive substring matched against each row's
    caller, location, and callee. It is applied BEFORE the output cap, so a
    specific caller in a heavily-called function is not hidden by truncation.
    """
    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    cg = builder.call_graph
    depth = max(1, int(depth))

    rows = []
    visited = {func.qualified_name}
    frontier = [func]  # functions whose callers we still need to expand
    for hop in range(1, depth + 1):
        next_frontier = []
        for callee in frontier:
            for caller, ref in cg.get_callers(callee.qualified_name):
                if caller.qualified_name in visited:
                    continue
                visited.add(caller.qualified_name)
                rows.append({
                    "caller": caller.qualified_name,
                    "location": _location(caller),
                    "relation": ref.relation,
                    "condition": ref.condition,
                    "confidence": ref.confidence,
                    "depth": hop,
                    "callee": callee.qualified_name,
                })
                next_frontier.append(caller)
        if not next_frontier:
            break
        frontier = next_frontier

    if filter:
        needle = filter.lower()
        rows = [
            r for r in rows
            if needle in f"{r['caller']} {r['location']} {r['callee']}".lower()
        ]

    rows, note = capped(rows)
    out = {"function": func.qualified_name, "callers": rows}
    if note:
        out["note"] = note
    return out


def _summarize_calls(cg, func) -> dict:
    db, http, raises, callees, resolved_callees = [], [], [], [], []
    for c in func.calls:
        resolved_callees.extend(
            target.qualified_name for target in cg._resolve_call_targets(c, func)
        )
        if c.is_raise:
            raises.append({"status": c.raise_status, "exception": c.func_name})
        elif c.is_db_call:
            detail = c.db_detail or {}
            parsed = detail.get("sql_parsed")
            row = {"op": detail.get("operation"),
                   "model": detail.get("model"),
                   "call": c.func_name,
                   "access": db_access_kind(detail)}
            table = detail.get("table") or (parsed or {}).get("table")
            if table:
                row["table"] = table
            if parsed:
                row["sql"] = parsed
            db.append(row)
        elif c.is_http_call:
            http.append({"method": (c.http_detail or {}).get("method"),
                         "call": c.func_name})
        else:
            callees.append(c.func_name)
    # Dedup callees preserving order.
    seen, uniq = set(), []
    for name in callees:
        if name not in seen:
            seen.add(name)
            uniq.append(name)
    uniq, _ = capped(uniq)
    resolved_uniq = list(dict.fromkeys(resolved_callees))
    resolved_uniq, _ = capped(resolved_uniq)
    return {
        "db": db,
        "http": http,
        "raises": raises,
        "callees": uniq,
        "resolved_callees": resolved_uniq,
    }


def what_does(builder, function: str) -> dict:
    """Summarize what a function does (signature + effects), no source read.

    ``calls`` and ``effects.direct`` describe this function's own call sites;
    ``risk`` scores those alone. ``effects.transitive`` names what it reaches
    only through callees — so a zero-risk wrapper over a database write still
    shows the write, attributed to the callee that performs it.
    """
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

    func, err = resolve_function(builder, function)
    if err is not None:
        return err

    kw = "async def" if func.is_async else "def"
    ret = f" -> {func.return_annotation}" if func.return_annotation else ""
    signature = f"{kw} {func.name}({', '.join(func.params)}){ret}"

    return {
        "function": func.qualified_name,
        "async": func.is_async,
        "signature": signature,
        "docstring": (func.docstring or "").strip(),
        "calls": _summarize_calls(builder.call_graph, func),
        "effects": _effect_closure(builder.call_graph, func),
        "effect_legend": _effect_legend(),
        "risk": ImpactAnalyzer._compute_function_risk(func),
        "risk_scope": "direct call sites only; see effects.transitive",
    }


def _diff_non_python_files(diff_text: str) -> list[str]:
    """Changed non-Python file paths from a unified diff (sorted, unique).

    Mirrors ``parse_unified_diff``'s ``+++ b/<path>`` header scan, but keeps
    only the paths it drops (non ``.py``) so the agent still learns which
    files changed even when no Python function was touched.
    """
    files = set()
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and not path.endswith(".py"):
                files.add(path)
    return sorted(files)


def analyze_impact(builder, diff_text: str | None = None,
                   git_ref: str | None = None, include_tests=False) -> dict:
    """Impact of a change: changed functions -> affected entrypoints.

    Uses flow_builder=None so no FlowGraph is ever built (risk comes from
    the standalone signal-based score).

    ``include_tests``: endpoints whose handler lives under a test path are
    excluded by default (consistent with ``list_entrypoints``), since a
    change reaching a test fixture is rarely the impact the agent cares
    about. Set True to keep them.
    """
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

    if not diff_text and git_ref is not None:
        from codecanvas_mcp.graph.impact import _is_safe_git_ref
        if not _is_safe_git_ref(git_ref):
            return {"error": f"Invalid git_ref: {git_ref!r}. "
                             f"Expected a git revision or range like 'HEAD~1..HEAD'."}

    analyzer = ImpactAnalyzer(
        builder.call_graph, builder.project_root,
        entrypoints=builder.get_entrypoints(), flow_builder=None,
    )
    if diff_text:
        result = analyzer.analyze_diff(diff_text)
    else:
        result = analyzer.analyze_git_ref(git_ref or "HEAD~1..HEAD")

    changed = [
        {"function": f.qualified_name, "location": f"{f.file_path}:{f.line_start}",
         "risk": f.risk_score, "risk_level": _risk_level(f.risk_score),
         "risk_factors": f.risk_factors, "change_type": f.change_type}
        for f in result.affected_functions
    ]
    affected_eps = result.affected_endpoints
    hidden_test_eps = 0
    if not include_tests:
        kept = [e for e in affected_eps
                if not _is_test_path(getattr(e, "handler_file", "") or "")]
        hidden_test_eps = len(affected_eps) - len(kept)
        affected_eps = kept
    entrypoints = [_entrypoint_row(e) for e in affected_eps]
    endpoints = [_legacy_endpoint_row(e) for e in affected_eps]
    changed, cnote = capped(changed)
    entrypoints, enote = capped(entrypoints)
    endpoints = endpoints[:len(entrypoints)]

    skipped = _diff_non_python_files(diff_text) if diff_text else []
    summary = result.summary
    if skipped and not changed:
        # No Python function changed, but the diff did touch other files —
        # say so instead of the bare "No Python changes detected."
        summary = (f"No Python changes detected; "
                   f"{len(skipped)} non-Python file(s) changed.")

    out = {"summary": summary,
           "language": "python",
           "risk_scale": _risk_scale(),
           "changed_functions": changed,
           "affected_entrypoints": entrypoints,
           "affected_endpoints": endpoints}
    if skipped:
        out["skipped_files"] = skipped
    tnote = (f"{hidden_test_eps} test-fixture endpoint(s) hidden; "
             f"pass include_tests=True to show them." if hidden_test_eps else "")
    note = "; ".join(n for n in (cnote, enote, tnote) if n)
    if note:
        out["note"] = note
    return out


def function_flow(builder, function: str) -> dict:
    """Return a de-noised control-flow outline of a function.

    Preserves branch/loop/try nesting, early returns (with dict-key shape),
    raises, and meaningful calls — dropping logging, docstrings, and
    literal-only assignments — so the logic can be grasped without reading
    the full body.
    """
    from codecanvas_mcp.mcp import outline

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    ast_node = builder.call_graph.get_ast_node(func.qualified_name)
    import ast as _ast
    if not isinstance(ast_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }
    flow, truncated = outline.function_flow_tree(ast_node)
    lines, outline_truncated = outline.function_flow_lines(ast_node)
    truncated = truncated or outline_truncated
    out = {
        "function": func.qualified_name,
        "location": f"{func.file_path}:{func.line_start}",
        "flow": flow,
        "outline": lines,
        "truncated": truncated,
    }
    if truncated:
        out["note"] = f"outline truncated at {len(lines)} lines"
    return out


def _cyclomatic(node) -> int:
    """Approximate McCabe complexity: 1 + count of decision points."""
    import ast
    count = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While,
                          ast.ExceptHandler, ast.IfExp)):
            count += 1
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
        elif isinstance(n, ast.comprehension):
            count += 1 + len(n.ifs)
        elif hasattr(ast, "match_case") and isinstance(n, ast.match_case):
            count += 1
    return count


def _yield_value(stmt):
    """If a statement is a bare ``yield``/``yield from`` expression, return its
    rendered value (for the outcome detail); else None if it holds no yield."""
    import ast
    from codecanvas_mcp.mcp import outline
    for node in ast.walk(stmt):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # don't descend into nested scopes — their yields aren't ours
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return outline._expr(node.value) if node.value is not None else ""
    return None


def _stmt_has_yield(stmt) -> bool:
    """True if a statement contains a yield not inside a nested function."""
    import ast
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if _stmt_has_yield(child):
            return True
    return False


def reaching_conditions(builder, function: str, target=None) -> dict:
    """Guard conditions under which each outcome (return/raise) is reached.

    This re-expresses control-flow-graph reasoning as *facts* an agent can
    act on, instead of a node/edge graph. For each outcome it reports the
    *lexically enclosing* branch guards (if/elif/else, except, loop) — enough
    to spot asymmetries like an error-path ``return`` that skips a guard the
    success path enforces (e.g. "payment saved" returned from an except).

    ``target``:
    - ``None`` (default): every return/raise/yield with its guards.
    - ``"return"`` / ``"raise"`` / ``"yield"``: only that kind.
    - ``"line:N"``: the guards enclosing the statement at line N.

    ``yield`` outcomes make this work for generators/async generators (a
    yield is an output point like a return, but does not terminate the block).

    Also returns approximate cyclomatic complexity and any statements that
    are unreachable (follow an unconditional return/raise/break in the same
    block). Guards are lexical, not full path conditions.
    """
    import ast
    from codecanvas_mcp.mcp import outline

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    node = builder.call_graph.get_ast_node(func.qualified_name)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }

    outcomes: list[dict] = []
    line_guards: dict[int, list[str]] = {}
    dead: list[int] = []
    try_types = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)

    def walk(stmts, guards):
        terminated = False
        for s in stmts:
            if terminated:
                dead.append(s.lineno)
            line_guards.setdefault(s.lineno, list(guards))
            if isinstance(s, ast.Return):
                outcomes.append({"at": s.lineno, "kind": "return",
                                 "detail": outline._return_val(s.value),
                                 "guards": list(guards)})
                terminated = True
            elif isinstance(s, ast.Raise):
                outcomes.append({"at": s.lineno, "kind": "raise",
                                 "detail": outline._raise_txt(s.exc),
                                 "guards": list(guards)})
                terminated = True
            elif isinstance(s, (ast.Break, ast.Continue)):
                terminated = True
            elif isinstance(s, ast.If):
                cond = outline._expr(s.test)
                walk(s.body, guards + [cond])
                if s.orelse:
                    walk(s.orelse, guards + [f"not ({cond})"])
            elif isinstance(s, try_types):
                walk(s.body, guards)
                for h in s.handlers:
                    typ = outline._expr(h.type) if h.type else ""
                    walk(h.body, guards + [f"except {typ}".strip()])
                walk(s.orelse, guards)
                walk(s.finalbody, guards)
            elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                walk(s.body, guards + ["loop"])
                walk(s.orelse, guards)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                walk(s.body, guards)
            elif _stmt_has_yield(s):
                # A yield is a generator's output point — an outcome like a
                # return, but it does not terminate the block.
                outcomes.append({"at": s.lineno, "kind": "yield",
                                 "detail": _yield_value(s) or "",
                                 "guards": list(guards)})

    walk(node.body, [])

    if target is None:
        selected = outcomes
    elif target in ("return", "raise", "yield"):
        selected = [o for o in outcomes if o["kind"] == target]
    elif target.startswith("line:") and target[5:].isdigit():
        ln = int(target[5:])
        g = line_guards.get(ln)
        selected = [{"at": ln, "kind": "line", "guards": g}] if g is not None else []
    else:
        return {"error": f"Invalid target {target!r}. "
                         f"Use 'return', 'raise', 'yield', or 'line:N'."}

    out = {
        "function": func.qualified_name,
        "location": _location(func),
        "outcomes": selected,
        "cyclomatic": _cyclomatic(node),
    }
    if dead:
        out["dead_code"] = sorted(set(dead))
    return out


def _claim_refs(claim: str) -> tuple[str, str] | None:
    match = re.search(
        r"(?P<left>.+?)\s+reaches\s+(?P<target>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)",
        claim,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    identifiers = re.findall(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
        match.group("left"),
    )
    ignored = {"root", "sub", "agent", "task", "chat", "mode"}
    sources = [
        identifier for identifier in identifiers
        if identifier.lower() not in ignored
    ]
    if not sources:
        return None
    return sources[-1], match.group("target")


def _claim_mode(claim: str) -> str | None:
    match = re.search(r"\b([A-Za-z_]\w*)[-_\s]+mode\b", claim, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(
        r"\bmode\s*(?:==|=|is)\s*['\"]?([A-Za-z_]\w*)",
        claim,
        re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


def _claim_scope(claim: str) -> str | None:
    lowered = claim.lower()
    if re.search(r"\b(root|root_agent)\b", lowered):
        return "root_agent"
    if re.search(r"\b(sub[-_ ]?agent|child[-_ ]?agent)\b", lowered):
        return "sub_agent"
    return None


_TRUTHY_LITERALS = {"true", "1", "yes", "on", "set", "enabled"}
_FALSY_LITERALS = {"false", "0", "no", "off", "none", "null", "unset", "disabled"}
# Words that carry mode/scope meaning (handled by _claim_mode/_claim_scope) or
# are pure prose, so they never become condition subjects of their own.
_QUALIFIER_NON_SUBJECTS = {
    "root", "sub", "agent", "task", "chat", "mode", "child", "self",
    "when", "while", "under", "during", "the", "a", "an", "it", "reaches",
}


def _claim_prefix(claim: str, source_ref: str) -> str:
    """The qualifier text sitting in front of the source in a claim."""
    match = re.search(r"(?P<left>.+?)\s+reaches\s+", claim, flags=re.IGNORECASE)
    if not match:
        return ""
    left = match.group("left")
    index = left.rfind(source_ref)
    return left[:index] if index >= 0 else left


def _claim_qualifiers(claim: str, source_ref: str) -> list[dict]:
    """Condition qualifiers stated in a claim, as (subject, expected) facts.

    A claim like ``dry-run publish reaches _call_api`` asserts more than
    reachability: it fixes ``dry_run`` truthy. These used to be dropped on the
    floor, so the verdict answered a question nobody asked. Only explicit
    forms are collected — an ``x=false`` / ``without X`` / ``dry-run`` shape —
    since bare words are far more often prose than flags.
    """
    prefix = _claim_prefix(claim, source_ref)
    if not prefix.strip():
        return []
    found: list[dict] = []

    def add(subject: str, expected: bool, text: str) -> None:
        normalized = subject.strip().replace("-", "_")
        if not normalized or normalized.lower() in _QUALIFIER_NON_SUBJECTS:
            return
        if normalized.lower().endswith(("_mode", "_agent")):
            return
        if any(item["subject"] == normalized for item in found):
            return
        found.append({
            "subject": normalized,
            "expected": expected,
            "text": text.strip(),
        })

    literal = "|".join(sorted(_TRUTHY_LITERALS | _FALSY_LITERALS))
    for match in re.finditer(
        rf"(?P<subject>[A-Za-z_][\w.-]*)\s*(?:==|=|:)\s*(?P<value>{literal})\b",
        prefix,
        flags=re.IGNORECASE,
    ):
        add(
            match.group("subject"),
            match.group("value").lower() in _TRUTHY_LITERALS,
            match.group(0),
        )
    for match in re.finditer(
        r"\b(?P<polarity>without|no|missing|unset|absent|lacking|with|given|having)"
        r"\s+(?P<subject>[A-Za-z_][\w.-]*)",
        prefix,
        flags=re.IGNORECASE,
    ):
        expected = match.group("polarity").lower() in {"with", "given", "having"}
        add(match.group("subject"), expected, match.group(0))
    for match in re.finditer(r"\b[A-Za-z_]\w*(?:-\w+)+\b", prefix):
        add(match.group(0), True, match.group(0))
    return found


def _unsupported_claim_prefix(claim: str, source_ref: str) -> str:
    """Return prefix text not covered by the supported qualifier grammar."""
    remaining = _claim_prefix(claim, source_ref)
    if not remaining.strip():
        return ""

    literal = "|".join(sorted(_TRUTHY_LITERALS | _FALSY_LITERALS))
    supported = (
        r"\b[A-Za-z_]\w*[-_\s]+mode\b",
        r"\bmode\s*(?:==|=|is)\s*['\"]?[A-Za-z_]\w*['\"]?",
        r"\b(?:root(?:[-_ ]?agent)?|sub[-_ ]?agent|child[-_ ]?agent)\b",
        rf"\b[A-Za-z_][\w.-]*\s*(?:==|=|:)\s*(?:{literal})\b",
        r"\b(?:without|no|missing|unset|absent|lacking|with|given|having)"
        r"\s+[A-Za-z_][\w.-]*\b",
        r"\b[A-Za-z_]\w*(?:-\w+)+\b",
        r"\b(?:when|while|under|during|the|a|an|it)\b",
    )
    for pattern in supported:
        remaining = re.sub(pattern, " ", remaining, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", remaining).strip(" \t,;")


def _mentions_subject(node, subject: str) -> bool:
    """True if an expression names ``subject`` directly or as a string key."""
    import ast

    target = subject.lower()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id.lower() == target:
            return True
        if isinstance(child, ast.Attribute) and child.attr.lower() == target:
            return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value.lower() == target
        ):
            return True
    return False


def _literal_truthiness(node) -> bool | None:
    """Truthiness of a ``True``/``False``/``None`` literal, else None."""
    import ast

    if isinstance(node, ast.Constant) and (
        node.value is None or isinstance(node.value, bool)
    ):
        return bool(node.value)
    return None


def _guard_subject_polarity(guard: str, subject: str) -> bool | None:
    """Truthiness a guard requires of ``subject``; None if it says nothing.

    Guards arrive as source text (``not (dry_run)``, ``not (not wake)``,
    ``not (os.getenv('OPENAI_API_KEY'))``), so they are parsed rather than
    pattern-matched — negation has to nest correctly to be trustworthy.
    Non-expression guards (``loop``, ``except ValueError``) simply do not parse.
    """
    import ast

    try:
        tree = ast.parse((guard or "").strip(), mode="eval")
    except SyntaxError:
        return None
    return _polarity_of(tree.body, subject, True)


def _polarity_of(node, subject: str, truth: bool) -> bool | None:
    import ast

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _polarity_of(node.operand, subject, not truth)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            # Either side may carry the guard, so nothing is required of one.
            return None
        for value in node.values:
            found = _polarity_of(value, subject, truth)
            if found is not None:
                return found
        return None
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, op, right = node.left, node.ops[0], node.comparators[0]
        for subject_side, literal_side in ((left, right), (right, left)):
            literal = _literal_truthiness(literal_side)
            if literal is None or not _mentions_subject(subject_side, subject):
                continue
            positive = isinstance(op, (ast.Eq, ast.Is))
            required = literal if positive else not literal
            return truth == required
    if _mentions_subject(node, subject):
        return truth
    return None


def _condition_contradictions(
    path: dict,
    qualifiers: list[dict],
    recognized: set[str],
) -> list[str]:
    """Qualifiers a path's guards rule out; records the ones it can model."""
    conflicts = []
    for edge in path["edges"]:
        for guard in edge["guards"]:
            for qualifier in qualifiers:
                polarity = _guard_subject_polarity(guard, qualifier["subject"])
                if polarity is None:
                    continue
                recognized.add(qualifier["subject"])
                if polarity != qualifier["expected"]:
                    conflicts.append(
                        f"`{qualifier['text']}` contradicts guard `{guard}` "
                        f"on {edge['caller']} -> {edge['callee']}"
                    )
    return conflicts


def _line_guards(node, line: int) -> list[str]:
    import ast
    from codecanvas_mcp.mcp import outline

    def contains(stmt) -> bool:
        return stmt.lineno <= line <= getattr(stmt, "end_lineno", stmt.lineno)

    def statement_terminates(stmt) -> bool:
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return True
        if isinstance(stmt, ast.If):
            return (
                bool(stmt.orelse)
                and block_terminates(stmt.body)
                and block_terminates(stmt.orelse)
            )
        return False

    def block_terminates(stmts) -> bool:
        return any(statement_terminates(stmt) for stmt in stmts)

    def visit(stmts, guards):
        active_guards = list(guards)
        for stmt in stmts:
            if not contains(stmt):
                if (
                    isinstance(stmt, ast.If)
                    and getattr(stmt, "end_lineno", stmt.lineno) < line
                ):
                    condition = outline._expr(stmt.test)
                    body_terminates = block_terminates(stmt.body)
                    else_terminates = block_terminates(stmt.orelse)
                    if body_terminates and not else_terminates:
                        active_guards.append(f"not ({condition})")
                    elif else_terminates and not body_terminates:
                        active_guards.append(condition)
                continue
            if isinstance(stmt, ast.If):
                condition = outline._expr(stmt.test)
                if any(contains(child) for child in stmt.body):
                    return visit(stmt.body, active_guards + [condition])
                if any(contains(child) for child in stmt.orelse):
                    return visit(
                        stmt.orelse,
                        active_guards + [f"not ({condition})"],
                    )
            if isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                if any(contains(child) for child in stmt.body):
                    return visit(stmt.body, active_guards)
                for handler in stmt.handlers:
                    if any(contains(child) for child in handler.body):
                        typ = outline._expr(handler.type) if handler.type else ""
                        return visit(
                            handler.body,
                            active_guards + [f"except {typ}".strip()],
                        )
                for block in (stmt.orelse, stmt.finalbody):
                    if any(contains(child) for child in block):
                        return visit(block, active_guards)
            if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                if any(contains(child) for child in stmt.body):
                    return visit(stmt.body, active_guards + ["loop"])
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                return visit(stmt.body, active_guards)
            return active_guards
        return active_guards

    return visit(getattr(node, "body", []), [])


def _guard_mode_fact(guard: str) -> tuple[str, str, bool] | None:
    match = re.search(
        r"(?P<subject>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.mode\s*"
        r"==\s*['\"](?P<mode>[A-Za-z_]\w*)['\"]",
        guard,
    )
    if not match:
        return None
    compact = guard.strip()
    negated = compact.startswith("not (") or compact.startswith("not ")
    return match.group("subject"), match.group("mode").lower(), negated


def _claim_subject_scope(subject: str) -> str:
    simple = subject.rsplit(".", 1)[-1].lower()
    if simple in {"sa", "subagent", "sub_agent", "child", "child_agent"}:
        return "sub_agent"
    if subject.startswith("self.") or simple in {"self", "agent", "root_agent"}:
        return "root_agent"
    return "local"


def _guard_contradiction(
    guard: str,
    mode: str | None,
    scope: str | None,
) -> str | None:
    if mode is None:
        return None
    fact = _guard_mode_fact(guard)
    if fact is None:
        return None
    subject, required, negated = fact
    if scope and _claim_subject_scope(subject) != scope:
        return None
    conflicts = (not negated and mode != required) or (negated and mode == required)
    return guard if conflicts else None


def _mode_type_guard_conditions(node) -> dict[str, list[str]]:
    """Map mode-bearing subjects to their enclosing direct isinstance guards."""
    import ast
    from codecanvas_mcp.mcp import outline

    contexts: dict[str, list[str]] = {}
    if node is None:
        return contexts
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.If) or not isinstance(stmt.test, ast.Call):
            continue
        call = stmt.test
        if (
            not isinstance(call.func, ast.Name)
            or call.func.id != "isinstance"
            or len(call.args) < 2
        ):
            continue
        subject = outline._expr(call.args[0])
        reads_subject_mode = any(
            isinstance(child, ast.Attribute)
            and child.attr == "mode"
            and outline._expr(child.value) == subject
            for body_stmt in stmt.body
            for child in ast.walk(body_stmt)
        )
        if not reads_subject_mode:
            continue
        condition = outline._expr(stmt.test)
        if condition not in contexts.setdefault(subject, []):
            contexts[subject].append(condition)
    return contexts


def _mode_type_contradictions(builder, edge, mode, scope) -> list[str]:
    if mode is None:
        return []
    caller_node = builder.call_graph.get_ast_node(edge["caller"])
    contexts = _mode_type_guard_conditions(caller_node)
    guards = set(edge["guards"])
    contradictions = []
    for subject, conditions in contexts.items():
        if scope and _claim_subject_scope(subject) != scope:
            continue
        if conditions and all(
            f"not ({condition})" in guards for condition in conditions
        ):
            contradictions.append(" or ".join(conditions))
    return contradictions


def _claim_source_functions(builder, source_ref: str):
    func, err = resolve_function(builder, source_ref)
    if err is not None:
        return [], err
    if func.definition_type != "class":
        return [func], None
    prefix = func.qualified_name + "."
    methods = [
        candidate
        for candidate in builder.call_graph.all_functions()
        if candidate.qualified_name.startswith(prefix)
        and candidate.definition_type != "class"
    ]
    return methods, None


def _claim_paths(builder, sources, target, max_depth: int = 6) -> list[dict]:
    cg = builder.call_graph
    paths: list[dict] = []
    for source in sources:
        frontier = [(source, [], {source.qualified_name})]
        while frontier:
            caller, edges, visited = frontier.pop(0)
            if len(edges) >= max_depth:
                continue
            caller_node = cg.get_ast_node(caller.qualified_name)
            for call in caller.calls:
                guards = (
                    _line_guards(caller_node, call.line)
                    if caller_node is not None else []
                )
                for callee, confidence in cg.resolve_call_candidates(call, caller):
                    edge = {
                        "caller": caller.qualified_name,
                        "callee": callee.qualified_name,
                        "at": call.line,
                        "guards": guards,
                        "confidence": confidence,
                    }
                    next_edges = edges + [edge]
                    if callee.qualified_name == target.qualified_name:
                        paths.append({
                            "source": source.qualified_name,
                            "target": target.qualified_name,
                            "edges": next_edges,
                        })
                        continue
                    if callee.qualified_name in visited:
                        continue
                    frontier.append(
                        (callee, next_edges, visited | {callee.qualified_name}),
                    )
    return paths


def _claim_path_metadata(path: dict | None) -> dict:
    edges = path["edges"] if path is not None else []
    inferred_edges = [
        edge for edge in edges if edge["confidence"] == "inferred"
    ]
    grade = (
        "inferred" if inferred_edges
        else "high" if any(edge["confidence"] == "high" for edge in edges)
        else "definite"
    )
    ambiguous_calls = [
        {
            "caller": edge["caller"],
            "callee": edge["callee"],
            "location": edge["at"],
        }
        for edge in inferred_edges
    ]
    metadata = {
        "evidence_grade": grade,
        "inferred_edge_count": len(inferred_edges),
        "ambiguous_calls": ambiguous_calls,
        "truncated": False,
        "safe_to_summarize": not inferred_edges,
    }
    if inferred_edges:
        metadata["response_guidance"] = (
            "Do not turn inferred call edges into unconditional claims. "
            "Name every ambiguous candidate or return an uncertain verdict."
        )
    return metadata


def _claim_path_rank(path: dict) -> tuple[int, int]:
    grade = _claim_path_metadata(path)["evidence_grade"]
    return ({"definite": 2, "high": 1, "inferred": 0}[grade], -len(path["edges"]))


def _flow_qualification(flow: list[dict], mode: str | None) -> str | None:
    if mode is None:
        return None

    def visit(rows, root_condition=None):
        for row in rows:
            if row.get("kind") == "branch":
                condition = row.get("condition", "")
                active_root = (
                    condition
                    if row.get("scope") == "root_agent"
                    else root_condition
                )
                for nested in row.get("nested_subjects", []):
                    if (
                        nested.get("scope") == "sub_agent"
                        and mode in nested.get("condition", "").lower()
                        and active_root
                    ):
                        return (
                            f"{mode}-mode sub-agent is nested under root guard "
                            f"{active_root}"
                        )
                found = visit(row.get("then", []), active_root)
                if found:
                    return found
                found = visit(row.get("else", []), active_root)
                if found:
                    return found
            for key in ("body", "else", "finally"):
                nested_rows = row.get(key)
                if isinstance(nested_rows, list):
                    found = visit(nested_rows, root_condition)
                    if found:
                        return found
            for handler in row.get("handlers", []):
                found = visit(handler.get("body", []), root_condition)
                if found:
                    return found
        return None

    return visit(flow)


def verify_claim(builder, claim: str, max_depth: int = 6) -> dict:
    """Conservatively verify ``source reaches target`` under mode qualifiers."""
    parsed = _claim_refs((claim or "").strip())
    if parsed is None:
        return {
            "error": (
                "claim must use '<source> reaches <target>', optionally with "
                "a root/sub-agent mode qualifier."
            ),
        }
    source_ref, target_ref = parsed
    unsupported_prefix = _unsupported_claim_prefix(claim, source_ref)
    if unsupported_prefix:
        return {
            "claim": claim,
            "error": (
                "Unsupported claim prefix. Use root/sub-agent mode context or "
                "a boolean qualifier such as flag=true, dry-run, or without FLAG."
            ),
            "unsupported_prefix": unsupported_prefix,
            "safe_to_summarize": False,
            "response_guidance": (
                "Correct or remove the unsupported prefix before evaluating "
                "the reachability claim."
            ),
        }
    sources, source_error = _claim_source_functions(builder, source_ref)
    if source_error is not None:
        return source_error
    target, target_error = resolve_function(builder, target_ref)
    if target_error is not None:
        return target_error

    mode = _claim_mode(claim)
    scope = _claim_scope(claim)
    qualifiers = _claim_qualifiers(claim, source_ref)
    modelled_subjects: set[str] = set()
    paths = _claim_paths(builder, sources, target, max_depth=max_depth)
    evaluated = []
    for path in paths:
        guard_contradictions = [
            conflict
            for edge in path["edges"]
            for guard in edge["guards"]
            if (conflict := _guard_contradiction(guard, mode, scope))
        ]
        type_contradictions = [
            conflict
            for edge in path["edges"]
            for conflict in _mode_type_contradictions(
                builder, edge, mode, scope,
            )
        ]
        contradictions = guard_contradictions + type_contradictions
        condition_conflicts = _condition_contradictions(
            path, qualifiers, modelled_subjects,
        )
        evaluated.append({
            **path,
            "contradictions": list(dict.fromkeys(contradictions)),
            "condition_contradictions": list(dict.fromkeys(condition_conflicts)),
        })
    viable = [
        path for path in evaluated
        if not path["contradictions"] and not path["condition_contradictions"]
    ]
    inferred_viable = [
        path for path in viable
        if any(edge["confidence"] == "inferred" for edge in path["edges"])
    ]

    if not paths or not viable:
        verdict = "false"
    elif len(inferred_viable) == len(viable):
        verdict = "uncertain"
    else:
        verdict = "true"

    witness_candidates = viable if viable else evaluated
    witness_path = (
        max(witness_candidates, key=_claim_path_rank)
        if witness_candidates
        else None
    )
    alternative_paths = [
        path for path in evaluated if path is not witness_path
    ]

    evidence_sources = sources
    if paths:
        path_sources = {path["source"] for path in paths}
        evidence_sources = [
            source for source in sources if source.qualified_name in path_sources
        ]
    flows = [function_flow(builder, source.qualified_name) for source in evidence_sources]
    conditions = [
        reaching_conditions(builder, source.qualified_name)
        for source in evidence_sources
    ]
    counterexamples = []
    for path in evaluated:
        for contradiction in path["contradictions"]:
            counterexamples.append(
                f"{scope or 'subject'} mode={mode} contradicts required {contradiction}"
            )
        counterexamples.extend(path["condition_contradictions"])
    if not paths:
        counterexamples.append(
            f"No static call path from {source_ref} to {target_ref}."
        )
    qualification = next(
        (
            found
            for flow in flows
            if (found := _flow_qualification(flow.get("flow", []), mode))
        ),
        None,
    )
    applied_qualifiers = [
        item for item in qualifiers if item["subject"] in modelled_subjects
    ]
    unsupported_qualifiers = [
        item for item in qualifiers if item["subject"] not in modelled_subjects
    ]
    metadata = _claim_path_metadata(witness_path)
    if unsupported_qualifiers:
        # A condition no guard on the path speaks to was silently dropped
        # before; a verdict that ignores it must not read as settled.
        if verdict == "true":
            verdict = "uncertain"
        metadata["safe_to_summarize"] = False
        named = ", ".join(item["text"] for item in unsupported_qualifiers)
        metadata["response_guidance"] = " ".join(filter(None, [
            metadata.get("response_guidance"),
            f"The claim states conditions ({named}) that no guard on any "
            f"call path constrains, so the verdict does not cover them. "
            f"Say so instead of answering as if it did.",
        ]))
    return {
        "claim": claim,
        "verdict": verdict,
        "source": source_ref,
        "target": target.qualified_name,
        "counterexample": (
            counterexamples[0]
            if verdict == "false" and counterexamples
            else None
        ),
        "qualification": qualification,
        "applied_qualifiers": applied_qualifiers,
        "unsupported_qualifiers": unsupported_qualifiers,
        "paths": evaluated,
        "witness_path": witness_path,
        "alternative_paths": alternative_paths,
        "evidence": {
            "function_flow": flows,
            "reaching_conditions": conditions,
        },
        **metadata,
    }


def _schema_fields(state_schema) -> tuple[list[str], list[str], str | None]:
    """Return (schema_keys, required_keys, error) from a small schema shape."""
    if isinstance(state_schema, (list, tuple, set)):
        keys = sorted({str(k) for k in state_schema if isinstance(k, str)})
        return keys, keys, None

    if not isinstance(state_schema, dict):
        return [], [], "state_schema must be a dict or a list of field names."

    props = state_schema.get("properties")
    required = state_schema.get("required")
    if isinstance(props, dict):
        keys = {str(k) for k in props.keys()}
    else:
        reserved = {"properties", "required", "type", "title", "description"}
        keys = {str(k) for k in state_schema.keys() if k not in reserved}

    if isinstance(required, list):
        req = {str(k) for k in required if isinstance(k, str)}
    else:
        req = set(keys)
    keys |= req
    return sorted(keys), sorted(req), None


def _literal_key(node) -> str | None:
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_keys_from_literal(node) -> tuple[list[str], bool]:
    import ast
    if not isinstance(node, ast.Dict):
        return [], True
    keys: list[str] = []
    unknown = False
    for key_node in node.keys:
        if key_node is None:
            unknown = True
            continue
        key = _literal_key(key_node)
        if key is None:
            unknown = True
        else:
            keys.append(key)
    return keys, unknown


def _state_field_from_node(node, state_var: str) -> tuple[str | None, str | None]:
    """Return (field, source_kind) for state['x'] or state.x."""
    import ast
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == state_var:
            return _literal_key(node.slice), "subscript"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == state_var:
            return node.attr, "attribute"
    return None, None


def _ast_state_param_names(node) -> list[str]:
    names = []
    names.extend(arg.arg for arg in getattr(node.args, "posonlyargs", []))
    names.extend(arg.arg for arg in node.args.args)
    names.extend(arg.arg for arg in node.args.kwonlyargs)
    return names


def _ast_state_param_annotation(node, state_var: str) -> str | None:
    args = []
    args.extend(getattr(node.args, "posonlyargs", []))
    args.extend(node.args.args)
    args.extend(node.args.kwonlyargs)
    for arg in args:
        if arg.arg == state_var and arg.annotation is not None:
            return _expr_text(arg.annotation)
    return None


def _state_var_param_error(func, state_var: str, params: list[str]) -> dict | None:
    if state_var in params:
        return None
    return {
        "error": (
            f"state_var {state_var!r} must match the function parameter that "
            "receives the state mapping."
        ),
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "parameters": params,
        "hint": (
            "These state tools expect node-style functions such as "
            "def node(state). For ordinary functions, add a small wrapper or "
            "set state_var to the parameter that receives the whole state mapping."
        ),
    }


def _state_var_annotation_error(func, state_var: str, annotation: str | None) -> dict | None:
    from codecanvas_mcp.mcp.simulator import _state_mapping_annotation_error

    message = _state_mapping_annotation_error(state_var, annotation)
    if message is None:
        return None
    return {
        "error": message,
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "annotation": annotation,
        "hint": (
            "These state tools expect node-style functions such as "
            "def node(state: dict). Use dict, Mapping, MutableMapping, "
            "or a TypedDict-like state annotation; for ordinary scalar "
            "functions, add a small wrapper."
        ),
    }


def _target_name(node) -> str | None:
    import ast
    return node.id if isinstance(node, ast.Name) else None


def _expr_text(node) -> str:
    import ast
    try:
        text = ast.unparse(node)
    except Exception:
        text = "<expr>"
    return " ".join(text.split())


class _StateSchemaVisitor:
    """Collect local state-field reads, writes, and dict-shaped returns."""

    def __init__(self, state_var: str):
        self.state_var = state_var
        self.reads: list[dict] = []
        self.writes: list[dict] = []
        self.returns: list[dict] = []
        self._dict_vars: dict[str, dict] = {}

    def visit(self, node) -> None:
        import ast
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        method = getattr(self, f"visit_{node.__class__.__name__}", None)
        if method is not None:
            method(node)
            return
        self.generic_visit(node)

    def generic_visit(self, node) -> None:
        import ast
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Assign(self, node) -> None:
        for target in node.targets:
            self._record_target(target, node.value, node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node) -> None:
        self._record_target(node.target, node.value, node.lineno)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node) -> None:
        field, source = _state_field_from_node(node.target, self.state_var)
        if field is not None:
            self._write(field, node.lineno, source or "state")
            self._read(field, node.lineno, source or "state")
        self.visit(node.value)

    def visit_Subscript(self, node) -> None:
        import ast
        field, source = _state_field_from_node(node, self.state_var)
        if field is not None and isinstance(node.ctx, ast.Load):
            self._read(field, node.lineno, source or "state")
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:
        import ast
        field, source = _state_field_from_node(node, self.state_var)
        if field is not None and isinstance(node.ctx, ast.Load):
            self._read(field, node.lineno, source or "state")
        self.generic_visit(node)

    def visit_Call(self, node) -> None:
        import ast
        if isinstance(node.func, ast.Attribute):
            owner = node.func.value
            method = node.func.attr
            if isinstance(owner, ast.Name) and owner.id == self.state_var:
                self._handle_state_call(method, node)
                return
            if isinstance(owner, ast.Name) and owner.id in self._dict_vars:
                self._handle_dict_var_call(owner.id, method, node)
                return
        self.generic_visit(node)

    def visit_Return(self, node) -> None:
        keys, unknown = self._return_keys(node.value)
        self.returns.append({
            "at": node.lineno,
            "keys": sorted(set(keys)),
            "unknown_keys": unknown,
            "detail": _expr_text(node.value) if node.value is not None else "",
        })
        if node.value is not None:
            self.visit(node.value)

    def _record_target(self, target, value, line: int) -> None:
        import ast
        field, source = _state_field_from_node(target, self.state_var)
        if field is not None:
            self._write(field, line, source or "state")
            return

        name = _target_name(target)
        if name is not None and isinstance(value, ast.Dict):
            keys, unknown = _dict_keys_from_literal(value)
            self._dict_vars[name] = {"keys": set(keys), "unknown": unknown}
            return

        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            var = target.value.id
            key = _literal_key(target.slice)
            if key is not None and var in self._dict_vars:
                self._dict_vars[var]["keys"].add(key)

    def _handle_state_call(self, method: str, node) -> None:
        if method in {"get", "setdefault", "pop"} and node.args:
            key = _literal_key(node.args[0])
            if key is not None:
                self._read(key, node.lineno, f"{self.state_var}.{method}")
                if method in {"setdefault", "pop"}:
                    self._write(key, node.lineno, f"{self.state_var}.{method}")
        elif method == "update" and node.args:
            keys, _unknown = _dict_keys_from_literal(node.args[0])
            for key in keys:
                self._write(key, node.lineno, f"{self.state_var}.update")

        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _handle_dict_var_call(self, name: str, method: str, node) -> None:
        if method == "update" and node.args:
            keys, unknown = _dict_keys_from_literal(node.args[0])
            self._dict_vars[name]["keys"].update(keys)
            self._dict_vars[name]["unknown"] = self._dict_vars[name]["unknown"] or unknown
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _return_keys(self, value) -> tuple[list[str], bool]:
        import ast
        if isinstance(value, ast.Dict):
            return _dict_keys_from_literal(value)
        if isinstance(value, ast.Name):
            if value.id == self.state_var:
                return [], True
            known = self._dict_vars.get(value.id)
            if known is not None:
                return sorted(known["keys"]), bool(known["unknown"])
        return [], True

    def _read(self, field: str, line: int, source: str) -> None:
        self.reads.append({"field": field, "at": line, "source": source})

    def _write(self, field: str, line: int, source: str) -> None:
        self.writes.append({"field": field, "at": line, "source": source})


def _dedup_records(records: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in records:
        key = tuple(sorted(row.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def validate_state_schema(builder, function: str, state_schema,
                          state_var: str = "state") -> dict:
    """Check function state-field usage against a caller-provided schema.

    ``state_schema`` may be a JSON-schema-like object with ``properties`` and
    ``required`` or a simple mapping/list of field names. This is a focused
    static repro helper: it does not prove a bug, but it flags branch returns
    missing required state keys and state fields that are outside the schema.
    """
    import ast

    schema_keys, required_keys, schema_err = _schema_fields(state_schema)
    if schema_err is not None:
        return {"error": schema_err}
    if not state_var:
        return {"error": "state_var must be a non-empty string."}

    func, err = resolve_function(builder, function)
    if err is not None:
        return err

    node = builder.call_graph.get_ast_node(func.qualified_name)
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {
            "error": f"No function body available for '{func.qualified_name}' "
                     f"(it may be a class or an unparsed definition).",
        }
    param_err = _state_var_param_error(func, state_var, _ast_state_param_names(node))
    if param_err is not None:
        return param_err

    visitor = _StateSchemaVisitor(state_var)
    for stmt in node.body:
        visitor.visit(stmt)

    reads = _dedup_records(visitor.reads)
    writes = _dedup_records(visitor.writes)
    returns = visitor.returns
    schema_set = set(schema_keys)
    required_set = set(required_keys)
    diagnostics = []

    seen_unknown: set[str] = set()
    if schema_set:
        for row in reads + writes:
            field = row["field"]
            if field not in schema_set and field not in seen_unknown:
                seen_unknown.add(field)
                diagnostics.append({
                    "type": "field_not_in_schema",
                    "field": field,
                    "at": row["at"],
                    "source": row["source"],
                })

    explicit_return_fields: set[str] = set()
    has_unknown_return = False
    for row in returns:
        keys = set(row["keys"])
        explicit_return_fields.update(keys)
        has_unknown_return = has_unknown_return or bool(row["unknown_keys"])
        if schema_set:
            extras = sorted(keys - schema_set)
            for field in extras:
                diagnostics.append({
                    "type": "field_not_in_schema",
                    "field": field,
                    "at": row["at"],
                    "source": "return",
                })
        if required_set and not row["unknown_keys"]:
            missing = sorted(required_set - keys)
            if missing:
                diagnostics.append({
                    "type": "missing_required_return_keys",
                    "at": row["at"],
                    "fields": missing,
                })

    observed = ({r["field"] for r in reads} |
                {w["field"] for w in writes} |
                explicit_return_fields)
    if required_set and not has_unknown_return:
        missing_observed = sorted(required_set - observed)
        if missing_observed:
            diagnostics.append({
                "type": "required_fields_not_observed",
                "fields": missing_observed,
            })

    reads, rnote = capped(reads)
    writes, wnote = capped(writes)
    returns, retnote = capped(returns)
    diagnostics, dnote = capped(diagnostics)
    note = "; ".join(n for n in (rnote, wnote, retnote, dnote) if n)

    out = {
        "function": func.qualified_name,
        "location": _location(func),
        "state_var": state_var,
        "schema_keys": schema_keys,
        "required_keys": required_keys,
        "reads": reads,
        "writes": writes,
        "returns": returns,
        "diagnostics": diagnostics,
    }
    if note:
        out["note"] = note
    return out


def simulate_state_transition(builder, function: str, state_schema: dict,
                              cases: list[dict] | None = None,
                              invariants: list[str] | None = None,
                              overrides: list[dict] | None = None,
                              state_var: str = "state",
                              timeout_seconds: float = 3.0,
                              import_timeout_seconds: float = 10.0,
                              max_cases: int = 12,
                              python_executable: str | None = None) -> dict:
    """Run focused state cases against a module-level function in isolation."""
    import ast
    from codecanvas_mcp.mcp.simulator import simulate

    if not state_var:
        return {"error": "state_var must be a non-empty string."}
    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    if func.class_name:
        return {
            "error": "Instance and class methods are not supported by the simulator MVP.",
            "function": func.qualified_name,
            "location": _location(func),
        }
    node = builder.call_graph.get_ast_node(func.qualified_name)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        params = _ast_state_param_names(node)
        param_err = _state_var_param_error(func, state_var, params)
        if param_err is None:
            param_err = _state_var_annotation_error(
                func, state_var, _ast_state_param_annotation(node, state_var)
            )
    else:
        param_err = _state_var_param_error(
            func, state_var, [p for p in func.params if not p.startswith("*")])
    if param_err is not None:
        return param_err

    out = simulate(
        project_root=builder.project_root,
        file_path=func.file_path,
        target_name=func.name,
        state_schema=state_schema,
        cases=cases,
        invariants=invariants,
        overrides=overrides,
        state_var=state_var,
        timeout_seconds=timeout_seconds,
        import_timeout_seconds=import_timeout_seconds,
        max_cases=max_cases,
        python_executable=python_executable,
    )
    out.setdefault("function", func.qualified_name)
    out.setdefault("location", _location(func))
    return out


def _effect_tags(func) -> list[str]:
    """Compact per-node effect/shape flags: db / http / raises / stub."""
    tags = []
    if any(c.is_db_call for c in func.calls):
        tags.append("db")
    if any(c.is_http_call for c in func.calls):
        tags.append("http")
    if any(c.is_raise for c in func.calls):
        tags.append("raises")
    if not tags and (func.is_protocol or func.is_abstract or
                     (not func.calls and not func.logic_steps)):
        tags.append("stub")
    return tags


def _effect_legend() -> dict:
    return {
        "db": "observed static database call",
        "http": "observed static outbound HTTP/network call",
        "raises": "observed raise path",
        "stub": "empty/pass/ellipsis body; db/http effects are not inferred",
        "direct": "effect at this function's own call sites",
        "transitive": (
            "effect this function only reaches through a callee; `via` names "
            "the nearest callee that carries it"
        ),
    }


def _effect_closure(cg, func, depth: int = 3, include_tests: bool = False) -> dict:
    """Split a function's effects into its own and those it merely reaches.

    ``what_does`` answers for one function and ``call_tree`` walks downstream;
    without this split a wrapper that reaches a database write and one that
    performs it were reported identically (both simply "db", or neither).
    """
    direct = [tag for tag in _effect_tags(func) if tag != "stub"]
    seen_effects = set(direct)
    transitive: list[str] = []
    via: list[dict] = []
    visited = {func.qualified_name}
    frontier = [func]
    for hop in range(1, max(1, int(depth)) + 1):
        next_frontier = []
        for caller in frontier:
            for call in caller.calls:
                for callee, _confidence in cg.resolve_call_candidates(call, caller):
                    if callee.qualified_name in visited:
                        continue
                    if not include_tests and _is_test_path(callee.file_path or ""):
                        continue
                    visited.add(callee.qualified_name)
                    next_frontier.append(callee)
                    for tag in _effect_tags(callee):
                        if tag == "stub" or tag in seen_effects:
                            continue
                        seen_effects.add(tag)
                        transitive.append(tag)
                        via.append({
                            "effect": tag,
                            "through": callee.qualified_name,
                            "depth": hop,
                        })
        if not next_frontier:
            return _effect_summary(func, direct, transitive, via, hop, False)
        frontier = next_frontier
    return _effect_summary(func, direct, transitive, via, depth, True)


def _effect_summary(func, direct, transitive, via, scanned, truncated) -> dict:
    summary = {
        "direct": direct,
        "transitive": transitive,
        "via": via,
        "depth_scanned": scanned,
        "truncated": truncated,
    }
    if not direct and not transitive:
        summary["stub"] = "stub" in _effect_tags(func)
    return summary


def call_tree(builder, function: str, depth: int = 2, filter=None,
              include_tests=False) -> dict:
    """Forward transitive call tree: what this function reaches, N hops down.

    Complements ``who_calls`` (reverse). Instead of hopping node-by-node,
    get the whole downstream tree in one call, each node tagged with its
    ``depth``, the ``via`` caller on the traced path, effect/shape flags
    (db/http/raises/stub), and risk. Only project-internal functions are nodes;
    library/builtin calls are surfaced as the parent's effect tags, not
    walked. Breadth-first with dedup by qualified name, so recursion/cycles
    terminate and no function appears twice.

    ``include_tests``: callees resolving into a test path (``tests/`` dir,
    ``test_*.py``) are dropped by default — a production function reaching
    test code is almost always a name-collision misresolution. Set True to
    keep them (e.g. when tracing test code itself).

    ``filter`` is a case-insensitive substring over function/location/via,
    applied before the output cap.
    """
    from codecanvas_mcp.graph.impact import ImpactAnalyzer

    func, err = resolve_function(builder, function)
    if err is not None:
        return err
    cg = builder.call_graph
    depth = max(1, int(depth))

    nodes = []
    visited = {func.qualified_name}
    frontier = [func]
    for hop in range(1, depth + 1):
        next_frontier = []
        for caller in frontier:
            for call in caller.calls:
                for callee, confidence in cg.resolve_call_candidates(call, caller):
                    if callee.qualified_name in visited:
                        continue
                    if not include_tests and _is_test_path(callee.file_path or ""):
                        continue
                    visited.add(callee.qualified_name)
                    nodes.append({
                        "function": callee.qualified_name,
                        "location": _location(callee),
                        "depth": hop,
                        "via": caller.qualified_name,
                        "confidence": confidence,
                        "effects": _effect_tags(callee),
                        "effect_scope": "direct",
                        "risk": ImpactAnalyzer._compute_function_risk(callee),
                    })
                    next_frontier.append(callee)
        if not next_frontier:
            break
        frontier = next_frontier

    if filter:
        needle = filter.lower()
        nodes = [
            n for n in nodes
            if needle in f"{n['function']} {n['location']} {n['via']}".lower()
        ]

    nodes, note = capped(nodes)
    out = {"function": func.qualified_name, "location": _location(func),
           "nodes": nodes,
           "effects": _effect_closure(
               cg, func, depth=depth, include_tests=include_tests,
           ),
           "effect_legend": _effect_legend()}
    if note:
        out["note"] = note
    return out
