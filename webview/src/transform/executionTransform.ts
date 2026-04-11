/**
 * Transform Execution / data-flow view from canonical FlowGraph.
 *
 * Reads `exec_step` nodes and `data_flow` edges via projectByKind, then
 * splits L3 (summary) vs L4 (detail) by node-id prefix.
 *
 * Domain-specific behavior preserved from the original transform:
 *   - sourceNodeIds-based runtime hit determination + hitUnknown badge
 *   - branch label resolution (if→yes, else→no) via branchId suffix
 *   - origin chain injection for selected respond steps
 *   - 3-way pathState (verified/unverified/runtime-only) + view-mode filter
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData } from '../types/flow';
import type { PathState } from './pathState';
import { projectByKind } from './projection';

// L3 summary and L4 detail use distinct kinds so projection by kind alone
// is unambiguous. The id prefixes survive for selection / origin chain
// resolution where consumers still need to know which level a node lives at.
const L3_KIND = 'exec_l3';
const L4_KIND = 'exec_l4';
const L3_ID_PREFIX = 'exec_l3:';
const L4_ID_PREFIX = 'exec:';

/**
 * Extract the structural ExecStep fields from a canonical exec_step node.
 * Returns a flat data shape compatible with DataFlowNode.tsx expectations.
 */
function extractStepData(node: FlowNodeData) {
  const m = node.metadata ?? {};
  return {
    id: node.id,
    label: node.name,
    operation: (m.operation as string) ?? 'process',
    phase: (m.phase as string) ?? '',
    scope: node.scope,
    depth: (m.depth as number) ?? 0,
    inputs: (m.inputs as string[]) ?? [],
    output: (m.output as string | null) ?? null,
    outputType: (m.output_type as string | null) ?? null,
    branchCondition: (m.branch_condition as string | null) ?? null,
    branchId: (m.branch_id as string | null) ?? null,
    errorLabel: (m.error_label as string | null) ?? null,
    filePath: node.filePath,
    lineStart: node.lineStart,
    lineEnd: node.lineEnd,
    calleeFunction: (m.callee_function as string | null) ?? null,
    sourceNodeIds: (m.source_node_ids as string[]) ?? [],
    confidence: node.confidence,
    metadata: m,
  };
}

export function transformExecutionGraph(
  flowData: FlowGraph,
  selectedNodeId: string | null,
  hasTrace: boolean,
  viewMode: 'all' | 'runtime' | 'static',
  originChain: Array<{ stepId: string; variable: string; label: string; operation: string }> | undefined,
  detailMode: 'summary' | 'detail',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Project canonical graph: exec_l3 OR exec_l4 nodes + data_flow edges.
  // Kind alone is sufficient — no prefix-string filtering needed.
  const wantKind = detailMode === 'summary' ? L3_KIND : L4_KIND;
  const wantIdPrefix = detailMode === 'summary' ? L3_ID_PREFIX : L4_ID_PREFIX;

  const projection = projectByKind(
    flowData,
    new Set([wantKind]),
    new Set(['data_flow']),
  );

  if (projection.nodes.length === 0) return { nodes, edges };

  // Build runtime-hit lookup from FlowGraph nodes (function-kind nodes with trace hits).
  const runtimeHitNodes = new Set<string>();
  if (hasTrace) {
    for (const n of Object.values(flowData.nodes)) {
      if (n.metadata?.runtime_hit) runtimeHitNodes.add(n.id);
    }
  }

  // Cache extracted step data + computed pathState for edge resolution.
  const stepDataById: Record<string, ReturnType<typeof extractStepData>> = {};
  const pathStateById: Record<string, PathState> = {};

  for (const node of projection.nodes) {
    const step = extractStepData(node);
    stepDataById[node.id] = step;

    const hasSourceIds = step.sourceNodeIds.length > 0;
    const hitKnown = hasTrace && hasSourceIds;
    const isHit = hitKnown
      ? step.sourceNodeIds.some((id) => runtimeHitNodes.has(id))
      : false;

    // 'static' = unverified only: hide hit steps AND runtime-only steps
    if (hasTrace && viewMode === 'static') {
      if (hitKnown && isHit) continue;
      if (step.confidence === 'runtime') continue;
    }
    if (hasTrace && viewMode === 'runtime' && hitKnown && !isHit) continue;

    let pathState: PathState = 'possible';
    if (hasTrace) {
      if (step.confidence === 'runtime') pathState = 'runtime-only';
      else if (hitKnown) pathState = isHit ? 'verified' : 'unverified';
      // hitUnknown steps (branch, respond) stay 'possible' — no definitive state
    }
    pathStateById[node.id] = pathState;

    nodes.push({
      id: node.id,
      type: 'dataFlow',
      position: { x: 0, y: 0 },
      data: {
        ...step,
        isSelected: node.id === selectedNodeId,
        isHit: hitKnown ? isHit : false,
        hitUnknown: hasTrace && !hasSourceIds,
        pathState,
        hasTrace,
      },
    });
  }

  const visibleIds = new Set(nodes.map((n) => n.id));

  // === Build data-flow edges ===
  for (const edge of projection.edges) {
    if (!visibleIds.has(edge.sourceId) || !visibleIds.has(edge.targetId)) continue;

    const srcStep = stepDataById[edge.sourceId];
    const tgtStep = stepDataById[edge.targetId];

    // Branch label resolution: branch source + target with branchId → if→yes, else→no
    const variable = (edge.metadata?.variable as string) ?? '';
    let edgeLabel = edge.label || variable || '';
    if (srcStep?.operation === 'branch' && tgtStep?.branchId) {
      const path = tgtStep.branchId.split(':').pop() || '';
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
        confidence: edge.confidence || 'definite',
      },
    });
  }

  // === Inject origin-trace edges when a respond step is selected ===
  if (selectedNodeId && originChain && originChain.length > 0) {
    // originChain stepIds are raw (unprefixed) — map them to canonical IDs.
    const chainIds = originChain.map((o) => `${wantIdPrefix}${o.stepId}`);
    const fullChain = [selectedNodeId, ...chainIds];
    for (let i = 0; i < fullChain.length - 1; i++) {
      const tgt = fullChain[i];
      const src = fullChain[i + 1];
      if (!visibleIds.has(src) || !visibleIds.has(tgt)) continue;
      const originEntry = originChain[i];
      edges.push({
        id: `origin-trace-${i}`,
        source: src,
        target: tgt,
        type: 'flowEdge',
        data: {
          color: '#3498db',
          dashed: true,
          label: originEntry?.variable || '',
          kind: 'origin',
          isOriginTrace: true,
        },
        zIndex: 10,
      });
    }
  }

  return { nodes, edges };
}
