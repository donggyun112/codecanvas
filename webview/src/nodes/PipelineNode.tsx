import React from 'react';
import NodeShell from './shared/NodeShell';

export default function PipelineNode({ data }: { data: any }) {
  return <NodeShell data={data} />;
}
