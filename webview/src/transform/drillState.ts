import type { FlowGraph, FlowNodeData } from '../types/flow';

export function directLogicChildren(flowData: FlowGraph, nodeId: string): FlowNodeData[] {
  return Object.values(flowData.nodes).filter(
    (n) => n.level === 4 && n.metadata?.function_id === nodeId,
  );
}

export function maxDrillDepth(flowData: FlowGraph, node: FlowNodeData): number {
  return directLogicChildren(flowData, node.id).length > 0 ? 1 : 0;
}

export function getFunctionFlowTarget(
  flowData: FlowGraph,
  node: FlowNodeData,
): { filePath: string; line: number } | null {
  if (
    node.filePath &&
    node.lineStart &&
    (node.level >= 3 || node.type === 'middleware' || node.type === 'dependency')
  ) {
    return { filePath: node.filePath, line: node.lineStart };
  }

  if (node.type === 'dependency') {
    for (const edge of flowData.edges) {
      if (edge.sourceId !== node.id || edge.type !== 'depends_on') continue;
      const target = flowData.nodes[edge.targetId];
      if (target?.filePath && target?.lineStart) {
        return { filePath: target.filePath, line: target.lineStart };
      }
    }
    if (node.level >= 3 && node.filePath && node.lineStart) {
      return { filePath: node.filePath, line: node.lineStart };
    }
  }

  return null;
}

export function getNestedCallTargets(
  node: FlowNodeData,
): Array<{ label: string; filePath: string; line: number }> {
  if (!node.metadata?.call_targets || !Array.isArray(node.metadata.call_targets)) return [];
  return node.metadata.call_targets
    .filter((t: any) => t?.file_path && t?.line_start)
    .map((t: any) => ({
      label: t.label || t.qualified_name || 'Open Call',
      filePath: t.file_path,
      line: t.line_start,
    }));
}
