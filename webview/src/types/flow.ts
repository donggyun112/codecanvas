export interface Evidence {
  source: string;
  detail: string;
  filePath?: string;
  lineNumber?: number;
}

export interface FlowNodeData {
  id: string;
  type: string;
  name: string;
  displayName: string;
  description: string;
  filePath: string | null;
  lineStart: number | null;
  lineEnd: number | null;
  confidence: string;
  evidence: Evidence[];
  metadata: Record<string, any>;
  children: string[];
  parentId: string | null;
  level: number;
}

export interface FlowEdgeData {
  id: string;
  sourceId: string;
  targetId: string;
  type: string;
  label: string;
  confidence: string;
  evidence: Evidence[];
  metadata: Record<string, any>;
  condition: string | null;
  isErrorPath: boolean;
}

export interface EntryPoint {
  kind: string;
  method?: string;
  path?: string;
  label?: string;
  handler_name?: string;
  metadata?: Record<string, any>;
}

export interface FlowGraph {
  entrypoint: EntryPoint;
  nodes: Record<string, FlowNodeData>;
  edges: FlowEdgeData[];
}

export interface HistoryItem {
  index: number;
  label: string;
}
