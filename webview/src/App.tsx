import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useFlowStore } from './store/useFlowStore';
import { nodeTypes } from './nodes';
import { edgeTypes } from './edges';
import { getVisible } from './transform/visibility';
import { transformToRfElements } from './transform';
import { transformExecutionGraph } from './transform/executionTransform';
import { transformCFG } from './transform/cfgTransform';
import { applyElkLayout } from './layout/elkLayout';
import TopBar from './components/TopBar';
import DetailPanel from './components/DetailPanel';
import ReviewBrief from './components/ReviewBrief';
import type { FlowGraph, HistoryItem } from './types/flow';

import './styles/index.css';
import './styles/nodes.css';
import './styles/detail.css';
import './styles/review-brief.css';

function decodeFlowData(encoded: string): any {
  const binary = atob(encoded);
  return JSON.parse(
    new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0))),
  );
}

export default function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const layoutRunning = useRef(false);

  const {
    flowData,
    currentLevel,
    viewMode,
    flowViewMode,
    dataFlowDetail,
    selectedNodeId,
    highlightedOriginIds,
    highlightedOriginChain,
    nodeDrillState,
    isFunctionContext,
    hasTrace,
    selectNode,
    advanceDrill,
    setFlowData,
    setRfElements,
  } = useFlowStore();

  // Initialize from embedded data
  useEffect(() => {
    const dataEl = document.getElementById('flowDataStore');
    if (!dataEl) return;

    const flowAttr = dataEl.getAttribute('data-flow');
    const histAttr = dataEl.getAttribute('data-history');
    if (!flowAttr) return;

    try {
      const flow: FlowGraph = decodeFlowData(flowAttr);
      const history: HistoryItem[] = histAttr ? decodeFlowData(histAttr) : [];
      setFlowData(flow, history);
    } catch (err) {
      console.error('Failed to decode flow data:', err);
    }
  }, [setFlowData]);

  // Transform + layout when flow data or parameters change
  useEffect(() => {
    if (!flowData) return;

    let rfNodes: Node[];
    let rfEdges: Edge[];
    let layoutDirection: 'DOWN' | 'RIGHT';

    if (flowViewMode === 'cfg') {
      // CFG mode: project cfg_block nodes from canonical FlowGraph
      const result = transformCFG(flowData, selectedNodeId, hasTrace, viewMode);
      if (result.nodes.length === 0) { setNodes([]); setEdges([]); return; }
      rfNodes = result.nodes;
      rfEdges = result.edges;
      layoutDirection = 'DOWN';
    } else if (flowViewMode === 'data') {
      // Data flow mode: project exec_step nodes (L3 summary or L4 detail)
      const result = transformExecutionGraph(
        flowData,
        selectedNodeId,
        hasTrace,
        viewMode,
        highlightedOriginChain,
        dataFlowDetail,
      );
      if (result.nodes.length === 0) { setNodes([]); setEdges([]); return; }
      rfNodes = result.nodes;
      rfEdges = result.edges;
      layoutDirection = 'RIGHT';
    } else {
      // Callstack mode: use FlowGraph with visibility
      const vis = getVisible(flowData, currentLevel, viewMode, isFunctionContext, hasTrace, nodeDrillState);
      if (vis.nodes.length === 0) { setNodes([]); setEdges([]); return; }
      const result = transformToRfElements(vis, nodeDrillState, hasTrace, selectedNodeId, isFunctionContext);
      rfNodes = result.nodes;
      rfEdges = result.edges;
      layoutDirection = 'DOWN';
    }

    if (rfNodes.length === 0) { setNodes([]); setEdges([]); return; }

    // Run layout
    if (!layoutRunning.current) {
      layoutRunning.current = true;
      applyElkLayout(rfNodes, rfEdges, layoutDirection)
        .then(({ nodes: laid }) => {
          setNodes(laid as Node[]);
          setEdges(rfEdges);
          setRfElements(laid as Node[], rfEdges);
        })
        .catch((err) => {
          console.error('Layout error:', err);
          setNodes(rfNodes as Node[]);
          setEdges(rfEdges);
        })
        .finally(() => {
          layoutRunning.current = false;
        });
    }
  }, [flowData, currentLevel, viewMode, flowViewMode, dataFlowDetail, nodeDrillState, isFunctionContext, hasTrace, selectedNodeId, highlightedOriginChain]);

  // Update selection + origin highlight without re-layout
  const originSet = useMemo(() => new Set(highlightedOriginIds), [highlightedOriginIds]);
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => ({
        ...n,
        data: {
          ...n.data,
          isSelected: n.id === selectedNodeId,
          isOriginHighlight: originSet.has(n.id),
        },
      })),
    );
  }, [selectedNodeId, originSet, setNodes]);

  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      const prevSelected = selectedNodeId;
      const isNewSelection = prevSelected !== node.id;
      selectNode(node.id);

      // Advance drill state (only for FlowGraph nodes, not execution steps)
      if (flowViewMode === 'callstack' && flowData?.nodes[node.id]) {
        advanceDrill(flowData.nodes[node.id], isNewSelection);
      }
    },
    [selectNode, advanceDrill, flowData, selectedNodeId, flowViewMode],
  );

  const onPaneClick = useCallback(() => {
    selectNode(null);
  }, [selectNode]);

  const memoNodeTypes = useMemo(() => nodeTypes, []);
  const memoEdgeTypes = useMemo(() => edgeTypes, []);

  return (
    <div id="app">
      <TopBar />
      <div className="main">
        {flowViewMode === 'brief' ? (
          <div className="canvas-wrap" style={{ overflow: 'auto' }}>
            <ReviewBrief />
          </div>
        ) : (
        <div className="canvas-wrap">
          {nodes.length === 0 && flowData ? (
            <div style={{ padding: 40, opacity: 0.5, textAlign: 'center' }}>
              No nodes at this level
            </div>
          ) : (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={memoNodeTypes}
              edgeTypes={memoEdgeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={onNodeClick}
              onPaneClick={onPaneClick}
              fitView
              minZoom={0.1}
              maxZoom={4}
              proOptions={{ hideAttribution: true }}
            >
              <Background />
              <Controls />
              <MiniMap
                pannable
                zoomable
                nodeColor={(n: any) => {
                  const data = n.data as any;
                  if (data?.isHit) return '#49cc90';
                  return '#666';
                }}
              />
            </ReactFlow>
          )}
        </div>
        )}
        {flowViewMode !== 'brief' && <DetailPanel />}
      </div>
    </div>
  );
}
