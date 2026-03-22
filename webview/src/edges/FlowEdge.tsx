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

  const color = (data as any)?.color || 'var(--vscode-foreground)';
  const dashed = (data as any)?.dashed;
  const isHit = (data as any)?.isHit;
  const hasTrace = (data as any)?.hasTrace;
  const edgeData = data as any;
  const condition = edgeData?.condition;
  const edgeLabel = edgeData?.label;
  const isFunctionContext = edgeData?.isFunctionContext;

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
          strokeWidth: isHit ? 2.5 : 1.5,
          strokeOpacity: hasTrace ? (isHit ? 0.9 : 0.15) : 0.5,
          strokeDasharray: dashed ? '6,3' : undefined,
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
