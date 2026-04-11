import React from 'react';
import { Handle, Position } from '@xyflow/react';

const OP_COLORS: Record<string, string> = {
  query: '#9b59b6',
  transform: '#3498db',
  validate: '#f39c12',
  branch: '#7f8c8d',
  respond: '#27ae60',
  side_effect: '#95a5a6',
  process: '#e67e22',
  error: '#e74c3c',
  pipeline: '#1abc9c',
};

const OP_LABELS: Record<string, string> = {
  query: 'QUERY',
  transform: 'TRANSFORM',
  validate: 'VALIDATE',
  branch: 'BRANCH',
  respond: 'RESPONSE',
  side_effect: 'EFFECT',
  process: 'PROCESS',
  error: 'ERROR',
  pipeline: 'PIPELINE',
};

export default function CodeFlowNode({ data }: { data: any }) {
  const op = data.operation || 'process';
  const color = OP_COLORS[op] || '#666';
  const opLabel = OP_LABELS[op] || op.toUpperCase();
  const label = data.label || '';
  const output = data.output || '';
  const outputType = data.outputType || '';
  const errorLabel = data.errorLabel || '';
  const branchCondition = data.branchCondition || '';
  const isSelected = data.isSelected;
  const isHit = data.isHit;
  const hitUnknown = data.hitUnknown;
  const hasTrace = data.hasTrace;
  const pathState = data.pathState || 'possible';
  const depth = data.depth || 0;
  const codeStatements: any[] = data.codeStatements || [];
  const hasCode = codeStatements.length > 0;
  const filePath = data.filePath || '';
  const lineStart = data.lineStart;
  const branchId = data.branchId || '';
  const branchPath = branchId.includes(':') ? branchId.split(':').pop() : '';

  // Build output subtitle
  let subtitle = '';
  if (output && outputType) {
    subtitle = `→ ${output}: ${outputType}`;
  } else if (output) {
    subtitle = `→ ${output}`;
  } else if (outputType) {
    subtitle = `→ ${outputType}`;
  }

  // Short file reference for callee nodes
  const fileRef = filePath
    ? filePath.split('/').slice(-2).join('/') + (lineStart ? `:${lineStart}` : '')
    : '';

  return (
    <div
      className={[
        'cf-node',
        isSelected && 'selected',
        pathState === 'verified' && 'hit',
        pathState === 'unverified' && 'cf-unverified',
        pathState === 'runtime-only' && 'cf-runtime-only',
        hasCode && 'cf-has-code',
      ].filter(Boolean).join(' ')}
      style={{
        borderColor: isSelected ? 'var(--vscode-focusBorder)' : color,
        marginLeft: depth > 0 ? 8 : 0,
      }}
    >
      <div className="cf-color-bar" style={{ background: color }} />
      {isHit && !hitUnknown && <div className="node-hit-glow" />}

      {/* Header: operation badge + label */}
      <div className="cf-header">
        <span className="cf-op" style={{ color }}>
          {opLabel}
          {branchPath && <span className="cf-branch-path"> ({branchPath})</span>}
          {depth > 0 && <span className="cf-depth">d{depth}</span>}
        </span>
        {pathState === 'verified' && hasTrace && (
          <span className="path-state-badge" style={{ background: '#27ae60' }}>HIT</span>
        )}
        {pathState === 'unverified' && hasTrace && (
          <span className="path-state-badge unverified">MISS</span>
        )}
        {hitUnknown && <span className="cf-unknown">?</span>}
      </div>

      {/* Semantic label */}
      <div className="cf-label">{label}</div>

      {/* Branch condition */}
      {op === 'branch' && branchCondition && (
        <div className="cf-condition">{branchCondition}</div>
      )}

      {/* Code block (from CFG overlap) */}
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

      {/* Callee reference (no CFG overlap) */}
      {!hasCode && fileRef && (
        <div className="cf-file-ref">{fileRef}</div>
      )}

      {/* Output / error */}
      {subtitle && <div className="cf-output">{subtitle}</div>}
      {errorLabel && <div className="cf-error">fail → {errorLabel}</div>}

      <Handle type="target" position={Position.Top} />
      <Handle type="source" position={Position.Bottom} />
      {errorLabel && (
        <Handle type="source" position={Position.Right} id="error" style={{ background: '#e74c3c' }} />
      )}
    </div>
  );
}
