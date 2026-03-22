import type { FlowGraph, FlowNodeData, FlowEdgeData } from '../types/flow';

const STRUCTURAL_TYPES: Record<string, boolean> = { file: true, module: true };
const PIPELINE_PHASES: Record<number, Record<string, boolean>> = {
  0: { trigger: true, api: true, entrypoint: true },
  1: { trigger: true, api: true, entrypoint: true, middleware: true, dependency: true, handler: true },
};

export interface VisibleResult {
  nodes: FlowNodeData[];
  edges: FlowEdgeData[];
  nodeMap: Record<string, FlowNodeData>;
}

export function getVisible(
  flowData: FlowGraph,
  level: number,
  viewMode: 'all' | 'runtime' | 'static',
  isFunctionContext: boolean,
  hasTrace: boolean,
  nodeDrillState: Record<string, number>,
): VisibleResult {
  const nodes: FlowNodeData[] = [];
  const nodeMap: Record<string, FlowNodeData> = {};

  // Pre-compute utility noise
  const incomingCallCounts: Record<string, number> = {};
  const outgoingCallIds: Record<string, number> = {};
  flowData.edges.forEach((e) => {
    if (e.type === 'calls') {
      incomingCallCounts[e.targetId] = (incomingCallCounts[e.targetId] || 0) + 1;
      outgoingCallIds[e.sourceId] = (outgoingCallIds[e.sourceId] || 0) + 1;
    }
  });

  function isUtilityNoise(n: FlowNodeData): boolean {
    const name = n.name || '';
    if (name.startsWith('_') && name !== '__init__') return true;
    if (!outgoingCallIds[n.id] && (incomingCallCounts[n.id] || 0) >= 3) return true;
    return false;
  }

  function shouldShowLocalLogic(nodeId: string): boolean {
    return (nodeDrillState[nodeId] ?? 0) >= 1;
  }

  const phases = isFunctionContext ? null : PIPELINE_PHASES[level];

  Object.values(flowData.nodes).forEach((n) => {
    if (STRUCTURAL_TYPES[n.type]) return;
    if (isFunctionContext && (n.type === 'trigger' || n.type === 'entrypoint')) return;

    if (isFunctionContext) {
      if (level === 0) {
        if (!n.metadata?.context_root) return;
      } else if (level === 1) {
        if (n.level > 3) return;
        if (n.level === 3) {
          const inContext =
            n.metadata?.context_root ||
            n.metadata?.upstream_distance != null ||
            n.metadata?.downstream_distance === 1;
          if (!inContext) return;
          if (
            isUtilityNoise(n) &&
            !(n.metadata?.context_root || n.metadata?.upstream_distance != null)
          ) {
            return;
          }
        }
      } else if (level === 2) {
        if (n.level > 3) return;
      } else {
        if (n.level > 4) return;
      }
    } else if (phases) {
      const phase = n.metadata?.pipeline_phase;
      if (!phase || !phases[phase]) return;
    } else if (level === 2) {
      if (n.level > 3) return;
      if (n.level === 3 && isUtilityNoise(n)) return;
    } else {
      if (n.level > 4) return;
    }

    // Runtime filter
    if (hasTrace && viewMode === 'runtime' && !n.metadata?.runtime_hit) return;
    if (hasTrace && viewMode === 'static' && n.metadata?.runtime_hit) return;

    nodes.push(n);
    nodeMap[n.id] = n;
  });

  // Add L4 children of drilled functions
  Object.values(flowData.nodes).forEach((n) => {
    if (n.level !== 4) return;
    if (!n.metadata?.function_id) return;
    if (!nodeMap[n.metadata.function_id]) return;
    if (!shouldShowLocalLogic(n.metadata.function_id)) return;
    if (hasTrace && viewMode === 'runtime' && !n.metadata?.runtime_hit) return;
    if (hasTrace && viewMode === 'static' && n.metadata?.runtime_hit) return;
    if (!nodeMap[n.id]) {
      nodes.push(n);
      nodeMap[n.id] = n;
    }
  });

  const ids = new Set(nodes.map((n) => n.id));
  const edges = flowData.edges.filter((e) => ids.has(e.sourceId) && ids.has(e.targetId));
  return { nodes, edges, nodeMap };
}
