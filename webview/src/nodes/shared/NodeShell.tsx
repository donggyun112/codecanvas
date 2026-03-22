import React from 'react';
import { Handle, Position } from '@xyflow/react';
import { getTypeColor } from './nodeColors';

interface NodeShellProps {
  data: any;
  children?: React.ReactNode;
}

function shortPath(p: string): string {
  const parts = p.split('/');
  return parts.slice(-2).join('/');
}

export default function NodeShell({ data, children }: NodeShellProps) {
  const typeColor = getTypeColor(data.type);
  const isHit = data.isHit;
  const isSelected = data.isSelected;
  const isUnresolved =
    data.confidence === 'inferred' || String(data.id || '').includes('unresolved.');
  const isContextRoot = !!data.metadata?.context_root;
  const isUpstream = data.metadata?.upstream_distance != null;
  const hasTrace = data.hasTrace ?? false;
  const displayName = data.displayName || data.name || '';
  const isFunctionLike =
    data.type === 'function' || data.type === 'method' || data.type === 'class';

  const filePath = data.filePath;
  const lineStart = data.lineStart;

  // Callsite from evidence
  let locationText = '';
  let locationKind = '';
  if (filePath) {
    locationText = `${shortPath(filePath)}:${lineStart || 1}`;
    locationKind = 'definition';
  } else if (data.evidence?.length) {
    const ev = data.evidence.find((e: any) => e.filePath && e.lineNumber);
    if (ev) {
      locationText = `via ${shortPath(ev.filePath)}:${ev.lineNumber}`;
      locationKind = 'callsite';
    }
  }

  return (
    <div
      className={`node-shell ${isSelected ? 'selected' : ''} ${isHit ? 'hit' : ''} ${hasTrace && !isHit ? 'dimmed' : ''} ${isUnresolved ? 'unresolved' : ''}`}
      style={{
        borderColor: isSelected ? 'var(--vscode-focusBorder)' : typeColor,
        background: isUpstream && !isContextRoot ? 'rgba(138, 164, 255, 0.08)' : undefined,
      }}
    >
      <div className="node-color-bar" style={{ background: typeColor }} />
      {isHit && <div className="node-hit-glow" />}
      <div className="node-type-label" style={{ color: typeColor }}>
        {data.type.replace(/_/g, ' ').toUpperCase()}
      </div>
      <div className={`node-name ${isFunctionLike ? '' : 'bold'}`}>
        {displayName.length > 35 ? displayName.slice(0, 35) + '...' : displayName}
      </div>
      {locationText && (
        <div className="node-location">{locationText}</div>
      )}
      {isHit && data.metadata?.execution_order && (
        <div className="node-order-badge">{data.metadata.execution_order}</div>
      )}
      {children}
      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
