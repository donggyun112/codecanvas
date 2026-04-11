import type { FlowGraph, FlowNodeData, FlowEdgeData } from '../types/flow';
import { resolveKind } from '../types/flow';
export { resolveKind };

// ---------------------------------------------------------------------------
// Projection rules per slider level
// ---------------------------------------------------------------------------

// Non-function-context (API flows): level → which kinds to show
// L1 includes 'function' so that handler (pipeline_phase=handler) is visible;
// the pipeline-phase filter below further narrows which functions appear.
const CALLSTACK_PROJECTION: Record<number, Set<string>> = {
  0: new Set(['trigger']),
  1: new Set(['trigger', 'pipeline', 'function']),
  2: new Set(['trigger', 'pipeline', 'function']),
  3: new Set(['trigger', 'pipeline', 'function', 'statement']),
};

// Function-context: different semantics per level
// L0: only context_root, L1: function+context, L2: all functions, L3: functions+statements
const FUNC_CTX_PROJECTION: Record<number, Set<string>> = {
  0: new Set(['function']),
  1: new Set(['function']),
  2: new Set(['function']),
  3: new Set(['function', 'statement']),
};

// L0/L1 non-function-context use pipeline_phase filter (unchanged)
const PIPELINE_PHASES: Record<number, Record<string, boolean>> = {
  0: { trigger: true, api: true, entrypoint: true },
  1: { trigger: true, api: true, entrypoint: true, middleware: true, dependency: true, handler: true, validation: true, serialization: true },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export interface VisibleResult {
  nodes: FlowNodeData[];
  edges: FlowEdgeData[];
  nodeMap: Record<string, FlowNodeData>;
}

/** True for class definitions that have no L4 logic children and are not
 *  protocol/abstract (DIP binding targets). These add no flow information. */
function isDefinitionOnlyClass(n: FlowNodeData, flowData: FlowGraph): boolean {
  if (resolveKind(n) !== 'function' || n.type !== 'class') return false;
  if (n.metadata?.is_protocol || n.metadata?.is_abstract) return false;
  for (const other of Object.values(flowData.nodes)) {
    if (resolveKind(other) === 'statement' && other.metadata?.function_id === n.id) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Main visibility function
// ---------------------------------------------------------------------------

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
    if (!handlerNodeId && n.metadata?.pipeline_phase === 'handler' && resolveKind(n) === 'function') {
      handlerNodeId = n.id;
    }
  });

  // Depth threshold per level for non-function-context API flows:
  //   L2 (Functions): handler (depth 0) + depth 1 callees
  //   L3 (Logic): no depth limit
  const depthThreshold = level === 2 ? 1 : Infinity;

  function shouldShowLocalLogic(nodeId: string): boolean {
    if (level >= 2 && nodeId === handlerNodeId) return true;
    return (nodeDrillState[nodeId] ?? 0) >= 1;
  }

  // Choose projection set
  const projectionKinds = isFunctionContext
    ? FUNC_CTX_PROJECTION[level] ?? FUNC_CTX_PROJECTION[3]
    : CALLSTACK_PROJECTION[level] ?? CALLSTACK_PROJECTION[3];
  const phases = isFunctionContext ? null : PIPELINE_PHASES[level];

  Object.values(flowData.nodes).forEach((n) => {
    const kind = resolveKind(n);

    // Merged IR nodes (cfg_block, exec_step) never shown in callstack view
    if (!projectionKinds.has(kind) && kind !== 'statement') return;
    // Statements handled separately via drill logic below
    if (kind === 'statement' && !projectionKinds.has('statement')) return;

    // File nodes always filtered (shown as grouping, not standalone)
    if (kind === 'file') return;

    if (isFunctionContext && (n.type === 'trigger' || n.type === 'entrypoint')) return;

    // Hide definition-only classes at L2+
    if (level >= 2 && isDefinitionOnlyClass(n, flowData)) return;

    if (isFunctionContext) {
      // Function-context specific rules
      if (level === 0) {
        if (!n.metadata?.context_root) return;
      } else if (level === 1) {
        if (kind === 'statement') return;
        if (kind === 'function') {
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
        if (kind === 'statement') return;
      }
      // level >= 3: function + statement both allowed by projection
    } else if (phases) {
      // L0/L1: pipeline phase filter
      const phase = n.metadata?.pipeline_phase;
      if (!phase || !phases[phase]) return;
    } else if (level === 2) {
      // L2: pipeline nodes + functions filtered by depth
      if (kind === 'statement') return;
      if (kind === 'function') {
        if (isUtilityNoise(n)) return;
        const depth = n.metadata?.downstream_distance;
        if (depth != null && depth > depthThreshold) return;
      }
    } else {
      // L3 (Logic): functions + all statements pass through
      if (isDefinitionOnlyClass(n, flowData)) return;
    }

    // Runtime filter (3-way path classification)
    if (hasTrace && viewMode === 'runtime' && !n.metadata?.runtime_hit) return;
    if (hasTrace && viewMode === 'static') {
      if (n.metadata?.runtime_hit) return;
      if (n.confidence === 'runtime') return;
    }

    nodes.push(n);
    nodeMap[n.id] = n;
  });

  // Add statement children of drilled/auto-drilled functions
  Object.values(flowData.nodes).forEach((n) => {
    if (resolveKind(n) !== 'statement') return;
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
