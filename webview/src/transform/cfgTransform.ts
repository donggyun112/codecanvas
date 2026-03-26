/**
 * Transform CFG (Control Flow Graph) into React Flow nodes + edges.
 * Overlays runtime trace data when available.
 */
import type { Node, Edge } from '@xyflow/react';
import type { CFGData, FlowGraph } from '../types/flow';
import type { PathState } from './pathState';

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
 * A line is hit if any L3 function node with runtime_hit contains that line.
 */
function buildHitLines(flowData: FlowGraph | null, cfgFilePath: string | null): Set<number> {
  const hitLines = new Set<number>();
  if (!flowData) return hitLines;

  for (const node of Object.values(flowData.nodes)) {
    if (!node.metadata?.runtime_hit) continue;
    // Match by file path
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

function isBlockHit(block: any, hitLines: Set<number>): boolean {
  if (!block.statements || block.statements.length === 0) return false;
  return block.statements.some((s: any) => hitLines.has(s.line));
}

export function transformCFG(
  cfg: CFGData,
  selectedNodeId: string | null,
  flowData: FlowGraph | null = null,
  hasTrace: boolean = false,
  viewMode: 'all' | 'runtime' | 'static' = 'all',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  if (!cfg.blocks.length) return { nodes, edges };

  // === Pass 1: Compute pathState for ALL blocks before filtering ===
  const hitLines = hasTrace ? buildHitLines(flowData, cfg.filePath) : new Set<number>();
  const blockHitMap = new Map<string, boolean>();
  const blockPathState = new Map<string, PathState>();

  // Initial hit computation
  for (const block of cfg.blocks) {
    if (hasTrace && block.kind === 'entry') {
      blockHitMap.set(block.id, true);
    } else {
      blockHitMap.set(block.id, hasTrace && isBlockHit(block, hitLines));
    }
  }

  // Infer terminal block hit from incoming exit edges
  if (hasTrace) {
    for (const edge of cfg.edges) {
      if (edge.kind === 'exit' && blockHitMap.get(edge.sourceBlockId)) {
        blockHitMap.set(edge.targetBlockId, true);
      }
    }
  }

  // Assign final pathState
  for (const block of cfg.blocks) {
    if (!hasTrace) {
      blockPathState.set(block.id, 'possible');
    } else {
      blockPathState.set(block.id, blockHitMap.get(block.id) ? 'verified' : 'unverified');
    }
  }

  // === Pass 2: Filter and create nodes ===
  for (const block of cfg.blocks) {
    const pathState = blockPathState.get(block.id)!;

    if (hasTrace && viewMode === 'runtime' && pathState === 'unverified') continue;
    if (hasTrace && viewMode === 'static' && pathState === 'verified') continue;

    nodes.push({
      id: block.id,
      type: 'cfgBlock',
      position: { x: 0, y: 0 },
      data: {
        ...block,
        isSelected: block.id === selectedNodeId,
        color: KIND_COLORS[block.kind] || '#666',
        pathState,
        hasTrace,
      },
    });
  }

  const visibleIds = new Set(nodes.map((n) => n.id));

  for (const edge of cfg.edges) {
    if (!visibleIds.has(edge.sourceBlockId) || !visibleIds.has(edge.targetBlockId)) continue;
    const srcHit = blockHitMap.get(edge.sourceBlockId) || false;
    const tgtHit = blockHitMap.get(edge.targetBlockId) || false;
    const edgeHit = hasTrace && srcHit && tgtHit;

    const color = EDGE_COLORS[edge.kind] || '#666';
    const dashed = edge.kind === 'back_edge' || edge.kind === 'exception';

    let pathState: PathState = 'possible';
    if (hasTrace) {
      pathState = edgeHit ? 'verified' : 'unverified';
    }

    edges.push({
      id: edge.id,
      source: edge.sourceBlockId,
      target: edge.targetBlockId,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label: edge.label,
        hasTrace,
        pathState,
        isHit: edgeHit,
        kind: edge.kind,
        condition: edge.condition,
      },
    });
  }

  return { nodes, edges };
}
