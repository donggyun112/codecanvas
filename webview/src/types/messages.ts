export interface NavigateToCodeMessage {
  type: 'navigateToCode';
  filePath: string;
  line: number;
}

export interface LoadCodePreviewMessage {
  type: 'loadCodePreview';
  requestId: number;
  cacheKey: string;
  nodeId: string;
  filePath: string;
  lineStart: number;
  lineEnd: number;
  locationKind: 'definition' | 'callsite';
}

export interface OpenFunctionFlowMessage {
  type: 'openFunctionFlow';
  filePath: string;
  line: number;
}

export interface NavigateHistoryMessage {
  type: 'navigateHistory';
  direction?: 'back';
  targetIndex?: number;
}

export type OutgoingMessage =
  | NavigateToCodeMessage
  | LoadCodePreviewMessage
  | OpenFunctionFlowMessage
  | NavigateHistoryMessage;

export interface CodePreviewResponse {
  type: 'codePreview';
  requestId: number;
  cacheKey: string;
  nodeId: string;
  preview?: string;
  startLine?: number;
  endLine?: number;
  truncated?: boolean;
  kind?: string;
  error?: string;
  loading?: boolean;
}
