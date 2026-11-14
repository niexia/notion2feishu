#!/usr/bin/env node
'use strict';

// 一个很薄的 npm 启动器：真正的逻辑仍然是仓库里的 Python 包。
// 它让 `npx github:niexia/notion2feishu` 可用，但本机仍需要 Python 3.9+ 和 lark-cli。

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const src = path.join(root, 'src');
const env = { ...process.env };
env.PYTHONPATH = src + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : '');

const candidates = [];
if (process.env.NOTION2FEISHU_PYTHON) {
  candidates.push(process.env.NOTION2FEISHU_PYTHON);
}
candidates.push('python3', 'python');

function fail() {
  console.error(
    'notion2feishu requires Python 3.9+ and lark-cli.\n' +
    'Install Python, or set NOTION2FEISHU_PYTHON to the interpreter path.'
  );
  process.exit(127);
}

function main() {
  const python = candidates.shift();
  if (!python) return fail();

  const probe = spawnSync(
    python,
    ['-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'],
    { stdio: 'ignore' }
  );
  if (probe.error || probe.status !== 0) {
    return main();
  }

  const child = spawn(
    python,
    ['-m', 'notion2feishu', ...process.argv.slice(2)],
    { stdio: 'inherit', env }
  );

  child.on('error', (err) => {
    if (err.code === 'ENOENT') return main();
    console.error(err.message);
    process.exit(1);
  });

  child.on('exit', (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
    } else {
      process.exit(code === null ? 1 : code);
    }
  });
}

main();
