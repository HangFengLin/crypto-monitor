const labels = {not_started:'尚未启动', running:'模拟盘运行中', stale:'扫描状态未及时更新', error:'需要处理异常', open:'持仓中', closed:'已平仓', rejected:'未成交', entry_pending:'开仓待确认', protection_pending:'止损待确认', exit_pending:'平仓待确认'};
const formatTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '尚无扫描记录';
async function refreshDemo() {
  try {
    const response = await fetch('/api/binance-demo/status', {cache:'no-store'});
    if (!response.ok) throw new Error('状态接口暂不可用');
    const data = await response.json();
    document.querySelector('#status').textContent = labels[data.status] || data.status;
    document.querySelector('#time').textContent = `最近完整扫描：${formatTime(data.last_scan_at)}`;
    document.querySelector('#fault').textContent = data.fault || data.scan_error || '';
    const rows = document.querySelector('#positions'); rows.replaceChildren();
    for (const p of [...data.positions].reverse()) {
      const tr = document.createElement('tr');
      const fees = Object.entries(p.commissions || {}).map(([asset, value]) => `${value} ${asset}`).join(' / ') || '待对账';
      for (const value of [`${p.symbol} / ${p.direction === 'long' ? '多' : '空'}`, labels[p.status] || p.status, p.strategy_version, p.reference_price, p.entry_order ? p.entry_price : '待成交', p.executed_qty || '—', fees]) {
        const td = document.createElement('td'); td.textContent = value ?? '—'; tr.append(td);
      }
      rows.append(tr);
    }
    if (!data.positions.length) {const tr = document.createElement('tr'); const td = document.createElement('td'); td.colSpan=7; td.textContent='暂无官方模拟盘交易记录'; tr.append(td); rows.append(tr);}
    const events = document.querySelector('#events'); events.replaceChildren();
    for (const event of [...data.events].reverse()) {const li = document.createElement('li'); li.textContent = `${formatTime(event.created_at)} · ${event.symbol || ''} · ${event.reason || event.type}`; events.append(li);}
  } catch (error) {document.querySelector('#status').textContent='无法读取模拟盘状态'; document.querySelector('#fault').textContent=error.message;}
  finally {setTimeout(refreshDemo, 15000);}
}
refreshDemo();
