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
};

const SIGNAL_BADGES: Record<string, { label: string; color: string; title: string }> = {
  db_write: { label: 'DB\u2009W', color: '#9b59b6', title: 'Database write' },
  db_read: { label: 'DB\u2009R', color: '#8e44ad', title: 'Database read' },
  http_call: { label: 'HTTP', color: '#e67e22', title: 'External HTTP call' },
  raises: { label: 'ERR', color: '#e74c3c', title: 'Raises exception' },
  raises_4xx: { label: '4xx', color: '#e74c3c', title: 'Client error (4xx)' },
  raises_5xx: { label: '5xx', color: '#c0392b', title: 'Server error (5xx)' },
  auth: { label: 'AUTH', color: '#f1c40f', title: 'Auth dependency' },
  io: { label: 'I/O', color: '#3498db', title: 'I/O operation' },
};

export default function DataFlowNode({ data }: { data: any }) {
  const op = data.operation || 'process';
  const color = OP_COLORS[op] || '#666';
  const opLabel = OP_LABELS[op] || op.toUpperCase();
  const label = data.label || '';
  const output = data.output || '';
  const outputType = data.outputType || '';
  const errorLabel = data.errorLabel || '';
  const isSelected = data.isSelected;
  const isHit = data.isHit;
  const hitUnknown = data.hitUnknown;  // structural step: no trace info
  const hasTrace = data.hasTrace;
  const branchId = data.branchId || '';
  const branchPath = branchId.includes(':') ? branchId.split(':').pop() : '';
  const depth = data.depth || 0;
  const pathState = data.pathState || 'possible';
  const isOriginHighlight = data.isOriginHighlight;
  const reviewSignals: string[] = data.metadata?.review_signals || [];
  const branchExplanation: string = data.metadata?.branch_explanation || data.metadata?.why || '';
  const dbQuery: any = data.metadata?.db_query;

  // Deduplicate: if specific status exists, skip generic raises
  const filteredSignals = reviewSignals.filter(
    (s) => !(s === 'raises' && (reviewSignals.includes('raises_4xx') || reviewSignals.includes('raises_5xx'))),
  );

  // Build subtitle: input → output
  let subtitle = '';
  if (output && outputType) {
    subtitle = `${output}: ${outputType}`;
  } else if (output) {
    subtitle = `\u2192 ${output}`;
  } else if (outputType) {
    subtitle = outputType;
  }

  return (
    <div
      className={[
        'df-node',
        isSelected && 'selected',
        isOriginHighlight && 'origin-highlight',
        pathState === 'verified' && 'hit',
        pathState === 'unverified' && 'df-unverified',
        pathState === 'runtime-only' && 'df-runtime-only',
        hasTrace && pathState === 'possible' && !isHit && !hitUnknown && 'dimmed',
      ].filter(Boolean).join(' ')}
      style={{
        borderColor: isSelected ? 'var(--vscode-focusBorder)' : color,
        marginLeft: depth > 0 ? 8 : 0,
      }}
    >
      <div className="df-color-bar" style={{ background: color }} />
      {isHit && !hitUnknown && <div className="node-hit-glow" />}
      {hitUnknown && <div className="df-unknown-badge" title="Runtime hit unknown — no trace data for this step">?</div>}
      {isOriginHighlight && <div className="df-origin-badge" title="Contributes to response">ORIGIN</div>}
      <div className="df-op-badge" style={{ color }}>
        {opLabel}
        {branchPath && <span className="df-branch-path"> ({branchPath})</span>}
        {depth > 0 && <span className="df-depth-badge">d{depth}</span>}
        {pathState === 'unverified' && <span className="path-state-badge unverified" style={{ marginLeft: 4 }} title="Not executed in trace">UNTESTED</span>}
        {pathState === 'runtime-only' && <span className="path-state-badge runtime-only" style={{ marginLeft: 4 }} title="Only seen at runtime">RUNTIME</span>}
        {filteredSignals.map((sig) => {
          const cfg = SIGNAL_BADGES[sig];
          if (!cfg) return null;
          return (
            <span
              key={sig}
              className="review-badge"
              style={{ background: cfg.color, marginLeft: 4 }}
              title={cfg.title}
            >
              {cfg.label}
            </span>
          );
        })}
      </div>
      <div className="df-label">{label}</div>
      {op === 'branch' && branchExplanation && (
        <div className="df-why">{branchExplanation}</div>
      )}
      {subtitle && <div className="df-subtitle">{subtitle}</div>}
      {op === 'query' && dbQuery && (
        <div className="df-db-detail">
          {dbQuery.model || dbQuery.table || ''}
          {dbQuery.filters?.length > 0 && (
            <span className="df-db-filter">
              {' '}| filter: {dbQuery.filters.slice(0, 2).map((f: any) => f.column || f.expr?.slice(0, 15) || '').join(', ')}
            </span>
          )}
          {dbQuery.joins?.length > 0 && (
            <span className="df-db-join"> | join: {dbQuery.joins.slice(0, 2).join(', ')}</span>
          )}
        </div>
      )}
      {errorLabel && (
        <div className="df-error">
          fail \u2192 {errorLabel}
        </div>
      )}
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
      {errorLabel && (
        <Handle type="source" position={Position.Bottom} id="error" style={{ background: '#e74c3c' }} />
      )}
    </div>
  );
}
