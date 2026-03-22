import React from 'react';
import NodeShell from './shared/NodeShell';

export default function ResourceNode({ data }: { data: any }) {
  return <NodeShell data={data} />;
}
