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
        this.panel.webview.html = this.getWebviewHtml(
            flowData,
            elkUri.toString(),
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
<script nonce="${nonce}">
(function() {
    var vscodeApi = typeof acquireVsCodeApi === 'function' ? acquireVsCodeApi() : null;

    function showFatalError(err) {
        var wrap = document.getElementById('canvasWrap');
        if (!wrap) return;
        wrap.textContent = '';
        var box = document.createElement('div');
        box.className = 'fatal-error';
        var message = err && err.stack ? err.stack : String(err);
        box.textContent = 'Flow panel failed to render.\\n\\n' + message;
        wrap.appendChild(box);
    }

    window.addEventListener('error', function(event) {
        showFatalError(event.error || event.message || 'Unknown error');
    });
    window.addEventListener('unhandledrejection', function(event) {
        showFatalError(event.reason || 'Unhandled promise rejection');
    });

    function decodeFlowData(encoded) {
        var binary = atob(encoded);
        if (typeof TextDecoder !== 'undefined') {
            return JSON.parse(new TextDecoder().decode(
                Uint8Array.from(binary, function(c){ return c.charCodeAt(0); })
            ));
        }
        try {
            return JSON.parse(decodeURIComponent(escape(binary)));
        } catch (_err) {
            return JSON.parse(binary);
        }
    }

    try {
    var flowData = decodeFlowData("${encodedData}");
    var historyTrail = decodeFlowData("${encodedHistory}");
    var entrypoint = flowData.entrypoint || flowData.endpoint;
    var TYPE_COLORS = {
        trigger:'#34495e',client:'#61affe',api:'#49cc90',entrypoint:'#16a085',
        router:'#49cc90',service:'#fca130',repository:'#9b59b6',database:'#e74c3c',
        external_api:'#e67e22',middleware:'#1abc9c',dependency:'#3498db',
        function:'#95a5a6',method:'#95a5a6','class':'#7f8c8d',
        exception:'#e74c3c',branch:'#f39c12',loop:'#2ecc71',
        assignment:'#2980b9','return':'#27ae60',step:'#7f8c8d',
        file:'#8e44ad',module:'#2c3e50'
    };
    var EDGE_COLORS = {
        calls:null, returns:'#27ae60', raises:'#e74c3c', queries:'#9b59b6',
        requests:'#e67e22', middleware_chain:'#1abc9c', injects:'#3498db', depends_on:'#3498db', binds:'#8e44ad'
    };
    var LEVEL_NAMES = {0:'Overview',1:'Pipeline',2:'Functions',3:'Logic'};
    var currentLevel = 1;
    var viewMode = 'all';
    var selectedNodeId = null;
    var renderVersion = 0;
    var codePreviewCache = {};
    var codePreviewRequestSeq = 0;
    var nodeDrillState = {};
    var hasTrace = !!(entrypoint && entrypoint.metadata && entrypoint.metadata.trace && entrypoint.metadata.trace.hitNodes);
    var isFunctionContext = !!(
        entrypoint
        && entrypoint.kind === 'function'
        && entrypoint.metadata
        && entrypoint.metadata.from_location
    );
    if (isFunctionContext) {
        LEVEL_NAMES = {0:'Selected',1:'Context',2:'Functions',3:'Logic'};
        currentLevel = 1;
    }
    Object.values(flowData.nodes).forEach(function(node){
        if (!selectedNodeId && node.metadata && node.metadata.context_root) {
            selectedNodeId = node.id;
        }
    });
    window.addEventListener('message', function(event) {
        var msg = event.data || {};
        if (msg.type !== 'codePreview' || !msg.cacheKey) return;
        codePreviewCache[msg.cacheKey] = msg;
        if (selectedNodeId && msg.nodeId === selectedNodeId && flowData.nodes[selectedNodeId]) {
            showDetail(flowData.nodes[selectedNodeId]);
        }
    });

    // Badge
    var breadcrumbEl = document.getElementById('breadcrumb');
    (historyTrail || []).forEach(function(item, index) {
        if (index > 0) {
            var sep = document.createElement('span');
            sep.className = 'breadcrumb-sep';
            sep.textContent = '›';
            breadcrumbEl.appendChild(sep);
        }
        var crumb = document.createElement('button');
        crumb.className = 'breadcrumb-item' + (index === historyTrail.length - 1 ? ' current' : '');
        crumb.textContent = item.label;
        if (index !== historyTrail.length - 1) {
            crumb.addEventListener('click', function(){
                if (!vscodeApi) return;
                vscodeApi.postMessage({ type: 'navigateHistory', targetIndex: item.index });
            });
        } else {
            crumb.disabled = true;
        }
        breadcrumbEl.appendChild(crumb);
    });

    var badgeEl = document.getElementById('endpointBadge');
    if (entrypoint.kind === 'api') {
        var ms = document.createElement('span');
        ms.className = 'method-' + entrypoint.method;
        ms.textContent = entrypoint.method + ' ';
        badgeEl.appendChild(ms);
        badgeEl.appendChild(document.createTextNode(entrypoint.path));
    } else {
        var ks = document.createElement('span');
        ks.className = 'kind-badge';
        ks.textContent = entrypoint.kind;
        badgeEl.appendChild(ks);
        badgeEl.appendChild(document.createTextNode(' ' + (entrypoint.label || entrypoint.handler_name)));
    }

    // View toggle
    if (hasTrace) document.getElementById('viewToggle').style.display = 'flex';
    document.getElementById('levelSlider').value = String(currentLevel);
    document.getElementById('levelLabel').textContent = LEVEL_NAMES[currentLevel] || ('L'+currentLevel);
    ['btnAll','btnRuntime','btnStatic'].forEach(function(id){
        document.getElementById(id).addEventListener('click', function(){
            viewMode = id.replace('btn','').toLowerCase();
            document.querySelectorAll('.view-toggle button').forEach(function(b){b.classList.remove('active');});
            document.getElementById(id).classList.add('active');
            renderFlow(currentLevel);
        });
    });
    var backBtn = document.getElementById('backBtn');
    if (backBtn) {
        backBtn.addEventListener('click', function(){
            if (!vscodeApi) return;
            vscodeApi.postMessage({ type: 'navigateHistory', direction: 'back' });
        });
    }

    document.getElementById('levelSlider').addEventListener('input', function(e){
        currentLevel = parseInt(e.target.value);
        document.getElementById('levelLabel').textContent = LEVEL_NAMES[currentLevel] || ('L'+currentLevel);
        renderFlow(currentLevel);
    });

    var STRUCTURAL_TYPES = {file:1, module:1};

    // L0: trigger + api/entrypoint only
    // L1: pipeline steps (trigger, api, middleware, dependency, handler)
    // L2: pipeline + all L3 functions (utility noise filtered)
    // L3: everything including L4 logic
    var PIPELINE_PHASES = {
        0: {trigger:1, api:1, entrypoint:1},
        1: {trigger:1, api:1, entrypoint:1, middleware:1, dependency:1, handler:1},
    };

    // Pre-compute utility noise detection
    var incomingCallCounts = {};
    flowData.edges.forEach(function(e){
        if (e.type === 'calls') {
            incomingCallCounts[e.targetId] = (incomingCallCounts[e.targetId] || 0) + 1;
        }
    });
    var outgoingCallIds = {};
    flowData.edges.forEach(function(e){
        if (e.type === 'calls') {
            outgoingCallIds[e.sourceId] = (outgoingCallIds[e.sourceId] || 0) + 1;
        }
    });

    function isUtilityNoise(n) {
        var name = n.name || '';
        if (name.startsWith('_') && name !== '__init__') return true;
        if (!outgoingCallIds[n.id] && (incomingCallCounts[n.id] || 0) >= 3) return true;
        return false;
    }

    function getVisible(level) {
        var nodes = []; var nodeMap = {};
        var phases = isFunctionContext ? null : PIPELINE_PHASES[level];

        Object.values(flowData.nodes).forEach(function(n){
            if (STRUCTURAL_TYPES[n.type]) return;
            if (isFunctionContext && (n.type === 'trigger' || n.type === 'entrypoint')) return;

            if (isFunctionContext) {
                if (level === 0) {
                    if (!(n.metadata && n.metadata.context_root)) return;
                } else if (level === 1) {
                    if (n.level > 3) return;
                    if (n.level === 3) {
                        var inContext = n.metadata && (
                            n.metadata.context_root
                            || n.metadata.upstream_distance != null
                            || n.metadata.downstream_distance === 1
                        );
                        if (!inContext) return;
                        if (isUtilityNoise(n) && !(n.metadata && (n.metadata.context_root || n.metadata.upstream_distance != null))) {
                            return;
                        }
                    }
                } else if (level === 2) {
                    if (n.level > 3) return;
                } else {
                    if (n.level > 4) return;
                }
            } else if (phases) {
                // L0/L1: only pipeline nodes with matching phase
                var phase = n.metadata && n.metadata.pipeline_phase;
                if (!phase || !phases[phase]) return;
            } else if (level === 2) {
                // L2: L1 pipeline + L3 functions, no L4, no utility noise
                if (n.level > 3) return;
                if (n.level === 3 && isUtilityNoise(n)) return;
            } else {
                // L3 (Logic): L2 + L4 logic nodes
                if (n.level > 4) return;
            }

            // Runtime filter
            if (hasTrace && viewMode === 'runtime' && !(n.metadata && n.metadata.runtime_hit)) return;
            if (hasTrace && viewMode === 'static' && (n.metadata && n.metadata.runtime_hit)) return;

            nodes.push(n); nodeMap[n.id] = n;
        });

        Object.values(flowData.nodes).forEach(function(n){
            if (n.level !== 4) return;
            if (!(n.metadata && n.metadata.function_id)) return;
            if (!nodeMap[n.metadata.function_id]) return;
            if (!shouldShowLocalLogic(n.metadata.function_id)) return;
            if (hasTrace && viewMode === 'runtime' && !(n.metadata && n.metadata.runtime_hit)) return;
            if (hasTrace && viewMode === 'static' && (n.metadata && n.metadata.runtime_hit)) return;
            if (!nodeMap[n.id]) {
                nodes.push(n);
                nodeMap[n.id] = n;
            }
        });

        var ids = new Set(nodes.map(function(n){return n.id;}));
        var edges = flowData.edges.filter(function(e){
            return ids.has(e.sourceId) && ids.has(e.targetId);
        });
        return {nodes:nodes, edges:edges, nodeMap:nodeMap};
    }

    function renderFlow(level) {
        var renderId = ++renderVersion;
        var vis = getVisible(level);
        if (vis.nodes.length === 0) {
            if (renderId !== renderVersion) return;
            var wrap = document.getElementById('canvasWrap');
            wrap.textContent = '';
            var msg = document.createElement('div');
            msg.style.cssText = 'padding:40px;opacity:0.5;text-align:center;';
            msg.textContent = 'No nodes at this level';
            wrap.appendChild(msg);
            return;
        }
        layoutAndDraw(vis, renderId);
    }

    function layoutAndDraw(vis, renderId) {
        try {
            if (typeof ELK === 'undefined') {
                throw new Error('ELK library not loaded');
            }
            var elk = new ELK();
            // Build compound graph: L3 functions become containers for their L4 children
            var functionChildren = {};  // functionNodeId -> [L4 nodes]
            var topLevelNodes = [];
            var l4NodeIds = new Set();

            vis.nodes.forEach(function(n) {
                if (n.level === 4 && n.metadata && n.metadata.function_id) {
                    var parentId = n.metadata.function_id;
                    if (vis.nodeMap[parentId]) {
                        if (!functionChildren[parentId]) functionChildren[parentId] = [];
                        functionChildren[parentId].push(n);
                        l4NodeIds.add(n.id);
                        return;
                    }
                }
                topLevelNodes.push(n);
            });

            function makeNodeSize(n) {
                var label = n.displayName || n.name;
                var w = Math.max(160, Math.min(280, label.length * 8 + 32));
                var h = n.filePath ? 46 : 36;
                return {w: w, h: h};
            }

            var elkChildren = topLevelNodes.map(function(n) {
                var size = makeNodeSize(n);
                var kids = functionChildren[n.id];
                if (kids && kids.length > 0) {
                    // Compound node: function contains its L4 logic
                    return {
                        id: n.id,
                        layoutOptions: {
                            'elk.algorithm': 'layered',
                            'elk.direction': 'DOWN',
                            'elk.padding': '[top=40,left=12,bottom=12,right=12]',
                            'elk.spacing.nodeNode': '16',
                            'elk.layered.spacing.nodeNodeBetweenLayers': '24',
                            'elk.edgeRouting': 'ORTHOGONAL',
                            'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
                        },
                        children: kids.map(function(child) {
                            var cs = makeNodeSize(child);
                            var childOpts = {};
                            if (child.type === 'exception') {
                                childOpts['elk.layered.crossingMinimization.semiInteractive'] = 'true';
                                childOpts['elk.position'] = '(1000, 0)';
                            }
                            return {id: child.id, width: cs.w, height: cs.h, layoutOptions: childOpts};
                        }),
                        edges: vis.edges.filter(function(e) {
                            var srcIn = kids.some(function(k){ return k.id === e.sourceId; });
                            var tgtIn = kids.some(function(k){ return k.id === e.targetId; });
                            return srcIn && tgtIn;
                        }).map(function(e) {
                            var edgeOpts = {};
                            if (e.isErrorPath || e.type === 'raises') {
                                edgeOpts['elk.layered.priority.direction'] = '0';
                                edgeOpts['elk.layered.priority.shortness'] = '0';
                            } else {
                                edgeOpts['elk.layered.priority.direction'] = '10';
                                edgeOpts['elk.layered.priority.shortness'] = '10';
                            }
                            return {id: e.id, sources: [e.sourceId], targets: [e.targetId], layoutOptions: edgeOpts};
                        }),
                    };
                }
                return {id: n.id, width: size.w, height: size.h};
            });

            // Top-level edges: edges between top-level nodes OR crossing compound boundaries
            var internalEdgeIds = new Set();
            elkChildren.forEach(function(c) {
                if (c.edges) c.edges.forEach(function(e) { internalEdgeIds.add(e.id); });
            });

            // Build set of L4 node IDs that are inside compounds
            var compoundChildIds = new Set();
            vis.nodes.forEach(function(n) {
                if (n.level === 4 && n.metadata && n.metadata.function_id && vis.nodeMap[n.metadata.function_id]) {
                    compoundChildIds.add(n.id);
                }
            });

            var elkEdges = vis.edges.filter(function(e) {
                if (internalEdgeIds.has(e.id)) return false;
                // Skip L3→L4 edges where L4 is inside a compound (parent→child entry)
                var srcNode = vis.nodeMap[e.sourceId];
                var tgtNode = vis.nodeMap[e.targetId];
                if (srcNode && tgtNode) {
                    if (srcNode.level === 3 && compoundChildIds.has(e.targetId)) return false;
                    // Skip L4→L3 edges crossing out of compound (handled by compound ports)
                    if (compoundChildIds.has(e.sourceId) && tgtNode.level === 3) return false;
                }
                return true;
            }).map(function(e) {
                var edgeOpts = {};
                if (e.isErrorPath || e.type === 'raises') {
                    edgeOpts['elk.layered.priority.direction'] = '0';
                } else if (e.type === 'middleware_chain' || (e.metadata && e.metadata.pipeline_edge)) {
                    edgeOpts['elk.layered.priority.direction'] = '15';
                } else {
                    edgeOpts['elk.layered.priority.direction'] = '5';
                }
                return {id: e.id, sources: [e.sourceId], targets: [e.targetId], layoutOptions: edgeOpts};
            });

            var elkGraph = {
                id: 'root',
                layoutOptions: {
                    'elk.algorithm': 'layered',
                    'elk.direction': 'DOWN',
                    'elk.spacing.nodeNode': '25',
                    'elk.layered.spacing.nodeNodeBetweenLayers': '40',
                    'elk.layered.spacing.edgeNodeBetweenLayers': '15',
                    'elk.padding': '[top=20,left=20,bottom=20,right=20]',
                    'elk.edgeRouting': 'ORTHOGONAL',
                    'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
                    'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
                },
                children: elkChildren,
                edges: elkEdges,
            };

            elk.layout(elkGraph).then(function(laid){
                if (renderId !== renderVersion) return;
                drawSvg(laid, vis, renderId);
            }).catch(function(err){
                if (renderId !== renderVersion) return;
                console.error('ELK layout error, falling back to simple layout', err);
                drawSvg(buildFallbackLayout(vis), vis, renderId);
            });
        } catch (err) {
            if (renderId !== renderVersion) return;
            console.error('ELK initialization error, falling back to simple layout', err);
            drawSvg(buildFallbackLayout(vis), vis, renderId);
        }
    }

    function buildFallbackLayout(vis) {
        var orderedNodes = topoSort(vis.nodes, vis.edges);
        var width = 360;
        var gapY = 44;
        var top = 20;
        var left = 40;
        var children = orderedNodes.map(function(n, index){
            var label = n.displayName || n.name;
            var nodeWidth = Math.max(160, Math.min(280, label.length * 8 + 32));
            var nodeHeight = n.filePath ? 46 : 36;
            return {
                id: n.id,
                x: left,
                y: top + index * (nodeHeight + gapY),
                width: nodeWidth,
                height: nodeHeight,
            };
        });
        var childMap = {};
        children.forEach(function(c){ childMap[c.id] = c; });

        var edges = vis.edges.map(function(e){
            var src = childMap[e.sourceId];
            var tgt = childMap[e.targetId];
            if (!src || !tgt) return { id: e.id, sections: [] };

            var startPoint = {
                x: src.x + src.width / 2,
                y: src.y + src.height,
            };
            var endPoint = {
                x: tgt.x + tgt.width / 2,
                y: tgt.y,
            };
            var bendY = startPoint.y + Math.max(16, (endPoint.y - startPoint.y) / 2);

            return {
                id: e.id,
                sections: [{
                    startPoint: startPoint,
                    bendPoints: [
                        { x: startPoint.x, y: bendY },
                        { x: endPoint.x, y: bendY },
                    ],
                    endPoint: endPoint,
                }],
            };
        });

        return {
            width: width,
            height: children.length
                ? (children[children.length - 1].y + children[children.length - 1].height + 20)
                : 200,
            children: children,
            edges: edges,
        };
    }

    function topoSort(nodes, edges) {
        var nodeMap = {};
        var graph = {};
        var indegree = {};
        nodes.forEach(function(n){
            nodeMap[n.id] = n;
            graph[n.id] = [];
            indegree[n.id] = 0;
        });

        edges.forEach(function(e){
            if (!(e.sourceId in graph) || !(e.targetId in graph)) return;
            graph[e.sourceId].push(e.targetId);
            indegree[e.targetId] += 1;
        });

        var queue = nodes
            .filter(function(n){ return indegree[n.id] === 0; })
            .map(function(n){ return n.id; });
        var ordered = [];

        while (queue.length) {
            var id = queue.shift();
            ordered.push(nodeMap[id]);
            graph[id].forEach(function(nextId){
                indegree[nextId] -= 1;
                if (indegree[nextId] === 0) queue.push(nextId);
            });
        }

        nodes.forEach(function(n){
            if (ordered.indexOf(n) === -1) ordered.push(n);
        });
        return ordered;
    }

    function drawSvg(laid, vis, renderId) {
        if (renderId !== renderVersion) return;
        var svgNS = 'http://www.w3.org/2000/svg';
        var pad = 40;
        var totalW = (laid.width || 800) + pad * 2;
        var totalH = (laid.height || 600) + pad * 2;

        var svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', String(totalW));
        svg.setAttribute('height', String(totalH));
        svg.setAttribute('viewBox', '0 0 ' + totalW + ' ' + totalH);

        // Defs
        var defs = document.createElementNS(svgNS, 'defs');
        var markerData = [
            {id:'arrow',fill:'var(--vscode-foreground)'},
            {id:'arrowErr',fill:'#e74c3c'},
            {id:'arrowQ',fill:'#9b59b6'},
            {id:'arrowInj',fill:'#3498db'},
            {id:'arrowMw',fill:'#1abc9c'},
            {id:'arrowHttp',fill:'#e67e22'}
        ];
        markerData.forEach(function(md){
            var m = document.createElementNS(svgNS, 'marker');
            m.setAttribute('id', md.id); m.setAttribute('viewBox','0 0 10 10');
            m.setAttribute('refX','9'); m.setAttribute('refY','5');
            m.setAttribute('markerWidth','6'); m.setAttribute('markerHeight','6');
            m.setAttribute('orient','auto-start-reverse');
            var p = document.createElementNS(svgNS, 'path');
            p.setAttribute('d','M 0 0 L 10 5 L 0 10 z');
            p.setAttribute('fill', md.fill);
            m.appendChild(p); defs.appendChild(m);
        });
        svg.appendChild(defs);

        // Top-most layer for edge labels — appended to svg after all nodes are drawn
        // so labels always render above node boxes.
        var edgeLabelLayer = document.createElementNS(svgNS, 'g');

        // Draw edges (recursive: handles both top-level and compound-internal)
        function drawEdges(edgeList, offsetX, offsetY) {
            (edgeList || []).forEach(function(le) {
                var origEdge = vis.edges.find(function(e){ return e.id === le.id; });
                if (!le.sections || !le.sections.length) return;
                var sec = le.sections[0];
                var points = [sec.startPoint];
                if (sec.bendPoints) points = points.concat(sec.bendPoints);
                points.push(sec.endPoint);

                var d = 'M ' + (points[0].x + offsetX) + ' ' + (points[0].y + offsetY);
                for (var i = 1; i < points.length; i++) {
                    d += ' L ' + (points[i].x + offsetX) + ' ' + (points[i].y + offsetY);
                }

                var path = document.createElementNS(svgNS, 'path');
                path.setAttribute('d', d);
                path.setAttribute('fill', 'none');

                var edgeColor = null;
                var markerId = 'arrow';
                if (origEdge) {
                    edgeColor = EDGE_COLORS[origEdge.type] || null;
                    if (origEdge.metadata && origEdge.metadata.upstream_edge) {
                        edgeColor = '#8aa4ff';
                        path.setAttribute('stroke-dasharray', '5,3');
                    }
                    if (origEdge.isErrorPath || origEdge.type === 'raises') { edgeColor = '#e74c3c'; markerId = 'arrowErr'; }
                    else if (origEdge.type === 'binds') { path.setAttribute('stroke-dasharray', '6,4'); }
                    else if (origEdge.type === 'queries') markerId = 'arrowQ';
                    else if (origEdge.type === 'injects') { markerId = 'arrowInj'; path.setAttribute('stroke-dasharray','6,3'); }
                    else if (origEdge.type === 'middleware_chain') markerId = 'arrowMw';
                    else if (origEdge.type === 'requests') markerId = 'arrowHttp';
                }

                var isHit = hasTrace && origEdge && origEdge.metadata && origEdge.metadata.runtime_hit;
                path.setAttribute('stroke', edgeColor || 'var(--vscode-foreground)');
                path.setAttribute('stroke-width', isHit ? '2.5' : '1.5');
                path.setAttribute('stroke-opacity', hasTrace ? (isHit ? '0.9' : '0.15') : '0.5');
                path.setAttribute('marker-end', 'url(#' + markerId + ')');
                svg.appendChild(path);

                var lbl = getEdgeLabel(origEdge);
                if (lbl) {
                    if (lbl.length > 28) lbl = lbl.slice(0, 28) + '…';
                    var mx = (points[0].x + points[points.length-1].x) / 2 + offsetX;
                    var my = (points[0].y + points[points.length-1].y) / 2 + offsetY;
                    var txt = document.createElementNS(svgNS, 'text');
                    txt.setAttribute('x', String(mx));
                    txt.setAttribute('y', String(my));
                    txt.setAttribute('font-size', '9');
                    txt.setAttribute('font-family', 'var(--vscode-editor-font-family, monospace)');
                    // Neutral foreground — color is already on the edge line itself
                    txt.setAttribute('fill', 'var(--vscode-foreground)');
                    txt.setAttribute('opacity', '0.75');
                    txt.setAttribute('dominant-baseline', 'middle');
                    txt.setAttribute('text-anchor', 'middle');
                    txt.textContent = lbl;
                    svg.appendChild(txt);
                    try {
                        var bbox = txt.getBBox();
                        var pad = 3;
                        var bg = document.createElementNS(svgNS, 'rect');
                        bg.setAttribute('x', String(bbox.x - pad));
                        bg.setAttribute('y', String(bbox.y - pad));
                        bg.setAttribute('width', String(bbox.width + pad * 2));
                        bg.setAttribute('height', String(bbox.height + pad * 2));
                        bg.setAttribute('rx', '3');
                        bg.setAttribute('ry', '3');
                        // Opaque fill so the label cleanly occludes nodes/lines behind it
                        bg.setAttribute('fill', 'var(--vscode-editor-background)');
                        // Thin colored border echoes the edge type without overwhelming
                        bg.setAttribute('stroke', edgeColor || 'var(--vscode-panel-border)');
                        bg.setAttribute('stroke-width', '0.75');
                        bg.setAttribute('stroke-opacity', '0.5');
                        svg.removeChild(txt);
                        edgeLabelLayer.appendChild(bg);
                        edgeLabelLayer.appendChild(txt);
                    } catch (_err) {
                        svg.removeChild(txt);
                        edgeLabelLayer.appendChild(txt);
                    }
                }
            });
        }

        // Draw nodes (recursive: handles compound nodes with children)
        function drawNodes(nodeList, offsetX, offsetY) {
            (nodeList || []).forEach(function(ln) {
                var n = vis.nodeMap[ln.id];
                if (!n) return;
                var x = ln.x + offsetX, y = ln.y + offsetY;
                var w = ln.width, h = ln.height;
                var typeColor = TYPE_COLORS[n.type] || '#666';
                var isHit = hasTrace && n.metadata && n.metadata.runtime_hit;
                var isSelected = n.id === selectedNodeId;
                var isCompound = ln.children && ln.children.length > 0;
                var isContextRoot = !!(n.metadata && n.metadata.context_root);
                var isUpstream = !!(n.metadata && n.metadata.upstream_distance != null);
                var isUnresolved = n.confidence === 'inferred' || String(n.id || '').indexOf('unresolved.') === 0;
                var isFunctionLike = n.type === 'function' || n.type === 'method' || n.type === 'class';
                var isContextRoot = !!(n.metadata && n.metadata.context_root);
                var isUpstream = !!(n.metadata && n.metadata.upstream_distance != null);
                var isUnresolved = n.confidence === 'inferred' || String(n.id || '').indexOf('unresolved.') === 0;
                var isFunctionLike = n.type === 'function' || n.type === 'method' || n.type === 'class';

                var g = document.createElementNS(svgNS, 'g');
                g.setAttribute('transform', 'translate(' + x + ',' + y + ')');
                g.setAttribute('cursor', 'pointer');
                g.addEventListener('click', function(ev) {
                    ev.stopPropagation();
                    selectNode(n.id, vis);
                });

                if (isCompound) {
                    // Compound node: draw as a container box with title bar
                    var box = document.createElementNS(svgNS, 'rect');
                    box.setAttribute('width', String(w)); box.setAttribute('height', String(h));
                    box.setAttribute('rx', '8'); box.setAttribute('ry', '8');
                    box.setAttribute('fill', 'rgba(255,255,255,0.03)');
                    box.setAttribute('stroke', typeColor);
                    box.setAttribute('stroke-width', isSelected ? '2' : '1');
                    box.setAttribute('stroke-dasharray', '4,2');
                    if (hasTrace && !isHit) box.setAttribute('opacity', '0.35');
                    g.appendChild(box);

                    // Title bar
                    var titleBar = document.createElementNS(svgNS, 'rect');
                    titleBar.setAttribute('width', String(w)); titleBar.setAttribute('height', '28');
                    titleBar.setAttribute('rx', '8'); titleBar.setAttribute('ry', '8');
                    titleBar.setAttribute('fill', typeColor);
                    titleBar.setAttribute('opacity', '0.15');
                    g.appendChild(titleBar);

                    // Type + name in title
                    var typeTxt = document.createElementNS(svgNS, 'text');
                    typeTxt.setAttribute('x', '10'); typeTxt.setAttribute('y', '12');
                    typeTxt.setAttribute('font-size', '8'); typeTxt.setAttribute('fill', typeColor);
                    typeTxt.setAttribute('opacity', '0.7');
                    typeTxt.textContent = n.type.replace('_', ' ').toUpperCase();
                    g.appendChild(typeTxt);

                    var nameTxt = document.createElementNS(svgNS, 'text');
                    nameTxt.setAttribute('x', '10'); nameTxt.setAttribute('y', '24');
                    nameTxt.setAttribute('font-size', '12'); nameTxt.setAttribute('font-weight', 'bold');
                    nameTxt.setAttribute('fill', 'var(--vscode-foreground)');
                    var dispName = n.displayName || n.name;
                    nameTxt.textContent = dispName.length > 40 ? dispName.slice(0, 40) + '...' : dispName;
                    g.appendChild(nameTxt);

                    svg.appendChild(g);

                    // Draw children inside this compound node
                    drawNodes(ln.children, x, y);
                    drawEdges(ln.edges, x, y);
                } else {
                    // Leaf node: regular box
                    var rect = document.createElementNS(svgNS, 'rect');
                    rect.setAttribute('width', String(w)); rect.setAttribute('height', String(h));
                    rect.setAttribute('rx', '6'); rect.setAttribute('ry', '6');
                    rect.setAttribute('fill', 'var(--vscode-editor-background)');
                    rect.setAttribute('stroke', isSelected ? 'var(--vscode-focusBorder)' : typeColor);
                    rect.setAttribute('stroke-width', isSelected ? '2' : '1.5');
                    if (isUpstream && !isContextRoot) rect.setAttribute('fill', 'rgba(138, 164, 255, 0.08)');
                    if (isUnresolved) rect.setAttribute('stroke-dasharray', '6,3');
                    if (hasTrace && !isHit) rect.setAttribute('opacity', '0.35');
                    g.appendChild(rect);

            // Top color bar
            var bar = document.createElementNS(svgNS, 'rect');
            bar.setAttribute('width', String(w)); bar.setAttribute('height', '3');
            bar.setAttribute('rx', '6'); bar.setAttribute('ry', '6');
            bar.setAttribute('fill', typeColor);
            if (hasTrace && !isHit) bar.setAttribute('opacity', '0.35');
            g.appendChild(bar);

            // Hit glow
            if (isHit) {
                var glow = document.createElementNS(svgNS, 'rect');
                glow.setAttribute('x', '-2'); glow.setAttribute('y', '-2');
                glow.setAttribute('width', String(w+4)); glow.setAttribute('height', String(h+4));
                glow.setAttribute('rx', '8'); glow.setAttribute('ry', '8');
                glow.setAttribute('fill', 'none'); glow.setAttribute('stroke', '#49cc90');
                glow.setAttribute('stroke-width', '2'); glow.setAttribute('opacity', '0.6');
                g.appendChild(glow);
            }

            // Type label
            var typeTxt = document.createElementNS(svgNS, 'text');
            typeTxt.setAttribute('x', '10'); typeTxt.setAttribute('y', '14');
            typeTxt.setAttribute('font-size', '9'); typeTxt.setAttribute('fill', typeColor);
            typeTxt.setAttribute('opacity', '0.7');
            typeTxt.textContent = n.type.replace('_',' ').toUpperCase();
            g.appendChild(typeTxt);

            // Name
            var nameTxt = document.createElementNS(svgNS, 'text');
            nameTxt.setAttribute('x', '10'); nameTxt.setAttribute('y', '30');
            nameTxt.setAttribute('font-size', '13');
            nameTxt.setAttribute('font-weight', isFunctionLike ? '400' : 'bold');
            nameTxt.setAttribute('fill', 'var(--vscode-foreground)');
            var dispName = n.displayName || n.name;
            nameTxt.textContent = dispName.length > 35 ? dispName.slice(0,35)+'...' : dispName;
            g.appendChild(nameTxt);

            // File / call-site reference
            var nodeLoc = getPrimaryLocation(n);
            if (nodeLoc) {
                var fileTxt = document.createElementNS(svgNS, 'text');
                fileTxt.setAttribute('x', '10'); fileTxt.setAttribute('y', String(h - 6));
                fileTxt.setAttribute('font-size', '9');
                fileTxt.setAttribute('fill', 'var(--vscode-foreground)');
                fileTxt.setAttribute('opacity', '0.35');
                fileTxt.textContent = (nodeLoc.kind === 'callsite' ? 'via ' : '')
                    + shortPath(nodeLoc.filePath) + ':' + nodeLoc.line;
                g.appendChild(fileTxt);
            }

            // Execution order badge
            if (isHit && n.metadata && n.metadata.execution_order) {
                var circle = document.createElementNS(svgNS, 'circle');
                circle.setAttribute('cx', String(w - 2)); circle.setAttribute('cy', '2');
                circle.setAttribute('r', '10'); circle.setAttribute('fill', '#49cc90');
                g.appendChild(circle);
                var orderTxt = document.createElementNS(svgNS, 'text');
                orderTxt.setAttribute('x', String(w - 2)); orderTxt.setAttribute('y', '6');
                orderTxt.setAttribute('text-anchor', 'middle');
                orderTxt.setAttribute('font-size', '9'); orderTxt.setAttribute('font-weight', 'bold');
                orderTxt.setAttribute('fill', '#000');
                orderTxt.textContent = String(n.metadata.execution_order);
                g.appendChild(orderTxt);
            }

                    svg.appendChild(g);
                } // end leaf node
            }); // end forEach
        } // end drawNodes

        // Render the graph — edges first, then nodes, then labels on top
        drawEdges(laid.edges, pad, pad);
        drawNodes(laid.children, pad, pad);
        svg.appendChild(edgeLabelLayer);

        if (renderId !== renderVersion) return;
        var wrap = document.getElementById('canvasWrap');
        wrap.textContent = '';
        wrap.appendChild(svg);
    }

    function selectNode(nodeId, vis) {
        var node = flowData.nodes[nodeId];
        var changed = selectedNodeId !== nodeId;
        selectedNodeId = nodeId;
        var drillChanged = advanceNodeDrill(node, changed);
        if (changed || drillChanged) renderFlow(currentLevel);
        showDetail(node);
    }

    function showDetail(node) {
        if (!node) return;
        var panel = document.getElementById('detailPanel');
        panel.classList.add('visible');
        panel.textContent = '';

        var h3 = document.createElement('h3');
        h3.textContent = node.displayName || node.name;
        panel.appendChild(h3);

        var drillMax = maxDrillDepth(node);
        if (drillMax > 0) {
            var actionLabel = document.createElement('div');
            actionLabel.className = 'action-label';
            actionLabel.textContent = 'Actions';
            panel.appendChild(actionLabel);

            var actions = document.createElement('div');
            actions.className = 'inline-actions';

            var cycleBtn = document.createElement('button');
            cycleBtn.className = 'action-btn primary';
            cycleBtn.textContent = 'Expand Logic';
            cycleBtn.addEventListener('click', function(){
                if (advanceNodeDrill(node, false)) {
                    renderFlow(currentLevel);
                    showDetail(node);
                }
            });
            actions.appendChild(cycleBtn);

            var collapseBtn = document.createElement('button');
            collapseBtn.className = 'action-btn secondary';
            collapseBtn.textContent = 'Collapse Logic';
            collapseBtn.addEventListener('click', function(){
                if (setNodeDrill(node.id, 0)) {
                    renderFlow(currentLevel);
                    showDetail(node);
                }
            });
            actions.appendChild(collapseBtn);

            panel.appendChild(actions);
        }

        var functionFlowTarget = getFunctionFlowTarget(node);
        if (functionFlowTarget) {
            if (drillMax <= 0) {
                var flowActionLabel = document.createElement('div');
                flowActionLabel.className = 'action-label';
                flowActionLabel.textContent = 'Actions';
                panel.appendChild(flowActionLabel);
            }
            var flowActions = document.createElement('div');
            flowActions.className = 'inline-actions';
            var openBtn = document.createElement('button');
            openBtn.className = 'action-btn primary';
            openBtn.textContent = 'Open Function Flow';
            openBtn.addEventListener('click', function(){
                vscodeApi.postMessage({
                    type: 'openFunctionFlow',
                    filePath: functionFlowTarget.filePath,
                    line: functionFlowTarget.line,
                });
            });
            flowActions.appendChild(openBtn);
            panel.appendChild(flowActions);
        }

        var nestedCallTargets = getNestedCallTargets(node);
        if (nestedCallTargets.length) {
            var nestedLabel = document.createElement('div');
            nestedLabel.className = 'action-label';
            nestedLabel.textContent = 'Follow Calls';
            panel.appendChild(nestedLabel);

            var nestedActions = document.createElement('div');
            nestedActions.className = 'inline-actions';
            nestedCallTargets.forEach(function(target){
                var targetBtn = document.createElement('button');
                targetBtn.className = 'action-btn primary';
                targetBtn.textContent = target.label;
                targetBtn.addEventListener('click', function(){
                    vscodeApi.postMessage({
                        type: 'openFunctionFlow',
                        filePath: target.filePath,
                        line: target.line,
                    });
                });
                nestedActions.appendChild(targetBtn);
            });
            panel.appendChild(nestedActions);
        }

        addSec(panel, 'Type', node.type);
        addSec(panel, 'Abstraction', 'L' + String(node.level));
        addSec(panel, 'Confidence', node.confidence);
        if (node.metadata && node.metadata.pipeline_phase) {
            var phase = String(node.metadata.pipeline_phase);
            if (node.metadata.pipeline_order != null) {
                phase += ' #' + String(node.metadata.pipeline_order);
            }
            addSec(panel, 'Phase', phase);
        }
        if (node.metadata && node.metadata.context_root) {
            addSec(panel, 'Context', 'Selected function');
        } else if (node.metadata && node.metadata.upstream_distance != null) {
            addSec(panel, 'Context', 'Caller depth ' + String(node.metadata.upstream_distance));
        }
        if (node.metadata && node.metadata.return_type) {
            addSec(panel, 'Returns', node.metadata.return_type);
        }
        if (node.metadata && (node.metadata.dependency_param || node.metadata.declared_type)) {
            var injects = String(node.metadata.dependency_param || 'value');
            if (node.metadata.declared_type) {
                injects += ': ' + String(node.metadata.declared_type);
            }
            addSec(panel, 'Injects', injects);
        }
        if (node.metadata && node.metadata.contract_type) {
            var contractText = String(node.metadata.contract_type);
            if (node.metadata.contract_kind) {
                contractText += ' (' + String(node.metadata.contract_kind) + ')';
            }
            addSec(panel, 'Contract', contractText);
        }
        if (node.metadata && node.metadata.bound_implementation) {
            addSec(panel, 'Bound To', String(node.metadata.bound_implementation));
        }
        if (node.metadata && node.metadata.is_protocol) {
            addSec(panel, 'Contract Role', 'Protocol');
        } else if (node.metadata && node.metadata.is_abstract) {
            addSec(panel, 'Contract Role', 'Abstract');
        }
        if (node.confidence === 'inferred') {
            addSec(panel, 'Resolution', 'Call site was found, but the target definition could not be resolved statically');
        }
        if (node.description) addSec(panel, 'Description', node.description);
        if (drillMax > 0) {
            addSec(panel, 'Layer', String(nodeDrillState[node.id] || 0) + ' / ' + String(drillMax));
        }

        if (hasTrace && node.metadata) {
            if (node.metadata.runtime_hit) {
                var rt = 'Hit #' + (node.metadata.execution_order || '?');
                if (node.metadata.duration_ms != null) rt += ' | ' + node.metadata.duration_ms.toFixed(2) + ' ms';
                addSec(panel, 'Runtime', rt);
            } else {
                addSec(panel, 'Runtime', 'Not executed');
            }
            if (node.metadata.runtime_exception) addSec(panel, 'Exception', node.metadata.runtime_exception);
        }

        var primaryLocation = getPrimaryLocation(node);
        if (primaryLocation) {
            var locSec = document.createElement('div');
            locSec.className = 'detail-section';
            var locTitle = document.createElement('div');
            locTitle.className = 'detail-section-title';
            locTitle.textContent = primaryLocation.kind === 'callsite' ? 'Call Site' : 'Location';
            locSec.appendChild(locTitle);
            var link = document.createElement('span');
            link.className = 'nav-link';
            link.textContent = shortPath(primaryLocation.filePath) + ':' + primaryLocation.line;
            link.addEventListener('click', function(){
                vscodeApi.postMessage({
                    type:'navigateToCode',
                    filePath:primaryLocation.filePath,
                    line:primaryLocation.line || 1,
                });
            });
            locSec.appendChild(link);
            panel.appendChild(locSec);
        }

        if (shouldShowCodePreview(node)) {
            renderCodePreview(panel, node, primaryLocation);
        }

        var extraLocations = getExtraLocations(node);
        if (extraLocations.length) {
            var observedSec = document.createElement('div');
            observedSec.className = 'detail-section';
            var observedTitle = document.createElement('div');
            observedTitle.className = 'detail-section-title';
            observedTitle.textContent = 'Observed At';
            observedSec.appendChild(observedTitle);

            extraLocations.forEach(function(loc){
                var item = document.createElement('div');
                item.className = 'evidence-item';
                var locLink = document.createElement('span');
                locLink.className = 'nav-link';
                locLink.textContent = shortPath(loc.filePath) + ':' + loc.line;
                locLink.addEventListener('click', function(){
                    vscodeApi.postMessage({
                        type:'navigateToCode',
                        filePath:loc.filePath,
                        line:loc.line || 1,
                    });
                });
                item.appendChild(locLink);
                observedSec.appendChild(item);
            });
            panel.appendChild(observedSec);
        }

        var connectionSummary = summarizeConnections(node);
        if (connectionSummary.items.length || connectionSummary.hiddenCount > 0) {
            var connSec = document.createElement('div');
            connSec.className = 'detail-section';
            var connTitle = document.createElement('div');
            connTitle.className = 'detail-section-title';
            connTitle.textContent = 'Connections';
            connSec.appendChild(connTitle);

            connectionSummary.items.forEach(function(entry){
                var item = document.createElement('div');
                item.className = 'evidence-item';
                if (entry.edge.isErrorPath) item.style.borderLeft = '2px solid #e74c3c';
                else if (hasTrace && entry.edge.metadata && entry.edge.metadata.runtime_hit) {
                    item.style.borderLeft = '2px solid #49cc90';
                }
                item.textContent = formatConnectionEntry(entry);
                connSec.appendChild(item);
            });

            if (connectionSummary.hiddenCount > 0) {
                var hint = document.createElement('div');
                hint.className = 'detail-section-value';
                hint.style.opacity = '0.65';
                hint.textContent = String(connectionSummary.hiddenCount) + ' connection'
                    + (connectionSummary.hiddenCount === 1 ? '' : 's')
                    + ' hidden at this abstraction level.';
                connSec.appendChild(hint);
            }
            panel.appendChild(connSec);
        }

        if (node.evidence && node.evidence.length) {
            var evSec = document.createElement('div');
            evSec.className = 'detail-section';
            var evTitle = document.createElement('div');
            evTitle.className = 'detail-section-title';
            evTitle.textContent = 'Evidence';
            evSec.appendChild(evTitle);

            node.evidence.forEach(function(ev){
                var item = document.createElement('div');
                item.className = 'evidence-item';
                var strong = document.createElement('strong');
                strong.textContent = ev.source;
                item.appendChild(strong);
                item.appendChild(document.createTextNode(': ' + ev.detail));
                if (ev.filePath) {
                    var lnk = document.createElement('span');
                    lnk.className = 'nav-link';
                    lnk.textContent = ' Go to code';
                    lnk.addEventListener('click', function(){
                        vscodeApi.postMessage({type:'navigateToCode',filePath:ev.filePath,line:ev.lineNumber||1});
                    });
                    item.appendChild(lnk);
                }
                evSec.appendChild(item);
            });
            panel.appendChild(evSec);
        }
    }

    function addSec(parent, title, value) {
        var s = document.createElement('div');
        s.className = 'detail-section';
        var t = document.createElement('div');
        t.className = 'detail-section-title';
        t.textContent = title;
        s.appendChild(t);
        var v = document.createElement('div');
        v.className = 'detail-section-value';
        v.textContent = String(value);
        s.appendChild(v);
        parent.appendChild(s);
    }

    function renderCodePreview(panel, node, primaryLocation) {
        if (!primaryLocation || !primaryLocation.filePath || !primaryLocation.line || !vscodeApi) {
            return;
        }

        var cacheKey = previewCacheKey(node, primaryLocation);
        var cached = codePreviewCache[cacheKey];

        if (!cached) {
            codePreviewRequestSeq += 1;
            codePreviewCache[cacheKey] = { loading: true, nodeId: node.id };
            vscodeApi.postMessage({
                type: 'loadCodePreview',
                requestId: codePreviewRequestSeq,
                cacheKey: cacheKey,
                nodeId: node.id,
                filePath: primaryLocation.filePath,
                lineStart: primaryLocation.line,
                lineEnd: primaryLocation.kind === 'definition'
                    ? (node.lineEnd || node.lineStart || primaryLocation.line)
                    : primaryLocation.line,
                locationKind: primaryLocation.kind,
            });
            cached = codePreviewCache[cacheKey];
        }

        var codeSec = document.createElement('div');
        codeSec.className = 'detail-section';
        var codeTitle = document.createElement('div');
        codeTitle.className = 'detail-section-title';
        codeTitle.textContent = 'Code';
        codeSec.appendChild(codeTitle);

        if (cached.error) {
            var err = document.createElement('div');
            err.className = 'detail-section-value';
            err.textContent = 'Could not load code preview: ' + cached.error;
            codeSec.appendChild(err);
            panel.appendChild(codeSec);
            return;
        }

        if (cached.loading || !cached.preview) {
            var loading = document.createElement('div');
            loading.className = 'detail-section-value';
            loading.textContent = 'Loading code preview...';
            codeSec.appendChild(loading);
            panel.appendChild(codeSec);
            return;
        }

        var meta = document.createElement('div');
        meta.className = 'code-preview-meta';
        meta.textContent = (
            cached.kind === 'callsite' ? 'Call-site preview' : 'Definition preview'
        ) + ' · lines ' + String(cached.startLine) + '-' + String(cached.endLine)
            + (cached.truncated ? ' (clipped)' : '');
        codeSec.appendChild(meta);

        var pre = document.createElement('pre');
        pre.className = 'code-preview';
        pre.textContent = String(cached.preview);
        codeSec.appendChild(pre);
        panel.appendChild(codeSec);
    }

    function previewCacheKey(node, primaryLocation) {
        var endLine = primaryLocation.kind === 'definition'
            ? (node.lineEnd || node.lineStart || primaryLocation.line)
            : primaryLocation.line;
        return [
            node.id,
            primaryLocation.kind,
            primaryLocation.filePath,
            primaryLocation.line,
            endLine,
        ].join('|');
    }

    function directLogicChildren(nodeId) {
        return Object.values(flowData.nodes).filter(function(n){
            return n.level === 4 && n.metadata && n.metadata.function_id === nodeId;
        });
    }

    function maxDrillDepth(node) {
        if (!node) return 0;
        return directLogicChildren(node.id).length > 0 ? 1 : 0;
    }

    function advanceNodeDrill(node, isNewSelection) {
        if (!node) return false;
        var maxDepth = maxDrillDepth(node);
        if (maxDepth <= 0) return false;
        var current = Number(nodeDrillState[node.id] || 0);
        var next = isNewSelection ? Math.max(current, 1) : ((current + 1) % (maxDepth + 1));
        if (next === current) return false;
        nodeDrillState[node.id] = next;
        return true;
    }

    function setNodeDrill(nodeId, depth) {
        var current = Number(nodeDrillState[nodeId] || 0);
        if (current === depth) return false;
        nodeDrillState[nodeId] = depth;
        return true;
    }

    function shouldShowLocalLogic(nodeId) {
        return Number(nodeDrillState[nodeId] || 0) >= 1;
    }

    function shouldShowCodePreview(node) {
        if (!node) return false;
        return !!getPrimaryLocation(node);
    }

    function getFunctionFlowTarget(node) {
        if (!node || !vscodeApi) return null;

        if (
            node.filePath
            && node.lineStart
            && (
                node.level >= 3
                || node.type === 'middleware'
                || node.type === 'dependency'
            )
        ) {
            return { filePath: node.filePath, line: node.lineStart };
        }

        if (node.type === 'dependency') {
            var resolved = null;
            flowData.edges.some(function(edge){
                if (edge.sourceId !== node.id || edge.type !== 'depends_on') return false;
                var target = flowData.nodes[edge.targetId];
                if (!target || !target.filePath || !target.lineStart) return false;
                resolved = { filePath: target.filePath, line: target.lineStart };
                return true;
            });
            if (resolved) return resolved;

            if (node.level >= 3 && node.filePath && node.lineStart) {
                return { filePath: node.filePath, line: node.lineStart };
            }
        }

        return null;
    }

    function getNestedCallTargets(node) {
        if (!node || !node.metadata || !Array.isArray(node.metadata.call_targets)) return [];
        return node.metadata.call_targets
            .filter(function(target){
                return target && target.file_path && target.line_start;
            })
            .map(function(target){
                return {
                    label: target.label || target.qualified_name || 'Open Call',
                    filePath: target.file_path,
                    line: target.line_start,
                    nodeType: target.node_type || '',
                };
            });
    }

    function summarizeConnections(node) {
        var groups = {};
        var hiddenCount = 0;
        var visibleNodeMap = getVisible(currentLevel).nodeMap;

        flowData.edges.forEach(function(edge){
            var direction = null;
            var otherId = null;

            if (edge.targetId === node.id) {
                direction = 'incoming';
                otherId = edge.sourceId;
            } else if (edge.sourceId === node.id) {
                direction = 'outgoing';
                otherId = edge.targetId;
            } else {
                return;
            }

            var other = flowData.nodes[otherId];
            if (!other) return;

            if ((edge.metadata && edge.metadata.structural_lift) || !visibleNodeMap[other.id]) {
                hiddenCount += 1;
                return;
            }

            var key = [
                direction,
                edge.type,
                other.id,
                edge.condition || '',
                edge.isErrorPath ? '1' : '0',
                edge.label || '',
            ].join('|');

            if (!groups[key]) {
                groups[key] = {
                    direction: direction,
                    other: other,
                    edge: edge,
                    count: 0,
                };
            }

            groups[key].count += 1;
        });

        var items = Object.values(groups);
        items.sort(function(a, b){
            return connectionWeight(a) - connectionWeight(b);
        });

        return {
            items: items.slice(0, 8),
            hiddenCount: hiddenCount + Math.max(0, items.length - 8),
        };
    }

    function connectionWeight(entry) {
        var weight = 0;
        if (entry.direction === 'outgoing') weight -= 5;
        if (entry.edge.metadata && entry.edge.metadata.runtime_hit) weight -= 40;
        if (entry.edge.metadata && entry.edge.metadata.pipeline_edge) weight -= 30;
        if (entry.edge.type === 'injects') weight -= 20;
        if (entry.edge.type === 'middleware_chain') weight -= 18;
        if (entry.edge.type === 'queries' || entry.edge.type === 'requests') weight -= 16;
        if (entry.edge.type === 'raises' || entry.edge.isErrorPath) weight -= 12;
        if (entry.other.metadata && entry.other.metadata.pipeline_order != null) {
            weight += Number(entry.other.metadata.pipeline_order);
        }
        weight += Number(entry.other.level || 0) * 2;
        weight += String(entry.other.displayName || entry.other.name || '').length / 100;
        return weight;
    }

    function formatConnectionEntry(entry) {
        var otherName = entry.other.displayName || entry.other.name || entry.other.id;
        var typeLabel = String(entry.edge.type || '').replace(/_/g, ' ');
        var prefix = entry.direction === 'incoming' ? '\u2190 from ' : '\u2192 to ';
        var text = prefix + otherName + ' (' + typeLabel + ')';

        if (entry.edge.condition) text += ' [' + entry.edge.condition + ']';
        else if (entry.edge.label && entry.edge.label !== otherName) text += ' [' + entry.edge.label + ']';

        if (entry.count > 1) text += ' x' + String(entry.count);
        return text;
    }

    function getEdgeLabel(edge) {
        if (!edge) return '';
        if (edge.condition) return edge.condition;
        if (isFunctionContext) {
            if (edge.metadata && edge.metadata.upstream_relation === 'reference') {
                return edge.label || 'reference';
            }
            return '';
        }
        return edge.label || '';
    }

    function getPrimaryLocation(node) {
        if (node.filePath) {
            return {
                filePath: node.filePath,
                line: node.lineStart || 1,
                kind: 'definition',
            };
        }
        var evidenceLocations = getEvidenceLocations(node);
        if (evidenceLocations.length) {
            return {
                filePath: evidenceLocations[0].filePath,
                line: evidenceLocations[0].line,
                kind: 'callsite',
            };
        }
        return null;
    }

    function getExtraLocations(node) {
        var primary = getPrimaryLocation(node);
        var evidenceLocations = getEvidenceLocations(node);
        if (!primary) return evidenceLocations.slice(0, 3);
        return evidenceLocations.filter(function(loc){
            return !(loc.filePath === primary.filePath && loc.line === primary.line);
        }).slice(0, 3);
    }

    function getEvidenceLocations(node) {
        var seen = {};
        var results = [];
        (node.evidence || []).forEach(function(ev){
            if (!ev || !ev.filePath || !ev.lineNumber) return;
            var key = ev.filePath + ':' + String(ev.lineNumber);
            if (seen[key]) return;
            seen[key] = true;
            results.push({ filePath: ev.filePath, line: ev.lineNumber });
        });
        return results;
    }

    function shortPath(p) { var parts = p.split('/'); return parts.slice(-2).join('/'); }

    var rt;
    window.addEventListener('resize', function(){ clearTimeout(rt); rt = setTimeout(function(){renderFlow(currentLevel);}, 200); });
    renderFlow(currentLevel);
    } catch (err) {
        showFatalError(err);
    }
})();
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
