import React from 'react';
import { useFlowStore } from '../store/useFlowStore';

interface CodePreviewProps {
  cacheKey: string;
}

export default function CodePreview({ cacheKey }: CodePreviewProps) {
  const cached = useFlowStore((s) => s.codePreviewCache[cacheKey]);

  if (!cached) return null;

  if (cached.error) {
    return (
      <div className="detail-section">
        <div className="detail-section-title">Code</div>
        <div className="detail-section-value">Could not load code preview: {cached.error}</div>
      </div>
    );
  }

  if (cached.loading || !cached.preview) {
    return (
      <div className="detail-section">
        <div className="detail-section-title">Code</div>
        <div className="detail-section-value">Loading code preview...</div>
      </div>
    );
  }

  return (
    <div className="detail-section">
      <div className="detail-section-title">Code</div>
      <div className="code-preview-meta">
        {cached.kind === 'callsite' ? 'Call-site preview' : 'Definition preview'}
        {' · lines '}
        {cached.startLine}-{cached.endLine}
        {cached.truncated ? ' (clipped)' : ''}
      </div>
      <pre className="code-preview">{cached.preview}</pre>
    </div>
  );
}
