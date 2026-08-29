import { build, context } from 'esbuild';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const preview = process.argv.includes('--serve');
const frontendDir = fileURLToPath(new URL('.', import.meta.url));
const previewDir = join(frontendDir, '.preview');
const katexModernFonts = {
  name: 'katex-modern-fonts',
  setup(buildApi) {
    buildApi.onLoad({ filter: /[\\/]katex(?:\.min)?\.css$/ }, async ({ path }) => ({
      contents: (await readFile(path, 'utf8')).replace(
        /,\s*url\([^)]*\.woff\)\s*format\(["']woff["']\)\s*,\s*url\([^)]*\.ttf\)\s*format\(["']truetype["']\)/g,
        '',
      ),
      loader: 'css',
      resolveDir: dirname(path),
    }));
  },
};
const options = {
  entryPoints: [join(frontendDir, preview ? 'tests/browser.ts' : 'src/pursuit-map.ts')],
  outfile: preview ? join(previewDir, 'browser.js') : join(frontendDir, '../static/pursuit-map.js'),
  bundle: true,
  format: 'esm',
  target: ['es2022'],
  minify: !preview,
  sourcemap: preview,
  legalComments: 'none',
  logLevel: 'info',
  loader: { '.woff2': 'dataurl' },
  plugins: [katexModernFonts],
};
if (preview) {
  await mkdir(previewDir, { recursive: true });
  await copyFile(join(frontendDir, 'tests/browser.html'), join(previewDir, 'index.html'));
  const runner = await context(options);
  await runner.watch();
  const server = await runner.serve({ servedir: previewDir, host: '127.0.0.1', port: 0 });
  console.log(`Frontend fixture: http://127.0.0.1:${server.port}`);
} else {
  await build(options);
  const licenseSources = [
    ['Mind Elixir', 'https://github.com/SSShooter/mind-elixir-core', 'mind-elixir'],
    ['Marked', 'https://marked.js.org/', 'marked'],
    ['DOMPurify', 'https://github.com/cure53/DOMPurify', 'dompurify'],
    ['KaTeX', 'https://katex.org/', 'katex'],
  ];
  const licenses = await Promise.all(licenseSources.map(async ([name, url, packageName]) => {
    const packageDir = join(frontendDir, 'node_modules', packageName);
    const metadata = JSON.parse(await readFile(join(packageDir, 'package.json'), 'utf8'));
    const body = await readFile(join(packageDir, 'LICENSE'), 'utf8');
    return `${name} ${metadata.version}\n${url}\n\n${body.trim()}`;
  }));
  await writeFile(join(frontendDir, '../static/pursuit-map.LICENSE.txt'), `${licenses.join('\n\n---\n\n')}\n`);
}
