declare global {
  interface Window {
    acquireVsCodeApi?: () => VsCodeApi;
  }
}

interface VsCodeApi {
  postMessage(msg: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}

let api: VsCodeApi | null = null;

export function getVsCodeApi(): VsCodeApi | null {
  if (api) return api;
  if (typeof window.acquireVsCodeApi === 'function') {
    api = window.acquireVsCodeApi();
  }
  return api;
}

export function postMessage(msg: unknown): void {
  getVsCodeApi()?.postMessage(msg);
}
