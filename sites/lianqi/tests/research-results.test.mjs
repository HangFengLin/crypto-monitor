import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

async function renderResult(result) {
  const sourceUrl = new URL('../app/lianqi-dashboard.tsx', import.meta.url);
  const require = createRequire(sourceUrl);
  const ts = require('typescript');
  const compiled = ts.transpileModule(await readFile(sourceUrl, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  });
  const loaded = { exports: {} };
  // Imports outside this component are ordinary TSX components; compile them on demand.
  const original = require.extensions['.tsx'];
  require.extensions['.tsx'] = (module, filename) => {
    const fs = require('node:fs');
    const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    });
    module._compile(output.outputText, filename);
  };
  try {
    new Function('require', 'module', 'exports', compiled.outputText)(require, loaded, loaded.exports);
    assert.equal(typeof loaded.exports.BacktestResults, 'function', 'Quick result must expose its consumer-visible component');
    const React = require('react');
    return require('react-dom/server').renderToStaticMarkup(React.createElement(loaded.exports.BacktestResults, { result }));
  } finally {
    if (original) require.extensions['.tsx'] = original;
    else delete require.extensions['.tsx'];
  }
}

test('quick results show uncertainty and accounting boundaries instead of account performance', async () => {
  const html = await renderResult({
    ok: true, symbol: 'BTCUSDT', interval: '15m',
    params: { return_model: 'linear_usdm_v1' },
    execution_metadata: { funding_included: false, independent_slippage_included: false },
    stop_mode_results: [{ stop_mode: 'structure_atr', metrics: {
      total_trades: 18, expectancy: -.003, expectancy_ci_low: -.008,
      expectancy_ci_high: .003, win_rate: .44, total_return: -.053, max_drawdown: -.072,
    } }],
    groups: [{ group_dimensions: 'signal', group_value: 'short', trades: 35, avg_return: -.001, win_rate: .4 }],
  });
  assert.match(html, /线性 USDT 合约/);
  assert.match(html, /置信区间/);
  assert.match(html, /-0\.80%/);
  assert.match(html, /0\.30%/);
  assert.match(html, /闭仓曲线回撤/);
  assert.match(html, /逐笔复合收益/);
  assert.match(html, /资金费/);
  assert.match(html, /short/);
  assert.doesNotMatch(html, /undefined|NaN|账户收益|验证通过/);
});

test('zero-trade quick results do not show zero as measured performance', async () => {
  const html = await renderResult({ metrics: { total_trades: 0, expectancy: 0, win_rate: 0, total_return: 0, max_drawdown: 0 } });
  assert.match(html, /暂无样本/);
  assert.doesNotMatch(html, /0\.00%/);
  assert.match(html, /历史口径|历史比例/);
});
