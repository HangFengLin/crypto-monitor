export function validateState(payload) {
  if (!payload || !Array.isArray(payload.watchlist) || payload.signal_tracking?.mode !== 'paper' || !Array.isArray(payload.signal_tracking.positions)) {
    throw new Error('后端版本不兼容：请启动当前项目的研究后端，检查 LIANQI_ORIGIN_URL。');
  }
  return payload;
}

export async function fetchWithTimeout(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  let timedOut = false;
  const onCallerAbort = () => controller.abort(options.signal.reason);
  const aborted = new Promise((_, reject) => {
    controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true });
  });
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  if (options.signal?.aborted) onCallerAbort();
  else options.signal?.addEventListener('abort', onCallerAbort, { once: true });
  try {
    const response = await Promise.race([fetch(url, { ...options, signal: controller.signal }), aborted]);
    // These finite API requests need a deadline for the entire download. Read a
    // clone so the returned native Response retains its URL/status/headers and
    // its original body remains available to existing json()/text() consumers.
    await Promise.race([response.clone().arrayBuffer(), aborted]);
    return response;
  } catch (error) {
    if (timedOut) throw new Error('请求超时，请检查后端连接或减少回测 K 线数量后重试。');
    if (options.signal?.aborted) throw options.signal.reason;
    throw new Error(`无法连接数据服务：${error instanceof Error ? error.message : error}`);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', onCallerAbort);
  }
}
