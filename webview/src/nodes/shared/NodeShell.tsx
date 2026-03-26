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
  const pathState = data.pathState || 'possible';
  const isOriginHighlight = data.isOriginHighlight;
  const displayName = data.displayName || data.name || '';
  const isFunctionLike =
    data.type === 'function' || data.type === 'method' || data.type === 'class';

  const filePath = data.filePath;
  const lineStart = data.lineStart;

  // Callsite from evidence
  let locationText = '';
  let locationKind = '';
  if (filePath && lineStart) {
    locationText = `${shortPath(filePath)}:${lineStart}`;
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
      className={[
        'node-shell',
        isSelected && 'selected',
        data.metadata?.change_impact?.changed && 'changed-node',
        isOriginHighlight && 'origin-highlight',
        pathState === 'verified' && 'hit',
        pathState === 'unverified' && 'unverified-path',
        pathState === 'runtime-only' && 'runtime-only-path',
        hasTrace && pathState === 'possible' && !isHit && 'dimmed',
        isUnresolved && 'unresolved',
      ].filter(Boolean).join(' ')}
      style={{
        borderColor: isSelected ? 'var(--vscode-focusBorder)' : typeColor,
        background: isUpstream && !isContextRoot ? 'rgba(138, 164, 255, 0.08)' : undefined,
      }}
    >
      <div className="node-color-bar" style={{ background: typeColor }} />
      {pathState === 'verified' && <div className="node-hit-glow" />}
      <div className="node-type-label" style={{ color: typeColor }}>
        {data.type.replace(/_/g, ' ').toUpperCase()}
        {pathState === 'unverified' && <span className="path-state-badge unverified" title="Not executed in trace">UNTESTED</span>}
        {pathState === 'runtime-only' && <span className="path-state-badge runtime-only" title="Only seen at runtime">RUNTIME</span>}
      </div>
      <div className={`node-name ${isFunctionLike ? '' : 'bold'}`}>
        {displayName.length > 35 ? displayName.slice(0, 35) + '...' : displayName}
        <RiskBadge score={data.metadata?.risk_score} level={data.metadata?.risk_level} />
      </div>
      {locationText && (
        <div className="node-location">{locationText}</div>
      )}
      {data.metadata?.change_impact?.changed && (
        <div className="review-signals">
          <span className="review-badge" style={{ background: '#e74c3c' }} title="Modified in diff">CHANGED</span>
        </div>
      )}
      <ReviewSignalBadges signals={data.metadata?.review_signals} />
      {data.metadata?.return_type && (
        <div className="node-return-type">
          &rarr; {data.metadata.return_type}
        </div>
      )}
      {isHit && data.metadata?.execution_order && (
        <div className="node-order-badge">{data.metadata.execution_order}</div>
      )}
      {children}
      <Handle type="target" position={Position.Top} id="top" />
      <Handle type="source" position={Position.Bottom} id="bottom" />
      <Handle type="target" position={Position.Left} id="left" />
      <Handle type="source" position={Position.Right} id="right" />
    </div>
  );
}

const SIGNAL_CONFIG: Record<string, { label: string; color: string; title: string }> = {
  db_write: { label: 'DB\u2009W', color: '#9b59b6', title: 'Database write operation' },
  db_read: { label: 'DB\u2009R', color: '#8e44ad', title: 'Database read operation' },
  http_call: { label: 'HTTP', color: '#e67e22', title: 'External HTTP call' },
  raises: { label: 'ERR', color: '#e74c3c', title: 'Raises exception' },
  raises_4xx: { label: '4xx', color: '#e74c3c', title: 'Raises client error (4xx)' },
  raises_5xx: { label: '5xx', color: '#c0392b', title: 'Raises server error (5xx)' },
  auth: { label: 'AUTH', color: '#f1c40f', title: 'Authentication / authorization' },
  io: { label: 'I/O', color: '#3498db', title: 'I/O operation' },
};

function ReviewSignalBadges({ signals }: { signals?: string[] }) {
  if (!signals || signals.length === 0) return null;
  // Deduplicate: if raises_4xx or raises_5xx exists, skip generic raises
  const filtered = signals.filter(
    (s) => !(s === 'raises' && (signals.includes('raises_4xx') || signals.includes('raises_5xx'))),
  );
  return (
    <div className="review-signals">
      {filtered.map((sig) => {
        const cfg = SIGNAL_CONFIG[sig];
        if (!cfg) return null;
        return (
          <span
            key={sig}
            className="review-badge"
            style={{ background: cfg.color }}
            title={cfg.title}
          >
            {cfg.label}
          </span>
        );
      })}
    </div>
  );
}

const RISK_COLORS: Record<string, string> = {
  critical: '#c0392b',
  high: '#e67e22',
  medium: '#f1c40f',
  low: '#27ae60',
};

function RiskBadge({ score, level }: { score?: number; level?: string }) {
  if (!score || !level) return null;
  const color = RISK_COLORS[level] || '#666';
  return (
    <span
      className="risk-badge"
      style={{ background: color }}
      title={`Risk: ${score} (${level})`}
    >
      {score}
    </span>
  );
}
