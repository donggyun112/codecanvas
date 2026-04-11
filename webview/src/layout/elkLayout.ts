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

function makeCFGBlockSize(data: any): { w: number; h: number } {
  const stmts: any[] = data.statements || [];
  if (stmts.length === 0) {
    // Empty terminal block (exit/error_exit)
    return { w: 100, h: 36 };
  }
  const maxText = stmts.reduce((mx: number, s: any) => Math.max(mx, (s.text || '').length), 0);
  const w = Math.max(200, Math.min(350, maxText * 7 + 60));
  const h = 30 + stmts.length * 18; // kind label + statements
  return { w, h };
}

function makeDataFlowNodeSize(data: any): { w: number; h: number } {
  const label = data.label || '';
  const output = data.output || '';
  const outputType = data.outputType || '';
  const errorLabel = data.errorLabel || '';

  // Width: max of label and subtitle
  const subtitle = output && outputType ? `${output}: ${outputType}` : output || outputType || '';
  const maxText = label.length > subtitle.length ? label : subtitle;
  const w = Math.max(180, Math.min(300, maxText.length * 8 + 48));

  // Height: base (badge + label) + optional rows
  let h = 48; // op badge (16) + label (18) + padding (14)
  if (subtitle) h += 16;
  if (errorLabel) h += 16;
  return { w, h };
}

