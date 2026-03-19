import * as vscode from 'vscode';
import { AnalysisServer } from './server';

interface RequestPreset {
    name: string;
    method: string;
    path: string;
    headers: Record<string, string>;
    body: string;
    authToken: string;
}

export class SidebarProvider implements vscode.WebviewViewProvider {
    private view?: vscode.WebviewView;
    private entrypoints: any[] = [];
    private presets: Record<string, RequestPreset[]> = {};

    constructor(
        private context: vscode.ExtensionContext,
        private server: AnalysisServer,
    ) {
        this.presets = context.globalState.get('codecanvas.presets', {});
    }

    resolveWebviewView(view: vscode.WebviewView) {
        this.view = view;
        view.webview.options = { enableScripts: true };
        this.updateHtml();

        view.webview.onDidReceiveMessage(async (msg) => {
            if (msg.type === 'selectEntryPoint') {
                vscode.commands.executeCommand('codecanvas.showFlow', msg.entryId);
            } else if (msg.type === 'traceRequest') {
                await this.handleTrace(msg);
            } else if (msg.type === 'savePreset') {
                this.savePreset(msg.entryId, msg.preset);
            } else if (msg.type === 'deletePreset') {
                this.deletePreset(msg.entryId, msg.presetName);
            } else if (msg.type === 'loadPresets') {
                const presets = this.presets[msg.entryId] || [];
                this.view?.webview.postMessage({ type: 'presetsLoaded', presets });
            }
        });
    }

    updateEntryPoints(entrypoints: any[]) {
        this.entrypoints = entrypoints;
        this.updateHtml();
    }

