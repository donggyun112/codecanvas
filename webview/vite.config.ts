import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  base: '',
  build: {
    outDir: path.resolve(__dirname, '../extension/media'),
    emptyOutDir: true,
    sourcemap: 'inline',
    minify: false,
    rollupOptions: {
      input: path.resolve(__dirname, 'index.html'),
      output: { sourcemapExcludeSources: false },
    },
    target: 'chrome120',
  },
});
