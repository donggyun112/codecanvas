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

  // Check if selected is a synthetic node (*.df.*, es.*, or bb.*)
  const isSyntheticStep = !node && selectedNodeId != null && (selectedNodeId.includes('.df.') || selectedNodeId.startsWith('es.'));
  const isCFGBlock = !node && selectedNodeId != null && selectedNodeId.startsWith('bb.');
  const rfNode = useFlowStore((s) => s.rfNodes.find((n) => n.id === selectedNodeId));
  const dfStepData = isSyntheticStep ? (rfNode?.data as any) : null;
  const cfgBlockData = isCFGBlock ? (rfNode?.data as any) : null;

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

  // CFG block detail
  if (isCFGBlock && cfgBlockData) {
    const stmts: any[] = cfgBlockData.statements || [];
    const pathState = cfgBlockData.pathState || 'possible';
    const blockKind = cfgBlockData.kind || 'block';

    return (
      <div className="detail-panel visible">
        <h3>{cfgBlockData.label || `Block ${selectedNodeId}`}</h3>
        <Section title="Kind" value={blockKind.replace(/_/g, ' ').toUpperCase()} />
        {cfgBlockData.hasTrace && (
          <Section title="Path State" value={pathState === 'verified' ? 'Executed' : pathState === 'unverified' ? 'Not executed' : 'Unknown'} />
        )}
        {stmts.length > 0 && (
          <div className="detail-section">
            <div className="detail-section-title">Statements</div>
            {stmts.map((s: any, i: number) => (
              <div key={i} style={{ fontSize: 11, fontFamily: 'var(--vscode-editor-font-family, monospace)', marginBottom: 2, display: 'flex', gap: 6 }}>
                <span style={{ opacity: 0.3, minWidth: 24, textAlign: 'right' }}>{s.line}</span>
                <span
                  className="nav-link"
                  onClick={() => cfgBlockData.filePath && postMessage({ type: 'navigateToCode', filePath: cfgBlockData.filePath, line: s.line })}
                >
                  {s.text}
                </span>
              </div>
            ))}
          </div>
        )}
        {cfgBlockData.filePath && cfgBlockData.lineStart && (
          <div className="detail-section">
            <div className="detail-section-title">Location</div>
            <span
              className="nav-link"
              onClick={() => postMessage({ type: 'navigateToCode', filePath: cfgBlockData.filePath, line: cfgBlockData.lineStart })}
            >
              {cfgBlockData.filePath.split('/').slice(-2).join('/')}:{cfgBlockData.lineStart}
            </span>
          </div>
        )}
      </div>
    );
  }

  // Synthetic step detail (DataFlowStep or ExecutionStep)
  if (isSyntheticStep && dfStepData) {
    const op = dfStepData.operation || '';
    const inputs = dfStepData.inputs || [];
    const output = dfStepData.output || '';
    const outputType = dfStepData.outputType || '';
    const errorLabel = dfStepData.errorLabel || '';
    const calleeFunc = dfStepData.calleeFunction || '';

    // Code preview for execution steps
    let stepCacheKey: string | null = null;
    if (dfStepData.filePath && dfStepData.lineStart) {
      const endLine = dfStepData.lineEnd || dfStepData.lineStart;
      stepCacheKey = [selectedNodeId, 'exec', dfStepData.filePath, dfStepData.lineStart, endLine].join('|');
      if (!codePreviewCache[stepCacheKey]) {
        const seq = nextPreviewSeq();
        updateCodePreview(stepCacheKey, { loading: true, nodeId: selectedNodeId });
        postMessage({
          type: 'loadCodePreview',
          requestId: seq,
          cacheKey: stepCacheKey,
          nodeId: selectedNodeId,
          filePath: dfStepData.filePath,
          lineStart: dfStepData.lineStart,
          lineEnd: endLine,
          locationKind: 'definition',
        });
      }
    }

    // Build step connections from execution graph links
    const eg = flowData?.executionGraph;
    const incomingLinks = eg?.links.filter((l) => l.targetStepId === selectedNodeId) || [];
    const outgoingLinks = eg?.links.filter((l) => l.sourceStepId === selectedNodeId) || [];
    const stepById: Record<string, { label: string; operation: string }> = {};
    eg?.steps.forEach((s) => { stepById[s.id] = { label: s.label, operation: s.operation }; });

    // Resolve callee location from FlowGraph nodes
    const calleeNode = calleeFunc && flowData
      ? Object.values(flowData.nodes).find(
          (n) => n.id === calleeFunc || n.name === calleeFunc.split('.').pop(),
        )
      : null;

    return (
      <div className="detail-panel visible">
        <h3>{dfStepData.label}</h3>

        {/* Callee navigation */}
        {calleeNode?.filePath && (
          <>
            <div className="action-label">Actions</div>
            <div className="inline-actions">
              <button
                className="action-btn primary"
                onClick={() =>
                  postMessage({
                    type: 'openFunctionFlow',
                    filePath: calleeNode.filePath,
                    line: calleeNode.lineStart || 1,
                  })
                }
              >
                Open {calleeFunc.split('.').pop()} Flow
              </button>
            </div>
          </>
        )}

        <Section title="Operation" value={op.toUpperCase()} />
        {dfStepData.metadata?.why && <Section title="Why" value={dfStepData.metadata.why} />}
        {dfStepData.phase && <Section title="Phase" value={dfStepData.phase} />}
        {dfStepData.depth != null && dfStepData.depth > 0 && <Section title="Depth" value={String(dfStepData.depth)} />}
        {dfStepData.scope && <Section title="Scope" value={dfStepData.scope} />}
        {inputs.length > 0 && <Section title="Inputs" value={inputs.join(', ')} />}
        {output && <Section title="Output" value={outputType ? `${output}: ${outputType}` : output} />}
        {errorLabel && <Section title="Error Path" value={`fail \u2192 ${errorLabel}`} />}
        {calleeFunc && <Section title="Callee" value={calleeFunc} />}
        {dfStepData.branchCondition && <Section title="Condition" value={dfStepData.branchCondition} />}
        {dfStepData.metadata?.branch_explanation && (
          <Section title="Explanation" value={dfStepData.metadata.branch_explanation} />
        )}
        {dfStepData.metadata?.db_query && (
          <div className="detail-section">
            <div className="detail-section-title">Query Detail</div>
            <div style={{ fontSize: 11, fontFamily: 'var(--vscode-editor-font-family, monospace)' }}>
              {dfStepData.metadata.db_query.model && <div>Model: {dfStepData.metadata.db_query.model}</div>}
              {dfStepData.metadata.db_query.table && <div>Table: {dfStepData.metadata.db_query.table}</div>}
              {dfStepData.metadata.db_query.operation && <div>Op: {dfStepData.metadata.db_query.operation}</div>}
              {dfStepData.metadata.db_query.filters?.length > 0 && (
                <div>Filters: {dfStepData.metadata.db_query.filters.map((f: any) => f.column || f.expr || '').join(', ')}</div>
              )}
              {dfStepData.metadata.db_query.joins?.length > 0 && (
                <div>Joins: {dfStepData.metadata.db_query.joins.join(', ')}</div>
              )}
              {dfStepData.metadata.db_query.order_by?.length > 0 && (
                <div>Order: {dfStepData.metadata.db_query.order_by.join(', ')}</div>
              )}
              {dfStepData.metadata.db_query.raw_sql && (
                <div style={{ marginTop: 4, opacity: 0.7, whiteSpace: 'pre-wrap' }}>SQL: {dfStepData.metadata.db_query.raw_sql}</div>
              )}
            </div>
          </div>
        )}
        {dfStepData.confidence && dfStepData.confidence !== 'definite' && (
          <Section title="Confidence" value={dfStepData.confidence} />
        )}
        {dfStepData.evidence && (
          <Section title="Evidence" value={dfStepData.evidence} />
        )}

        {/* Runtime info */}
        {dfStepData.hasTrace && (
          <Section
            title="Runtime"
            value={dfStepData.isHit ? 'Executed' : dfStepData.hitUnknown ? 'Unknown' : 'Not executed'}
          />
        )}

        {/* Location */}
        {dfStepData.filePath && dfStepData.lineStart && (
          <div className="detail-section">
            <div className="detail-section-title">Location</div>
            <span
              className="nav-link"
              onClick={() =>
                postMessage({ type: 'navigateToCode', filePath: dfStepData.filePath, line: dfStepData.lineStart })
              }
            >
              {dfStepData.filePath.split('/').slice(-2).join('/')}:{dfStepData.lineStart}
            </span>
          </div>
        )}

        {/* Code preview */}
        {stepCacheKey && <CodePreview cacheKey={stepCacheKey} />}

        {/* Response origin chain */}
        {op === 'respond' && dfStepData.metadata?.response_origins?.length > 0 && (
          <div className="detail-section">
            <div className="detail-section-title">Response Origin</div>
            {dfStepData.metadata.return_expression && (
              <div style={{ fontSize: 11, fontFamily: 'var(--vscode-editor-font-family, monospace)', opacity: 0.6, marginBottom: 6 }}>
                return {dfStepData.metadata.return_expression}
              </div>
            )}
            {(dfStepData.metadata.response_origins as any[]).map((origin: any) => (
              <div
                key={origin.stepId}
                className="nav-link"
                style={{ fontSize: 12, marginBottom: 3, display: 'flex', alignItems: 'center', gap: 4 }}
                onClick={() => selectNode(origin.stepId)}
              >
                <span className="origin-op-badge" data-op={origin.operation}>
                  {origin.operation.toUpperCase().slice(0, 5)}
                </span>
                <span>{origin.label}</span>
                <span style={{ opacity: 0.4, fontSize: 10 }}>via {origin.variable}</span>
              </div>
            ))}
          </div>
        )}

        {/* Connections */}
        {(incomingLinks.length > 0 || outgoingLinks.length > 0) && (
          <div className="detail-section">
            <div className="detail-section-title">Connections</div>
            {incomingLinks.length > 0 && (
              <div style={{ marginBottom: 6 }}>
                <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 2 }}>Incoming</div>
                {incomingLinks.map((l) => {
                  const src = stepById[l.sourceStepId];
                  return (
                    <div
                      key={l.id}
                      className="nav-link"
                      style={{ fontSize: 12, marginBottom: 2 }}
                      onClick={() => selectNode(l.sourceStepId)}
                    >
                      {src?.label || l.sourceStepId}
                      {l.variable && <span style={{ opacity: 0.5 }}> ({l.variable})</span>}
                      {l.kind !== 'sequence' && <span style={{ opacity: 0.5 }}> [{l.kind}]</span>}
                    </div>
                  );
                })}
              </div>
            )}
            {outgoingLinks.length > 0 && (
              <div>
                <div style={{ fontSize: 10, opacity: 0.6, marginBottom: 2 }}>Outgoing</div>
                {outgoingLinks.map((l) => {
                  const tgt = stepById[l.targetStepId];
                  return (
                    <div
                      key={l.id}
                      className="nav-link"
                      style={{ fontSize: 12, marginBottom: 2 }}
                      onClick={() => selectNode(l.targetStepId)}
                    >
                      {tgt?.label || l.targetStepId}
                      {l.variable && <span style={{ opacity: 0.5 }}> ({l.variable})</span>}
                      {l.kind !== 'sequence' && <span style={{ opacity: 0.5 }}> [{l.kind}]</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

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

      {/* Change impact */}
      {node.metadata?.change_impact?.changed && (
        <div className="detail-section">
          <div className="detail-section-title">Change Impact</div>
          <div style={{ fontSize: 12, color: '#e74c3c', fontWeight: 600 }}>
            Modified in diff
          </div>
          {(node.metadata.change_impact.hunks || []).map((h: any, i: number) => (
            <div key={i} style={{ fontSize: 11, opacity: 0.7 }}>
              Lines {h.startLine}-{h.endLine}
            </div>
          ))}
        </div>
      )}

      {/* Risk score */}
      {node.metadata?.risk_score != null && (
        <div className="detail-section">
          <div className="detail-section-title">Risk</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span
              className="risk-badge"
              style={{ background: ({ critical: '#c0392b', high: '#e67e22', medium: '#f1c40f', low: '#27ae60' } as any)[node.metadata.risk_level] || '#666' }}
            >
              {node.metadata.risk_score}
            </span>
            <span style={{ fontSize: 12, textTransform: 'capitalize' }}>{node.metadata.risk_level}</span>
          </div>
          {(node.metadata.risk_factors || []).map((f: any, i: number) => (
            <div key={i} style={{ fontSize: 11, opacity: 0.7 }}>
              {f.points > 0 ? `+${f.points}` : ''} {f.factor}{f.detail ? ` (${f.detail})` : ''}
            </div>
          ))}
        </div>
      )}

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
