/**
 * Generic projection helper for canonical FlowGraph.
 *
 * Filters the unified node/edge list down to a subset matching given
 * `kind` and edge `type` filters. Used by CFG / data-flow / callstack
 * transforms to read from the same canonical graph.
 */
import type { FlowGraph, FlowNodeData, FlowEdgeData } from '../types/flow';
import { resolveKind } from '../types/flow';

export interface ProjectionResult {
  nodes: FlowNodeData[];
  edges: FlowEdgeData[];
  nodeMap: Record<string, FlowNodeData>;
}

/**
 * Project a canonical FlowGraph by node kind and edge type.
 *
 * @param flowData - canonical graph
 * @param nodeKinds - allowed `kind` values for nodes
 * @param edgeTypes - allowed edge `type` values
 * @param nodeFilter - optional extra predicate for fine-grained selection
 */
export function projectByKind(
  flowData: FlowGraph,
  nodeKinds: Set<string>,
  edgeTypes: Set<string>,
  nodeFilter?: (n: FlowNodeData) => boolean,
): ProjectionResult {
  const nodes: FlowNodeData[] = [];
  const nodeMap: Record<string, FlowNodeData> = {};

  for (const n of Object.values(flowData.nodes)) {
    if (!nodeKinds.has(resolveKind(n))) continue;
    if (nodeFilter && !nodeFilter(n)) continue;
    nodes.push(n);
    nodeMap[n.id] = n;
  }

  const edges = flowData.edges.filter(
    (e) =>
      edgeTypes.has(e.type) &&
      nodeMap[e.sourceId] !== undefined &&
      nodeMap[e.targetId] !== undefined,
  );

  return { nodes, edges, nodeMap };
}
