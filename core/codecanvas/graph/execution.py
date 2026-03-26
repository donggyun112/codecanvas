"""Execution-step graph: the 1st-class model for flow visualization.

Unlike the function-centric FlowGraph (nodes = functions, edges = calls),
the execution graph models **what happens step by step** when an API
is called.

    ExecutionStep = one meaningful action
    DataLink = data flows from step A's output to step B's input

Projections:
    - Pipeline view: middleware → validate → handler → serialize
    - Data flow view: steps connected by DataLinks
    - Callstack view: steps grouped by function scope
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionStep:
    """One meaningful action in the request execution."""
    id: str
    label: str                          # Human-readable: "Verify user", "Query DB"
    operation: str                      # pipeline | query | transform | validate | branch | respond | side_effect
    phase: str = ""                     # Pipeline phase: trigger, middleware, validation, handler, serialization
    scope: str = ""                     # Function scope this step belongs to (qualified name)
    depth: int = 0                      # Call depth from handler (0 = handler step, 1 = callee, 2 = callee's callee)

    # Data flow
    inputs: list[str] = field(default_factory=list)    # Variable names consumed
    output: str | None = None           # Variable name produced
    output_type: str | None = None      # Type annotation of output

    # Branching
    branch_condition: str | None = None # For branch steps
    branch_id: str | None = None        # Groups steps into the same branch path
    error_label: str | None = None      # For validate failure

    # Source
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    # Back-references
    callee_function: str | None = None  # Qualified name of called function (for drill-down)
    source_node_ids: list[str] = field(default_factory=list)  # Original FlowNode IDs

    confidence: str = "definite"        # definite | high | inferred | runtime
    evidence: str = ""                  # How this step was determined

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DataLink:
    """Connection between execution steps."""
    id: str
    source_step_id: str
    target_step_id: str
    kind: str = "sequence"              # sequence | data | branch | error
    variable: str = ""                  # The variable being passed (for kind=data)
    label: str = ""                     # Edge label
    is_error_path: bool = False
    confidence: str = "definite"        # definite | high | inferred
    evidence: str = ""                  # How this link was determined


@dataclass
class ExecutionGraph:
    """The complete execution-step graph for one API endpoint."""
    steps: list[ExecutionStep] = field(default_factory=list)
    links: list[DataLink] = field(default_factory=list)

    def merge_to_l3(self) -> "ExecutionGraph":
        """Merge granular L4 steps into meaningful L3 summary steps.

        Rules:
        - Group consecutive non-branching steps by scope
        - Keep branch/respond/error steps as individual nodes
        - For merged groups, pick the highest-priority operation label
        - Rewire links to point to merged step IDs
        """
        if not self.steps:
            return ExecutionGraph()

        OP_PRIORITY = {"query": 5, "side_effect": 4, "validate": 3,
                       "transform": 2, "pipeline": 1, "process": 0}
        KEEP_OPS = {"branch", "respond", "error"}

        merged_steps: list[ExecutionStep] = []
        merged_links: list[DataLink] = []
        # Map old step IDs → new merged step ID
        id_map: dict[str, str] = {}

        # Group consecutive steps that can be merged
        groups: list[list[ExecutionStep]] = []
        current_group: list[ExecutionStep] = []

        for step in self.steps:
            if step.operation in KEEP_OPS:
                # Flush current group
                if current_group:
                    groups.append(current_group)
                    current_group = []
                groups.append([step])  # solo group
            else:
                # Check if should merge with current group
                if current_group and current_group[-1].scope != step.scope:
                    groups.append(current_group)
                    current_group = []
                current_group.append(step)

        if current_group:
            groups.append(current_group)

        link_counter = 0
        for group in groups:
            if len(group) == 1:
                # Keep as-is
                merged_steps.append(group[0])
                id_map[group[0].id] = group[0].id
            else:
                # Merge: pick best operation, combine labels
                best_op = max(group, key=lambda s: OP_PRIORITY.get(s.operation, 0))
                # Collect all inputs/outputs
                all_inputs: list[str] = []
                for s in group:
                    all_inputs.extend(s.inputs)
                all_inputs = list(dict.fromkeys(all_inputs))
                last_output = group[-1].output
                last_output_type = group[-1].output_type

                # Build summary label: use best step's why or combine top labels
                why = best_op.metadata.get("why", "")
                if not why:
                    labels = [s.label for s in group[:3]]
                    why = " → ".join(labels)

                merged_id = f"m.{group[0].id}"
                merged_step = ExecutionStep(
                    id=merged_id,
                    label=why if len(why) <= 60 else best_op.label,
                    operation=best_op.operation,
                    phase=best_op.phase,
                    scope=best_op.scope,
                    depth=min(s.depth for s in group),
                    inputs=all_inputs[:5],
                    output=last_output,
                    output_type=last_output_type,
                    branch_id=group[0].branch_id,
                    file_path=group[0].file_path,
                    line_start=group[0].line_start,
                    line_end=group[-1].line_end or group[-1].line_start,
                    source_node_ids=[sid for s in group for sid in s.source_node_ids],
                    confidence=best_op.confidence,
                    metadata={
                        **best_op.metadata,
                        "merged_step_ids": [s.id for s in group],
                        "merged_count": len(group),
                    },
                )
                merged_steps.append(merged_step)
                for s in group:
                    id_map[s.id] = merged_id

        # Rewire links
        seen_links: set[tuple[str, str]] = set()
        for link in self.links:
            new_src = id_map.get(link.source_step_id, link.source_step_id)
            new_tgt = id_map.get(link.target_step_id, link.target_step_id)
            if new_src == new_tgt:
                continue  # internal to merged group
            key = (new_src, new_tgt)
            if key in seen_links:
                continue
            seen_links.add(key)
            link_counter += 1
            merged_links.append(DataLink(
                id=f"ml.{link_counter}",
                source_step_id=new_src,
                target_step_id=new_tgt,
                kind=link.kind,
                variable=link.variable,
                label=link.label,
                is_error_path=link.is_error_path,
                confidence=link.confidence,
            ))

        return ExecutionGraph(steps=merged_steps, links=merged_links)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "id": s.id,
                    "label": s.label,
                    "operation": s.operation,
                    "phase": s.phase,
                    "scope": s.scope,
                    "depth": s.depth,
                    "inputs": s.inputs,
                    "output": s.output,
                    "outputType": s.output_type,
                    "branchCondition": s.branch_condition,
                    "branchId": s.branch_id,
                    "errorLabel": s.error_label,
                    "filePath": s.file_path,
                    "lineStart": s.line_start,
                    "lineEnd": s.line_end,
                    "calleeFunction": s.callee_function,
                    "sourceNodeIds": s.source_node_ids,
                    "confidence": s.confidence,
                    "evidence": s.evidence,
                    "metadata": s.metadata,
                }
                for s in self.steps
            ],
            "links": [
                {
                    "id": l.id,
                    "sourceStepId": l.source_step_id,
                    "targetStepId": l.target_step_id,
                    "kind": l.kind,
                    "variable": l.variable,
                    "label": l.label,
                    "isErrorPath": l.is_error_path,
                    "confidence": l.confidence,
                    "evidence": l.evidence,
                }
                for l in self.links
            ],
        }
