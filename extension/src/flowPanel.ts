import * as vscode from 'vscode';
import { AnalysisServer } from './server';

export class FlowPanelProvider {
    private panel: vscode.WebviewPanel | null = null;

    constructor(
        private context: vscode.ExtensionContext,
        private server: AnalysisServer,
    ) {}

    showFlow(flowData: any) {
        if (this.panel) {
            this.panel.reveal(vscode.ViewColumn.Two);
        } else {
            this.panel = vscode.window.createWebviewPanel(
                'codecanvas.flow',
                `Flow: ${flowData.endpoint.method} ${flowData.endpoint.path}`,
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
                if (msg.type === 'navigateToCode') {
                    const uri = vscode.Uri.file(msg.filePath);
                    const doc = await vscode.workspace.openTextDocument(uri);
                    const editor = await vscode.window.showTextDocument(doc, vscode.ViewColumn.One);
                    const line = Math.max(0, (msg.line || 1) - 1);
                    const range = new vscode.Range(line, 0, line, 0);
                    editor.selection = new vscode.Selection(range.start, range.end);
                    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
                }
                if (msg.type === 'requestLevel') {
                    this.panel?.webview.postMessage({
                        type: 'updateFlow',
                        data: flowData,
                        level: msg.level,
                    });
                }
            });
        }

