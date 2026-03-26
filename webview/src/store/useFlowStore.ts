import { create } from 'zustand';
import type { Node, Edge } from '@xyflow/react';
import type { FlowGraph, FlowNodeData, HistoryItem } from '../types/flow';

interface FlowState {
  flowData: FlowGraph | null;
  historyTrail: HistoryItem[];
  currentLevel: number;
  viewMode: 'all' | 'runtime' | 'static';
  flowViewMode: 'data' | 'callstack' | 'cfg' | 'brief';
  dataFlowDetail: 'summary' | 'detail';
  selectedNodeId: string | null;
  highlightedOriginIds: string[];
  highlightedOriginChain: Array<{ stepId: string; variable: string; label: string; operation: string }>;
  nodeDrillState: Record<string, number>;
  rfNodes: Node[];
  rfEdges: Edge[];
  isFunctionContext: boolean;
  hasTrace: boolean;
  handlerNodeId: string | null;
  codePreviewCache: Record<string, any>;
  codePreviewSeq: number;

  setFlowData: (data: FlowGraph, history: HistoryItem[]) => void;
  setLevel: (level: number) => void;
  setViewMode: (mode: 'all' | 'runtime' | 'static') => void;
  setFlowViewMode: (mode: 'data' | 'callstack' | 'cfg' | 'brief') => void;
  setDataFlowDetail: (detail: 'summary' | 'detail') => void;
  selectNode: (id: string | null) => void;
  setDrillState: (nodeId: string, depth: number) => void;
  advanceDrill: (node: FlowNodeData, isNewSelection: boolean) => boolean;
  setRfElements: (nodes: Node[], edges: Edge[]) => void;
  updateCodePreview: (cacheKey: string, data: any) => void;
  nextPreviewSeq: () => number;
}

export const useFlowStore = create<FlowState>((set, get) => ({
  flowData: null,
  historyTrail: [],
  currentLevel: 1,
  viewMode: 'all',
  flowViewMode: 'brief',
  dataFlowDetail: 'summary',
  selectedNodeId: null,
  highlightedOriginIds: [],
  highlightedOriginChain: [],
  nodeDrillState: {},
  rfNodes: [],
  rfEdges: [],
  isFunctionContext: false,
  hasTrace: false,
  handlerNodeId: null,
  codePreviewCache: {},
  codePreviewSeq: 0,

  setFlowData: (data, history) => {
    const entrypoint = data.entrypoint;
    const isFunctionContext = !!(
      entrypoint?.kind === 'function' &&
      entrypoint?.metadata?.from_location
    );
    const hasTrace = !!(
      entrypoint?.metadata?.trace?.hitNodes
    );

    let handlerNodeId: string | null = null;
    const drillState: Record<string, number> = {};

    Object.values(data.nodes).forEach((node) => {
      if (
        !handlerNodeId &&
        node.metadata?.pipeline_phase === 'handler' &&
        node.level === 3
      ) {
        handlerNodeId = node.id;
      }
    });

    if (handlerNodeId) {
      drillState[handlerNodeId] = 1;
    }

    let selectedNodeId: string | null = null;
    Object.values(data.nodes).forEach((node) => {
      if (!selectedNodeId && node.metadata?.context_root) {
        selectedNodeId = node.id;
      }
    });

    set({
      flowData: data,
      historyTrail: history,
      isFunctionContext,
      hasTrace,
      handlerNodeId,
      nodeDrillState: drillState,
      selectedNodeId,
      currentLevel: isFunctionContext ? 1 : 1,
      viewMode: hasTrace ? 'runtime' : 'all',
    });
  },

  setLevel: (level) => set({ currentLevel: level }),
  setViewMode: (mode) => set({ viewMode: mode }),
  setFlowViewMode: (mode) => set({ flowViewMode: mode }),
  setDataFlowDetail: (detail) => set({ dataFlowDetail: detail }),

  selectNode: (id) => {
    const state = get();
    let originIds: string[] = [];
    let originChain: Array<{ stepId: string; variable: string; label: string; operation: string }> = [];
    // When selecting a respond step, compute its origin chain
    if (id && state.flowData?.executionGraph) {
      const step = state.flowData.executionGraph.steps.find((s) => s.id === id);
      if (step?.operation === 'respond' && step.metadata?.response_origins) {
        const origins = step.metadata.response_origins as any[];
        originIds = origins.map((o) => o.stepId);
        originChain = origins;
      }
    }
    set({ selectedNodeId: id, highlightedOriginIds: originIds, highlightedOriginChain: originChain });
  },

  setDrillState: (nodeId, depth) =>
    set((state) => ({
      nodeDrillState: { ...state.nodeDrillState, [nodeId]: depth },
    })),

  advanceDrill: (node, isNewSelection) => {
    const state = get();
    const children = Object.values(state.flowData?.nodes ?? {}).filter(
      (n) => n.level === 4 && n.metadata?.function_id === node.id,
    );
    if (children.length === 0) return false;

    const maxDepth = 1;
    const current = state.nodeDrillState[node.id] ?? 0;
    const next = isNewSelection
      ? Math.max(current, 1)
      : (current + 1) % (maxDepth + 1);
    if (next === current) return false;

    set({
      nodeDrillState: { ...state.nodeDrillState, [node.id]: next },
    });
    return true;
  },

  setRfElements: (nodes, edges) => set({ rfNodes: nodes, rfEdges: edges }),

  updateCodePreview: (cacheKey, data) =>
    set((state) => ({
      codePreviewCache: { ...state.codePreviewCache, [cacheKey]: data },
    })),

  nextPreviewSeq: () => {
    const next = get().codePreviewSeq + 1;
    set({ codePreviewSeq: next });
    return next;
  },
}));
