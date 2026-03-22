import React, { useEffect } from 'react';
import { useFlowStore } from '../store/useFlowStore';
import { postMessage } from '../vscode';
import CodePreview from './CodePreview';
import ConnectionList from './ConnectionList';
import { maxDrillDepth, getFunctionFlowTarget, getNestedCallTargets } from '../transform/drillState';
import { getVisible } from '../transform/visibility';
import type { FlowNodeData } from '../types/flow';

function shortPath(p: string): string {
  const parts = p.split('/');
  return parts.slice(-2).join('/');
}

function getPrimaryLocation(node: FlowNodeData): { filePath: string; line: number; kind: string } | null {
  if (node.filePath) {
    return { filePath: node.filePath, line: node.lineStart || 1, kind: 'definition' };
  }
  const ev = node.evidence?.find((e) => e.filePath && e.lineNumber);
  if (ev) {
    return { filePath: ev.filePath!, line: ev.lineNumber!, kind: 'callsite' };
  }
  return null;
}

function getExtraLocations(node: FlowNodeData) {
  const primary = getPrimaryLocation(node);
  const seen: Record<string, boolean> = {};
  const results: Array<{ filePath: string; line: number }> = [];
  (node.evidence || []).forEach((ev) => {
    if (!ev.filePath || !ev.lineNumber) return;
    const key = `${ev.filePath}:${ev.lineNumber}`;
    if (seen[key]) return;
    seen[key] = true;
    results.push({ filePath: ev.filePath, line: ev.lineNumber });
  });
  if (!primary) return results.slice(0, 3);
  return results
    .filter((loc) => !(loc.filePath === primary.filePath && loc.line === primary.line))
    .slice(0, 3);
}

