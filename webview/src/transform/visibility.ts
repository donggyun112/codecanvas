import type { FlowGraph, FlowNodeData, FlowEdgeData } from '../types/flow';

const STRUCTURAL_TYPES: Record<string, boolean> = { file: true, module: true };
const PIPELINE_PHASES: Record<number, Record<string, boolean>> = {
  0: { trigger: true, api: true, entrypoint: true },
  1: { trigger: true, api: true, entrypoint: true, middleware: true, dependency: true, handler: true, validation: true, serialization: true },
};

export interface VisibleResult {
  nodes: FlowNodeData[];
  edges: FlowEdgeData[];
  nodeMap: Record<string, FlowNodeData>;
}

/** True for class definitions that have no L4 logic children and are not
 *  protocol/abstract (DIP binding targets). These add no flow information. */
function isDefinitionOnlyClass(n: FlowNodeData, flowData: FlowGraph): boolean {
  if (n.level !== 3 || n.type !== 'class') return false;
  if (n.metadata?.is_protocol || n.metadata?.is_abstract) return false;
  // Has L4 children → not definition-only
  for (const other of Object.values(flowData.nodes)) {
    if (other.level === 4 && other.metadata?.function_id === n.id) return false;
  }
  return true;
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

  // Find handler node ID
  let handlerNodeId: string | null = null;
  Object.values(flowData.nodes).forEach((n) => {
    if (!handlerNodeId && n.metadata?.pipeline_phase === 'handler' && n.level === 3) {
      handlerNodeId = n.id;
    }
  });

  // Depth threshold per level for non-function-context API flows:
  //   L2 (Functions): handler (depth 0) + depth 1 callees
  //   L3 (Logic): no depth limit
  const depthThreshold = level === 2 ? 1 : Infinity;

  function shouldShowLocalLogic(nodeId: string): boolean {
    // At L2+, always auto-drill the handler
    if (level >= 2 && nodeId === handlerNodeId) return true;
    return (nodeDrillState[nodeId] ?? 0) >= 1;
  }

  const phases = isFunctionContext ? null : PIPELINE_PHASES[level];

  Object.values(flowData.nodes).forEach((n) => {
    if (STRUCTURAL_TYPES[n.type]) return;
    if (isFunctionContext && (n.type === 'trigger' || n.type === 'entrypoint')) return;

    // Hide definition-only classes at L2+
    if (level >= 2 && isDefinitionOnlyClass(n, flowData)) return;

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
      // L0/L1: pipeline phase filter
      const phase = n.metadata?.pipeline_phase;
      if (!phase || !phases[phase]) return;
    } else if (level === 2) {
      // L2: pipeline nodes + L3 functions filtered by depth
      if (n.level > 3) return;
      if (n.level === 3) {
        if (isUtilityNoise(n)) return;
        // Depth filter: only show callees within threshold
        const depth = n.metadata?.downstream_distance;
        if (depth != null && depth > depthThreshold) return;
      }
    } else {
      // L3 (Logic): all L3 functions + L4 via drill
      if (n.level > 4) return;
      if (n.level === 3 && isDefinitionOnlyClass(n, flowData)) return;
    }

    // Runtime filter (3-way path classification)
    if (hasTrace && viewMode === 'runtime' && !n.metadata?.runtime_hit) return;
    if (hasTrace && viewMode === 'static') {
      // 'static' = unverified paths: hide verified nodes and runtime-only nodes
      if (n.metadata?.runtime_hit) return;
      if (n.confidence === 'runtime') return;
    }

    nodes.push(n);
    nodeMap[n.id] = n;
  });

  // Add L4 children of drilled/auto-drilled functions
  Object.values(flowData.nodes).forEach((n) => {
    if (n.level !== 4) return;
    if (!n.metadata?.function_id) return;
    if (!nodeMap[n.metadata.function_id]) return;
    if (!shouldShowLocalLogic(n.metadata.function_id)) return;
    if (hasTrace && viewMode === 'runtime' && !n.metadata?.runtime_hit) return;
    if (hasTrace && viewMode === 'static') {
      if (n.metadata?.runtime_hit) return;
      if (n.confidence === 'runtime') return;
    }
    if (!nodeMap[n.id]) {
      nodes.push(n);
      nodeMap[n.id] = n;
    }
  });

  const ids = new Set(nodes.map((n) => n.id));
  const edges = flowData.edges.filter((e) => ids.has(e.sourceId) && ids.has(e.targetId));
  return { nodes, edges, nodeMap };
}

