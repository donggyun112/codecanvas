import { cpSync, existsSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(scriptDir, '..');
const sourceDir = join(projectRoot, 'core');
const targetDir = join(projectRoot, 'extension', 'core');

const command = process.argv[2];
const excludedDirs = new Set([
    '.git',
    '.mypy_cache',
    '.pytest_cache',
    '.ruff_cache',
    '.tox',
    '.venv',
    '__pycache__',
    'build',
    'dist',
    'node_modules',
    'venv',
]);

function removeTarget() {
    rmSync(targetDir, { recursive: true, force: true });
}

function pruneTree(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const fullPath = join(dir, entry.name);

        if (entry.isDirectory()) {
            if (excludedDirs.has(entry.name) || entry.name.endsWith('.egg-info')) {
                rmSync(fullPath, { recursive: true, force: true });
                continue;
            }
            pruneTree(fullPath);
            continue;
        }

        if (entry.name.endsWith('.pyc') || entry.name.endsWith('.pyo')) {
            rmSync(fullPath, { force: true });
        }
    }
}

switch (command) {
    case 'copy':
        if (!existsSync(sourceDir)) {
            console.error(`Source core directory not found: ${sourceDir}`);
            process.exit(1);
        }
        removeTarget();
        cpSync(sourceDir, targetDir, { recursive: true });
        pruneTree(targetDir);
        break;
    case 'clean':
        removeTarget();
        break;
    default:
        console.error('Usage: node ./scripts/sync-core.mjs <copy|clean>');
        process.exit(1);
}
