import ELK from 'elkjs/lib/elk.bundled.js';
import type { Node, Edge } from '@xyflow/react';

const elk = new ELK();

interface ElkNode {
  id: string;
  width: number;
  height: number;
  x?: number;
  y?: number;
  children?: ElkNode[];
  edges?: any[];
  layoutOptions?: Record<string, string>;
}

function makeNodeSize(label: string, hasFile: boolean): { w: number; h: number } {
  const w = Math.max(160, Math.min(280, label.length * 8 + 32));
  const h = hasFile ? 46 : 36;
  return { w, h };
}

export async function applyElkLayout(
  nodes: Node[],
  edges: Edge[],
): Promise<{ nodes: Node[]; edges: Edge[] }> {
  if (nodes.length === 0) return { nodes, edges };

  const parentNodes = new Set<string>();
  const childrenByParent: Record<string, Node[]> = {};

  nodes.forEach((n) => {
    if (n.parentId) {
      parentNodes.add(n.parentId);
      if (!childrenByParent[n.parentId]) childrenByParent[n.parentId] = [];
      childrenByParent[n.parentId].push(n);
    }
  });

  const topLevelNodes = nodes.filter((n) => !n.parentId);
  const childIdSet = new Set(nodes.filter((n) => n.parentId).map((n) => n.id));

  // Build ELK children
  const elkChildren: ElkNode[] = topLevelNodes.map((n) => {
    const data = n.data as any;
    const label = data.displayName || data.name || '';
    const size = makeNodeSize(label, !!data.filePath);
    const kids = childrenByParent[n.id];

    if (kids && kids.length > 0) {
      const kidIds = new Set(kids.map((k) => k.id));
      const internalEdges = edges.filter(
        (e) => kidIds.has(e.source) && kidIds.has(e.target),
      );

      return {
        id: n.id,
        width: 0,
        height: 0,
        layoutOptions: {
          'elk.algorithm': 'layered',
          'elk.direction': 'DOWN',
          'elk.padding': '[top=40,left=12,bottom=12,right=12]',
          'elk.spacing.nodeNode': '16',
          'elk.layered.spacing.nodeNodeBetweenLayers': '24',
          'elk.edgeRouting': 'ORTHOGONAL',
          'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
        },
        children: kids.map((child) => {
          const cd = child.data as any;
          const cs = makeNodeSize(cd.displayName || cd.name || '', !!cd.filePath);
          const childOpts: Record<string, string> = {};
          if (cd.type === 'exception') {
            childOpts['elk.layered.crossingMinimization.semiInteractive'] = 'true';
            childOpts['elk.position'] = '(1000, 0)';
          }
          return { id: child.id, width: cs.w, height: cs.h, layoutOptions: childOpts };
        }),
        edges: internalEdges.map((e) => {
          const ed = (e.data as any) || {};
          const edgeOpts: Record<string, string> = {};
          if (ed.isErrorPath || ed.type === 'raises') {
            edgeOpts['elk.layered.priority.direction'] = '0';
            edgeOpts['elk.layered.priority.shortness'] = '0';
          } else {
            edgeOpts['elk.layered.priority.direction'] = '10';
            edgeOpts['elk.layered.priority.shortness'] = '10';
          }
          return { id: e.id, sources: [e.source], targets: [e.target], layoutOptions: edgeOpts };
        }),
      };
    }

    return { id: n.id, width: size.w, height: size.h };
  });

  // Collect internal edge IDs
  const internalEdgeIds = new Set<string>();
  elkChildren.forEach((c) => {
    c.edges?.forEach((e: any) => internalEdgeIds.add(e.id));
  });

  // Top-level edges
  const elkEdges = edges
    .filter((e) => !internalEdgeIds.has(e.id) && !childIdSet.has(e.source) && !childIdSet.has(e.target))
    .map((e) => {
      const ed = (e.data as any) || {};
      const edgeOpts: Record<string, string> = {};
      if (ed.isErrorPath || ed.type === 'raises') {
        edgeOpts['elk.layered.priority.direction'] = '0';
      } else if (ed.type === 'middleware_chain' || ed.metadata?.pipeline_edge) {
        edgeOpts['elk.layered.priority.direction'] = '15';
      } else {
        edgeOpts['elk.layered.priority.direction'] = '5';
      }
      return { id: e.id, sources: [e.source], targets: [e.target], layoutOptions: edgeOpts };
    });

  // Also include cross-compound edges (source or target in compound)
  edges.forEach((e) => {
    if (internalEdgeIds.has(e.id)) return;
    if (elkEdges.some((ee) => ee.id === e.id)) return;

    // Remap source/target to parent if they're compound children
    let src = e.source;
    let tgt = e.target;
    const srcNode = nodes.find((n) => n.id === src);
    const tgtNode = nodes.find((n) => n.id === tgt);
    if (srcNode?.parentId) src = srcNode.parentId;
    if (tgtNode?.parentId) tgt = tgtNode.parentId;
    if (src === tgt) return; // internal

    // Don't add duplicate
    if (elkEdges.some((ee) => ee.sources[0] === src && ee.targets[0] === tgt)) return;

    elkEdges.push({
      id: e.id,
      sources: [src],
      targets: [tgt],
      layoutOptions: { 'elk.layered.priority.direction': '5' },
    });
  });

  const elkGraph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '25',
      'elk.layered.spacing.nodeNodeBetweenLayers': '40',
      'elk.layered.spacing.edgeNodeBetweenLayers': '15',
      'elk.padding': '[top=20,left=20,bottom=20,right=20]',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
    },
    children: elkChildren,
    edges: elkEdges,
  };

  try {
    const laid = await elk.layout(elkGraph as any);
    return applyPositions(nodes, laid as any);
  } catch (err) {
    console.error('ELK layout failed, using fallback', err);
    return fallbackLayout(nodes);
  }
}

