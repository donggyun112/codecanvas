/**
 * Code Flow view: function-level call graph with inline source code.
 *
 * Like callstack view (function → function), but each function node
 * displays its full source code from cfg_block statements. Depth-0
 * is the handler, depth-1 are direct callees (dependencies, services).
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData } from '../types/flow';
import type { PathState } from './pathState';

interface CodeStatement {
  line: number;
  lineEnd: number | null;
  text: string;
  kind: string;
}

/**
 * Collect all cfg_block statements belonging to a function scope.
 * Sorts by line number for display order.
 */
function collectFunctionCode(
  flowData: FlowGraph,
  scope: string,
): CodeStatement[] {
  const stmts: CodeStatement[] = [];
  const seen = new Set<number>();

  for (const n of Object.values(flowData.nodes)) {
    if (n.kind !== 'cfg_block') continue;
    if (n.scope !== scope) continue;
    const blockStmts = (n.metadata?.statements as CodeStatement[]) ?? [];
    for (const s of blockStmts) {
      if (!seen.has(s.line)) {
        seen.add(s.line);
        stmts.push(s);
      }
    }
  }
  return stmts.sort((a, b) => a.line - b.line);
}

/**
 * Build a map of function scope → exec_l4 operations for annotations.
 */
function collectFunctionOps(
  flowData: FlowGraph,
): Map<string, Array<{ operation: string; label: string; line: number | null }>> {
  const ops = new Map<string, Array<{ operation: string; label: string; line: number | null }>>();
  for (const n of Object.values(flowData.nodes)) {
    if (n.kind !== 'exec_l4') continue;
    const m = n.metadata ?? {};
    const scope = n.scope || '';
    if (!scope) continue;
    const list = ops.get(scope) ?? [];
    list.push({
      operation: (m.operation as string) ?? 'process',
      label: n.name,
      line: n.lineStart,
    });
    ops.set(scope, list);
  }
  return ops;
}

