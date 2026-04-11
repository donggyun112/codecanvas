/**
 * Code Flow view: execution-order nodes with inline source code.
 *
 * Groups consecutive exec_l4 steps that share the same CFG basic block
 * into a single node, producing large code-visible nodes similar to CFG
 * blocks but with semantic operation annotations and cross-function flow.
 *
 * Steps that don't map to any CFG block (callees, pipeline) remain as
 * individual compact nodes with their semantic label.
 */
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData } from '../types/flow';
import type { PathState } from './pathState';
import { projectByKind } from './projection';

interface CodeStatement {
  line: number;
  lineEnd: number | null;
  text: string;
  kind: string;
}

// ---------------------------------------------------------------------------
// CFG cross-reference helpers
// ---------------------------------------------------------------------------

/**
 * Build line → CodeStatement[] index from all cfg_block nodes.
 * Also returns line → cfg_block_id for grouping.
 */
function buildCfgIndex(flowData: FlowGraph): {
  lineToStmts: Map<number, CodeStatement[]>;
  lineToCfgBlock: Map<number, string>;
} {
  const lineToStmts = new Map<number, CodeStatement[]>();
  const lineToCfgBlock = new Map<number, string>();
  for (const n of Object.values(flowData.nodes)) {
    if (n.kind !== 'cfg_block') continue;
    const stmts = (n.metadata?.statements as CodeStatement[]) ?? [];
    for (const s of stmts) {
      lineToCfgBlock.set(s.line, n.id);
      const existing = lineToStmts.get(s.line);
      if (existing) {
        if (!existing.some((e) => e.text === s.text)) existing.push(s);
      } else {
        lineToStmts.set(s.line, [s]);
      }
    }
  }
  return { lineToStmts, lineToCfgBlock };
}

/**
 * Find all code statements within a line range across ALL cfg_blocks.
 * This handles cases where a compound statement (for/if) spans multiple
 * CFG basic blocks.
 */
function collectStatementsInRange(
  lineStart: number | null,
  lineEnd: number | null,
  lineToStmts: Map<number, CodeStatement[]>,
): CodeStatement[] {
  if (!lineStart) return [];
  const end = lineEnd ?? lineStart;
  const result: CodeStatement[] = [];
  const seen = new Set<number>();
  for (let line = lineStart; line <= end; line++) {
    const stmts = lineToStmts.get(line);
    if (!stmts) continue;
    for (const s of stmts) {
      if (!seen.has(s.line)) {
        seen.add(s.line);
        result.push(s);
      }
    }
  }
  return result.sort((a, b) => a.line - b.line);
}

// ---------------------------------------------------------------------------
// Grouping: consecutive exec_steps sharing a CFG block → one merged node
// ---------------------------------------------------------------------------

interface ExecItem {
  node: FlowNodeData;
  cfgBlockId: string | null; // null = no CFG match (callee/pipeline)
}

interface MergedGroup {
  /** The node IDs of exec_l4 steps in this group. */
  stepIds: string[];
  /** The original exec_l4 nodes. */
  steps: FlowNodeData[];
  /** Shared CFG block id (null for ungrouped). */
  cfgBlockId: string | null;
}

/**
 * Group consecutive exec_l4 steps by their containing CFG block.
 * - Steps in the same CFG block that appear consecutively → merge.
 * - Steps with no CFG block → standalone (1 step per group).
 * - Branch/error steps always stay standalone (they're semantic anchors).
 */
function groupByCfgBlock(
  items: ExecItem[],
): MergedGroup[] {
  const groups: MergedGroup[] = [];
  let current: MergedGroup | null = null;

  const BREAK_OPS = new Set(['branch', 'error', 'respond']);

  for (const item of items) {
    const op = (item.node.metadata?.operation as string) ?? '';
    const forceBreak =
      !item.cfgBlockId ||
      BREAK_OPS.has(op) ||
      (item.node.metadata?.depth ?? 0) > 0;

    if (
      !forceBreak &&
      current &&
      current.cfgBlockId === item.cfgBlockId
    ) {
      // Extend current group
      current.stepIds.push(item.node.id);
      current.steps.push(item.node);
    } else {
      // Start new group
      current = {
        stepIds: [item.node.id],
        steps: [item.node],
        cfgBlockId: forceBreak ? null : item.cfgBlockId,
      };
      groups.push(current);
    }
  }
  return groups;
}

