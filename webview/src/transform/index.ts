import type { Node, Edge } from '@xyflow/react';
import type { FlowNodeData, FlowEdgeData } from '../types/flow';
import type { VisibleResult } from './visibility';
import { resolveKind } from './visibility';
import { computeNodePathState, computeEdgePathState } from './pathState';

const EDGE_COLORS: Record<string, string | null> = {
  calls: null,
  returns: '#27ae60',
  raises: '#e74c3c',
  queries: '#9b59b6',
  requests: '#e67e22',
  middleware_chain: '#1abc9c',
  injects: '#3498db',
  depends_on: '#3498db',
  binds: '#8e44ad',
};

function nodeTypeKey(n: FlowNodeData): string {
  const kind = resolveKind(n);
  if (kind === 'statement') return 'logicStep';
  switch (n.type) {
    case 'trigger':
    case 'api':
    case 'entrypoint':
    case 'middleware':
    case 'dependency':
      return 'pipeline';
    case 'database':
    case 'external_api':
      return 'resource';
    case 'function':
    case 'method':
    case 'class':
      return 'function';
    default:
      return 'function';
  }
}

export function transformToRfElements(
  vis: VisibleResult,
  nodeDrillState: Record<string, number>,
  hasTrace: boolean,
  selectedNodeId: string | null,
  isFunctionContext: boolean,
): { nodes: Node[]; edges: Edge[] } {
  const rfNodes: Node[] = [];
  const rfEdges: Edge[] = [];

  // Identify statement nodes extracted from compounds (return_detail_edge)
  const extractedReturnIds = new Set<string>();
  vis.edges.forEach((e) => {
    if (e.metadata?.return_detail_edge) {
      extractedReturnIds.add(e.sourceId);
    }
  });

  // Classify nodes: compound children vs top-level
  const functionChildren: Record<string, FlowNodeData[]> = {};
  const topLevelNodes: FlowNodeData[] = [];
  const stmtInCompound = new Set<string>();

  vis.nodes.forEach((n) => {
    const kind = resolveKind(n);
    if (kind === 'statement' && n.metadata?.function_id) {
      const parentId = n.metadata.function_id;
      if (vis.nodeMap[parentId]) {
        if (extractedReturnIds.has(n.id)) {
          topLevelNodes.push(n);
          return;
        }
        if (!functionChildren[parentId]) functionChildren[parentId] = [];
        functionChildren[parentId].push(n);
        stmtInCompound.add(n.id);
        return;
      }
    }
    topLevelNodes.push(n);
  });

  // Build RF nodes
  topLevelNodes.forEach((n) => {
    const kids = functionChildren[n.id];
    const isCompound = kids && kids.length > 0;
    const drillable =
      Object.values(vis.nodeMap).some(
        (child) => resolveKind(child) === 'statement' && child.metadata?.function_id === n.id,
      ) || (nodeDrillState[n.id] ?? 0) > 0;

    if (isCompound) {
      // Compound container node
      const ps = computeNodePathState(hasTrace, n.metadata?.runtime_hit, n.confidence);
      rfNodes.push({
        id: n.id,
        type: 'compound',
        position: { x: 0, y: 0 },
        data: {
          ...n,
          isHit: hasTrace && !!n.metadata?.runtime_hit,
          pathState: ps,
          hasTrace,
          isSelected: n.id === selectedNodeId,
          drillable,
          childCount: kids.length,
        },
        style: { width: 300, height: 200 },
      });

      // Child nodes inside compound
      kids.forEach((child) => {
        const cps = computeNodePathState(hasTrace, child.metadata?.runtime_hit, child.confidence);
        rfNodes.push({
          id: child.id,
          type: 'logicStep',
          position: { x: 0, y: 0 },
          parentId: n.id,
          extent: 'parent' as const,
          data: {
            ...child,
            isHit: hasTrace && !!child.metadata?.runtime_hit,
            pathState: cps,
            hasTrace,
            isSelected: child.id === selectedNodeId,
            drillable: false,
          },
        });
      });
    } else {
      const ps = computeNodePathState(hasTrace, n.metadata?.runtime_hit, n.confidence);
      rfNodes.push({
        id: n.id,
        type: nodeTypeKey(n),
        position: { x: 0, y: 0 },
        data: {
          ...n,
          isHit: hasTrace && !!n.metadata?.runtime_hit,
          pathState: ps,
          hasTrace,
          isSelected: n.id === selectedNodeId,
          drillable,
        },
      });
    }
  });

  // Track which edges are internal to compounds
  const internalEdgeIds = new Set<string>();
  Object.entries(functionChildren).forEach(([parentId, kids]) => {
    const kidIds = new Set(kids.map((k) => k.id));
    vis.edges.forEach((e) => {
      if (kidIds.has(e.sourceId) && kidIds.has(e.targetId)) {
        internalEdgeIds.add(e.id);
      }
    });
  });

  // Pre-index step_call relationships to suppress duplicate function→function edges
  const stepCallCovered = new Set<string>();
  vis.edges.forEach((e) => {
    if (!e.metadata?.step_call) return;
    const src = vis.nodeMap[e.sourceId];
    const parentId = src?.metadata?.function_id;
    if (parentId) stepCallCovered.add(`${parentId}:${e.targetId}`);
  });

  // Build edges
  vis.edges.forEach((e) => {
    // In callstack mode: skip function→function if step_call covers it
    if (!e.metadata?.step_call && !e.metadata?.display_only) {
      const src = vis.nodeMap[e.sourceId];
      const tgt = vis.nodeMap[e.targetId];
      if (
        src && tgt &&
        resolveKind(src) === 'function' && resolveKind(tgt) === 'function' &&
        stepCallCovered.has(`${e.sourceId}:${e.targetId}`)
      ) {
        return;
      }
    }

    // Skip cross-compound edges that don't make sense at top level
    if (!internalEdgeIds.has(e.id)) {
      if (e.metadata?.return_detail_edge && extractedReturnIds.has(e.sourceId)) {
        // keep
      } else if (e.metadata?.in_return && e.metadata?.return_node_id && extractedReturnIds.has(e.metadata.return_node_id)) {
        return; // hide
      } else {
        const srcNode = vis.nodeMap[e.sourceId];
        const tgtNode = vis.nodeMap[e.targetId];
        if (srcNode && tgtNode) {
          const srcKind = resolveKind(srcNode);
          const tgtKind = resolveKind(tgtNode);
          // Allow step_call (statement→function) across compound boundary
          if (stmtInCompound.has(e.sourceId) && tgtKind === 'function') {
            if (!e.metadata?.step_call) return;
          } else {
            if (srcKind === 'function' && stmtInCompound.has(e.targetId)) return;
            if (stmtInCompound.has(e.sourceId) && extractedReturnIds.has(e.targetId)) return;
          }
        }
      }
    }

    const edgeColor = EDGE_COLORS[e.type] ?? undefined;
    const isUpstream = !!e.metadata?.upstream_edge;
    const isDashed =
      e.type === 'binds' ||
      e.type === 'injects' ||
      isUpstream;

    const eps = computeEdgePathState(hasTrace, e.metadata?.runtime_hit, e.confidence);
    rfEdges.push({
      id: e.id,
      source: e.sourceId,
      target: e.targetId,
      type: 'flowEdge',
      data: {
        ...e,
        color: isUpstream ? '#8aa4ff' : edgeColor,
        dashed: isDashed,
        animated: hasTrace && !!e.metadata?.runtime_hit,
        isHit: hasTrace && !!e.metadata?.runtime_hit,
        pathState: eps,
        hasTrace,
        isFunctionContext,
      },
    });
  });

  // Add synthetic edges from compound to extracted return nodes
  extractedReturnIds.forEach((retId) => {
    const retNode = vis.nodeMap[retId];
    if (!retNode?.metadata?.function_id) return;
    const parentId = retNode.metadata.function_id;
    if (!vis.nodeMap[parentId]) return;
    rfEdges.push({
      id: `synth_${parentId}_${retId}`,
      source: parentId,
      target: retId,
      type: 'flowEdge',
      data: { color: undefined, dashed: false, animated: false, isHit: false, hasTrace, type: 'calls' },
    });
  });

  return { nodes: rfNodes, edges: rfEdges };
}
