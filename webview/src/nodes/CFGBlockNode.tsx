import React from 'react';
import { Handle, Position } from '@xyflow/react';

export default function CFGBlockNode({ data }: { data: any }) {
  const label = data.label || '';
  const kind = data.kind || 'block';
  const color = data.color || '#666';
  const isSelected = data.isSelected;
  const pathState = data.pathState || 'possible';
  const hasTrace = data.hasTrace || false;
  const statements: any[] = data.statements || [];

  const isTerminal = kind === 'exit' || kind === 'error_exit';
  const isEmpty = statements.length === 0 && isTerminal;

  return (
    <div
      className={[
        'cfg-block',
        `cfg-${kind}`,
        isSelected && 'selected',
        pathState === 'verified' && 'cfg-hit',
        pathState === 'unverified' && hasTrace && 'cfg-unhit',
      ].filter(Boolean).join(' ')}
      style={{ borderColor: isSelected ? 'var(--vscode-focusBorder)' : color }}
    >
      <div className="cfg-color-bar" style={{ background: color }} />
      {pathState === 'verified' && <div className="node-hit-glow" />}
      <div className="cfg-kind" style={{ color }}>
        {kind.replace(/_/g, ' ').toUpperCase()}
        {pathState === 'verified' && hasTrace && <span className="path-state-badge" style={{ background: '#27ae60', marginLeft: 4 }}>HIT</span>}
        {pathState === 'unverified' && hasTrace && <span className="path-state-badge unverified" style={{ marginLeft: 4 }}>MISS</span>}
      </div>
      {data.metadata?.branch_explanation && (
        <div className="cfg-explanation">{data.metadata.branch_explanation}</div>
      )}
      {!isEmpty && statements.length > 0 ? (
        <div className="cfg-stmts">
          {statements.map((s: any, i: number) => (
            <div key={i} className={`cfg-stmt cfg-stmt-${s.kind}`}>
              <span className="cfg-line">{s.line}</span>
              <span className="cfg-text">{s.text}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="cfg-label">{label}</div>
      )}
      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
