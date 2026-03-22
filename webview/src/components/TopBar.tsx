import React from 'react';
import { useFlowStore } from '../store/useFlowStore';
import { postMessage } from '../vscode';

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
        {hasTrace && (
          <span className="view-toggle">
            {(['all', 'runtime', 'static'] as const).map((mode) => (
              <button
                key={mode}
                className={viewMode === mode ? 'active' : ''}
                onClick={() => setViewMode(mode)}
              >
                {mode.charAt(0).toUpperCase() + mode.slice(1)}
              </button>
            ))}
          </span>
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
          </span>
        )}
      </div>
    </div>
  );
}
