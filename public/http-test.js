const symbolInput = document.querySelector("#symbolInput");
const runTestButton = document.querySelector("#runTest");
const serverResult = document.querySelector("#serverResult");
const browserResult = document.querySelector("#browserResult");

function formatResult(result) {
  if (!result.ok) {
    return `失败：${result.error || "未知错误"} · ${result.duration_ms || "--"}ms`;
  }
  return `成功：${result.symbol} 最新价 ${result.lastPrice} · ${result.duration_ms || "--"}ms`;
}

async function testServer(symbol) {
  const response = await fetch(`/api/network-test?symbol=${encodeURIComponent(symbol)}`);
  return response.json();
}

async function testBrowser(symbol) {
  const startedAt = performance.now();
  const url = `https://api.binance.com/api/v3/ticker/24hr?symbols=${encodeURIComponent(JSON.stringify([symbol]))}`;
  try {
    const response = await fetch(url);
    const payload = await response.json();
    const ticker = Array.isArray(payload) ? payload[0] : payload;
    return {
      ok: response.ok,
      source: "browser",
      symbol: ticker?.symbol || symbol,
      lastPrice: Number(ticker?.lastPrice),
      error: response.ok ? "" : JSON.stringify(payload),
      duration_ms: Math.round(performance.now() - startedAt),
    };
  } catch (error) {
    return {
      ok: false,
      source: "browser",
      symbol,
      error: error.message,
      duration_ms: Math.round(performance.now() - startedAt),
    };
  }
}

async function runTest() {
  const symbol = symbolInput.value.trim().toUpperCase() || "BTCUSDT";
  symbolInput.value = symbol;
  serverResult.textContent = "测试中...";
  browserResult.textContent = "测试中...";

  const [server, browser] = await Promise.all([testServer(symbol), testBrowser(symbol)]);
  serverResult.textContent = formatResult(server);
  browserResult.textContent = formatResult(browser);
}

runTestButton.addEventListener("click", runTest);
runTest();
