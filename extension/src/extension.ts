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
