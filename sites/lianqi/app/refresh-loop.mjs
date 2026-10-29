/** Run finite refreshes one at a time, scheduling the next after settlement. */
export function createRefreshLoop(load, { intervalMs = 15000, timers = globalThis } = {}) {
  let timer = null, pending = null, controller = null, stopped = false, paused = false;
  const clearTimer = () => {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
  };
  const schedule = () => {
    if (stopped || paused || pending) return;
    clearTimer();
    timer = timers.setTimeout(() => {
      timer = null;
      void refresh().catch(() => {});
    }, intervalMs);
  };
  const refresh = () => {
    if (stopped) return Promise.resolve();
    if (pending) return pending;
    clearTimer();
    controller = new AbortController();
    const signal = controller.signal;
    pending = Promise.resolve().then(() => {
      if (!signal.aborted) return load(signal);
    }).finally(() => {
      pending = null;
      controller = null;
      schedule();
    });
    return pending;
  };
  return {
    refresh,
    pause({ abortPending = false } = {}) { paused = true; clearTimer(); if (abortPending) controller?.abort(); },
    resume() {
      if (stopped || !paused) return;
      paused = false;
      void refresh().catch(() => {});
    },
    stop() { stopped = true; clearTimer(); controller?.abort(); },
  };
}

/**
 * Keep polling only as a fallback while the live stream is unavailable.
 * @template Snapshot
 * @param {(signal: AbortSignal) => Promise<unknown>} load
 * @param {{onSnapshot: (snapshot: Snapshot) => void,
 *   onConnection: (status: 'online' | 'offline') => void,
 *   onError?: (error: unknown) => void, createStream?: (url: string) => EventSource,
 *   timers?: typeof globalThis | Window, intervalMs?: number}} options
 */
export function startLiveStateUpdates(load, {
  onSnapshot, onConnection, onError,
  createStream = url => new EventSource(url), timers = globalThis, intervalMs = 30000,
} = {}) {
  const polling = createRefreshLoop(load, { intervalMs, timers });
  let stopped = false, stream = null, reconnectTimer = null;
  const clearReconnect = () => {
    if (reconnectTimer !== null) timers.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  };
  const scheduleReconnect = () => {
    if (stopped || reconnectTimer !== null) return;
    reconnectTimer = timers.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 5000);
  };
  const offline = () => {
    if (stopped) return;
    onConnection('offline');
    polling.resume();
  };
  const online = (abortPending = false) => {
    if (stopped) return;
    polling.pause({ abortPending });
    onConnection('online');
  };
  void polling.refresh().catch(() => {});
  const connect = () => {
    if (stopped) return;
    try {
      const current = createStream('/api/events?detail=compact&delta=1');
      stream = current;
      let baseline = null, sequence = null;
      const isCurrent = () => !stopped && stream === current;
      current.onopen = () => {
        if (!isCurrent()) return;
        baseline = null; sequence = null;
        clearReconnect();
        online();
      };
      current.onerror = () => {
        if (!isCurrent()) return;
        baseline = null; sequence = null;
        offline();
        // CONNECTING retains the browser's own retry loop. HTTP failures such
        // as 503 can instead become permanently CLOSED and need a new object.
        if (current.readyState === 2) {
          stream = null;
          current.close();
          scheduleReconnect();
        }
      };
      current.onmessage = message => {
        if (!isCurrent()) return;
        try {
          const frame = JSON.parse(message.data);
          const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
          let next;
          if (frame?.protocol !== 'lianqi-delta-v1') {
            // Older servers ignore the delta query and return complete snapshots.
            if (!object(frame)) throw Error('Invalid state snapshot');
            next = frame; baseline = null; sequence = null;
          } else if (frame.type === 'snapshot' && frame.seq === 0 && object(frame.state)) {
            next = frame.state; sequence = 0;
          } else if (frame.type === 'patch' && baseline !== null && frame.seq === sequence + 1 &&
                     object(frame.set) && Array.isArray(frame.remove) &&
                     frame.remove.every(key => typeof key === 'string')) {
            next = { ...baseline, ...frame.set };
            for (const key of frame.remove) delete next[key];
            sequence = frame.seq;
          } else {
            throw Error('State stream lost synchronization');
          }
          onSnapshot(next);
          if (frame?.protocol === 'lianqi-delta-v1') baseline = next;
          clearReconnect(); online(true);
        } catch (error) {
          onError?.(error); offline();
          // A skipped or invalid patch cannot be recovered without a fresh snapshot.
          stream = null; current.close(); scheduleReconnect();
        }
      };
    } catch (error) {
      onError?.(error);
      offline();
      scheduleReconnect();
    }
  };
  connect();
  return {
    refresh: polling.refresh,
    stop() {
      stopped = true;
      clearReconnect();
      const current = stream;
      stream = null;
      current?.close();
      polling.stop();
    },
  };
}
