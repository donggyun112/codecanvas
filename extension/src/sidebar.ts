import * as vscode from 'vscode';
import { AnalysisServer } from './server';

export class SidebarProvider implements vscode.WebviewViewProvider {
    private view?: vscode.WebviewView;
    private endpoints: any[] = [];

    constructor(
        private context: vscode.ExtensionContext,
        private server: AnalysisServer,
    ) {}

    resolveWebviewView(view: vscode.WebviewView) {
        this.view = view;
        view.webview.options = { enableScripts: true };
        this.updateHtml();

        view.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'selectEndpoint') {
                vscode.commands.executeCommand(
                    'codecanvas.showFlow',
                    msg.method,
                    msg.path,
                );
            }
        });
    }

    updateEndpoints(endpoints: any[]) {
        this.endpoints = endpoints;
        this.updateHtml();
    }

    private updateHtml() {
        if (!this.view) return;

        const nonce = getNonce();

        // Encode endpoint data as base64 JSON to avoid HTML injection
        const dataJson = JSON.stringify(this.endpoints.map(ep => ({
            method: ep.method,
            path: ep.path,
            handler_name: ep.handler_name,
        })));
        const dataBase64 = Buffer.from(dataJson).toString('base64');

        this.view.webview.html = `<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
    body { font-family: var(--vscode-font-family); padding: 8px; color: var(--vscode-foreground); }
    .endpoint { padding: 8px; margin: 4px 0; border-radius: 4px; cursor: pointer;
                background: var(--vscode-list-hoverBackground); }
    .endpoint:hover { background: var(--vscode-list-activeSelectionBackground); }
    .method { font-weight: bold; font-size: 11px; margin-right: 6px; }
    .path { font-size: 13px; }
    .handler { font-size: 11px; opacity: 0.6; margin-top: 2px; }
    h3 { margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase; opacity: 0.6; }
    .empty { opacity: 0.5; text-align: center; padding: 20px; }
</style>
</head>
<body>
    <h3>Endpoints</h3>
    <div id="list"></div>
    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        const methodColors = {
            GET: '#61affe', POST: '#49cc90', PUT: '#fca130',
            DELETE: '#f93e3e', PATCH: '#50e3c2',
        };

        const data = JSON.parse(atob('${dataBase64}'));
        const list = document.getElementById('list');

        if (data.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = 'Run "CodeCanvas: Analyze Project" to discover endpoints';
            list.appendChild(empty);
        } else {
            data.forEach(ep => {
                const div = document.createElement('div');
                div.className = 'endpoint';
                div.addEventListener('click', () => {
                    vscode.postMessage({ type: 'selectEndpoint', method: ep.method, path: ep.path });
                });

                const method = document.createElement('span');
                method.className = 'method';
                method.style.color = methodColors[ep.method] || '#999';
                method.textContent = ep.method;

                const path = document.createElement('span');
                path.className = 'path';
                path.textContent = ep.path;

                const handler = document.createElement('div');
                handler.className = 'handler';
                handler.textContent = ep.handler_name;

                div.appendChild(method);
                div.appendChild(path);
                div.appendChild(handler);
                list.appendChild(div);
            });
        }
    </script>
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
