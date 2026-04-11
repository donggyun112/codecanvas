import { useMemo, useRef } from 'react';
import { useNodes, type Node } from '@xyflow/react';

function resolveAbsPos(
  n: Node,
  byId: Map<string, Node>,
): { x: number; y: number } {
  if (!n.parentId) return n.position;
  const parent = byId.get(n.parentId);
  if (!parent) return n.position;
  const pp = resolveAbsPos(parent, byId);
  return { x: n.position.x + pp.x, y: n.position.y + pp.y };
}

function toAbsoluteNodes(nodes: Node[]): Node[] {
  const byId = new Map<string, Node>();
  for (const n of nodes) byId.set(n.id, n);
  return nodes.map((n) => {
    if (!n.parentId) return n;
    return { ...n, position: resolveAbsPos(n, byId) };
  });
}

function geometryKey(nodes: Node[]): string {
  let key = '';
  for (const n of nodes) {
    const w = n.measured?.width ?? n.width ?? 0;
    const h = n.measured?.height ?? n.height ?? 0;
    key += `${n.id}:${n.position.x},${n.position.y},${w},${h};`;
  }
  return key;
}

// Module-level cache shared across all FlowEdge instances in the same render.
// useNodes() returns the same reference for all hooks in the same ReactFlow,
// so the geometry key will be identical — only the first edge triggers the compute.
let _cachedKey = '';
let _cachedNodes: Node[] = [];

/**
 * Returns absolute-position nodes for smart-edge obstacle calculation.
 * Geometry key + toAbsoluteNodes are computed once per render cycle and
 * shared across all edge components via module-level cache.
 */
export function useAbsoluteNodes(): Node[] {
  const rawNodes = useNodes();
  const key = geometryKey(rawNodes);
  if (key !== _cachedKey) {
    _cachedKey = key;
    _cachedNodes = toAbsoluteNodes(rawNodes);
  }
  return _cachedNodes;
}
