import React from 'react';
import { Handle, Position } from '@xyflow/react';

const PHASE_COLORS: Record<string, string> = {
  handler: '#27ae60',
  callee: '#3498db',
  dependency: '#9b59b6',
  middleware: '#1abc9c',
};

export default function CodeFlowNode({ data }: { data: any }) {
  const label = data.label || '';
  const phase = data.phase || 'callee';
  const color = PHASE_COLORS[phase] || '#666';
  const isSelected = data.isSelected;
  const isHit = data.isHit;
  const hasTrace = data.hasTrace;
  const pathState = data.pathState || 'possible';
  const codeStatements: any[] = data.codeStatements || [];
  const hasCode = codeStatements.length > 0;
  const depth = data.depth || 0;
  const filePath = data.filePath || '';
  const lineStart = data.lineStart;
  const lineEnd = data.lineEnd;

  const fileRef = filePath
    ? filePath.split('/').slice(-2).join('/') +
      (lineStart ? `:${lineStart}` : '') +
      (lineEnd && lineEnd !== lineStart ? `-${lineEnd}` : '')
    : '';

  return (
    <div
      className={[
        'cf-node',
        isSelected && 'selected',
        pathState === 'verified' && 'hit',
        pathState === 'unverified' && 'cf-unverified',
        hasCode && 'cf-has-code',
      ].filter(Boolean).join(' ')}
      style={{ borderColor: isSelected ? 'var(--vscode-focusBorder)' : color }}
    >
      <div className="cf-color-bar" style={{ background: color }} />
      {isHit && <div className="node-hit-glow" />}

      {/* Header: function name + phase */}
      <div className="cf-header">
        <span className="cf-func-name">{label}</span>
        <span className="cf-phase" style={{ color }}>
          {phase.toUpperCase()}
          {depth > 0 && <span className="cf-depth"> d{depth}</span>}
        </span>
      </div>

      {/* File reference */}
      {fileRef && <div className="cf-file-ref">{fileRef}</div>}

      {/* Trace badges */}
      {hasTrace && pathState === 'verified' && (
        <div className="cf-trace-badge cf-hit">HIT</div>
      )}
      {hasTrace && pathState === 'unverified' && (
        <div className="cf-trace-badge cf-miss">MISS</div>
      )}

      {/* Code block */}
      {hasCode && (
        <div className="cf-code">
          {codeStatements.map((s: any, i: number) => (
            <div key={i} className={`cf-stmt cf-stmt-${s.kind}`}>
              <span className="cf-line-num">{s.line}</span>
              <span className="cf-line-text">{s.text}</span>
            </div>
          ))}
        </div>
      )}

      {/* No code fallback */}
      {!hasCode && (
        <div className="cf-no-code">No CFG data available</div>
      )}

      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
