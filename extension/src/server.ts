import * as vscode from 'vscode';
import { ChildProcess, spawn, execFile } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

const CORE_DEPS = ['fastapi', 'uvicorn', 'libcst>=1.0.0', 'httpx>=0.24.0'];

export class AnalysisServer {
    private process: ChildProcess | null = null;
    private ready = false;
    private startupError: string | null = null;
    private envReady = false;
    private resolvedPython: string | null = null;
    private serverPort: number | null = null;

    constructor(
        private extensionPath: string,
        private storagePath: string,
    ) {}

    private get baseUrl(): string {
        return `http://127.0.0.1:${this.serverPort}`;
    }

    async ensureRunning(): Promise<void> {
        if (this.ready && this.serverPort) return;

        // Ensure Python environment is ready (venv + deps)
        if (!this.envReady) {
            await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: 'CodeCanvas' },
                (progress) => this.ensureEnvironment(progress),
            );
        }

        await this.start();
    }

    private getCorePath(): string {
        // Resolve symlinks so ../core works when extension dir is a symlink
        const realExtPath = fs.realpathSync(this.extensionPath);
        const bundledCore = path.join(realExtPath, 'core');
        if (fs.existsSync(path.join(bundledCore, 'codecanvas'))) {
            return bundledCore;
        }
        return path.join(realExtPath, '..', 'core');
    }

    private findBasePython(): string {
        const config = vscode.workspace.getConfiguration('codecanvas');
        const configured = config.get<string>('pythonPath');
        if (configured) return configured;
        return process.platform === 'win32' ? 'python' : 'python3';
    }

    private venvPython(): string {
        const venvDir = path.join(this.storagePath, 'venv');
        return process.platform === 'win32'
            ? path.join(venvDir, 'Scripts', 'python.exe')
            : path.join(venvDir, 'bin', 'python');
    }

    /** Safe wrapper around execFile (no shell injection). */
    private execSafe(cmd: string, args: string[]): Promise<string> {
        return new Promise((resolve, reject) => {
            execFile(cmd, args, { timeout: 120_000 }, (err, stdout, stderr) => {
                if (err) reject(new Error(stderr || err.message));
                else resolve(stdout);
            });
        });
    }

    private async ensureEnvironment(
        progress: vscode.Progress<{ message?: string; increment?: number }>,
    ): Promise<void> {
        const corePath = this.getCorePath();

        // Option 1: Dev mode — core/.venv exists and has deps
        const devVenvPy = process.platform === 'win32'
            ? path.join(corePath, '.venv', 'Scripts', 'python.exe')
            : path.join(corePath, '.venv', 'bin', 'python');
        if (fs.existsSync(devVenvPy)) {
            if (await this.checkDeps(devVenvPy)) {
                this.resolvedPython = devVenvPy;
                this.envReady = true;
                return;
            }
        }

        // Option 2: Managed venv in extension global storage
        fs.mkdirSync(this.storagePath, { recursive: true });
        const managedPy = this.venvPython();
        const venvDir = path.join(this.storagePath, 'venv');

        if (fs.existsSync(managedPy) && await this.checkDeps(managedPy)) {
            this.resolvedPython = managedPy;
            this.envReady = true;
            return;
        }

        // Create managed venv
        progress.report({ message: 'Creating Python environment...' });
        const basePython = this.findBasePython();
        try {
            await this.execSafe(basePython, ['-m', 'venv', venvDir]);
        } catch (err: any) {
            vscode.window.showErrorMessage(
                `CodeCanvas: Failed to create venv. Ensure Python 3.10+ is installed.\n${err.message}`,
            );
            throw err;
        }

        // Install dependencies
        progress.report({ message: 'Installing dependencies (fastapi, uvicorn, libcst)...' });
        try {
            await this.execSafe(managedPy, ['-m', 'pip', 'install', '--quiet', ...CORE_DEPS]);
        } catch (err: any) {
            vscode.window.showErrorMessage(
                `CodeCanvas: Failed to install dependencies.\n${err.message}`,
            );
            throw err;
        }

        this.resolvedPython = managedPy;
        this.envReady = true;
    }

    private async checkDeps(pythonPath: string): Promise<boolean> {
        try {
            await this.execSafe(pythonPath, [
                '-c', 'import fastapi; import uvicorn; import libcst',
            ]);
            return true;
        } catch {
            return false;
        }
    }

    private async start(): Promise<void> {
        this.startupError = null;
        const pythonPath = this.resolvedPython || this.findBasePython();
        const corePath = this.getCorePath();
        const stderrChunks: string[] = [];

        return new Promise<void>((resolve, reject) => {
            // Pass port=0 so the Python server picks a free port
            // and prints CODECANVAS_PORT=<port> on stdout.
            this.process = spawn(
                pythonPath,
                ['-m', 'codecanvas.server.app', '0'],
                {
                    cwd: corePath,
                    env: {
                        ...process.env,
                        PYTHONPATH: corePath,
                    },
                },
            );

            const timeout = setTimeout(() => {
                if (!this.ready) {
                    const errMsg = stderrChunks.join('').trim();
                    this.startupError = errMsg || 'Server did not start within 15 seconds';
                    vscode.window.showErrorMessage(
                        `CodeCanvas server failed to start: ${this.startupError}`,
                    );
                    this.process?.kill();
                    reject(new Error(this.startupError));
                }
            }, 15000);

            // Read the dynamically assigned port from stdout.
            this.process.stdout?.on('data', (data: Buffer) => {
                const msg = data.toString();
                const match = msg.match(/CODECANVAS_PORT=(\d+)/);
                if (match) {
                    this.serverPort = parseInt(match[1], 10);
                }
            });

            this.process.stderr?.on('data', (data: Buffer) => {
                const msg = data.toString();
                stderrChunks.push(msg);
                if (msg.includes('Uvicorn running') || msg.includes('Application startup complete')) {
                    if (this.serverPort) {
                        this.ready = true;
                        clearTimeout(timeout);
                        resolve();
                    } else {
                        // Port not yet received — wait briefly for stdout flush
                        setTimeout(() => {
                            if (this.serverPort) {
                                this.ready = true;
                                clearTimeout(timeout);
                                resolve();
                            }
                        }, 200);
                    }
                }
            });

            this.process.on('error', (err) => {
                clearTimeout(timeout);
                this.startupError = err.message;
                if (err.message.includes('ENOENT')) {
                    vscode.window.showErrorMessage(
                        `CodeCanvas: "${pythonPath}" not found. Install Python 3.10+ or set codecanvas.pythonPath.`,
                    );
                } else {
                    vscode.window.showErrorMessage(`CodeCanvas server failed: ${err.message}`);
                }
                reject(err);
            });

            this.process.on('exit', (code) => {
                clearTimeout(timeout);
                this.ready = false;
                this.process = null;
                this.serverPort = null;
                if (code && code !== 0 && !this.startupError) {
                    const errMsg = stderrChunks.join('').trim();
                    vscode.window.showErrorMessage(
                        `CodeCanvas server exited (code ${code}): ${errMsg.slice(-200)}`,
                    );
                }
            });
        });
    }

    async analyze(projectPath: string): Promise<any> {
        if (!this.ready) {
            vscode.window.showErrorMessage('CodeCanvas server is not running.');
            return null;
        }
        try {
            const res = await fetch(`${this.baseUrl}/analyze`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_path: projectPath }),
            });
            if (!res.ok) {
                const body = await res.text();
                vscode.window.showErrorMessage(`Analysis failed (${res.status}): ${body.slice(0, 200)}`);
                return null;
            }
            return await res.json();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Analysis failed: ${err.message}`);
            return null;
        }
    }

    async getFlow(projectPath: string, entryId: string): Promise<any> {
        if (!this.ready) {
            vscode.window.showErrorMessage('CodeCanvas server is not running.');
            return null;
        }
        try {
            const res = await fetch(`${this.baseUrl}/flow`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_path: projectPath, entry_id: entryId }),
            });
            if (!res.ok) {
                const body = await res.text();
                vscode.window.showErrorMessage(`Flow generation failed (${res.status}): ${body.slice(0, 200)}`);
                return null;
            }
            return await res.json();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Flow generation failed: ${err.message}`);
            return null;
        }
    }

    async getFunctionFlow(projectPath: string, filePath: string, line: number): Promise<any> {
        if (!this.ready) {
            vscode.window.showErrorMessage('CodeCanvas server is not running.');
            return null;
        }
        try {
            const res = await fetch(`${this.baseUrl}/flow/from-location`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_path: projectPath,
                    file_path: filePath,
                    line,
                }),
            });
            if (!res.ok) {
                const body = await res.text();
                vscode.window.showErrorMessage(`Function flow failed (${res.status}): ${body.slice(0, 200)}`);
                return null;
            }
            return await res.json();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Function flow failed: ${err.message}`);
            return null;
        }
    }

    async traceFlow(
        projectPath: string,
        entryId: string,
        request: { method: string; path: string; headers?: Record<string, string>; body?: any },
    ): Promise<any> {
        if (!this.ready) {
            vscode.window.showErrorMessage('CodeCanvas server is not running.');
            return null;
        }
        try {
            const res = await fetch(`${this.baseUrl}/trace`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_path: projectPath,
                    entry_id: entryId,
                    request,
                }),
            });
            if (!res.ok) {
                const body = await res.text();
                vscode.window.showErrorMessage(`Trace failed (${res.status}): ${body.slice(0, 200)}`);
                return null;
            }
            return await res.json();
        } catch (err: any) {
            vscode.window.showErrorMessage(`Trace failed: ${err.message}`);
            return null;
        }
    }

    async getImpact(
        projectPath: string,
        opts: { diffText?: string; gitRef?: string; entryId?: string } = {},
    ): Promise<any> {
        if (!this.ready) { return null; }
        try {
            const res = await fetch(`${this.baseUrl}/impact`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_path: projectPath,
                    diff_text: opts.diffText,
                    git_ref: opts.gitRef,
                    entry_id: opts.entryId,
                }),
            });
            if (!res.ok) { return null; }
            return await res.json();
        } catch {
            return null;
        }
    }

    stop(): void {
        this.process?.kill();
        this.process = null;
        this.ready = false;
        this.serverPort = null;
    }
}
