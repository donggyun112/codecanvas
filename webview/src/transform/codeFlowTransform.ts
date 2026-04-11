/**
 * Code Flow view: execution-order nodes with inline source code.
 *
 * Combines the semantic structure of exec_l4 (operation labels, data flow,
 * branch/error paths) with the code visibility of cfg_block (actual source
 * statements). The cross-kind join happens here at render time via line-range
 * overlap — no new edges or links are added to the canonical graph.
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData } from '../types/flow';
import type { PathState } from './pathState';
import { projectByKind } from './projection';

interface CodeStatement {
  line: number;
  lineEnd: number | null;
  text: string;
  kind: string;
}

/**
 * Build a line→statements index from all cfg_block nodes in the graph.
 */
function buildCfgLineIndex(
  flowData: FlowGraph,
): Map<number, CodeStatement[]> {
  const index = new Map<number, CodeStatement[]>();
  for (const n of Object.values(flowData.nodes)) {
    if (n.kind !== 'cfg_block') continue;
    const stmts = (n.metadata?.statements as CodeStatement[]) ?? [];
    for (const s of stmts) {
      const existing = index.get(s.line);
      if (existing) {
        // Deduplicate by line number (same line can appear in multiple blocks
        // only if CFG has overlapping ranges, which shouldn't happen but guard).
        if (!existing.some((e) => e.line === s.line && e.text === s.text)) {
          existing.push(s);
        }
      } else {
        index.set(s.line, [s]);
      }
    }
  }
  return index;
}

/**
 * Find code statements overlapping an exec_step's line range.
 */
function findOverlappingStatements(
  lineStart: number | null,
  lineEnd: number | null,
  cfgIndex: Map<number, CodeStatement[]>,
): CodeStatement[] {
  if (!lineStart) return [];
  const end = lineEnd ?? lineStart;
  const result: CodeStatement[] = [];
  const seen = new Set<number>();
  for (let line = lineStart; line <= end; line++) {
    const stmts = cfgIndex.get(line);
    if (!stmts) continue;
    for (const s of stmts) {
      if (!seen.has(s.line)) {
        seen.add(s.line);
        result.push(s);
      }
    }
  }
  return result.sort((a, b) => a.line - b.line);
}

export function transformCodeFlow(
  flowData: FlowGraph,
  selectedNodeId: string | null,
  hasTrace: boolean,
  viewMode: 'all' | 'runtime' | 'static',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Project exec_l4 nodes + data_flow edges
  const projection = projectByKind(
    flowData,
    new Set(['exec_l4']),
    new Set(['data_flow']),
  );

  if (projection.nodes.length === 0) return { nodes, edges };

  // Build line→code index from cfg_blocks
  const cfgIndex = buildCfgLineIndex(flowData);

  // Runtime hit lookup
  const runtimeHitNodes = new Set<string>();
  if (hasTrace) {
    for (const n of Object.values(flowData.nodes)) {
      if (n.metadata?.runtime_hit) runtimeHitNodes.add(n.id);
    }
  }

  const pathStateById: Record<string, PathState> = {};

  for (const node of projection.nodes) {
    const m = node.metadata ?? {};
    const sourceNodeIds = (m.source_node_ids as string[]) ?? [];
    const hasSourceIds = sourceNodeIds.length > 0;
    const hitKnown = hasTrace && hasSourceIds;
    const isHit = hitKnown
      ? sourceNodeIds.some((id) => runtimeHitNodes.has(id))
      : false;

    if (hasTrace && viewMode === 'static') {
      if (hitKnown && isHit) continue;
      if (node.confidence === 'runtime') continue;
    }
    if (hasTrace && viewMode === 'runtime' && hitKnown && !isHit) continue;

    let pathState: PathState = 'possible';
    if (hasTrace) {
      if (node.confidence === 'runtime') pathState = 'runtime-only';
      else if (hitKnown) pathState = isHit ? 'verified' : 'unverified';
    }
    pathStateById[node.id] = pathState;

    // Cross-kind join: find code statements from cfg_blocks
    const codeStatements = findOverlappingStatements(
      node.lineStart,
      node.lineEnd,
      cfgIndex,
    );

    const operation = (m.operation as string) ?? 'process';
    const output = (m.output as string | null) ?? null;
    const outputType = (m.output_type as string | null) ?? null;
    const errorLabel = (m.error_label as string | null) ?? null;
    const branchCondition = (m.branch_condition as string | null) ?? null;
    const branchId = (m.branch_id as string | null) ?? null;
    const depth = (m.depth as number) ?? 0;

    nodes.push({
      id: node.id,
      type: 'codeFlow',
      position: { x: 0, y: 0 },
      data: {
        id: node.id,
        label: node.name,
        operation,
        output,
        outputType,
        errorLabel,
        branchCondition,
        branchId,
        depth,
        filePath: node.filePath,
        lineStart: node.lineStart,
        lineEnd: node.lineEnd,
        codeStatements,
        hasCode: codeStatements.length > 0,
        metadata: m,
        isSelected: node.id === selectedNodeId,
        isHit: hitKnown ? isHit : false,
        hitUnknown: hasTrace && !hasSourceIds,
        pathState,
        hasTrace,
      },
    });
  }

  const visibleIds = new Set(nodes.map((n) => n.id));

  // Build edges (same logic as executionTransform)
  for (const edge of projection.edges) {
    if (!visibleIds.has(edge.sourceId) || !visibleIds.has(edge.targetId)) continue;

    const variable = (edge.metadata?.variable as string) ?? '';
    let edgeLabel = edge.label || variable || '';

    // Branch label resolution
    const srcNode = projection.nodeMap[edge.sourceId];
    const tgtNode = projection.nodeMap[edge.targetId];
    if (srcNode?.metadata?.operation === 'branch' && tgtNode?.metadata?.branch_id) {
      const path = (tgtNode.metadata.branch_id as string).split(':').pop() || '';
      if (path === 'if') edgeLabel = 'yes';
      else if (path === 'else') edgeLabel = 'no';
      else if (path) edgeLabel = path;
    }

    const kind = (edge.metadata?.data_kind as string) || 'sequence';
    let color: string | undefined;
    let dashed = false;
    if (kind === 'error' || edge.isErrorPath) { color = '#e74c3c'; dashed = true; }
    else if (kind === 'data') { color = '#3498db'; }
    else if (kind === 'branch') { color = '#f39c12'; }

    const srcVerified = pathStateById[edge.sourceId] === 'verified';
    const tgtVerified = pathStateById[edge.targetId] === 'verified';
    const edgeHit = hasTrace && srcVerified && tgtVerified;
    let edgePathState: PathState = 'possible';
    if (hasTrace) {
      edgePathState = edgeHit ? 'verified' : 'unverified';
    }

    edges.push({
      id: edge.id,
      source: edge.sourceId,
      target: edge.targetId,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label: edgeLabel,
        hasTrace,
        isHit: edgeHit,
        pathState: edgePathState,
        kind,
      },
    });
  }

  return { nodes, edges };
}
