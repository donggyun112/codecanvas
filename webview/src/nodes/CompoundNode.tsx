import React from 'react';
import { Handle, Position } from '@xyflow/react';
import { getTypeColor } from './shared/nodeColors';

export default function CompoundNode({ data }: { data: any }) {
  const typeColor = getTypeColor(data.type);
  const isSelected = data.isSelected;
  const isHit = data.isHit;
  const hasTrace = data.hasTrace ?? false;
  const displayName = data.displayName || data.name || '';

  return (
    <div
      className={`compound-node ${isSelected ? 'selected' : ''} ${hasTrace && !isHit ? 'dimmed' : ''}`}
      style={{
        borderColor: typeColor,
        width: '100%',
        height: '100%',
      }}
    >
      <div className="compound-title-bar" style={{ background: typeColor }}>
        <span className="compound-type" style={{ color: typeColor }}>
          {data.type.replace(/_/g, ' ').toUpperCase()}
        </span>
        <span className="compound-name">
          {displayName.length > 40 ? displayName.slice(0, 40) + '...' : displayName}
        </span>
      </div>
      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