export default function DetailPanel() {
  const {
    selectedNodeId,
    flowData,
    hasTrace,
    nodeDrillState,
    advanceDrill,
    setDrillState,
    selectNode,
    currentLevel,
    viewMode,
    isFunctionContext,
    codePreviewCache,
    updateCodePreview,
    nextPreviewSeq,
  } = useFlowStore();

  const node = flowData?.nodes[selectedNodeId || ''];

  // Listen for code preview responses
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;
      if (msg?.type === 'codePreview' && msg.cacheKey) {
        updateCodePreview(msg.cacheKey, msg);
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [updateCodePreview]);

  if (!node || !flowData) {
    return <div className="detail-panel" />;
  }

  const drillMax = maxDrillDepth(flowData, node);
  const primaryLocation = getPrimaryLocation(node);
  const extraLocations = getExtraLocations(node);
  const functionFlowTarget = getFunctionFlowTarget(flowData, node);
  const nestedCallTargets = getNestedCallTargets(node);

  // Code preview
  let cacheKey: string | null = null;
  if (primaryLocation) {
    const endLine =
      primaryLocation.kind === 'definition'
        ? node.lineEnd || node.lineStart || primaryLocation.line
        : primaryLocation.line;
    cacheKey = [node.id, primaryLocation.kind, primaryLocation.filePath, primaryLocation.line, endLine].join('|');

    if (!codePreviewCache[cacheKey]) {
      const seq = nextPreviewSeq();
      updateCodePreview(cacheKey, { loading: true, nodeId: node.id });
      postMessage({
        type: 'loadCodePreview',
        requestId: seq,
        cacheKey,
        nodeId: node.id,
        filePath: primaryLocation.filePath,
        lineStart: primaryLocation.line,
        lineEnd: endLine,
        locationKind: primaryLocation.kind,
      });
    }
  }

  const vis = getVisible(flowData, currentLevel, viewMode, isFunctionContext, hasTrace, nodeDrillState);

  return (
    <div className="detail-panel visible">
      <h3>{node.displayName || node.name}</h3>

      {/* Drill actions */}
      {drillMax > 0 && (
        <>
          <div className="action-label">Actions</div>
          <div className="inline-actions">
            <button
              className="action-btn primary"
              onClick={() => {
                advanceDrill(node, false);
              }}
            >
              Expand Logic
            </button>
            <button
              className="action-btn secondary"
              onClick={() => {
                setDrillState(node.id, 0);
              }}
            >
              Collapse Logic
            </button>
          </div>
        </>
      )}

      {/* Function flow */}
      {functionFlowTarget && (
        <>
          {drillMax <= 0 && <div className="action-label">Actions</div>}
          <div className="inline-actions">
            <button
              className="action-btn primary"
              onClick={() =>
                postMessage({
                  type: 'openFunctionFlow',
                  filePath: functionFlowTarget.filePath,
                  line: functionFlowTarget.line,
                })
              }
            >
              Open Function Flow
            </button>
          </div>
        </>
      )}

      {/* Nested call targets */}
      {nestedCallTargets.length > 0 && (
        <>
          <div className="action-label">Follow Calls</div>
          <div className="inline-actions">
            {nestedCallTargets.map((target, i) => (
              <button
                key={i}
                className="action-btn primary"
                onClick={() =>
                  postMessage({
                    type: 'openFunctionFlow',
                    filePath: target.filePath,
                    line: target.line,
                  })
                }
              >
                {target.label}
              </button>
            ))}
          </div>
        </>
      )}

      {/* Metadata sections */}
      <Section title="Type" value={node.type} />
      <Section title="Abstraction" value={`L${node.level}`} />
      <Section title="Confidence" value={node.confidence} />

      {node.metadata?.pipeline_phase && (
        <Section
          title="Phase"
          value={
            node.metadata.pipeline_phase +
            (node.metadata.pipeline_order != null ? ` #${node.metadata.pipeline_order}` : '')
          }
        />
      )}

      {node.metadata?.context_root && <Section title="Context" value="Selected function" />}
      {!node.metadata?.context_root && node.metadata?.upstream_distance != null && (
        <Section title="Context" value={`Caller depth ${node.metadata.upstream_distance}`} />
      )}

      {node.metadata?.return_type && <Section title="Returns" value={node.metadata.return_type} />}

      {(node.metadata?.dependency_param || node.metadata?.declared_type) && (
        <Section
          title="Injects"
          value={`${node.metadata.dependency_param || 'value'}${node.metadata.declared_type ? ': ' + node.metadata.declared_type : ''}`}
        />
      )}

      {node.metadata?.contract_type && (
        <Section
          title="Contract"
          value={`${node.metadata.contract_type}${node.metadata.contract_kind ? ` (${node.metadata.contract_kind})` : ''}`}
        />
      )}

      {node.metadata?.bound_implementation && (
        <Section title="Bound To" value={node.metadata.bound_implementation} />
      )}

      {node.metadata?.is_protocol && <Section title="Contract Role" value="Protocol" />}
      {!node.metadata?.is_protocol && node.metadata?.is_abstract && (
        <Section title="Contract Role" value="Abstract" />
      )}

      {node.confidence === 'inferred' && (
        <Section
          title="Resolution"
          value="Call site was found, but the target definition could not be resolved statically"
        />
      )}

      {node.description && <Section title="Description" value={node.description} />}

      {drillMax > 0 && (
        <Section title="Layer" value={`${nodeDrillState[node.id] || 0} / ${drillMax}`} />
      )}

      {hasTrace && node.metadata && (
        <>
          {node.metadata.runtime_hit ? (
            <Section
              title="Runtime"
              value={`Hit #${node.metadata.execution_order || '?'}${
                node.metadata.duration_ms != null
                  ? ` | ${node.metadata.duration_ms.toFixed(2)} ms`
                  : ''
              }`}
            />
          ) : (
            <Section title="Runtime" value="Not executed" />
          )}
          {node.metadata.runtime_exception && (
            <Section title="Exception" value={node.metadata.runtime_exception} />
          )}
        </>
      )}

      {/* Location */}
      {primaryLocation && (
        <div className="detail-section">
          <div className="detail-section-title">
            {primaryLocation.kind === 'callsite' ? 'Call Site' : 'Location'}
          </div>
          <span
            className="nav-link"
            onClick={() =>
              postMessage({
                type: 'navigateToCode',
                filePath: primaryLocation.filePath,
                line: primaryLocation.line,
              })
            }
          >
            {shortPath(primaryLocation.filePath)}:{primaryLocation.line}
          </span>
        </div>
      )}

      {/* Code preview */}
      {cacheKey && <CodePreview cacheKey={cacheKey} />}

      {/* Observed at */}
      {extraLocations.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">Observed At</div>
          {extraLocations.map((loc, i) => (
            <div key={i} className="evidence-item">
              <span
                className="nav-link"
                onClick={() =>
                  postMessage({ type: 'navigateToCode', filePath: loc.filePath, line: loc.line })
                }
              >
                {shortPath(loc.filePath)}:{loc.line}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Connections */}
      <ConnectionList
        node={node}
        flowData={flowData}
        visibleNodeMap={vis.nodeMap}
        hasTrace={hasTrace}
      />

      {/* Evidence */}
      {node.evidence && node.evidence.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">Evidence</div>
          {node.evidence.map((ev, i) => (
            <div key={i} className="evidence-item">
              <strong>{ev.source}</strong>: {ev.detail}
              {ev.filePath && (
                <span
                  className="nav-link"
                  onClick={() =>
                    postMessage({
                      type: 'navigateToCode',
                      filePath: ev.filePath!,
                      line: ev.lineNumber || 1,
                    })
                  }
                >
                  {' '}
                  Go to code
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Section({ title, value }: { title: string; value: string }) {
  return (
    <div className="detail-section">
      <div className="detail-section-title">{title}</div>
      <div className="detail-section-value">{value}</div>
    </div>
  );
}
