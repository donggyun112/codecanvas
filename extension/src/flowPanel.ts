import * as vscode from 'vscode';
import { AnalysisServer } from './server';

export class FlowPanelProvider {
    private panel: vscode.WebviewPanel | null = null;
    private flowHistory: any[] = [];
    private historyIndex = -1;

    constructor(
        private context: vscode.ExtensionContext,
        private server: AnalysisServer,
    ) {}

    showFlow(flowData: any, options?: { historyMode?: 'reset' | 'push' | 'restore' }) {
        const historyMode = options?.historyMode || 'reset';
        this.updateHistory(flowData, historyMode);
        const entry = flowData.entrypoint || flowData.endpoint;
        const title = entry?.kind === 'api'
            ? `Flow: ${entry.method} ${entry.path}`
            : `Flow: ${entry?.label || entry?.handler_name || 'Entrypoint'}`;
        if (this.panel) {
            this.panel.reveal(vscode.ViewColumn.Two);
        } else {
            this.panel = vscode.window.createWebviewPanel(
                'codecanvas.flow',
                title,
                vscode.ViewColumn.Two,
                {
                    enableScripts: true,
                    retainContextWhenHidden: true,
                    localResourceRoots: [
                        vscode.Uri.joinPath(this.context.extensionUri, 'media'),
                    ],
                },
            );

            this.panel.onDidDispose(() => {
                this.panel = null;
            });

            this.panel.webview.onDidReceiveMessage(async (msg) => {
                await this.handleWebviewMessage(msg);
            });
        }

        this.panel.title = title;
        const elkUri = this.panel.webview.asWebviewUri(
            vscode.Uri.joinPath(this.context.extensionUri, 'media', 'elk.bundled.js'),
        );
        const rendererUri = this.panel.webview.asWebviewUri(
            vscode.Uri.joinPath(this.context.extensionUri, 'media', 'flowRenderer.js'),
        );
        this.panel.webview.html = this.getWebviewHtml(
            flowData,
            elkUri.toString(),
            rendererUri.toString(),
            this.panel.webview.cspSource,
            this.historyIndex > 0,
            this.flowHistory.slice(0, this.historyIndex + 1).map((item, index) => ({
                index,
                label: this.historyLabel(item),
            })),
        );
    }

    private updateHistory(flowData: any, historyMode: 'reset' | 'push' | 'restore'): void {
        if (historyMode === 'restore') {
            return;
        }
        if (historyMode === 'push') {
            this.flowHistory = this.flowHistory.slice(0, this.historyIndex + 1);
            this.flowHistory.push(flowData);
            this.historyIndex = this.flowHistory.length - 1;
            return;
        }
        this.flowHistory = [flowData];
        this.historyIndex = 0;
    }

    private historyLabel(flowData: any): string {
        const entry = flowData?.entrypoint || flowData?.endpoint;
        if (!entry) return 'Flow';
        if (entry.kind === 'api') {
            return `${entry.method} ${entry.path}`.trim();
        }
        return entry.label || entry.handler_name || 'Function';
    }

    private async handleWebviewMessage(msg: any): Promise<void> {
        if (!msg) return;

        if (msg.type === 'navigateToCode') {
            const uri = vscode.Uri.file(msg.filePath);
            const doc = await vscode.workspace.openTextDocument(uri);
            const editor = await vscode.window.showTextDocument(doc, vscode.ViewColumn.One);
            const line = Math.max(0, (msg.line || 1) - 1);
            const range = new vscode.Range(line, 0, line, 0);
            editor.selection = new vscode.Selection(range.start, range.end);
            editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
            return;
        }

        if (msg.type === 'loadCodePreview' && this.panel) {
            const payload = await this.loadCodePreview(msg);
            if (!this.panel) return;
            await this.panel.webview.postMessage({
                type: 'codePreview',
                requestId: msg.requestId,
                cacheKey: msg.cacheKey,
                nodeId: msg.nodeId,
                ...payload,
            });
            return;
        }

        if (msg.type === 'openFunctionFlow') {
            const filePath = typeof msg.filePath === 'string' ? msg.filePath : '';
            const line = Number(msg.line || 0);
            if (!filePath || !Number.isFinite(line) || line < 1) return;

            const workspaceFolder = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(filePath))
                || vscode.workspace.workspaceFolders?.[0];
            if (!workspaceFolder) {
                vscode.window.showWarningMessage('The current file is not inside an open workspace');
                return;
            }

            await this.server.ensureRunning();
            const flow = await this.server.getFunctionFlow(
                workspaceFolder.uri.fsPath,
                filePath,
                line,
            );
            if (flow) {
                this.showFlow(flow, { historyMode: 'push' });
            }
            return;
        }

