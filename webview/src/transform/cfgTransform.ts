/**
 * Transform CFG (Control Flow Graph) view from canonical FlowGraph.
 *
 * Reads `cfg_block` nodes and `cfg_flow` edges from the unified graph
 * via projectByKind, then overlays runtime trace data when available.
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData } from '../types/flow';
import type { PathState } from './pathState';
import { projectByKind } from './projection';

const KIND_COLORS: Record<string, string> = {
  entry: '#1abc9c',
  exit: '#27ae60',
  error_exit: '#e74c3c',
  merge: '#95a5a6',
  block: '#3498db',
};

const EDGE_COLORS: Record<string, string> = {
  true: '#27ae60',
  false: '#e74c3c',
  exception: '#e67e22',
  back_edge: '#9b59b6',
  exit: '#95a5a6',
  fall_through: '#666',
};

/**
 * Build a set of "hit" line numbers from FlowGraph runtime data.
 * A line is hit if any function-kind node with runtime_hit contains it.
 */
function buildHitLines(flowData: FlowGraph, cfgFilePath: string | null): Set<number> {
  const hitLines = new Set<number>();
  for (const node of Object.values(flowData.nodes)) {
    if (!node.metadata?.runtime_hit) continue;
    if (cfgFilePath && node.filePath && !node.filePath.endsWith(cfgFilePath) && cfgFilePath !== node.filePath) {
      // Allow suffix match (relative vs absolute paths)
      if (!cfgFilePath.includes(node.filePath) && !node.filePath.includes(cfgFilePath)) continue;
    }
    if (node.lineStart) {
      const end = node.lineEnd || node.lineStart;
      for (let l = node.lineStart; l <= end; l++) hitLines.add(l);
    }
  }
  return hitLines;
}

function isBlockHit(blockNode: FlowNodeData, hitLines: Set<number>): boolean {
  const stmts = (blockNode.metadata?.statements as Array<{ line: number }> | undefined) ?? [];
  if (stmts.length === 0) return false;
  return stmts.some((s) => hitLines.has(s.line));
}

export function transformCFG(
  flowData: FlowGraph,
  selectedNodeId: string | null,
  hasTrace: boolean = false,
  viewMode: 'all' | 'runtime' | 'static' = 'all',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Project canonical graph: handler's cfg_block nodes only (prefix "cfg:")
  // Callee CFGs (prefix "cfg_<qname>:") are for Code Flow view, not CFG view.
  const projection = projectByKind(
    flowData,
    new Set(['cfg_block']),
    new Set(['cfg_flow']),
    (n) => n.id.startsWith('cfg:'),
  );

  if (projection.nodes.length === 0) return { nodes, edges };

  // Determine the file path of this CFG (all blocks share one function)
  const cfgFilePath = projection.nodes.find((n) => n.filePath)?.filePath ?? null;

  // === Pass 1: Compute pathState for ALL blocks before filtering ===
  const hitLines = hasTrace ? buildHitLines(flowData, cfgFilePath) : new Set<number>();
  const blockHitMap = new Map<string, boolean>();
  const blockPathState = new Map<string, PathState>();

  // Initial hit computation
  for (const block of projection.nodes) {
    const cfgKind = block.metadata?.cfg_kind;
    if (hasTrace && cfgKind === 'entry') {
      blockHitMap.set(block.id, true);
    } else {
      blockHitMap.set(block.id, hasTrace && isBlockHit(block, hitLines));
    }
  }

  // Infer terminal block hit from incoming exit edges
  if (hasTrace) {
    for (const edge of projection.edges) {
      const edgeKind = edge.metadata?.cfg_kind;
      if (edgeKind === 'exit' && blockHitMap.get(edge.sourceId)) {
        blockHitMap.set(edge.targetId, true);
      }
    }
  }

  // Assign final pathState
  for (const block of projection.nodes) {
    if (!hasTrace) {
      blockPathState.set(block.id, 'possible');
    } else {
      blockPathState.set(block.id, blockHitMap.get(block.id) ? 'verified' : 'unverified');
    }
  }

  // === Pass 2: Filter and create RF nodes ===
  for (const block of projection.nodes) {
    const pathState = blockPathState.get(block.id)!;

    if (hasTrace && viewMode === 'runtime' && pathState === 'unverified') continue;
    if (hasTrace && viewMode === 'static' && pathState === 'verified') continue;

    const cfgKind = (block.metadata?.cfg_kind as string) || 'block';
    const statements = block.metadata?.statements ?? [];

    nodes.push({
      id: block.id,
      type: 'cfgBlock',
      position: { x: 0, y: 0 },
      data: {
        id: block.id,
        label: block.name,
        kind: cfgKind,
        scope: block.scope,
        filePath: block.filePath,
        lineStart: block.lineStart,
        lineEnd: block.lineEnd,
        statements,
        metadata: block.metadata,
        isSelected: block.id === selectedNodeId,
        color: KIND_COLORS[cfgKind] || '#666',
        pathState,
        hasTrace,
      },
    });
  }

  const visibleIds = new Set(nodes.map((n) => n.id));

  // === Build RF edges ===
  for (const edge of projection.edges) {
    if (!visibleIds.has(edge.sourceId) || !visibleIds.has(edge.targetId)) continue;
    const srcHit = blockHitMap.get(edge.sourceId) || false;
    const tgtHit = blockHitMap.get(edge.targetId) || false;
    const edgeHit = hasTrace && srcHit && tgtHit;

    const cfgKind = (edge.metadata?.cfg_kind as string) || 'fall_through';
    const color = EDGE_COLORS[cfgKind] || '#666';
    const dashed = cfgKind === 'back_edge' || cfgKind === 'exception';

    let pathState: PathState = 'possible';
    if (hasTrace) {
      pathState = edgeHit ? 'verified' : 'unverified';
    }

    edges.push({
      id: edge.id,
      source: edge.sourceId,
      target: edge.targetId,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label: edge.label,
        hasTrace,
        pathState,
        isHit: edgeHit,
        kind: cfgKind,
        condition: edge.condition,
      },
    });
  }

  return { nodes, edges };
}
