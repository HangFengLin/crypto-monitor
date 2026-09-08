export function validateState(payload) {
  if (!payload || !Array.isArray(payload.watchlist) || payload.signal_tracking?.mode !== 'paper' || !Array.isArray(payload.signal_tracking.positions)) {
    throw new Error('后端版本不兼容：请启动当前项目的研究后端，检查 LIANQI_ORIGIN_URL。');
  }
  return payload;
}

export async function fetchWithTimeout(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new Error('请求超时，请检查后端连接或减少回测 K 线数量后重试。');
    throw new Error(`无法连接数据服务：${error instanceof Error ? error.message : error}`);
  } finally { clearTimeout(timer); }
}