export async function applyElkLayout(
  nodes: Node[],
  edges: Edge[],
  direction: 'DOWN' | 'RIGHT' = 'DOWN',
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
    const label = data.displayName || data.name || data.label || '';
    const size = n.type === 'cfgBlock'
      ? makeCFGBlockSize(data)
      : n.type === 'dataFlow'
        ? makeDataFlowNodeSize(data)
        : makeNodeSize(label, !!data.filePath);
    const kids = childrenByParent[n.id];

    if (kids && kids.length > 0) {
      const kidIds = new Set(kids.map((k) => k.id));
      const internalEdges = edges.filter(
        (e) => kidIds.has(e.source) && kidIds.has(e.target),
      );

      // Collect step_call edges: child → external top-level node.
      // These go into the compound's edges so ELK positions callees near
      // the calling step.
      const stepCallEdges = edges.filter((e) => {
        const ed = (e.data as any) || {};
        return ed.step_call && kidIds.has(e.source) && !kidIds.has(e.target);
      });

      return {
        id: n.id,
        width: 0,
        height: 0,
        layoutOptions: {
          'elk.algorithm': 'layered',
          'elk.direction': direction,
          'elk.padding': '[top=40,left=12,bottom=12,right=12]',
          'elk.spacing.nodeNode': '16',
          'elk.spacing.edgeEdge': '12',
          'elk.spacing.edgeNode': '16',
          'elk.layered.spacing.nodeNodeBetweenLayers': '24',
          'elk.layered.spacing.edgeEdgeBetweenLayers': '12',
          'elk.layered.spacing.edgeNodeBetweenLayers': '16',
          'elk.edgeRouting': 'SPLINES',
          'elk.layered.edgeRouting.splines.mode': 'CONSERVATIVE',
          'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
          'elk.layered.crossingMinimization.greedySwitch.type': 'TWO_SIDED',
          'elk.layered.thoroughness': '15',
        },
        children: kids.map((child) => {
          const cd = child.data as any;
          const cs = child.type === 'dataFlow'
            ? makeDataFlowNodeSize(cd)
            : makeNodeSize(cd.displayName || cd.name || cd.label || '', !!cd.filePath);
          const childOpts: Record<string, string> = {};
          if (cd.type === 'exception') {
            childOpts['elk.layered.crossingMinimization.semiInteractive'] = 'true';
            childOpts['elk.position'] = '(1000, 0)';
          }
          return { id: child.id, width: cs.w, height: cs.h, layoutOptions: childOpts };
        }),
        edges: [
          ...internalEdges.map((e) => {
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
          // step_call edges go from child to external — ELK uses these for
          // compound port placement but needs the targets to exist at root level.
          // We add them as compound-level edges for layout influence.
        ],
      };
    }

    return { id: n.id, width: size.w, height: size.h };
  });

  // Collect internal edge IDs
  const internalEdgeIds = new Set<string>();
  elkChildren.forEach((c) => {
    c.edges?.forEach((e: any) => internalEdgeIds.add(e.id));
  });

  // Top-level edges (between top-level nodes only)
  const elkEdges: any[] = [];
  edges.forEach((e) => {
    if (internalEdgeIds.has(e.id)) return;

    const ed = (e.data as any) || {};
    let src = e.source;
    let tgt = e.target;
    const srcNode = nodes.find((n) => n.id === src);
    const tgtNode = nodes.find((n) => n.id === tgt);

    // step_call: child → top-level. Remap source to parent compound
    // so ELK places the callee near the compound.
    if (ed.step_call && srcNode?.parentId) {
      src = srcNode.parentId;
    } else if (srcNode?.parentId) {
      src = srcNode.parentId;
    }
    if (tgtNode?.parentId) {
      tgt = tgtNode.parentId;
    }

    if (src === tgt) return;
    if (elkEdges.some((ee) => ee.id === e.id)) return;
    if (elkEdges.some((ee) => ee.sources[0] === src && ee.targets[0] === tgt)) return;

    const edgeOpts: Record<string, string> = {};
    if (ed.isErrorPath || ed.type === 'raises') {
      edgeOpts['elk.layered.priority.direction'] = '0';
    } else if (ed.type === 'middleware_chain' || ed.metadata?.pipeline_edge) {
      edgeOpts['elk.layered.priority.direction'] = '15';
    } else {
      edgeOpts['elk.layered.priority.direction'] = '5';
    }

    elkEdges.push({
      id: e.id,
      sources: [src],
      targets: [tgt],
      layoutOptions: edgeOpts,
    });
  });

  // Validate: only include edges whose endpoints exist
  const allElkNodeIds = new Set<string>();
  elkChildren.forEach((c) => {
    allElkNodeIds.add(c.id);
    c.children?.forEach((child) => allElkNodeIds.add(child.id));
  });
  const validElkEdges = elkEdges.filter(
    (e) => allElkNodeIds.has(e.sources[0]) && allElkNodeIds.has(e.targets[0]),
  );

  const elkGraph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': direction,
      'elk.spacing.nodeNode': '25',
      'elk.spacing.edgeEdge': '15',
      'elk.spacing.edgeNode': '20',
      'elk.layered.spacing.nodeNodeBetweenLayers': '40',
      'elk.layered.spacing.edgeEdgeBetweenLayers': '15',
      'elk.layered.spacing.edgeNodeBetweenLayers': '20',
      'elk.padding': '[top=20,left=20,bottom=20,right=20]',
      'elk.edgeRouting': 'SPLINES',
      'elk.layered.edgeRouting.splines.mode': 'CONSERVATIVE',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
      'elk.layered.crossingMinimization.greedySwitch.type': 'TWO_SIDED',
      'elk.layered.thoroughness': '20',
      'elk.layered.nodePlacement.favorStraightEdges': 'true',
    },
    children: elkChildren,
    edges: validElkEdges,
  };

  try {
    const laid = await elk.layout(elkGraph as any);
    return applyPositions(nodes, laid as any);
  } catch (err) {
    console.error('ELK layout failed, using fallback', err);
    return fallbackLayout(nodes, direction);
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

    if (n.type === 'compound' && pos.width && pos.height) {
      update.style = { ...n.style, width: pos.width, height: pos.height };
    }

    return update;
  });

  return { nodes: updatedNodes, edges: [] };
}

function fallbackLayout(nodes: Node[], direction: 'DOWN' | 'RIGHT' = 'DOWN'): { nodes: Node[]; edges: Edge[] } {
  const topLevel = nodes.filter((n) => !n.parentId);
  let x = 20;
  let y = 20;

  const posMap: Record<string, { x: number; y: number }> = {};
  topLevel.forEach((n) => {
    const data = n.data as any;
    const label = data.displayName || data.name || data.label || '';
    const size = makeNodeSize(label, !!data.filePath);
    posMap[n.id] = { x, y };
    if (direction === 'RIGHT') {
      x += size.w + 40;
    } else {
      y += size.h + 44;
    }
  });

  nodes
    .filter((n) => n.parentId)
    .forEach((n, i) => {
      if (direction === 'RIGHT') {
        posMap[n.id] = { x: 12 + i * 180, y: 36 };
      } else {
        posMap[n.id] = { x: 12, y: 40 + i * 60 };
      }
    });

  const updatedNodes = nodes.map((n) => ({
    ...n,
    position: posMap[n.id] ?? { x: 0, y: 0 },
  }));

  return { nodes: updatedNodes, edges: [] };
}
