import * as vscode from 'vscode';
import { AnalysisServer } from './server';

export class SidebarProvider implements vscode.WebviewViewProvider {
    private view?: vscode.WebviewView;
    private entrypoints: any[] = [];

    constructor(
        private context: vscode.ExtensionContext,
        private server: AnalysisServer,
    ) {}

    resolveWebviewView(view: vscode.WebviewView) {
        this.view = view;
        view.webview.options = { enableScripts: true };
        this.updateHtml();

        view.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'selectEntryPoint') {
                vscode.commands.executeCommand(
                    'codecanvas.showFlow',
                    msg.entryId,
                );
            }
        });
    }

    updateEntryPoints(entrypoints: any[]) {
        this.entrypoints = entrypoints;
        this.updateHtml();
    }

    private updateHtml() {
        if (!this.view) return;

        const nonce = getNonce();

        // Encode entrypoint data as base64 JSON to avoid HTML injection
        const dataJson = JSON.stringify(this.entrypoints.map(entry => ({
            id: entry.id,
            kind: entry.kind,
            group: entry.group,
            label: entry.label,
            path: entry.path,
            method: entry.method,
            handler_name: entry.handler_name,
        })));
        const dataBase64 = Buffer.from(dataJson).toString('base64');

        this.view.webview.html = `<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
    body { font-family: var(--vscode-font-family); padding: 8px; color: var(--vscode-foreground); }
    .group { margin-bottom: 14px; }
    .group-title { font-size: 11px; text-transform: uppercase; opacity: 0.5; margin: 10px 0 6px; }
    .entrypoint { padding: 8px; margin: 4px 0; border-radius: 4px; cursor: pointer;
                background: var(--vscode-list-hoverBackground); }
    .entrypoint:hover { background: var(--vscode-list-activeSelectionBackground); }
    .method { font-weight: bold; font-size: 11px; margin-right: 6px; }
    .label { font-size: 13px; }
    .handler { font-size: 11px; opacity: 0.6; margin-top: 2px; }
    h3 { margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase; opacity: 0.6; }
    .empty { opacity: 0.5; text-align: center; padding: 20px; }
</style>
</head>
<body>
    <h3>Entry Points</h3>
    <div id="list"></div>
    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        const methodColors = {
            GET: '#61affe', POST: '#49cc90', PUT: '#fca130',
            DELETE: '#f93e3e', PATCH: '#50e3c2',
        };

        const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('${dataBase64}'), function(c) { return c.charCodeAt(0); })));
        const list = document.getElementById('list');

        if (data.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = 'Run "CodeCanvas: Analyze Project" to discover entry points';
            list.appendChild(empty);
        } else {
            const groups = new Map();
            data.forEach(entry => {
                const groupName = entry.group || 'Entrypoints';
                if (!groups.has(groupName)) groups.set(groupName, []);
                groups.get(groupName).push(entry);
            });

            groups.forEach((entries, groupName) => {
                const section = document.createElement('div');
                section.className = 'group';

                const title = document.createElement('div');
                title.className = 'group-title';
                title.textContent = groupName;
                section.appendChild(title);

                entries.forEach(entry => {
                const div = document.createElement('div');
                div.className = 'entrypoint';
                div.addEventListener('click', () => {
                    vscode.postMessage({ type: 'selectEntryPoint', entryId: entry.id });
                });

                const method = document.createElement('span');
                method.className = 'method';
                const badge = entry.kind === 'api' ? entry.method : entry.kind.toUpperCase();
                method.style.color = methodColors[entry.method] || '#999';
                method.textContent = badge;

                const label = document.createElement('span');
                label.className = 'label';
                label.textContent = entry.label;

                const handler = document.createElement('div');
                handler.className = 'handler';
                handler.textContent = entry.handler_name;

                div.appendChild(method);
                div.appendChild(label);
                div.appendChild(handler);
                section.appendChild(div);
                });

                list.appendChild(section);
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
