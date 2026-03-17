import * as vscode from 'vscode';
import { ChildProcess, spawn, execFile } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

const SERVER_PORT = 9120;
const BASE_URL = `http://127.0.0.1:${SERVER_PORT}`;
const CORE_DEPS = ['fastapi', 'uvicorn', 'libcst>=1.0.0'];

export class AnalysisServer {
    private process: ChildProcess | null = null;
    private ready = false;
    private startupError: string | null = null;
    private envReady = false;
    private resolvedPython: string | null = null;

    constructor(
        private extensionPath: string,
        private storagePath: string,
    ) {}

    async ensureRunning(): Promise<void> {
        if (this.ready) return;

        // Try to connect to existing server
        try {
            const res = await fetch(`${BASE_URL}/docs`);
            if (res.ok) {
                this.ready = true;
                return;
            }
        } catch {}

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
        const bundledCore = path.join(this.extensionPath, 'core');
        if (fs.existsSync(path.join(bundledCore, 'codecanvas'))) {
            return bundledCore;
        }
        return path.join(this.extensionPath, '..', 'core');
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
            this.process = spawn(
                pythonPath,
                ['-m', 'codecanvas.server.app', String(SERVER_PORT)],
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

            this.process.stderr?.on('data', (data: Buffer) => {
                const msg = data.toString();
                stderrChunks.push(msg);
                if (msg.includes('Uvicorn running') || msg.includes('Application startup complete')) {
                    this.ready = true;
                    clearTimeout(timeout);
                    resolve();
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
            const res = await fetch(`${BASE_URL}/analyze`, {
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

    async getFlow(projectPath: string, method: string, endpointPath: string): Promise<any> {
        if (!this.ready) {
            vscode.window.showErrorMessage('CodeCanvas server is not running.');
            return null;
        }
        try {
            const res = await fetch(`${BASE_URL}/flow`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project_path: projectPath, method, path: endpointPath }),
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

    stop(): void {
        this.process?.kill();
        this.process = null;
        this.ready = false;
    }
}