        if (msg.type === 'navigateHistory') {
            let nextIndex = this.historyIndex;
            if (typeof msg.targetIndex === 'number' && Number.isFinite(msg.targetIndex)) {
                nextIndex = msg.targetIndex;
            } else if (msg.direction === 'back') {
                nextIndex = this.historyIndex - 1;
            }
            if (nextIndex < 0 || nextIndex >= this.flowHistory.length || nextIndex === this.historyIndex) {
                return;
            }
            this.historyIndex = nextIndex;
            this.showFlow(this.flowHistory[this.historyIndex], { historyMode: 'restore' });
        }
    }

    private async loadCodePreview(msg: any): Promise<Record<string, unknown>> {
        const filePath = typeof msg.filePath === 'string' ? msg.filePath : '';
        const locationKind = msg.locationKind === 'callsite' ? 'callsite' : 'definition';
        const rawStart = Number(msg.lineStart || 0);
        const rawEnd = Number(msg.lineEnd || rawStart);

        if (!filePath || !Number.isFinite(rawStart) || rawStart < 1) {
            return {
                error: 'No source location available for this node.',
                kind: locationKind,
            };
        }

        try {
            const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
            if (doc.lineCount === 0) {
                return {
                    preview: '',
                    startLine: rawStart,
                    endLine: rawStart,
                    truncated: false,
                    kind: locationKind,
                };
            }

            const anchorLine = Math.max(1, Math.min(doc.lineCount, rawStart));
            let startLine = anchorLine;
            let endLine = Math.max(anchorLine, Math.min(doc.lineCount, rawEnd || anchorLine));
            let truncated = false;

            if (locationKind === 'callsite') {
                startLine = Math.max(1, anchorLine - 2);
                endLine = Math.min(doc.lineCount, anchorLine + 2);
            } else if (endLine <= anchorLine) {
                endLine = Math.min(doc.lineCount, anchorLine + 11);
                truncated = endLine < doc.lineCount;
            } else if (endLine - startLine + 1 > 24) {
                endLine = startLine + 23;
                truncated = true;
            } else if (rawEnd > endLine) {
                truncated = true;
            }

            const width = String(endLine).length;
            const lines: string[] = [];
            for (let lineNo = startLine; lineNo <= endLine; lineNo += 1) {
                const marker = lineNo === anchorLine ? '>' : ' ';
                lines.push(
                    `${marker}${String(lineNo).padStart(width, ' ')} | ${doc.lineAt(lineNo - 1).text}`,
                );
            }

            return {
                preview: lines.join('\n'),
                startLine,
                endLine,
                truncated,
                kind: locationKind,
            };
        } catch (error) {
            return {
                error: error instanceof Error ? error.message : String(error),
                kind: locationKind,
            };
        }
    }

    private getWebviewHtml(
        flowData: any,
        elkSrc: string,
        rendererSrc: string,
        cspSource: string,
        canGoBack: boolean,
        historyTrail: Array<{ index: number; label: string }>,
    ): string {
        const encodedData = Buffer.from(JSON.stringify(flowData)).toString('base64');
        const encodedHistory = Buffer.from(JSON.stringify(historyTrail)).toString('base64');
        const nonce = getNonce();

        return /* html */ `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; img-src ${cspSource} data:; style-src 'nonce-${nonce}' ${cspSource}; script-src 'nonce-${nonce}' ${cspSource}; worker-src ${cspSource} blob:; child-src ${cspSource} blob:;" />
<style nonce="${nonce}">
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--vscode-font-family);
    background: var(--vscode-editor-background);
    color: var(--vscode-foreground);
    overflow: hidden; width: 100vw; height: 100vh;
}
#app { width: 100%; height: 100%; display: flex; flex-direction: column; }
.topbar {
    display: flex; align-items: center; gap: 12px;
    padding: 8px 16px;
    background: var(--vscode-titleBar-activeBackground);
    border-bottom: 1px solid var(--vscode-panel-border);
    flex-shrink: 0; flex-wrap: wrap;
}
.topbar label { font-size: 12px; opacity: 0.7; }
.topbar input[type=range] { flex: 0 1 200px; }
.level-label { font-size: 12px; font-weight: bold; min-width: 160px; }
.topbar-main { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.topbar-meta { display: flex; align-items: center; gap: 12px; margin-left: auto; flex-wrap: wrap; }
.back-btn {
    font-size: 12px;
    font-weight: 600;
    padding: 6px 10px;
    border: 1px solid transparent;
    border-radius: 4px;
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    cursor: pointer;
}
.back-btn:hover { filter: brightness(1.08); }
.breadcrumb { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
.breadcrumb-item {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid var(--vscode-panel-border);
    background: var(--vscode-editor-background);
    color: var(--vscode-foreground);
    cursor: pointer;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.breadcrumb-item.current {
    background: var(--vscode-badge-background);
    color: var(--vscode-badge-foreground);
    border-color: transparent;
    cursor: default;
}
.breadcrumb-sep { opacity: 0.4; font-size: 11px; }
.endpoint-badge { font-size: 13px; font-weight: bold; display: flex; align-items: center; gap: 8px; }
.method-GET { color: #61affe; } .method-POST { color: #49cc90; }
.method-PUT { color: #fca130; } .method-DELETE { color: #f93e3e; }
.kind-badge {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
    padding: 2px 6px; border-radius: 999px;
    background: var(--vscode-badge-background); color: var(--vscode-badge-foreground);
}
.view-toggle { display: flex; gap: 2px; margin-left: 8px; }
.view-toggle button {
    font-size: 10px; padding: 2px 8px; border: 1px solid var(--vscode-panel-border);
    background: transparent; color: var(--vscode-foreground); cursor: pointer; border-radius: 3px;
}
.view-toggle button.active { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
.main { display: flex; flex: 1; overflow: hidden; }
.canvas-wrap { flex: 1; overflow: auto; position: relative; }
.detail-panel {
    width: 320px; border-left: 1px solid var(--vscode-panel-border);
    overflow-y: auto; padding: 16px; flex-shrink: 0; display: none;
}
.detail-panel.visible { display: block; }
.fatal-error {
    margin: 24px;
    padding: 16px 18px;
    border: 1px solid rgba(231, 76, 60, 0.45);
    background: rgba(231, 76, 60, 0.08);
    color: #ffb3ad;
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
}
.detail-panel h3 { font-size: 14px; margin-bottom: 12px; }
.detail-section { margin-bottom: 16px; }
.detail-section-title { font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-bottom: 4px; }
.detail-section-value { font-size: 13px; line-height: 1.4; }
.evidence-item {
    font-size: 11px; padding: 4px 8px;
    background: var(--vscode-textBlockQuote-background); border-radius: 4px; margin-top: 4px;
}
.nav-link { color: var(--vscode-textLink-foreground); cursor: pointer; text-decoration: underline; font-size: 12px; }
.code-preview-meta { font-size: 11px; opacity: 0.65; margin-bottom: 6px; }
.code-preview {
    font-family: var(--vscode-editor-font-family, monospace);
    font-size: 12px;
    line-height: 1.5;
    background: var(--vscode-editor-background);
    border: 1px solid var(--vscode-panel-border);
    border-radius: 6px;
    padding: 10px 12px;
    overflow: auto;
    white-space: pre;
}
.inline-actions { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.action-btn {
    font-size: 12px;
    font-weight: 600;
    padding: 6px 10px;
    border: 1px solid transparent;
    border-radius: 4px;
    cursor: pointer;
}
.action-btn.primary {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
}
.action-btn.secondary {
    background: var(--vscode-button-secondaryBackground, transparent);
    color: var(--vscode-button-secondaryForeground, var(--vscode-foreground));
    border-color: var(--vscode-panel-border);
}
.action-btn:hover {
    filter: brightness(1.08);
}
.action-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    opacity: 0.6;
    margin-bottom: 6px;
}
</style>
</head>
<body>
<div id="app">
    <div class="topbar">
        <div class="topbar-main">
            <button id="backBtn" class="back-btn" style="display:${canGoBack ? 'inline-flex' : 'none'};">← Back</button>
            <div class="breadcrumb" id="breadcrumb"></div>
        </div>
        <div class="topbar-meta">
            <label>Level:</label>
            <input type="range" id="levelSlider" min="0" max="3" value="1" step="1" />
            <span class="level-label" id="levelLabel">Pipeline</span>
            <span class="view-toggle" id="viewToggle" style="display:none;">
                <button id="btnAll" class="active">All</button>
                <button id="btnRuntime">Runtime</button>
                <button id="btnStatic">Static</button>
            </span>
            <span class="endpoint-badge" id="endpointBadge"></span>
        </div>
    </div>
    <div class="main">
        <div class="canvas-wrap" id="canvasWrap"></div>
        <div class="detail-panel" id="detailPanel"></div>
    </div>
</div>

<script nonce="${nonce}" src="${elkSrc}"></script>
<div id="flowDataStore" data-flow="${encodedData}" data-history="${encodedHistory}" style="display:none;"></div>
<script nonce="${nonce}" src="${elkSrc}"></script>
<script nonce="${nonce}" src="${rendererSrc}"></script>
</body>
</html>`;
    }
}

function getNonce(): string {
    let text = '';
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
        text += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return text;
}
