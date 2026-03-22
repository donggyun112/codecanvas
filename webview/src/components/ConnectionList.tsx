import React from 'react';
import type { FlowGraph, FlowNodeData, FlowEdgeData } from '../types/flow';

interface ConnectionEntry {
  direction: 'incoming' | 'outgoing';
  other: FlowNodeData;
  edge: FlowEdgeData;
  count: number;
}

function connectionWeight(entry: ConnectionEntry, hasTrace: boolean): number {
  let weight = 0;
  if (entry.direction === 'outgoing') weight -= 5;
  if (hasTrace && entry.edge.metadata?.runtime_hit) weight -= 40;
  if (entry.edge.metadata?.pipeline_edge) weight -= 30;
  if (entry.edge.type === 'injects') weight -= 20;
  if (entry.edge.type === 'middleware_chain') weight -= 18;
  if (entry.edge.type === 'queries' || entry.edge.type === 'requests') weight -= 16;
  if (entry.edge.type === 'raises' || entry.edge.isErrorPath) weight -= 12;
  if (entry.other.metadata?.pipeline_order != null) {
    weight += Number(entry.other.metadata.pipeline_order);
  }
  weight += (entry.other.level || 0) * 2;
  weight += (entry.other.displayName || entry.other.name || '').length / 100;
  return weight;
}

function formatEntry(entry: ConnectionEntry): string {
  const otherName = entry.other.displayName || entry.other.name || entry.other.id;
  const typeLabel = (entry.edge.type || '').replace(/_/g, ' ');
  const prefix = entry.direction === 'incoming' ? '\u2190 from ' : '\u2192 to ';
  let text = `${prefix}${otherName} (${typeLabel})`;
  if (entry.edge.condition) text += ` [${entry.edge.condition}]`;
  else if (entry.edge.label && entry.edge.label !== otherName) text += ` [${entry.edge.label}]`;
  if (entry.count > 1) text += ` x${entry.count}`;
  return text;
}

interface ConnectionListProps {
  node: FlowNodeData;
  flowData: FlowGraph;
  visibleNodeMap: Record<string, FlowNodeData>;
  hasTrace: boolean;
}

export default function ConnectionList({
  node,
  flowData,
  visibleNodeMap,
  hasTrace,
}: ConnectionListProps) {
  const groups: Record<string, ConnectionEntry> = {};
  let hiddenCount = 0;

  flowData.edges.forEach((edge) => {
    let direction: 'incoming' | 'outgoing' | null = null;
    let otherId: string | null = null;

    if (edge.targetId === node.id) {
      direction = 'incoming';
      otherId = edge.sourceId;
    } else if (edge.sourceId === node.id) {
      direction = 'outgoing';
      otherId = edge.targetId;
    } else {
      return;
    }

    const other = flowData.nodes[otherId!];
    if (!other) return;

    if (edge.metadata?.structural_lift || !visibleNodeMap[other.id]) {
      hiddenCount += 1;
      return;
    }

    const key = [
      direction,
      edge.type,
      other.id,
      edge.condition || '',
      edge.isErrorPath ? '1' : '0',
      edge.label || '',
    ].join('|');

    if (!groups[key]) {
      groups[key] = { direction: direction!, other, edge, count: 0 };
    }
    groups[key].count += 1;
  });

  const items = Object.values(groups)
    .sort((a, b) => connectionWeight(a, hasTrace) - connectionWeight(b, hasTrace))
    .slice(0, 8);

  const totalHidden = hiddenCount + Math.max(0, Object.values(groups).length - 8);

  if (items.length === 0 && totalHidden === 0) return null;

  return (
    <div className="detail-section">
      <div className="detail-section-title">Connections</div>
      {items.map((entry, i) => (
        <div
          key={i}
          className="evidence-item"
          style={{
            borderLeft: entry.edge.isErrorPath
              ? '2px solid #e74c3c'
              : hasTrace && entry.edge.metadata?.runtime_hit
                ? '2px solid #49cc90'
                : undefined,
          }}
        >
          {formatEntry(entry)}
        </div>
      ))}
      {totalHidden > 0 && (
        <div className="detail-section-value" style={{ opacity: 0.65 }}>
          {totalHidden} connection{totalHidden === 1 ? '' : 's'} hidden at this abstraction level.
        </div>
      )}
    </div>
  );
}
