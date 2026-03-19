import * as vscode from 'vscode';
import { SidebarProvider } from './sidebar';
import { FlowPanelProvider } from './flowPanel';
import { AnalysisServer } from './server';

let server: AnalysisServer;

export function activate(context: vscode.ExtensionContext) {
    server = new AnalysisServer(context.extensionPath, context.globalStorageUri.fsPath);

    const sidebarProvider = new SidebarProvider(context, server);
    const flowPanel = new FlowPanelProvider(context, server);

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('codecanvas.sidebar', sidebarProvider),

        vscode.commands.registerCommand('codecanvas.analyze', async () => {
            const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
            if (!workspaceFolder) {
                vscode.window.showErrorMessage('No workspace folder open');
                return;
            }
            await server.ensureRunning();
            const result = await server.analyze(workspaceFolder.uri.fsPath);
            if (result) {
                const entrypoints = result.entrypoints || result.endpoints || [];
                sidebarProvider.updateEntryPoints(entrypoints);
                vscode.window.showInformationMessage(
                    `CodeCanvas: Found ${result.entrypoint_count ?? entrypoints.length} entry points`
                );
            }
        }),

        vscode.commands.registerCommand('codecanvas.showFlow', async (entryId?: string) => {
            const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
            if (!workspaceFolder) return;

            if (!entryId) {
                // Show quick pick if not provided
                await server.ensureRunning();
                const result = await server.analyze(workspaceFolder.uri.fsPath);
                const entrypoints = result?.entrypoints || result?.endpoints || [];
                if (!result || !entrypoints.length) {
                    vscode.window.showWarningMessage('No entry points found');
                    return;
                }
                interface EntryPointItem extends vscode.QuickPickItem {
                    entryId: string;
                }
                const items: EntryPointItem[] = entrypoints.map((entry: any) => ({
                    label: entry.label,
                    description: `${entry.group} -> ${entry.handler_name}`,
                    detail: entry.description,
                    entryId: entry.id,
                }));
                const picked = await vscode.window.showQuickPick(items, {
                    placeHolder: 'Select entry point to visualize',
                });
                if (!picked) return;
                entryId = picked.entryId;
            }

            const flow = await server.getFlow(workspaceFolder.uri.fsPath, entryId!);
            if (flow) {
                flowPanel.showFlow(flow);
            }
        }),

        vscode.commands.registerCommand('codecanvas.showFunctionFlow', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showWarningMessage('No active editor');
                return;
            }
            if (editor.document.languageId !== 'python') {
                vscode.window.showWarningMessage('CodeCanvas function flow only supports Python files');
                return;
            }

            const workspaceFolder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
            if (!workspaceFolder) {
                vscode.window.showWarningMessage('The current file is not inside an open workspace');
                return;
            }

            await server.ensureRunning();
            const flow = await server.getFunctionFlow(
                workspaceFolder.uri.fsPath,
                editor.document.uri.fsPath,
                editor.selection.active.line + 1,
            );
            if (flow) {
                flowPanel.showFlow(flow);
            }
        }),

        vscode.commands.registerCommand('codecanvas.traceFlow', async (entryId?: string) => {
            const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
            if (!workspaceFolder) return;

            await server.ensureRunning();

            if (!entryId) {
                const result = await server.analyze(workspaceFolder.uri.fsPath);
                const entrypoints = result?.entrypoints || result?.endpoints || [];
                if (!entrypoints.length) {
                    vscode.window.showWarningMessage('No entry points found');
                    return;
                }
                interface EntryPointItem extends vscode.QuickPickItem {
                    entryId: string;
                    method: string;
                    path: string;
                }
                const items: EntryPointItem[] = entrypoints
                    .filter((e: any) => e.kind === 'api')
                    .map((entry: any) => ({
                        label: entry.label,
                        description: `${entry.group} -> ${entry.handler_name}`,
                        detail: entry.description,
                        entryId: entry.id,
                        method: entry.method,
                        path: entry.path,
                    }));
                const picked = await vscode.window.showQuickPick(items, {
                    placeHolder: 'Select API endpoint to trace',
                });
                if (!picked) return;
                entryId = picked.entryId;

                // Ask for request body (for POST/PUT/PATCH)
                let body: any = undefined;
                if (['POST', 'PUT', 'PATCH'].includes(picked.method)) {
                    const bodyStr = await vscode.window.showInputBox({
                        prompt: `JSON body for ${picked.method} ${picked.path}`,
                        placeHolder: '{"key": "value"}',
                    });
                    if (bodyStr) {
                        try { body = JSON.parse(bodyStr); } catch { body = bodyStr; }
                    }
                }

                const flow = await server.traceFlow(
                    workspaceFolder.uri.fsPath,
                    entryId!,
                    { method: picked.method, path: picked.path, body },
                );
                if (flow) {
                    flowPanel.showFlow(flow);
                }
                return;
            }

            // Direct call with entryId (from sidebar)
            const result = await server.analyze(workspaceFolder.uri.fsPath);
            const entrypoints = result?.entrypoints || result?.endpoints || [];
            const entry = entrypoints.find((e: any) => e.id === entryId);
            if (!entry) return;

            const flow = await server.traceFlow(
                workspaceFolder.uri.fsPath,
                entryId,
                { method: entry.method || 'GET', path: entry.path || '/' },
            );
            if (flow) {
                flowPanel.showFlow(flow);
            }
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codecanvas.showFlowData', (flow: any) => {
            if (flow) {
                flowPanel.showFlow(flow);
            }
        }),
    );

    // Auto-analyze on startup if workspace has Python files
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (workspaceFolder) {
        vscode.workspace.findFiles('**/*.py', '**/node_modules/**', 1).then(files => {
            if (files.length > 0) {
                vscode.commands.executeCommand('codecanvas.analyze');
            }
        });
    }
}

export function deactivate() {
    server?.stop();
}
