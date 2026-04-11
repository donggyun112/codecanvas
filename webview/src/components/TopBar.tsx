import React, { useMemo } from 'react';
import { useFlowStore } from '../store/useFlowStore';
import { postMessage } from '../vscode';
import type { FlowGraph } from '../types/flow';

const LEVEL_NAMES: Record<number, string> = {
  0: 'Overview',
  1: 'Pipeline',
  2: 'Functions',
  3: 'Logic',
};

const FUNC_LEVEL_NAMES: Record<number, string> = {
  0: 'Selected',
  1: 'Context',
  2: 'Functions',
  3: 'Logic',
};

export default function TopBar() {
  const {
    currentLevel,
    setLevel,
    viewMode,
    setViewMode,
    flowViewMode,
    setFlowViewMode,
    dataFlowDetail,
    setDataFlowDetail,
    hasTrace,
    isFunctionContext,
    historyTrail,
    flowData,
  } = useFlowStore();

  const names = isFunctionContext ? FUNC_LEVEL_NAMES : LEVEL_NAMES;
  const canGoBack = historyTrail.length > 1;
  const entrypoint = flowData?.entrypoint;

  return (
    <div className="topbar">
      <div className="topbar-main">
        {canGoBack && (
          <button
            className="back-btn"
            onClick={() => postMessage({ type: 'navigateHistory', direction: 'back' })}
          >
            &larr; Back
          </button>
        )}
        <div className="breadcrumb">
          {historyTrail.map((item, index) => {
            const isCurrent = index === historyTrail.length - 1;
            return (
              <React.Fragment key={item.index}>
                {index > 0 && <span className="breadcrumb-sep">&rsaquo;</span>}
                <button
                  className={`breadcrumb-item ${isCurrent ? 'current' : ''}`}
                  disabled={isCurrent}
                  onClick={() =>
                    !isCurrent && postMessage({ type: 'navigateHistory', targetIndex: item.index })
                  }
                >
                  {item.label}
                </button>
              </React.Fragment>
            );
          })}
        </div>
      </div>
      <div className="topbar-meta">
        {flowViewMode === 'callstack' && (
          <>
            <label>Level:</label>
            <input
              type="range"
              min={0}
              max={3}
              step={1}
              value={currentLevel}
              onChange={(e) => setLevel(parseInt(e.target.value))}
            />
            <span className="level-label">{names[currentLevel] || `L${currentLevel}`}</span>
          </>
        )}
        <span className="view-toggle">
          {([
            ['brief', 'Brief'],
            ['codeflow', 'Code Flow'],
            ['data', 'Data Flow'],
            ['callstack', 'Call Stack'],
            ['cfg', 'CFG'],
          ] as const).map(([mode, label]) => (
            <button
              key={mode}
              className={flowViewMode === mode ? 'active' : ''}
              onClick={() => setFlowViewMode(mode as any)}
            >
              {label}
            </button>
          ))}
        </span>
        {flowViewMode === 'data' && (
          <span className="view-toggle">
            {([
              ['summary', 'Summary'],
              ['detail', 'Detail'],
            ] as const).map(([mode, label]) => (
              <button
                key={mode}
                className={dataFlowDetail === mode ? 'active' : ''}
                onClick={() => setDataFlowDetail(mode as any)}
              >
                {label}
              </button>
            ))}
          </span>
        )}
        {hasTrace && (
          <span className="view-toggle">
            {([
              ['all', 'All'],
              ['runtime', 'Verified'],
              ['static', 'Unverified'],
            ] as const).map(([mode, label]) => (
              <button
                key={mode}
                className={viewMode === mode ? 'active' : ''}
                onClick={() => setViewMode(mode as any)}
              >
                {label}
              </button>
            ))}
          </span>
        )}
        {hasTrace && flowData && (
          <CoverageStats flowData={flowData} />
        )}
        {entrypoint && (
          <span className="endpoint-badge">
            {entrypoint.kind === 'api' ? (
              <>
                <span className={`method-${entrypoint.method}`}>
                  {entrypoint.method}{' '}
                </span>
                {entrypoint.path}
              </>
            ) : (
              <>
                <span className="kind-badge">{entrypoint.kind}</span>{' '}
                {entrypoint.label || entrypoint.handler_name}
              </>
            )}
            <EndpointRisk metadata={entrypoint.metadata} />
          </span>
        )}
      </div>
      {entrypoint?.metadata?.review_summary && (
        <ReviewSummaryBar
          summary={entrypoint.metadata.review_summary}
          narrative={entrypoint.metadata.flow_narrative}
        />
      )}
    </div>
  );
}

function ReviewSummaryBar({ summary, narrative }: { summary: any; narrative?: string }) {
  const concerns: any[] = summary.concerns || [];
  const focusAreas: any[] = summary.focusAreas || [];

  if (concerns.length === 0 && focusAreas.length === 0 && !narrative) return null;

  const severityColor: Record<string, string> = {
    high: '#e74c3c',
    medium: '#f39c12',
    low: '#27ae60',
  };

  return (
    <div className="review-summary-bar">
      {narrative && (
        <div className="flow-narrative">{narrative}</div>
      )}
      {concerns.map((c: any, i: number) => (
        <span
          key={i}
          className="review-concern"
          title={c.label}
        >
          <span className="concern-dot" style={{ background: severityColor[c.severity] || '#666' }} />
          {c.label}
        </span>
      ))}
      {focusAreas.length > 0 && (
        <span className="review-focus">
          Focus: {focusAreas.map((f: any) => `${f.name} (${f.level})`).join(', ')}
        </span>
      )}
    </div>
  );
}

function CoverageStats({ flowData }: { flowData: FlowGraph }) {
  const stats = useMemo(() => {
    let verified = 0;
    let unverified = 0;
    let runtimeOnly = 0;
    const l3Nodes = Object.values(flowData.nodes).filter((n) => n.level === 3);
    for (const n of l3Nodes) {
      if (n.confidence === 'runtime') runtimeOnly++;
      else if (n.metadata?.runtime_hit) verified++;
      else unverified++;
    }
    const total = l3Nodes.length;
    const pct = total > 0 ? Math.round((verified / total) * 100) : 0;
    return { verified, unverified, runtimeOnly, total, pct };
  }, [flowData]);

  if (stats.total === 0) return null;

  return (
    <span className="coverage-stats" title={`${stats.verified} verified, ${stats.unverified} unverified, ${stats.runtimeOnly} runtime-only out of ${stats.total} functions`}>
      <span className="coverage-verified">{stats.verified}</span>
      <span className="coverage-sep">/</span>
      <span className="coverage-total">{stats.total}</span>
      <span className="coverage-pct">({stats.pct}%)</span>
    </span>
  );
}

const RISK_COLORS: Record<string, string> = {
  critical: '#c0392b',
  high: '#e67e22',
  medium: '#f1c40f',
  low: '#27ae60',
};

function EndpointRisk({ metadata }: { metadata?: Record<string, any> }) {
  const score = metadata?.risk_score;
  const level = metadata?.risk_level;
  if (!score || !level) return null;
  const color = RISK_COLORS[level] || '#666';
  const breakdown = metadata?.risk_breakdown || [];
  const title = breakdown.map((b: any) => `${b.node}: ${b.score} (${b.level})`).join('\n');
  return (
    <span
      className="endpoint-risk"
      style={{ background: color }}
      title={`Risk ${score} (${level})\n${title}`}
    >
      RISK {score}
    </span>
  );
}