export function transformCodeFlow(
  flowData: FlowGraph,
  selectedNodeId: string | null,
  hasTrace: boolean,
  viewMode: 'all' | 'runtime' | 'static',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // ---------------------------------------------------------------
  // Step 1: Find handler + direct callee functions
  // ---------------------------------------------------------------

  // Handler node: pipeline_phase=handler OR context_root (any kind)
  const handlerNode = Object.values(flowData.nodes).find(
    (n) => n.metadata?.pipeline_phase === 'handler' || n.metadata?.context_root,
  );

  if (!handlerNode) return { nodes, edges };

  // Collect callable nodes to show: handler + depth-1 callees
  const functionScopes = new Map<string, FlowNodeData>();
  const callerOf = new Map<string, string>();

  // Index all callable nodes (function, pipeline with handler phase, etc.)
  const nodeById = new Map<string, FlowNodeData>();
  for (const n of Object.values(flowData.nodes)) {
    nodeById.set(n.id, n);
  }

  // Start with handler
  functionScopes.set(handlerNode.id, handlerNode);

  // Find callees from exec_l4 callee_function references
  for (const n of Object.values(flowData.nodes)) {
    if (n.kind !== 'exec_l4') continue;
    const m = n.metadata ?? {};
    const callee = m.callee_function as string | undefined;
    if (!callee) continue;

    const calleeNode = nodeById.get(callee);
    if (calleeNode && !functionScopes.has(calleeNode.id)) {
      functionScopes.set(calleeNode.id, calleeNode);
      callerOf.set(calleeNode.id, handlerNode.id);
    }
  }

  // Find callees from calls/depends_on/injects edges originating from handler
  for (const e of flowData.edges) {
    if (e.type !== 'calls' && e.type !== 'depends_on' && e.type !== 'injects') continue;
    if (e.sourceId !== handlerNode.id) continue;
    const target = flowData.nodes[e.targetId];
    if (target && (target.kind === 'function' || target.kind === 'pipeline') &&
        !functionScopes.has(target.id)) {
      functionScopes.set(target.id, target);
      callerOf.set(target.id, handlerNode.id);
    }
  }

  // ---------------------------------------------------------------
  // Step 2: Build RF nodes — one per function with full code
  // ---------------------------------------------------------------

  const funcOps = collectFunctionOps(flowData);

  for (const [scope, funcNode] of functionScopes) {
    const codeStatements = collectFunctionCode(flowData, scope);
    const ops = funcOps.get(scope) ?? [];

    // Determine primary operation for the color
    const opCounts: Record<string, number> = {};
    for (const op of ops) {
      opCounts[op.operation] = (opCounts[op.operation] ?? 0) + 1;
    }
    const primaryOp = Object.entries(opCounts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? 'process';

    const isHandler = funcNode.metadata?.pipeline_phase === 'handler' || funcNode.metadata?.context_root;
    const pipelinePhase = (funcNode.metadata?.pipeline_phase as string) ?? '';
    const phase = isHandler ? 'handler' : pipelinePhase || 'callee';

    // Runtime hit
    const isHit = hasTrace && !!funcNode.metadata?.runtime_hit;
    let pathState: PathState = 'possible';
    if (hasTrace) {
      if (funcNode.confidence === 'runtime') pathState = 'runtime-only';
      else pathState = isHit ? 'verified' : 'unverified';
    }

    if (hasTrace && viewMode === 'runtime' && pathState === 'unverified') continue;
    if (hasTrace && viewMode === 'static' && pathState === 'verified') continue;

    nodes.push({
      id: funcNode.id,
      type: 'codeFlow',
      position: { x: 0, y: 0 },
      data: {
        id: funcNode.id,
        label: funcNode.displayName || funcNode.name,
        operation: isHandler ? 'handler' : primaryOp,
        phase,
        output: null,
        outputType: null,
        errorLabel: null,
        branchCondition: null,
        branchId: null,
        depth: isHandler ? 0 : 1,
        filePath: funcNode.filePath,
        lineStart: funcNode.lineStart,
        lineEnd: funcNode.lineEnd,
        codeStatements,
        hasCode: codeStatements.length > 0,
        stepCount: ops.length,
        operations: ops.map((o) => o.operation),
        metadata: funcNode.metadata,
        isSelected: funcNode.id === selectedNodeId,
        isHit,
        hitUnknown: false,
        pathState,
        hasTrace,
      },
    });
  }

  if (nodes.length === 0) return { nodes, edges };

  // ---------------------------------------------------------------
  // Step 3: Build edges — function-to-function calls
  // ---------------------------------------------------------------

  const visibleIds = new Set(nodes.map((n) => n.id));

  // From callstack graph edges (type=calls)
  const seenEdges = new Set<string>();
  for (const e of flowData.edges) {
    if (e.type !== 'calls' && e.type !== 'depends_on' && e.type !== 'injects') continue;
    if (!visibleIds.has(e.sourceId) || !visibleIds.has(e.targetId)) continue;
    const key = `${e.sourceId}→${e.targetId}`;
    if (seenEdges.has(key)) continue;
    seenEdges.add(key);

    let color: string | undefined;
    let dashed = false;
    let label = e.label || '';
    if (e.type === 'depends_on' || e.type === 'injects') {
      color = '#3498db';
      dashed = true;
      label = label || 'Depends';
    }

    edges.push({
      id: `cfe:${key}`,
      source: e.sourceId,
      target: e.targetId,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label,
        hasTrace,
        isHit: false,
        pathState: 'possible' as PathState,
        kind: e.type,
      },
    });
  }

  // Also add edges from callerOf map (for deps discovered via exec_l4)
  for (const [calleeId, callerId] of callerOf) {
    if (!visibleIds.has(calleeId) || !visibleIds.has(callerId)) continue;
    const key = `${callerId}→${calleeId}`;
    if (seenEdges.has(key)) continue;
    seenEdges.add(key);

    edges.push({
      id: `cfe:${key}`,
      source: callerId,
      target: calleeId,
      type: 'flowEdge',
      data: {
        color: '#3498db',
        dashed: true,
        label: 'calls',
        hasTrace,
        isHit: false,
        pathState: 'possible' as PathState,
        kind: 'calls',
      },
    });
  }

  return { nodes, edges };
}