    private async handleTrace(msg: any) {
        const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
        if (!workspaceFolder) return;

        await this.server.ensureRunning();

        const headers: Record<string, string> = {};
        if (msg.headers) {
            try {
                Object.assign(headers, JSON.parse(msg.headers));
            } catch {
                for (const line of msg.headers.split('\n')) {
                    const idx = line.indexOf(':');
                    if (idx > 0) {
                        headers[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
                    }
                }
            }
        }
        if (msg.authToken) {
            headers['Authorization'] = `Bearer ${msg.authToken}`;
        }

        let body: any = undefined;
        if (msg.body) {
            try { body = JSON.parse(msg.body); } catch { body = msg.body; }
        }

        const flow = await this.server.traceFlow(
            workspaceFolder.uri.fsPath,
            msg.entryId,
            { method: msg.method, path: msg.path, headers, body },
        );
        if (flow) {
            vscode.commands.executeCommand('codecanvas.showFlowData', flow);
        }
    }

    private savePreset(entryId: string, preset: RequestPreset) {
        if (!this.presets[entryId]) this.presets[entryId] = [];
        const existing = this.presets[entryId].findIndex(p => p.name === preset.name);
        if (existing >= 0) {
            this.presets[entryId][existing] = preset;
        } else {
            this.presets[entryId].push(preset);
        }
        this.context.globalState.update('codecanvas.presets', this.presets);
        this.view?.webview.postMessage({
            type: 'presetsLoaded',
            presets: this.presets[entryId],
        });
    }

    private deletePreset(entryId: string, presetName: string) {
        if (!this.presets[entryId]) return;
        this.presets[entryId] = this.presets[entryId].filter(p => p.name !== presetName);
        this.context.globalState.update('codecanvas.presets', this.presets);
        this.view?.webview.postMessage({
            type: 'presetsLoaded',
            presets: this.presets[entryId],
        });
    }

    private updateHtml() {
        if (!this.view) return;

        const nonce = getNonce();
        const dataJson = JSON.stringify(this.entrypoints.map(entry => ({
            id: entry.id,
            kind: entry.kind,
            group: entry.group,
            label: entry.label,
            path: entry.path,
            method: entry.method,
            handler_name: entry.handler_name,
            request_body: entry.request_body,
            response_model: entry.response_model,
        })));
        const dataBase64 = Buffer.from(dataJson).toString('base64');

        this.view.webview.html = buildSidebarHtml(nonce, dataBase64);
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

/**
 * Build the sidebar HTML using safe DOM construction in the script.
 * All user-controlled data is passed via base64-encoded JSON and
 * rendered exclusively through textContent / setAttribute — no
 * innerHTML with user data.
 */
function buildSidebarHtml(nonce: string, dataBase64: string): string {
    return `<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
    body { font-family: var(--vscode-font-family); padding: 8px; color: var(--vscode-foreground); }
    .group { margin-bottom: 14px; }
    .group-title { font-size: 11px; text-transform: uppercase; opacity: 0.5; margin: 10px 0 6px; }
    .entrypoint { padding: 8px; margin: 4px 0; border-radius: 4px;
                background: var(--vscode-list-hoverBackground); }
    .ep-header { display: flex; align-items: center; cursor: pointer; }
    .ep-header:hover { opacity: 0.8; }
    .method { font-weight: bold; font-size: 11px; margin-right: 6px; }
    .label { font-size: 13px; flex: 1; }
    .handler { font-size: 11px; opacity: 0.6; margin-top: 2px; }
    .trace-btn { background: var(--vscode-button-background); color: var(--vscode-button-foreground);
                 border: none; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 11px; }
    .trace-btn:hover { background: var(--vscode-button-hoverBackground); }
    .trace-form { display: none; margin-top: 8px; padding-top: 8px;
                  border-top: 1px solid var(--vscode-widget-border); }
    .trace-form.open { display: block; }
    .form-group { margin-bottom: 6px; }
    .form-label { font-size: 10px; text-transform: uppercase; opacity: 0.5; margin-bottom: 2px; }
    .form-input, .form-textarea { width: 100%; box-sizing: border-box;
        background: var(--vscode-input-background); color: var(--vscode-input-foreground);
        border: 1px solid var(--vscode-input-border); border-radius: 3px;
        padding: 4px 6px; font-family: var(--vscode-editor-font-family); font-size: 12px; }
    .form-textarea { min-height: 60px; resize: vertical; }
    .form-actions { display: flex; gap: 6px; margin-top: 8px; }
    .form-actions button { flex: 1; }
    .send-btn { background: var(--vscode-button-background); color: var(--vscode-button-foreground);
                border: none; padding: 5px; border-radius: 3px; cursor: pointer; font-size: 12px; }
    .send-btn:hover { background: var(--vscode-button-hoverBackground); }
    .save-btn { background: transparent; color: var(--vscode-foreground); border: 1px solid var(--vscode-widget-border);
                padding: 5px; border-radius: 3px; cursor: pointer; font-size: 11px; }
    .preset-bar { display: flex; gap: 4px; margin-bottom: 6px; flex-wrap: wrap; }
    .preset-chip { font-size: 10px; padding: 2px 6px; border-radius: 10px; cursor: pointer;
                   background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
    .preset-chip:hover { opacity: 0.8; }
    .schema-info { font-size: 10px; opacity: 0.5; margin-top: 2px; }
    h3 { margin: 0 0 8px 0; font-size: 12px; text-transform: uppercase; opacity: 0.6; }
    .empty { opacity: 0.5; text-align: center; padding: 20px; }
</style>
</head>
<body>
    <h3>Entry Points</h3>
    <div id="list"></div>
    <script nonce="${nonce}">
    (function() {
        var vscode = acquireVsCodeApi();
        var methodColors = {
            GET: '#61affe', POST: '#49cc90', PUT: '#fca130',
            DELETE: '#f93e3e', PATCH: '#50e3c2'
        };
        var data = JSON.parse(new TextDecoder().decode(
            Uint8Array.from(atob('${dataBase64}'), function(c) { return c.charCodeAt(0); })
        ));
        var list = document.getElementById('list');
        var activePresetBar = null;
        var activeEntryId = null;
        var formRefs = {};

        window.addEventListener('message', function(event) {
            if (event.data.type === 'presetsLoaded') renderPresets(event.data.presets);
        });

        function renderPresets(presets) {
            if (!activePresetBar || !activeEntryId) return;
            while (activePresetBar.firstChild) activePresetBar.removeChild(activePresetBar.firstChild);
            (presets || []).forEach(function(p) {
                var chip = document.createElement('span');
                chip.className = 'preset-chip';
                chip.appendChild(document.createTextNode(p.name + ' '));
                var del = document.createElement('span');
                del.textContent = 'x';
                del.style.opacity = '0.5';
                del.style.cursor = 'pointer';
                del.addEventListener('click', function(e) {
                    e.stopPropagation();
                    vscode.postMessage({ type: 'deletePreset', entryId: activeEntryId, presetName: p.name });
                });
                chip.appendChild(del);
                chip.addEventListener('click', function() {
                    applyPreset(p);
                });
                activePresetBar.appendChild(chip);
            });
        }

        function applyPreset(preset) {
            if (!activeEntryId || !formRefs[activeEntryId]) return;
            var refs = formRefs[activeEntryId];
            refs.bodyInput.value = preset.body || '';
            refs.headersInput.value = preset.headers ? JSON.stringify(preset.headers) : '';
            refs.authInput.value = preset.authToken || '';
        }

        function buildForm(entry) {
            var form = document.createElement('div');
            form.className = 'trace-form';

            var presetBar = document.createElement('div');
            presetBar.className = 'preset-bar';
            form.appendChild(presetBar);

            var authGroup = document.createElement('div');
            authGroup.className = 'form-group';
            var authLabel = document.createElement('div');
            authLabel.className = 'form-label';
            authLabel.textContent = 'Auth Token';
            var authInput = document.createElement('input');
            authInput.className = 'form-input auth-input';
            authInput.placeholder = 'Bearer token (optional)';
            authGroup.appendChild(authLabel);
            authGroup.appendChild(authInput);
            form.appendChild(authGroup);

            var headersGroup = document.createElement('div');
            headersGroup.className = 'form-group';
            var headersLabel = document.createElement('div');
            headersLabel.className = 'form-label';
            headersLabel.textContent = 'Headers (JSON or Key: Value)';
            var headersInput = document.createElement('textarea');
            headersInput.className = 'form-textarea headers-input';
            headersInput.rows = 2;
            headersInput.placeholder = '{"Content-Type": "application/json"}';
            headersGroup.appendChild(headersLabel);
            headersGroup.appendChild(headersInput);
            form.appendChild(headersGroup);

            var bodyInput = document.createElement('textarea');
            bodyInput.className = 'form-textarea body-input';
            if (['POST','PUT','PATCH'].indexOf(entry.method) >= 0) {
                var bodyGroup = document.createElement('div');
                bodyGroup.className = 'form-group';
                var bodyLabel = document.createElement('div');
                bodyLabel.className = 'form-label';
                bodyLabel.textContent = 'Body (JSON)';
                bodyInput.rows = 4;
                bodyInput.placeholder = '{"key": "value"}';
                bodyGroup.appendChild(bodyLabel);
                bodyGroup.appendChild(bodyInput);
                form.appendChild(bodyGroup);
            } else {
                bodyInput.style.display = 'none';
                form.appendChild(bodyInput);
            }

            var actions = document.createElement('div');
            actions.className = 'form-actions';
            var sendBtn = document.createElement('button');
            sendBtn.className = 'send-btn';
            sendBtn.textContent = 'Send & Trace';
            sendBtn.addEventListener('click', function() {
                vscode.postMessage({
                    type: 'traceRequest',
                    entryId: entry.id,
                    method: entry.method,
                    path: entry.path,
                    body: bodyInput.value,
                    headers: headersInput.value,
                    authToken: authInput.value,
                });
            });
            var saveBtn = document.createElement('button');
            saveBtn.className = 'save-btn';
            saveBtn.textContent = 'Save Preset';
            saveBtn.addEventListener('click', function() {
                var name = prompt('Preset name:');
                if (!name) return;
                var parsedHeaders = {};
                try { parsedHeaders = JSON.parse(headersInput.value); } catch(e) {}
                vscode.postMessage({
                    type: 'savePreset',
                    entryId: entry.id,
                    preset: {
                        name: name,
                        method: entry.method,
                        path: entry.path,
                        headers: parsedHeaders,
                        body: bodyInput.value,
                        authToken: authInput.value,
                    },
                });
            });
            actions.appendChild(sendBtn);
            actions.appendChild(saveBtn);
            form.appendChild(actions);

            formRefs[entry.id] = {
                form: form,
                presetBar: presetBar,
                bodyInput: bodyInput,
                headersInput: headersInput,
                authInput: authInput,
            };

            return form;
        }

        function toggleForm(entryId) {
            document.querySelectorAll('.trace-form').forEach(function(f) {
                f.classList.remove('open');
            });
            var refs = formRefs[entryId];
            if (!refs) return;
            refs.form.classList.add('open');
            activePresetBar = refs.presetBar;
            activeEntryId = entryId;
            vscode.postMessage({ type: 'loadPresets', entryId: entryId });
        }

        if (data.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'empty';
            empty.textContent = 'Run "CodeCanvas: Analyze Project" to discover entry points';
            list.appendChild(empty);
        } else {
            var groups = new Map();
            data.forEach(function(entry) {
                var groupName = entry.group || 'Entrypoints';
                if (!groups.has(groupName)) groups.set(groupName, []);
                groups.get(groupName).push(entry);
            });

            groups.forEach(function(entries, groupName) {
                var section = document.createElement('div');
                section.className = 'group';
                var title = document.createElement('div');
                title.className = 'group-title';
                title.textContent = groupName;
                section.appendChild(title);

                entries.forEach(function(entry) {
                    var div = document.createElement('div');
                    div.className = 'entrypoint';

                    var header = document.createElement('div');
                    header.className = 'ep-header';

                    var method = document.createElement('span');
                    method.className = 'method';
                    method.style.color = methodColors[entry.method] || '#999';
                    method.textContent = entry.kind === 'api' ? entry.method : entry.kind.toUpperCase();

                    var label = document.createElement('span');
                    label.className = 'label';
                    label.textContent = entry.label;

                    header.appendChild(method);
                    header.appendChild(label);

                    if (entry.kind === 'api') {
                        var traceBtn = document.createElement('button');
                        traceBtn.className = 'trace-btn';
                        traceBtn.textContent = '\\u25B6 Trace';
                        traceBtn.addEventListener('click', function(e) {
                            e.stopPropagation();
                            toggleForm(entry.id);
                        });
                        header.appendChild(traceBtn);
                    }

                    header.addEventListener('click', function() {
                        vscode.postMessage({ type: 'selectEntryPoint', entryId: entry.id });
                    });

                    var handler = document.createElement('div');
                    handler.className = 'handler';
                    handler.textContent = entry.handler_name;
                    div.appendChild(header);
                    div.appendChild(handler);

                    if (entry.request_body || entry.response_model) {
                        var schemaInfo = document.createElement('div');
                        schemaInfo.className = 'schema-info';
                        var parts = [];
                        if (entry.request_body) parts.push('Body: ' + entry.request_body);
                        if (entry.response_model) parts.push('Response: ' + entry.response_model);
                        schemaInfo.textContent = parts.join(' | ');
                        div.appendChild(schemaInfo);
                    }

                    if (entry.kind === 'api') {
                        div.appendChild(buildForm(entry));
                    }

                    section.appendChild(div);
                });

                list.appendChild(section);
            });
        }
    })();
    </script>
</body>
</html>`;
}
