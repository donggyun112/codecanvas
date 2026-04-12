"""Change impact analysis: git diff → affected functions → affected endpoints.

Given a unified diff, identifies which functions changed and traces
upstream through the call graph to find affected API endpoints / entrypoints.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from codecanvas.graph.models import FlowGraph, FlowNode, EdgeType
from codecanvas.parser.call_graph import CallGraphBuilder


@dataclass
class ChangedHunk:
    """A contiguous range of changed lines in a file."""
    file_path: str        # Relative path
    start_line: int
    end_line: int
    change_type: str = "modified"  # modified, added, deleted


@dataclass
class AffectedFunction:
    """A function affected by a change."""
    qualified_name: str
    name: str
    file_path: str
    line_start: int
    line_end: int | None
    change_type: str      # modified, added, deleted
    hunks: list[ChangedHunk] = field(default_factory=list)
    risk_score: float = 0


@dataclass
class AffectedEndpoint:
    """An endpoint affected through the call chain."""
    endpoint_id: str
    label: str
    method: str
    path: str
    affected_functions: list[str]  # qualified names
    max_depth: int = 0             # how far the change is from the handler
    aggregate_risk: float = 0


@dataclass
class ImpactResult:
    """Complete impact analysis result."""
    changed_hunks: list[ChangedHunk] = field(default_factory=list)
    affected_functions: list[AffectedFunction] = field(default_factory=list)
    affected_endpoints: list[AffectedEndpoint] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "changedHunks": [
                {"filePath": h.file_path, "startLine": h.start_line,
                 "endLine": h.end_line, "changeType": h.change_type}
                for h in self.changed_hunks
            ],
            "affectedFunctions": [
                {"qualifiedName": f.qualified_name, "name": f.name,
                 "filePath": f.file_path, "lineStart": f.line_start,
                 "lineEnd": f.line_end, "changeType": f.change_type,
                 "riskScore": f.risk_score}
                for f in self.affected_functions
            ],
            "affectedEndpoints": [
                {"endpointId": e.endpoint_id, "label": e.label,
                 "method": e.method, "path": e.path,
                 "affectedFunctions": e.affected_functions,
                 "maxDepth": e.max_depth, "aggregateRisk": e.aggregate_risk}
                for e in self.affected_endpoints
            ],
            "summary": self.summary,
        }


def parse_unified_diff(diff_text: str) -> list[ChangedHunk]:
    """Parse unified diff format to extract changed file+line ranges."""
    hunks: list[ChangedHunk] = []
    current_file: str | None = None

    for line in diff_text.splitlines():
        # File header: +++ b/path/to/file.py
        if line.startswith("+++ b/"):
            current_file = line[6:]
            # Only track Python files
            if not current_file.endswith(".py"):
                current_file = None
            continue

        # Hunk header: @@ -old_start,old_count +new_start,new_count @@
        if line.startswith("@@") and current_file:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                hunks.append(ChangedHunk(
                    file_path=current_file,
                    start_line=start,
                    end_line=start + max(count - 1, 0),
                ))

    return hunks


def get_git_diff(project_root: str, ref_range: str = "HEAD~1..HEAD") -> str:
    """Get unified diff from git."""
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", ref_range],
            cwd=project_root,
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


class ImpactAnalyzer:
    """Analyze change impact on the call graph."""

    def __init__(self, call_graph: CallGraphBuilder, project_root: str,
                 entrypoints: list | None = None,
                 flow_builder: Any = None):
        self.cg = call_graph
        self.cg.analyze_project()
        self.project_root = os.path.abspath(project_root)
        self._entrypoints = entrypoints or []
        self._flow_builder = flow_builder  # FlowGraphBuilder for risk lookup

    def analyze_diff(self, diff_text: str) -> ImpactResult:
        """Analyze impact from a unified diff string."""
        hunks = parse_unified_diff(diff_text)
        if not hunks:
            return ImpactResult(summary="No Python changes detected.")
        return self._analyze_hunks(hunks)

    def analyze_git_ref(self, ref_range: str = "HEAD~1..HEAD") -> ImpactResult:
        """Analyze impact from a git ref range."""
        diff_text = get_git_diff(self.project_root, ref_range)
        if not diff_text:
            return ImpactResult(summary="No diff available.")
        return self.analyze_diff(diff_text)

    def _analyze_hunks(self, hunks: list[ChangedHunk]) -> ImpactResult:
        """Core analysis: hunks → affected functions → affected endpoints."""
        result = ImpactResult(changed_hunks=hunks)

        # 1. Map hunks to functions
        affected_qnames: set[str] = set()
        for hunk in hunks:
            funcs = self._find_functions_at(hunk.file_path, hunk.start_line, hunk.end_line)
            for func in funcs:
                if func.qualified_name not in affected_qnames:
                    affected_qnames.add(func.qualified_name)
                    result.affected_functions.append(AffectedFunction(
                        qualified_name=func.qualified_name,
                        name=func.name,
                        file_path=func.file_path,
                        line_start=func.line_start,
                        line_end=func.line_end,
                        change_type=hunk.change_type,
                        hunks=[hunk],
                    ))
                else:
                    # Add hunk to existing
                    af = next(f for f in result.affected_functions
                             if f.qualified_name == func.qualified_name)
                    af.hunks.append(hunk)

        # 1b. Compute risk scores — use FlowGraph risk if builder available
        risk_cache: dict[str, float] = {}  # qname → score from flow graphs

        # 2. Trace upstream to find affected endpoints
        all_entrypoints = self._entrypoints
        # Build reverse call index: callee → set of callers
        caller_index = self._build_caller_index()

        for ep in all_entrypoints:
            # BFS from endpoint handler to see if any affected function is reachable
            handler_func = self.cg._find_function(ep.handler_name, ep.handler_file, ep.handler_line)
            if not handler_func:
                continue

            reachable = self._find_reachable(handler_func.qualified_name)
            overlap = affected_qnames & reachable
            if overlap:
                max_depth = max(
                    self._call_depth(handler_func.qualified_name, qn)
                    for qn in overlap
                )

                # Build the flow graph for this endpoint to get risk scores
                # for functions that appear in THIS endpoint's graph.
                if self._flow_builder:
                    try:
                        graph = self._flow_builder.build_flow(ep)
                        for node in graph.nodes.values():
                            score = node.metadata.get("risk_score", 0)
                            if score and node.id not in risk_cache:
                                risk_cache[node.id] = score
                    except Exception:
                        pass

                # Fill per-function risk from cache
                for af in result.affected_functions:
                    if af.risk_score == 0 and af.qualified_name in risk_cache:
                        af.risk_score = risk_cache[af.qualified_name]

                agg_risk = sum(
                    af.risk_score for af in result.affected_functions
                    if af.qualified_name in overlap
                )
                result.affected_endpoints.append(AffectedEndpoint(
                    endpoint_id=ep.id,
                    label=ep.label,
                    method=ep.method,
                    path=ep.path,
                    affected_functions=sorted(overlap),
                    max_depth=max_depth,
                    aggregate_risk=round(agg_risk, 1),
                ))

        # 3. Summary
        nf = len(result.affected_functions)
        ne = len(result.affected_endpoints)
        result.summary = f"{nf} function(s) changed, {ne} endpoint(s) affected."

        return result

    @staticmethod
    def _compute_function_risk(func) -> float:
        """Compute risk score for a single function (mirrors builder logic)."""
        SIGNAL_POINTS = {
            "db_write": 3, "db_read": 1, "http_call": 3,
            "raises_5xx": 4, "raises_4xx": 2, "raises": 1,
            "auth": 2, "io": 1,
        }
        signals = CallGraphBuilder._aggregate_review_signals(func)
        raw = 0
        for sig in signals:
            if sig == "raises" and ("raises_4xx" in signals or "raises_5xx" in signals):
                continue
            raw += SIGNAL_POINTS.get(sig, 0)
        # Error paths from calls
        err_count = sum(1 for c in func.calls if c.is_raise)
        raw += err_count
        return round(raw, 1)

    def _find_functions_at(self, rel_path: str, start: int, end: int) -> list:
        """Find functions whose line range overlaps [start, end]."""
        results = []
        for func in self.cg._functions.values():
            # Normalize paths for comparison
            func_rel = os.path.relpath(func.file_path, self.project_root) if os.path.isabs(func.file_path) else func.file_path
            if func_rel != rel_path and not rel_path.endswith(func_rel) and not func_rel.endswith(rel_path):
                continue
            func_end = func.line_end or func.line_start
            if func.line_start <= end and func_end >= start:
                results.append(func)
        return results

    def _find_reachable(self, root_qname: str, max_depth: int = 10) -> set[str]:
        """BFS: find all functions reachable from root via calls + Depends().

        Follows both explicit function calls and FastAPI Depends() parameters
        so that dependency-injected functions are counted as reachable.
        """
        visited: set[str] = set()
        frontier = {root_qname}
        for _ in range(max_depth):
            if not frontier:
                break
            visited |= frontier
            next_frontier: set[str] = set()
            for qn in frontier:
                func = self.cg._functions.get(qn)
                if not func:
                    continue
                # Follow explicit calls
                for call in func.calls:
                    target = self.cg._resolve_call(call, func)
                    if target and target.qualified_name not in visited:
                        next_frontier.add(target.qualified_name)
                # Follow Depends() parameters
                for dep_qn in self._get_depends_targets(func):
                    if dep_qn not in visited:
                        next_frontier.add(dep_qn)
            frontier = next_frontier
        return visited

    def _get_depends_targets(self, func) -> list[str]:
        """Extract Depends() function qualified names from handler parameters."""
        import ast
        ast_node = self.cg.get_ast_node(func.qualified_name)
        if not ast_node or not isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return []

        from codecanvas.graph.ast_execution import ASTExecutionBuilder
        results = []
        # Check annotations + defaults for Depends()
        for arg in ast_node.args.args + ast_node.args.kwonlyargs:
            if arg.annotation:
                name = ASTExecutionBuilder._extract_depends_name(arg.annotation)
                if name:
                    resolved = self.cg._resolve_by_name(name, func.file_path)
                    if resolved:
                        results.append(resolved.qualified_name)
        for default in list(ast_node.args.defaults) + list(ast_node.args.kw_defaults):
            if default is None:
                continue
            name = ASTExecutionBuilder._extract_depends_name(default)
            if name:
                resolved = self.cg._resolve_by_name(name, func.file_path)
                if resolved:
                    results.append(resolved.qualified_name)
        return results

    def _call_depth(self, from_qn: str, to_qn: str, max_depth: int = 10) -> int:
        """BFS depth from from_qn to to_qn (follows calls + Depends)."""
        if from_qn == to_qn:
            return 0
        visited: set[str] = set()
        frontier = {from_qn}
        for depth in range(1, max_depth + 1):
            next_frontier: set[str] = set()
            for qn in frontier:
                func = self.cg._functions.get(qn)
                if not func:
                    continue
                targets: set[str] = set()
                for call in func.calls:
                    target = self.cg._resolve_call(call, func)
                    if target:
                        targets.add(target.qualified_name)
                for dep_qn in self._get_depends_targets(func):
                    targets.add(dep_qn)
                for tqn in targets:
                    if tqn == to_qn:
                        return depth
                    if tqn not in visited:
                        next_frontier.add(tqn)
            visited |= frontier
            frontier = next_frontier
        return max_depth

    def _build_caller_index(self) -> dict[str, set[str]]:
        """Build reverse index: callee_qname → set of caller_qnames."""
        index: dict[str, set[str]] = {}
        for func in self.cg._functions.values():
            for call in func.calls:
                target = self.cg._resolve_call(call, func)
                if target:
                    index.setdefault(target.qualified_name, set()).add(func.qualified_name)
        return index


def annotate_flow_graph_impact(graph: FlowGraph, impact: ImpactResult) -> None:
    """Mark affected nodes in a FlowGraph with impact metadata."""
    affected_qnames = {f.qualified_name for f in impact.affected_functions}
    affected_map = {f.qualified_name: f for f in impact.affected_functions}

    for node in graph.nodes.values():
        if node.id in affected_qnames:
            af = affected_map[node.id]
            node.metadata["change_impact"] = {
                "changed": True,
                "change_type": af.change_type,
                "hunks": [{"startLine": h.start_line, "endLine": h.end_line}
                          for h in af.hunks],
            }
