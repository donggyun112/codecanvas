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

export interface CFGStatement {
  line: number;
  lineEnd: number | null;
  text: string;
  kind: string;
}

export interface CFGBlock {
  id: string;
  label: string;
  kind: string;
  scope: string;
  filePath: string | null;
  lineStart: number | null;
  lineEnd: number | null;
  statements: CFGStatement[];
  metadata: Record<string, any>;
}

export interface CFGEdgeData {
  id: string;
  sourceBlockId: string;
  targetBlockId: string;
  kind: string;
  label: string;
  condition: string;
}

export interface CFGData {
  functionName: string;
  filePath: string | null;
  blocks: CFGBlock[];
  edges: CFGEdgeData[];
}

export interface FlowGraph {
  entrypoint: EntryPoint;
  nodes: Record<string, FlowNodeData>;
  edges: FlowEdgeData[];
  executionGraph?: ExecutionGraphData;
  executionGraphL3?: ExecutionGraphData;
  cfg?: CFGData;
}

export interface ExecStep {
  id: string;
  label: string;
  operation: string;
  phase: string;
  scope: string;
  depth: number;
  inputs: string[];
  output: string | null;
  outputType: string | null;
  branchCondition: string | null;
  branchId: string | null;
  errorLabel: string | null;
  filePath: string | null;
  lineStart: number | null;
  lineEnd: number | null;
  calleeFunction: string | null;
  sourceNodeIds: string[];
  confidence: string;
  evidence: string;
  metadata: Record<string, any>;
}

export interface ExecLink {
  id: string;
  sourceStepId: string;
  targetStepId: string;
  kind: string;
  variable: string;
  label: string;
  isErrorPath: boolean;
  confidence: string;
  evidence: string;
}

export interface ExecutionGraphData {
  steps: ExecStep[];
  links: ExecLink[];
}

export interface HistoryItem {
  index: number;
  label: string;
}