// ---------------------------------------------------------------------------
// Main transform
// ---------------------------------------------------------------------------

export function transformCodeFlow(
  flowData: FlowGraph,
  selectedNodeId: string | null,
  hasTrace: boolean,
  viewMode: 'all' | 'runtime' | 'static',
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Project exec_l4 nodes + data_flow edges
  const projection = projectByKind(
    flowData,
    new Set(['exec_l4']),
    new Set(['data_flow']),
  );

  if (projection.nodes.length === 0) return { nodes, edges };

  const { lineToStmts, lineToCfgBlock } = buildCfgIndex(flowData);

  // Runtime hit lookup
  const runtimeHitNodes = new Set<string>();
  if (hasTrace) {
    for (const n of Object.values(flowData.nodes)) {
      if (n.metadata?.runtime_hit) runtimeHitNodes.add(n.id);
    }
  }

  // --- Step 1: Classify each exec_l4 and compute pathState ---
  const pathStateOf: Record<string, PathState> = {};
  const passedNodes: ExecItem[] = [];

  for (const node of projection.nodes) {
    const m = node.metadata ?? {};
    const sourceNodeIds = (m.source_node_ids as string[]) ?? [];
    const hasSourceIds = sourceNodeIds.length > 0;
    const hitKnown = hasTrace && hasSourceIds;
    const isHit = hitKnown
      ? sourceNodeIds.some((id) => runtimeHitNodes.has(id))
      : false;

    if (hasTrace && viewMode === 'static') {
      if (hitKnown && isHit) continue;
      if (node.confidence === 'runtime') continue;
    }
    if (hasTrace && viewMode === 'runtime' && hitKnown && !isHit) continue;

    let pathState: PathState = 'possible';
    if (hasTrace) {
      if (node.confidence === 'runtime') pathState = 'runtime-only';
      else if (hitKnown) pathState = isHit ? 'verified' : 'unverified';
    }
    pathStateOf[node.id] = pathState;

    const cfgBlockId = node.lineStart ? (lineToCfgBlock.get(node.lineStart) ?? null) : null;
    passedNodes.push({ node, cfgBlockId });
  }

  // --- Step 2: Group by CFG block ---
  const groups = groupByCfgBlock(passedNodes);

  // Map: original exec_l4 id → merged group node id (for edge remapping)
  const stepToGroupId: Record<string, string> = {};

  for (const group of groups) {
    const primary = group.steps[0];
    const m = primary.metadata ?? {};
    const groupId =
      group.steps.length === 1
        ? primary.id
        : `cfgroup:${group.steps.map((s) => s.id).join('+')}`;

    for (const s of group.steps) {
      stepToGroupId[s.id] = groupId;
    }

    // Collect all code statements for the group.
    // For grouped steps: union of all steps' line ranges across cfg blocks.
    // For single steps: full lineStart..lineEnd range (catches for-loop body
    // that spans multiple cfg blocks).
    let codeStatements: CodeStatement[] = [];
    if (group.steps.length > 1) {
      // Merged group: collect from all steps' line ranges
      const seen = new Set<number>();
      for (const s of group.steps) {
        for (const stmt of collectStatementsInRange(s.lineStart, s.lineEnd, lineToStmts)) {
          if (!seen.has(stmt.line)) {
            seen.add(stmt.line);
            codeStatements.push(stmt);
          }
        }
      }
      codeStatements.sort((a, b) => a.line - b.line);
    } else {
      // Single step: use full line range
      codeStatements = collectStatementsInRange(
        primary.lineStart,
        primary.lineEnd ?? primary.lineStart,
        lineToStmts,
      );
    }

    // Merge operation labels for grouped steps
    const operations = group.steps.map(
      (s) => (s.metadata?.operation as string) ?? 'process',
    );
    const primaryOp = operations[0];
    const label =
      group.steps.length === 1
        ? primary.name
        : group.steps.map((s) => s.name).join(' → ');

    // Aggregate pathState: worst wins
    const groupPathStates = group.steps.map((s) => pathStateOf[s.id]);
    let groupPathState: PathState = 'possible';
    if (groupPathStates.includes('verified')) groupPathState = 'verified';
    if (groupPathStates.includes('unverified')) groupPathState = 'unverified';
    if (!hasTrace) groupPathState = 'possible';

    const isHitAny = group.steps.some((s) => {
      const sids = (s.metadata?.source_node_ids as string[]) ?? [];
      return sids.some((id) => runtimeHitNodes.has(id));
    });

    const depth = (m.depth as number) ?? 0;
    const output = (m.output as string | null) ?? null;
    const outputType = (m.output_type as string | null) ?? null;
    const errorLabel = (m.error_label as string | null) ?? null;
    const branchCondition = (m.branch_condition as string | null) ?? null;
    const branchId = (m.branch_id as string | null) ?? null;

    // For multi-step groups, collect all outputs
    const allOutputs = group.steps
      .map((s) => s.metadata?.output as string)
      .filter(Boolean);
    const groupOutput =
      group.steps.length === 1
        ? output
        : allOutputs.length > 0
          ? allOutputs.join(', ')
          : null;
    const lastStep = group.steps[group.steps.length - 1];
    const groupOutputType =
      group.steps.length === 1
        ? outputType
        : (lastStep.metadata?.output_type as string | null) ?? null;

    nodes.push({
      id: groupId,
      type: 'codeFlow',
      position: { x: 0, y: 0 },
      data: {
        id: groupId,
        label,
        operation: primaryOp,
        operations: operations.length > 1 ? operations : undefined,
        output: groupOutput,
        outputType: groupOutputType,
        errorLabel,
        branchCondition,
        branchId,
        depth,
        filePath: primary.filePath,
        lineStart: primary.lineStart,
        lineEnd: lastStep.lineEnd ?? lastStep.lineStart,
        codeStatements,
        hasCode: codeStatements.length > 0,
        stepCount: group.steps.length,
        metadata: m,
        isSelected: group.stepIds.includes(selectedNodeId ?? ''),
        isHit: hasTrace && isHitAny,
        hitUnknown:
          hasTrace &&
          group.steps.every(
            (s) => ((s.metadata?.source_node_ids as string[]) ?? []).length === 0,
          ),
        pathState: groupPathState,
        hasTrace,
      },
    });
  }

  // --- Step 3: Remap edges to group IDs ---
  const visibleGroupIds = new Set(nodes.map((n) => n.id));
  const seenEdges = new Set<string>();

  for (const edge of projection.edges) {
    const srcGroup = stepToGroupId[edge.sourceId];
    const tgtGroup = stepToGroupId[edge.targetId];
    if (!srcGroup || !tgtGroup) continue;
    if (srcGroup === tgtGroup) continue; // internal to same group

    const edgeKey = `${srcGroup}→${tgtGroup}`;
    if (seenEdges.has(edgeKey)) continue; // deduplicate merged edges
    seenEdges.add(edgeKey);

    if (!visibleGroupIds.has(srcGroup) || !visibleGroupIds.has(tgtGroup)) continue;

    const variable = (edge.metadata?.variable as string) ?? '';
    let edgeLabel = edge.label || variable || '';

    // Branch label resolution
    const srcNode = projection.nodeMap[edge.sourceId];
    const tgtNode = projection.nodeMap[edge.targetId];
    if (srcNode?.metadata?.operation === 'branch' && tgtNode?.metadata?.branch_id) {
      const path = (tgtNode.metadata.branch_id as string).split(':').pop() || '';
      if (path === 'if') edgeLabel = 'yes';
      else if (path === 'else') edgeLabel = 'no';
      else if (path) edgeLabel = path;
    }

    const kind = (edge.metadata?.data_kind as string) || 'sequence';
    let color: string | undefined;
    let dashed = false;
    if (kind === 'error' || edge.isErrorPath) {
      color = '#e74c3c';
      dashed = true;
    } else if (kind === 'data') {
      color = '#3498db';
    } else if (kind === 'branch') {
      color = '#f39c12';
    }

    const srcPS = pathStateOf[edge.sourceId] ?? 'possible';
    const tgtPS = pathStateOf[edge.targetId] ?? 'possible';
    const edgeHit = hasTrace && srcPS === 'verified' && tgtPS === 'verified';
    let edgePathState: PathState = 'possible';
    if (hasTrace) {
      edgePathState = edgeHit ? 'verified' : 'unverified';
    }

    edges.push({
      id: `cfe:${edgeKey}`,
      source: srcGroup,
      target: tgtGroup,
      type: 'flowEdge',
      data: {
        color,
        dashed,
        label: edgeLabel,
        hasTrace,
        isHit: edgeHit,
        pathState: edgePathState,
        kind,
      },
    });
  }

  return { nodes, edges };
}
