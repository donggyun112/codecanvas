/**
 * Transform ExecutionGraph (1st-class model) into React Flow nodes + edges.
 * No visibility heuristics, no compound node hacks — direct projection.
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, ExecutionGraphData, ExecStep } from '../types/flow';
import type { PathState } from './pathState';

export function transformExecutionGraph(
  eg: ExecutionGraphData,
  selectedNodeId: string | null,
  flowData: FlowGraph | null,
  hasTrace: boolean,
  viewMode: 'all' | 'runtime' | 'static',
  originChain?: Array<{ stepId: string; variable: string; label: string; operation: string }>,
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  if (!eg.steps.length) return { nodes, edges };

  // Build runtime hit lookup from source nodes
  const runtimeHitNodes = new Set<string>();
  if (hasTrace && flowData) {
    for (const n of Object.values(flowData.nodes)) {
      if (n.metadata?.runtime_hit) runtimeHitNodes.add(n.id);
    }
  }

  for (const step of eg.steps) {
    // Runtime hit determination:
    // - Steps with sourceNodeIds: definite hit/miss from trace
    // - Steps without (branch, respond, error): unknown — no glow, no dim
    const hasSourceIds = step.sourceNodeIds.length > 0;
    const hitKnown = hasTrace && hasSourceIds;
    const isHit = hitKnown
      ? step.sourceNodeIds.some((id: string) => runtimeHitNodes.has(id))
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

    nodes.push({
      id: step.id,
      type: 'dataFlow',
      position: { x: 0, y: 0 },
      data: {
        ...step,
        isSelected: step.id === selectedNodeId,
        isHit: hitKnown ? isHit : false,
        hitUnknown: hasTrace && !hasSourceIds,
        pathState,
        hasTrace,
      },
    });
  }

  const visibleIds = new Set(nodes.map((n) => n.id));

  // Build step lookup for branch label resolution
  const stepById: Record<string, ExecStep> = {};
  for (const s of eg.steps) stepById[s.id] = s;

  for (const link of eg.links) {
    if (!visibleIds.has(link.sourceStepId) || !visibleIds.has(link.targetStepId)) continue;

    // Derive branch path label: if source is a branch node and target has branchId
    let edgeLabel = link.label || link.variable || '';
    const srcStep = stepById[link.sourceStepId];
    const tgtStep = stepById[link.targetStepId];
    if (srcStep?.operation === 'branch' && tgtStep?.branchId) {
      const path = tgtStep.branchId.split(':').pop() || '';
      if (path === 'if') edgeLabel = 'yes';
      else if (path === 'else') edgeLabel = 'no';
      else if (path) edgeLabel = path;
    }

    const kind = link.kind || 'sequence';
    let color: string | undefined;
    let dashed = false;
    if (kind === 'error' || link.isErrorPath) { color = '#e74c3c'; dashed = true; }
    else if (kind === 'data') { color = '#3498db'; }
    else if (kind === 'branch') { color = '#f39c12'; }

    // Compute edge hit state from source/target node pathStates
    const srcNode = nodes.find((n) => n.id === link.sourceStepId);
    const tgtNode = nodes.find((n) => n.id === link.targetStepId);
    const srcVerified = (srcNode?.data as any)?.pathState === 'verified';
    const tgtVerified = (tgtNode?.data as any)?.pathState === 'verified';
    const edgeHit = hasTrace && srcVerified && tgtVerified;
    let edgePathState: PathState = 'possible';
    if (hasTrace) {
      edgePathState = edgeHit ? 'verified' : 'unverified';
    }

    edges.push({
      id: link.id,
      source: link.sourceStepId,
      target: link.targetStepId,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label: edgeLabel,
        hasTrace,
        isHit: edgeHit,
        pathState: edgePathState,
        kind,
        confidence: link.confidence || 'definite',
        evidence: link.evidence || '',
      },
    });
  }

  // Inject origin-trace edges when a respond step is selected
  if (selectedNodeId && originChain && originChain.length > 0) {
    const chainIds = originChain.map((o) => o.stepId);
    // Build chain: respond ← origin[0] ← origin[1] ← ...
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