        this.panel.title = `Flow: ${flowData.endpoint.method} ${flowData.endpoint.path}`;
        this.panel.webview.html = this.getWebviewHtml(flowData);
    }

    private getWebviewHtml(flowData: any): string {
        const encodedData = Buffer.from(JSON.stringify(flowData)).toString('base64');
        const nonce = getNonce();

        return `<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';" />
    <style nonce="${nonce}">
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: var(--vscode-font-family);
            background: var(--vscode-editor-background);
            color: var(--vscode-foreground);
            overflow: hidden;
            width: 100vw; height: 100vh;
        }
        #app { width: 100%; height: 100%; display: flex; flex-direction: column; }

        .topbar {
            display: flex; align-items: center; gap: 12px;
            padding: 8px 16px;
            background: var(--vscode-titleBar-activeBackground);
            border-bottom: 1px solid var(--vscode-panel-border);
            flex-shrink: 0;
        }
        .topbar label { font-size: 12px; opacity: 0.7; }
        .topbar input[type=range] { flex: 1; max-width: 300px; }
        .level-label { font-size: 12px; font-weight: bold; min-width: 180px; }
        .endpoint-badge { font-size: 13px; font-weight: bold; margin-left: auto; }
        .method-GET { color: #61affe; }
        .method-POST { color: #49cc90; }
        .method-PUT { color: #fca130; }
        .method-DELETE { color: #f93e3e; }

        .main { display: flex; flex: 1; overflow: hidden; }
        .canvas-container { flex: 1; overflow: auto; padding: 24px; }

        /* DAG layout */
        .flow-container { position: relative; display: inline-block; min-width: 100%; }
        .flow-nodes {
            display: flex; flex-direction: column; gap: 20px;
            align-items: center; position: relative; z-index: 1;
            padding: 0 120px;
        }
        .edge-svg {
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            pointer-events: none; overflow: visible; z-index: 0;
        }

        .flow-node {
            background: var(--vscode-editor-background);
            border: 1px solid var(--vscode-panel-border);
            border-radius: 8px;
            padding: 12px 16px;
            min-width: 200px; max-width: 360px;
            cursor: pointer;
            transition: all 0.15s ease;
            position: relative;
        }
        .flow-node:hover {
            border-color: var(--vscode-focusBorder);
            box-shadow: 0 0 8px rgba(100, 149, 237, 0.3);
        }
        .flow-node.selected { border-color: var(--vscode-focusBorder); background: var(--vscode-list-activeSelectionBackground); }
        .flow-node .node-type { font-size: 10px; text-transform: uppercase; opacity: 0.5; margin-bottom: 2px; }
        .flow-node .node-name { font-weight: bold; font-size: 14px; }
        .flow-node .node-desc { font-size: 11px; opacity: 0.6; margin-top: 4px; max-width: 300px; }
        .flow-node .node-file { font-size: 10px; opacity: 0.4; margin-top: 4px; }
        .confidence-definite { border-left: 3px solid #49cc90; }
        .confidence-high { border-left: 3px solid #61affe; }
        .confidence-inferred { border-left: 3px solid #fca130; border-left-style: dashed; }
        .confidence-runtime { border-left: 3px solid #9b59b6; border-left-style: dotted; }

        .type-client { border-top: 3px solid #61affe; }
        .type-api { border-top: 3px solid #49cc90; }
        .type-router { border-top: 3px solid #49cc90; }
        .type-service { border-top: 3px solid #fca130; }
        .type-repository { border-top: 3px solid #9b59b6; }
        .type-database { border-top: 3px solid #e74c3c; }
        .type-external_api { border-top: 3px solid #e67e22; }
        .type-middleware { border-top: 3px solid #1abc9c; }
        .type-dependency { border-top: 3px solid #3498db; }
        .type-function { border-top: 3px solid #95a5a6; }
        .type-method { border-top: 3px solid #95a5a6; }
        .type-exception { border-top: 3px solid #e74c3c; background: rgba(231,76,60,0.08); }
        .type-branch { border-top: 3px solid #f39c12; }
        .type-loop { border-top: 3px solid #2ecc71; }
        .type-file { border-top: 3px solid #8e44ad; }
        .type-module { border-top: 3px solid #2c3e50; }

        .detail-panel {
            width: 320px;
            border-left: 1px solid var(--vscode-panel-border);
            overflow-y: auto; padding: 16px; flex-shrink: 0;
            display: none;
        }
        .detail-panel.visible { display: block; }
        .detail-panel h3 { font-size: 14px; margin-bottom: 12px; }
        .detail-section { margin-bottom: 16px; }
        .detail-section h4 { font-size: 11px; text-transform: uppercase; opacity: 0.5; margin-bottom: 4px; }
        .detail-section .value { font-size: 13px; }
        .evidence-item {
            font-size: 11px; padding: 4px 8px;
            background: var(--vscode-textBlockQuote-background);
            border-radius: 4px; margin-top: 4px;
        }
        .nav-link { color: var(--vscode-textLink-foreground); cursor: pointer; text-decoration: underline; font-size: 12px; }
        pre { font-size: 11px; white-space: pre-wrap; word-break: break-all; }
    </style>
</head>
<body>
    <div id="app">
        <div class="topbar">
            <label>Abstraction Level:</label>
            <input type="range" id="levelSlider" min="0" max="4" value="3" step="1" />
            <span class="level-label" id="levelLabel">Level 3: Functions</span>
            <span class="endpoint-badge" id="endpointBadge"></span>
        </div>
        <div class="main">
            <div class="canvas-container" id="canvas"></div>
            <div class="detail-panel" id="detailPanel"></div>
        </div>
    </div>

    <script nonce="${nonce}">
        var vscodeApi = acquireVsCodeApi();
        var flowData = JSON.parse(atob("${encodedData}"));

        var LEVEL_NAMES = {
            0: 'Level 0: System Overview',
            1: 'Level 1: Service Layers',
            2: 'Level 2: Files',
            3: 'Level 3: Functions',
            4: 'Level 4: Logic / Branches'
        };

        var EDGE_COLORS = {
            calls: null,
            returns: '#27ae60',
            raises: '#e74c3c',
            queries: '#9b59b6',
            requests: '#e67e22',
            middleware_chain: '#1abc9c',
            injects: '#3498db',
            depends_on: '#3498db'
        };

        var currentLevel = 3;
        var selectedNodeId = null;

        // Set endpoint badge
        var badge = document.getElementById('endpointBadge');
        var methodSpan = document.createElement('span');
        methodSpan.className = 'method-' + flowData.endpoint.method;
        methodSpan.textContent = flowData.endpoint.method + ' ';
        badge.appendChild(methodSpan);
        badge.appendChild(document.createTextNode(flowData.endpoint.path));

        renderFlow(currentLevel);

        document.getElementById('levelSlider').addEventListener('input', function(e) {
            currentLevel = parseInt(e.target.value);
            document.getElementById('levelLabel').textContent = LEVEL_NAMES[currentLevel];
            renderFlow(currentLevel);
        });

        function renderFlow(level) {
            var canvas = document.getElementById('canvas');
            var nodes = flowData.nodes;
            var edges = flowData.edges;

            var visibleNodes = Object.values(nodes).filter(function(n) { return n.level <= level; });
            var visibleIds = new Set(visibleNodes.map(function(n) { return n.id; }));
            var visibleEdges = edges.filter(function(e) {
                return visibleIds.has(e.sourceId) && visibleIds.has(e.targetId);
            });

            var sorted = topoSort(visibleNodes, visibleEdges);

            // Build DOM
            canvas.textContent = '';
            var container = document.createElement('div');
            container.className = 'flow-container';

            var nodesDiv = document.createElement('div');
            nodesDiv.className = 'flow-nodes';

            var nodeEls = {};
            for (var i = 0; i < sorted.length; i++) {
                var node = sorted[i];
                var nodeEl = createNodeEl(node);
                nodesDiv.appendChild(nodeEl);
                nodeEls[node.id] = nodeEl;
            }

            container.appendChild(nodesDiv);
            canvas.appendChild(container);

            // Draw SVG edges after layout settles
            requestAnimationFrame(function() {
                drawEdges(container, nodesDiv, nodeEls, sorted, visibleEdges);
            });
        }

        function createNodeEl(node) {
            var el = document.createElement('div');
            el.className = 'flow-node type-' + node.type + ' confidence-' + node.confidence;
            if (node.id === selectedNodeId) el.className += ' selected';
            el.dataset.id = node.id;

            var typeEl = document.createElement('div');
            typeEl.className = 'node-type';
            typeEl.textContent = node.type.replace('_', ' ');
            el.appendChild(typeEl);

            var nameEl = document.createElement('div');
            nameEl.className = 'node-name';
            nameEl.textContent = node.displayName;
            el.appendChild(nameEl);

            if (node.description) {
                var descEl = document.createElement('div');
                descEl.className = 'node-desc';
                descEl.textContent = node.description.length > 80
                    ? node.description.slice(0, 80) + '...'
                    : node.description;
                el.appendChild(descEl);
            }

            if (node.filePath) {
                var fileEl = document.createElement('div');
                fileEl.className = 'node-file';
                fileEl.textContent = shortPath(node.filePath) + ':' + (node.lineStart || '');
                el.appendChild(fileEl);
            }

            (function(nid) {
                el.addEventListener('click', function() { selectNode(nid); });
            })(node.id);

            return el;
        }

        function drawEdges(container, nodesDiv, nodeEls, sorted, edges) {
            var svgNS = 'http://www.w3.org/2000/svg';
            var svg = document.createElementNS(svgNS, 'svg');
            svg.setAttribute('class', 'edge-svg');

            var containerRect = container.getBoundingClientRect();
            svg.style.height = container.scrollHeight + 'px';

            // Arrow markers
            var defs = document.createElementNS(svgNS, 'defs');
            var markers = [
                { id: 'arrow', fill: 'var(--vscode-foreground)' },
                { id: 'arrowError', fill: '#e74c3c' },
                { id: 'arrowQuery', fill: '#9b59b6' },
                { id: 'arrowInject', fill: '#3498db' },
                { id: 'arrowMw', fill: '#1abc9c' },
                { id: 'arrowHttp', fill: '#e67e22' }
            ];
            markers.forEach(function(m) {
                var marker = document.createElementNS(svgNS, 'marker');
                marker.setAttribute('id', m.id);
                marker.setAttribute('viewBox', '0 0 10 10');
                marker.setAttribute('refX', '9');
                marker.setAttribute('refY', '5');
                marker.setAttribute('markerWidth', '6');
                marker.setAttribute('markerHeight', '6');
                marker.setAttribute('orient', 'auto-start-reverse');
                var p = document.createElementNS(svgNS, 'path');
                p.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
                p.setAttribute('fill', m.fill);
                marker.appendChild(p);
                defs.appendChild(marker);
            });
            svg.appendChild(defs);

            // Build node index for position lookup
            var nodeIndex = {};
            sorted.forEach(function(n, idx) { nodeIndex[n.id] = idx; });
            var parallelEdges = new Map();
            edges.forEach(function(edge) {
                var key = edge.sourceId + '>' + edge.targetId;
                if (!parallelEdges.has(key)) parallelEdges.set(key, []);
                parallelEdges.get(key).push(edge);
            });

            edges.forEach(function(edge) {
                var srcEl = nodeEls[edge.sourceId];
                var tgtEl = nodeEls[edge.targetId];
                if (!srcEl || !tgtEl) return;

                var srcRect = srcEl.getBoundingClientRect();
                var tgtRect = tgtEl.getBoundingClientRect();

                // Source: bottom center, Target: top center (relative to container)
                var sx = srcRect.left + srcRect.width / 2 - containerRect.left;
                var sy = srcRect.bottom - containerRect.top;
                var tx = tgtRect.left + tgtRect.width / 2 - containerRect.left;
                var ty = tgtRect.top - containerRect.top;

                var srcIdx = nodeIndex[edge.sourceId];
                var tgtIdx = nodeIndex[edge.targetId];
                var gap = (tgtIdx !== undefined && srcIdx !== undefined) ? tgtIdx - srcIdx : 1;
                var edgeGroup = parallelEdges.get(edge.sourceId + '>' + edge.targetId) || [edge];
                var edgeOffsetIndex = edgeGroup.findIndex(function(groupedEdge) {
                    return groupedEdge.id === edge.id;
                });
                var parallelOffset = (edgeOffsetIndex - (edgeGroup.length - 1) / 2) * 24;

                var path = document.createElementNS(svgNS, 'path');
                var d;

                if (gap === 1 && edgeGroup.length === 1) {
                    // Adjacent: straight line
                    d = 'M' + sx + ',' + sy + ' L' + tx + ',' + ty;
                } else if (gap === 1) {
                    // Adjacent parallel edges: bend them apart
                    d = 'M' + sx + ',' + sy
                      + ' C' + (sx + parallelOffset) + ',' + (sy + 28)
                      + ' ' + (tx + parallelOffset) + ',' + (ty - 28)
                      + ' ' + tx + ',' + ty;
                } else if (gap > 1) {
                    // Forward non-adjacent: curve right
                    var offset = Math.min(gap * 25, 110) + parallelOffset;
                    var my = (sy + ty) / 2;
                    d = 'M' + sx + ',' + sy
                      + ' C' + (sx + offset) + ',' + (sy + 30)
                      + ' ' + (tx + offset) + ',' + (ty - 30)
                      + ' ' + tx + ',' + ty;
                } else {
                    // Backward or side: curve left
                    var offsetB = -Math.min(Math.abs(gap) * 25 + 40, 130) + parallelOffset;
                    d = 'M' + sx + ',' + sy
                      + ' C' + (sx + offsetB) + ',' + (sy + 30)
                      + ' ' + (tx + offsetB) + ',' + (ty - 30)
                      + ' ' + tx + ',' + ty;
                }

                path.setAttribute('d', d);
                path.setAttribute('fill', 'none');
                path.setAttribute('stroke-width', edge.isErrorPath ? '2' : '1.5');
                path.setAttribute('stroke-opacity', '0.6');

                // Color + marker by edge type
                var color = EDGE_COLORS[edge.type] || null;
                var markerId = 'arrow';
                if (edge.isErrorPath || edge.type === 'raises') {
                    color = '#e74c3c'; markerId = 'arrowError';
                } else if (edge.type === 'queries') {
                    markerId = 'arrowQuery';
                } else if (edge.type === 'injects') {
                    markerId = 'arrowInject';
                    path.setAttribute('stroke-dasharray', '6,3');
                } else if (edge.type === 'middleware_chain') {
                    markerId = 'arrowMw';
                } else if (edge.type === 'requests') {
                    markerId = 'arrowHttp';
                }
                path.setAttribute('stroke', color || 'var(--vscode-foreground)');
                path.setAttribute('marker-end', 'url(#' + markerId + ')');
                svg.appendChild(path);

                // Edge label
                var label = edge.condition || edge.label || '';
                if (label) {
                    var mx, my2;
                    if (gap === 1) {
                        mx = (sx + tx) / 2 + parallelOffset + 8;
                        my2 = (sy + ty) / 2;
                    } else if (gap > 1) {
                        var labelOff = Math.min(gap * 15, 60) + parallelOffset;
                        mx = (sx + tx) / 2 + labelOff + 8;
                        my2 = (sy + ty) / 2;
                    } else {
                        mx = (sx + tx) / 2 - 60 + parallelOffset;
                        my2 = (sy + ty) / 2;
                    }
                    var text = document.createElementNS(svgNS, 'text');
                    text.setAttribute('x', String(mx));
                    text.setAttribute('y', String(my2));
                    text.setAttribute('font-size', '10');
                    text.setAttribute('fill', color || 'var(--vscode-foreground)');
                    text.setAttribute('opacity', '0.8');
                    text.textContent = label.length > 35 ? label.slice(0, 35) + '...' : label;
                    svg.appendChild(text);
                }
            });

            container.insertBefore(svg, nodesDiv);
        }

        function selectNode(nodeId) {
            selectedNodeId = nodeId;
            var node = flowData.nodes[nodeId];
            if (!node) return;
            renderFlow(currentLevel);
            showDetail(node);
        }

        function showDetail(node) {
            var panel = document.getElementById('detailPanel');
            panel.classList.add('visible');
            panel.textContent = '';

            var h3 = document.createElement('h3');
            h3.textContent = node.displayName;
            panel.appendChild(h3);

            addSection(panel, 'Type', node.type);
            addSection(panel, 'Confidence', node.confidence);
            if (node.description) addSection(panel, 'Description', node.description);

            if (node.filePath) {
                var locSection = document.createElement('div');
                locSection.className = 'detail-section';
                var locH4 = document.createElement('h4');
                locH4.textContent = 'Location';
                locSection.appendChild(locH4);
                var locLink = document.createElement('span');
                locLink.className = 'nav-link';
                locLink.textContent = shortPath(node.filePath) + ':' + (node.lineStart || '');
                locLink.addEventListener('click', function() {
                    vscodeApi.postMessage({ type: 'navigateToCode', filePath: node.filePath, line: node.lineStart || 1 });
                });
                locSection.appendChild(locLink);
                panel.appendChild(locSection);
            }

            // Connections summary
            var incoming = flowData.edges.filter(function(e) { return e.targetId === node.id; });
            var outgoing = flowData.edges.filter(function(e) { return e.sourceId === node.id; });
            if (incoming.length + outgoing.length > 0) {
                var connSection = document.createElement('div');
                connSection.className = 'detail-section';
                var connH4 = document.createElement('h4');
                connH4.textContent = 'Connections';
                connSection.appendChild(connH4);

                incoming.forEach(function(e) {
                    var srcNode = flowData.nodes[e.sourceId];
                    var item = document.createElement('div');
                    item.className = 'evidence-item';
                    var arrow = document.createTextNode(String.fromCharCode(8592) + ' ');
                    item.appendChild(arrow);
                    var b = document.createElement('strong');
                    b.textContent = e.type;
                    item.appendChild(b);
                    item.appendChild(document.createTextNode(' from ' + (srcNode ? srcNode.displayName : e.sourceId)));
                    if (e.condition) item.appendChild(document.createTextNode(' [' + e.condition + ']'));
                    connSection.appendChild(item);
                });
                outgoing.forEach(function(e) {
                    var tgtNode = flowData.nodes[e.targetId];
                    var item = document.createElement('div');
                    item.className = 'evidence-item';
                    if (e.isErrorPath) item.style.borderLeft = '2px solid #e74c3c';
                    var arrow = document.createTextNode(String.fromCharCode(8594) + ' ');
                    item.appendChild(arrow);
                    var b = document.createElement('strong');
                    b.textContent = e.type;
                    item.appendChild(b);
                    item.appendChild(document.createTextNode(' to ' + (tgtNode ? tgtNode.displayName : e.targetId)));
                    if (e.condition) item.appendChild(document.createTextNode(' [' + e.condition + ']'));
                    connSection.appendChild(item);
                });
                panel.appendChild(connSection);
            }

            if (node.metadata && Object.keys(node.metadata).length > 0) {
                var metaSection = document.createElement('div');
                metaSection.className = 'detail-section';
                var metaH4 = document.createElement('h4');
                metaH4.textContent = 'Metadata';
                metaSection.appendChild(metaH4);
                var pre = document.createElement('pre');
                pre.textContent = JSON.stringify(node.metadata, null, 2);
                metaSection.appendChild(pre);
                panel.appendChild(metaSection);
            }

            if (node.evidence && node.evidence.length > 0) {
                var evSection = document.createElement('div');
                evSection.className = 'detail-section';
                var evH4 = document.createElement('h4');
                evH4.textContent = 'Evidence';
                evSection.appendChild(evH4);
                node.evidence.forEach(function(ev) {
                    var item = document.createElement('div');
                    item.className = 'evidence-item';
                    var strong = document.createElement('strong');
                    strong.textContent = ev.source;
                    item.appendChild(strong);
                    item.appendChild(document.createTextNode(': ' + ev.detail));
                    if (ev.filePath) {
                        var link = document.createElement('span');
                        link.className = 'nav-link';
                        link.textContent = ' Go to code';
                        link.addEventListener('click', function() {
                            vscodeApi.postMessage({ type: 'navigateToCode', filePath: ev.filePath, line: ev.lineNumber || 1 });
                        });
                        item.appendChild(link);
                    }
                    evSection.appendChild(item);
                });
                panel.appendChild(evSection);
            }
        }

        function addSection(parent, title, value) {
            var section = document.createElement('div');
            section.className = 'detail-section';
            var h4 = document.createElement('h4');
            h4.textContent = title;
            section.appendChild(h4);
            var valDiv = document.createElement('div');
            valDiv.className = 'value';
            valDiv.textContent = value;
            section.appendChild(valDiv);
            parent.appendChild(section);
        }

        function topoSort(nodes, edges) {
            var graph = new Map();
            var inDegree = new Map();
            nodes.forEach(function(n) { graph.set(n.id, []); inDegree.set(n.id, 0); });
            edges.forEach(function(e) {
                if (graph.has(e.sourceId) && graph.has(e.targetId)) {
                    graph.get(e.sourceId).push(e.targetId);
                    inDegree.set(e.targetId, (inDegree.get(e.targetId) || 0) + 1);
                }
            });
            var queue = [];
            inDegree.forEach(function(deg, id) { if (deg === 0) queue.push(id); });
            var result = [];
            while (queue.length > 0) {
                var id = queue.shift();
                var node = nodes.find(function(n) { return n.id === id; });
                if (node) result.push(node);
                (graph.get(id) || []).forEach(function(next) {
                    inDegree.set(next, inDegree.get(next) - 1);
                    if (inDegree.get(next) === 0) queue.push(next);
                });
            }
            nodes.forEach(function(n) { if (!result.includes(n)) result.push(n); });
            return result;
        }

        function shortPath(p) {
            var parts = p.split('/');
            return parts.slice(-2).join('/');
        }

        window.addEventListener('message', function(event) {
            var msg = event.data;
            if (msg.type === 'updateFlow') {
                Object.assign(flowData, msg.data);
                renderFlow(msg.level || currentLevel);
            }
        });
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