function applyPositions(
  nodes: Node[],
  laid: { children?: ElkNode[] },
): { nodes: Node[]; edges: Edge[] } {
  const posMap: Record<string, { x: number; y: number; width?: number; height?: number }> = {};

  function traverse(elkNode: ElkNode) {
    posMap[elkNode.id] = {
      x: elkNode.x ?? 0,
      y: elkNode.y ?? 0,
      width: elkNode.width,
      height: elkNode.height,
    };
    elkNode.children?.forEach((child) => {
      posMap[child.id] = {
        x: child.x ?? 0,
        y: child.y ?? 0,
        width: child.width,
        height: child.height,
      };
    });
  }

  laid.children?.forEach(traverse);

  const updatedNodes = nodes.map((n) => {
    const pos = posMap[n.id];
    if (!pos) return n;

    const update: any = {
      ...n,
      position: { x: pos.x, y: pos.y },
    };

    // Compound nodes get their computed size
    if (n.type === 'compound' && pos.width && pos.height) {
      update.style = { ...n.style, width: pos.width, height: pos.height };
    }

    return update;
  });

  return { nodes: updatedNodes, edges: [] };
}

function fallbackLayout(nodes: Node[]): { nodes: Node[]; edges: Edge[] } {
  const topLevel = nodes.filter((n) => !n.parentId);
  let y = 20;

  const posMap: Record<string, { x: number; y: number }> = {};
  topLevel.forEach((n) => {
    const data = n.data as any;
    const label = data.displayName || data.name || '';
    const size = makeNodeSize(label, !!data.filePath);
    posMap[n.id] = { x: 40, y };
    y += size.h + 44;
  });

  // Position children relative to parent
  nodes
    .filter((n) => n.parentId)
    .forEach((n, i) => {
      posMap[n.id] = { x: 12, y: 40 + i * 60 };
    });

  const updatedNodes = nodes.map((n) => ({
    ...n,
    position: posMap[n.id] ?? { x: 0, y: 0 },
  }));

  return { nodes: updatedNodes, edges: [] };
}
