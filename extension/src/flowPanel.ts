import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
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
        this.panel.webview.html = this.getWebviewHtml(
            flowData,
            this.panel.webview.cspSource,
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
        cspSource: string,
        historyTrail: Array<{ index: number; label: string }>,
    ): string {
        const encodedData = Buffer.from(JSON.stringify(flowData)).toString('base64');
        const encodedHistory = Buffer.from(JSON.stringify(historyTrail)).toString('base64');
        const mediaPath = path.join(this.context.extensionPath, 'media');
        const mediaUri = this.panel!.webview.asWebviewUri(
            vscode.Uri.joinPath(this.context.extensionUri, 'media'),
        ).toString();

        const htmlPath = path.join(mediaPath, 'index.html');
        let html = fs.readFileSync(htmlPath, 'utf-8');

        // Replace asset paths with webview URIs
        html = html.replace(/src="\.\/assets\//g, `src="${mediaUri}/assets/`);
        html = html.replace(/href="\.\/assets\//g, `href="${mediaUri}/assets/`);

        // Replace CSP placeholder
        html = html.replace(/__CSP__/g, cspSource);

        // Inject flow data into the store element
        html = html.replace(
            'id="flowDataStore"',
            `id="flowDataStore" data-flow="${encodedData}" data-history="${encodedHistory}"`,
        );

        return html;
    }
}
