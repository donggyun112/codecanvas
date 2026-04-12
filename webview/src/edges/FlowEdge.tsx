import React, { useMemo } from 'react';
import {
  BaseEdge,
  getBezierPath,
  type EdgeProps,
} from '@xyflow/react';
import { getSmartEdge } from '@jalez/react-flow-smart-edge';
import { useAbsoluteNodes } from './SmartEdgeContext';

export default function FlowEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  markerEnd,
}: EdgeProps) {
  // Shared across all edges — computed once per geometry change via module cache
  const absNodes = useAbsoluteNodes();

  // Memoize smart edge path — only recompute when endpoints or obstacle map change
  const { edgePath, labelX, labelY } = useMemo(() => {
    const smartResult = getSmartEdge({
      sourcePosition,
      targetPosition,
      sourceX,
      sourceY,
      targetX,
      targetY,
      nodes: absNodes,
      options: { nodePadding: 8, gridRatio: 10 },
    });

    if (smartResult) {
      return {
        edgePath: smartResult.svgPathString,
        labelX: smartResult.edgeCenterX,
        labelY: smartResult.edgeCenterY,
      };
    }

    const [bp, bx, by] = getBezierPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
    });
    return { edgePath: bp, labelX: bx, labelY: by };
  }, [absNodes, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition]);

  const edgeData = data as any;
  const baseColor = edgeData?.color || 'var(--vscode-foreground)';
  const baseDashed = edgeData?.dashed || edgeData?.confidence === 'inferred';
  const hasTrace = edgeData?.hasTrace;
  const pathState = edgeData?.pathState || 'possible';
  const condition = edgeData?.condition;
  const edgeLabel = edgeData?.label;
  const isFunctionContext = edgeData?.isFunctionContext;
  const isOriginTrace = edgeData?.isOriginTrace;

  // Sequence edges (just "next step") are visually demoted so meaningful
  // edges (data flow, branch, error) stand out.
  const edgeKind = edgeData?.kind || '';
  const isSequence = edgeKind === 'sequence' || edgeKind === 'fall_through';

  // 3-way path state edge styling
  let color = baseColor;
  let strokeWidth = isSequence ? 1 : 1.5;
  let strokeOpacity = isSequence ? 0.15 : 0.5;
  let strokeDasharray: string | undefined = baseDashed ? '6,3' : undefined;

  if (isOriginTrace) {
    color = '#3498db';
    strokeWidth = 2.5;
    strokeOpacity = 0.85;
    strokeDasharray = '8,4';
  }

  if (hasTrace) {
    switch (pathState) {
      case 'verified':
        strokeWidth = 2.5;
        strokeOpacity = 0.9;
        break;
      case 'unverified':
        color = '#f39c12';
        strokeWidth = 1.5;
        strokeOpacity = 0.45;
        strokeDasharray = '8,4';
        break;
      case 'runtime-only':
        color = '#3498db';
        strokeWidth = 1.5;
        strokeOpacity = 0.6;
        strokeDasharray = '3,3';
        break;
      default:
        strokeOpacity = 0.15;
        break;
    }
  }

  let label = '';
  if (condition) {
    label = condition;
  } else if (isFunctionContext) {
    if (edgeData?.metadata?.upstream_relation === 'reference') {
      label = edgeLabel || 'reference';
    }
  } else {
    label = edgeLabel || '';
  }
  if (label && label.length > 28) label = label.slice(0, 28) + '...';

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: color,
          strokeWidth,
          strokeOpacity,
          strokeDasharray,
        }}
        markerEnd={markerEnd}
      />
      {label && (
        <foreignObject
          x={labelX - 60}
          y={labelY - 10}
          width={120}
          height={20}
          style={{ overflow: 'visible', pointerEvents: 'none' }}
        >
          <div className="edge-label" style={{ borderColor: color }}>
            {label}
          </div>
        </foreignObject>
      )}
    </>
  );
}
