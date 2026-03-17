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
                sidebarProvider.updateEndpoints(result.endpoints);
                vscode.window.showInformationMessage(
                    `CodeCanvas: Found ${result.endpoint_count} endpoints`
                );
            }
        }),

        vscode.commands.registerCommand('codecanvas.showFlow', async (method?: string, path?: string) => {
            const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
            if (!workspaceFolder) return;

            if (!method || !path) {
                // Show quick pick if not provided
                await server.ensureRunning();
                const result = await server.analyze(workspaceFolder.uri.fsPath);
                if (!result || !result.endpoints.length) {
                    vscode.window.showWarningMessage('No endpoints found');
                    return;
                }
                interface EndpointItem extends vscode.QuickPickItem {
                    method: string;
                    endpointPath: string;
                }
                const items: EndpointItem[] = result.endpoints.map((ep: any) => ({
                    label: `${ep.method} ${ep.path}`,
                    description: ep.handler_name,
                    detail: ep.description,
                    method: ep.method,
                    endpointPath: ep.path,
                }));
                const picked = await vscode.window.showQuickPick(items, {
                    placeHolder: 'Select endpoint to visualize',
                });
                if (!picked) return;
                method = picked.method;
                path = picked.endpointPath;
            }

            const flow = await server.getFlow(workspaceFolder.uri.fsPath, method!, path!);
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
