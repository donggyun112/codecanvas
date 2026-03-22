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
import { applyElkLayout } from './layout/elkLayout';
import TopBar from './components/TopBar';
import DetailPanel from './components/DetailPanel';
import type { FlowGraph, HistoryItem } from './types/flow';

import './styles/index.css';
import './styles/nodes.css';
import './styles/detail.css';

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
    selectedNodeId,
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

    const vis = getVisible(
      flowData,
      currentLevel,
      viewMode,
      isFunctionContext,
      hasTrace,
      nodeDrillState,
    );

    if (vis.nodes.length === 0) {
      setNodes([]);
      setEdges([]);
      return;
    }

    const { nodes: rfNodes, edges: rfEdges } = transformToRfElements(
      vis,
      nodeDrillState,
      hasTrace,
      selectedNodeId,
      isFunctionContext,
    );

    // Run layout
    if (!layoutRunning.current) {
      layoutRunning.current = true;
      applyElkLayout(rfNodes, rfEdges)
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
  }, [flowData, currentLevel, viewMode, nodeDrillState, isFunctionContext, hasTrace]);

  // Update selection highlight without re-layout
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => ({
        ...n,
        data: { ...n.data, isSelected: n.id === selectedNodeId },
      })),
    );
  }, [selectedNodeId, setNodes]);

  const onNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      const prevSelected = selectedNodeId;
      const isNewSelection = prevSelected !== node.id;
      selectNode(node.id);

      // Advance drill state
      if (flowData?.nodes[node.id]) {
        advanceDrill(flowData.nodes[node.id], isNewSelection);
      }
    },
    [selectNode, advanceDrill, flowData, selectedNodeId],
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
        <DetailPanel />
      </div>
    </div>
  );
}
