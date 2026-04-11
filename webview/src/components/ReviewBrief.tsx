import React from 'react';
import { useFlowStore } from '../store/useFlowStore';
import { postMessage } from '../vscode';

export default function ReviewBrief() {
  const { flowData } = useFlowStore();
  if (!flowData) return null;

  const ep = flowData.entrypoint;
  const meta = ep?.metadata || {};
  const narrative = meta.flow_narrative || '';
  const review = meta.review_summary || {};
  const concerns: any[] = review.concerns || [];
  const focusAreas: any[] = review.focusAreas || [];
  const riskScore = meta.risk_score;
  const riskLevel = meta.risk_level;
  // Project canonical nodes once for all review-brief sections.
  // Use L4 detail (kind=exec_l4) so per-step "why"/db_query/response_origins
  // remain available, since merge_to_l3 may collapse them.
  const allNodes = Object.values(flowData.nodes);
  const cfgBlocks = allNodes.filter((n) => n.kind === 'cfg_block');
  const execSteps = allNodes.filter((n) => n.kind === 'exec_l4');

  // Collect branch explanations from CFG blocks
  const branches: { condition: string; explanation: string; line?: number }[] = [];
  for (const block of cfgBlocks) {
    const expl = block.metadata?.branch_explanation;
    if (!expl) continue;
    const statements = (block.metadata?.statements as any[]) || [];
    const stmt = statements.find((s: any) => s.kind === 'branch_test');
    branches.push({
      condition: stmt?.text || '',
      explanation: expl,
      line: stmt?.line,
    });
  }

  // Collect step annotations with "why"
  const keySteps: { op: string; label: string; why: string; filePath?: string; line?: number }[] = [];
  for (const step of execSteps) {
    const why = step.metadata?.why;
    const op = (step.metadata?.operation as string) || '';
    if (why && op !== 'pipeline') {
      keySteps.push({
        op,
        label: step.name,
        why,
        filePath: step.filePath ?? undefined,
        line: step.lineStart ?? undefined,
      });
    }
  }

  // Collect response origins from respond steps
  const origins: { label: string; origins: any[] }[] = [];
  for (const step of execSteps) {
    const ro = step.metadata?.response_origins;
    if (ro?.length && step.metadata?.operation === 'respond') {
      origins.push({ label: step.name, origins: ro });
    }
  }

  // Error paths
  const errorSteps = execSteps.filter((s) => s.metadata?.operation === 'error');

  // Data access summary: collect all DB queries across steps
  const dataAccess: { label: string; model: string; operation: string; filters: string[] }[] = [];
  for (const step of execSteps) {
    const dbq = step.metadata?.db_query;
    if (!dbq) continue;
    const model = dbq.model || dbq.table || '';
    const op = dbq.operation || '';
    const filters = (dbq.filters || []).map((f: any) => f.column || f.expr || '').filter(Boolean);
    dataAccess.push({ label: step.name, model, operation: op, filters });
  }

  const severityColor: Record<string, string> = {
    high: '#e74c3c',
    medium: '#f39c12',
    low: '#27ae60',
  };
  const riskColor: Record<string, string> = {
    critical: '#c0392b',
    high: '#e67e22',
    medium: '#f1c40f',
    low: '#27ae60',
  };

  return (
    <div className="review-brief">
      {/* Header */}
      <div className="rb-header">
        <div className="rb-title">Review Brief</div>
        {riskScore != null && (
          <span className="rb-risk" style={{ background: riskColor[riskLevel] || '#666' }}>
            RISK {riskScore} ({riskLevel})
          </span>
        )}
      </div>

      {/* Narrative */}
      {narrative && (
        <div className="rb-section">
          <div className="rb-narrative">{narrative}</div>
        </div>
      )}

      {/* Concerns */}
      {concerns.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Concerns</div>
          {concerns.map((c, i) => (
            <div key={i} className="rb-concern">
              <span className="rb-dot" style={{ background: severityColor[c.severity] || '#666' }} />
              <span className="rb-severity">{c.severity}</span>
              {c.label}
            </div>
          ))}
        </div>
      )}

      {/* Decision Points */}
      {branches.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Decision Points</div>
          {branches.map((b, i) => (
            <div key={i} className="rb-branch">
              <code className="rb-condition">{b.condition}</code>
              <div className="rb-explanation">{b.explanation}</div>
            </div>
          ))}
        </div>
      )}

      {/* Key Steps */}
      {keySteps.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Key Steps</div>
          {keySteps.map((s, i) => (
            <div key={i} className="rb-step">
              <span className={`rb-op rb-op-${s.op}`}>{s.op.toUpperCase().slice(0, 5)}</span>
              <span className="rb-step-label">{s.label}</span>
              <div className="rb-step-why">{s.why}</div>
            </div>
          ))}
        </div>
      )}

      {/* Error Paths */}
      {errorSteps.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Error Paths</div>
          {errorSteps.map((s, i) => (
            <div key={i} className="rb-error">
              <span className="rb-error-label">{s.name}</span>
              {s.metadata?.why && <span className="rb-error-why"> — {s.metadata.why}</span>}
            </div>
          ))}
        </div>
      )}

      {/* Data Access */}
      {dataAccess.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Data Access</div>
          {dataAccess.map((da, i) => (
            <div key={i} className="rb-data-access">
              <span className="rb-op rb-op-query">{da.operation}</span>{' '}
              <strong>{da.model}</strong>
              {da.filters.length > 0 && (
                <span style={{ opacity: 0.7 }}> where {da.filters.join(', ')}</span>
              )}
              <span style={{ opacity: 0.5, marginLeft: 4 }}>({da.label})</span>
            </div>
          ))}
        </div>
      )}

      {/* Response Origin */}
      {origins.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Response Origin</div>
          {origins.map((o, i) => (
            <div key={i} className="rb-origin">
              <div className="rb-origin-label">{o.label}</div>
              {o.origins.map((ori: any, j: number) => (
                <div key={j} className="rb-origin-step">
                  ← <span className={`rb-op rb-op-${ori.operation}`}>{ori.operation}</span>{' '}
                  {ori.label} <span className="rb-origin-var">via {ori.variable}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Focus Areas */}
      {focusAreas.length > 0 && (
        <div className="rb-section">
          <div className="rb-section-title">Focus Areas</div>
          {focusAreas.map((f, i) => (
            <div key={i} className="rb-focus">
              <span className="rb-focus-name">{f.name}</span>
              <span className="rb-focus-score" style={{ color: riskColor[f.level] || '#666' }}>
                {f.score} ({f.level})
              </span>
              {f.phase && <span className="rb-focus-phase">{f.phase}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
