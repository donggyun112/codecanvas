import React from 'react';
import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react';

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
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  const edgeData = data as any;
  const baseColor = edgeData?.color || 'var(--vscode-foreground)';
  const baseDashed = edgeData?.dashed || edgeData?.confidence === 'inferred';
  const isHit = edgeData?.isHit;
  const hasTrace = edgeData?.hasTrace;
  const pathState = edgeData?.pathState || 'possible';
  const condition = edgeData?.condition;
  const edgeLabel = edgeData?.label;
  const isFunctionContext = edgeData?.isFunctionContext;
  const isOriginTrace = edgeData?.isOriginTrace;

  // 3-way path state edge styling
  let color = baseColor;
  let strokeWidth = 1.5;
  let strokeOpacity = 0.5;
  let strokeDasharray: string | undefined = baseDashed ? '6,3' : undefined;

  // Origin trace edges: distinctive blue animated dashes
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
        // 'possible' with trace — dimmed
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
