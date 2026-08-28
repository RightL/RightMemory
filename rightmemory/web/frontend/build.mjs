import { build, context } from 'esbuild';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';

const preview = process.argv.includes('--serve');
const options = {
  entryPoints: [preview ? 'tests/browser.ts' : 'src/pursuit-map.ts'],
  outfile: preview ? '.preview/browser.js' : '../static/pursuit-map.js',
  bundle: true,
  format: 'esm',
  target: ['es2022'],
  minify: !preview,
  sourcemap: preview,
  legalComments: 'none',
  logLevel: 'info',
};
if (preview) {
  await mkdir('.preview', { recursive: true });
  await copyFile('tests/browser.html', '.preview/index.html');
  const runner = await context(options);
  await runner.watch();
  const server = await runner.serve({ servedir: '.preview', host: '127.0.0.1', port: 8767 });
  console.log(`Frontend fixture: http://127.0.0.1:${server.port}`);
} else {
  await build(options);
  const license = await readFile('node_modules/mind-elixir/LICENSE', 'utf8');
  await writeFile('../static/pursuit-map.LICENSE.txt', `Mind Elixir 5.15.1\nhttps://github.com/SSShooter/mind-elixir-core\n\n${license}`);
}
