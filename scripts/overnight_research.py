"""Bounded overnight research. Owns only its worker process group and reports."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from product_settings import atomic_json  # noqa: E402


def deadline_reached(now, deadline):
    return now >= deadline


def split_ranges(count):
    split = int(count * 0.7)
    return split, split - 96


def pause_reason(health):
    if health["thermal"] is None:
        return "无法读取系统热压力"
    if health["thermal"] >= 2:
        return "系统热压力偏高"
    if health["cpu"] >= 60:
        return "整机 CPU 占用超过 60%"
    if health["available_gb"] < 4:
        return "可用内存不足 4GB"
    if health["disk_gb"] < 15:
        return "可用磁盘不足 15GB"
    if not health["plugged"]:
        return "已断开电源，暂停回测"
    return None


def thermal_state():
    try:
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        objc.objc_msgSend.restype = ctypes.c_void_p
        obj = objc.objc_msgSend(objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
        return int(objc.objc_msgSend(obj, objc.sel_registerName(b"thermalState")) or 0)
    except Exception:
        return None


def health_snapshot():
    battery = psutil.sensors_battery()
    return {
        "at": time.time(),
        "thermal": thermal_state(),
        "cpu": psutil.cpu_percent(),
        "available_gb": psutil.virtual_memory().available / 2**30,
        "disk_gb": psutil.disk_usage(ROOT).free / 2**30,
        "plugged": bool(battery and battery.power_plugged),
    }


def worker(folder, job):
    import pandas as pd

    import project_signal_backtest as backtest
    from strategy import StrategyConfig

    os.nice(10)
    manifest = json.loads((folder / "manifest.json").read_text())
    cache = folder / "cache" / f"{job['symbol']}-{job['interval']}.json"
    if cache.exists():
        bars = json.loads(cache.read_text())
        for bar in bars:
            bar["time"] = pd.Timestamp(bar["time"])
    else:
        raw = backtest.fetch_exchange_klines("binance_usdm", job["symbol"], job["interval"], 10500)
        raw = [b for b in raw if b["close_time"] < manifest["data_cutoff_ms"]][-10000:]
        if len(raw) < 1000:
            raise ValueError("历史不足 1000 根，跳过，不补造数据")
        for previous, current in zip(raw, raw[1:]):
            if current["open_time"] != previous["close_time"] + 1:
                raise ValueError("K 线存在缺口，跳过")
        bars = backtest.fetch_higher_timeframe_context(
            job["symbol"], backtest.calculate_indicators(raw), job["interval"], exchange="binance_usdm"
        )
        cache.parent.mkdir(exist_ok=True)
        # Pandas timestamps are serialized only in the immutable input cache.
        payload = json.loads(json.dumps(bars, default=str))
        atomic_json(cache, payload)
    split, fit_end = split_ranges(len(bars))
    cfg = StrategyConfig(**manifest["settings"]["effective_strategy"])
    results = {}
    all_trades = {}
    for name, subset, start, end in [
        ("fit", bars[:split], bars[250]["time"], bars[fit_end]["time"]),
        ("holdout", bars[split - 250 :], bars[split]["time"], None),
    ]:
        trades, curve = backtest.run_backtest(
            subset,
            2,
            96,
            job["fee"],
            job["mode"],
            strategy_config=cfg,
            min_signal_score=manifest["settings"]["values"]["paper"]["min_signal_score"],
            signal_direction=manifest["settings"]["values"]["paper"]["signal_direction"],
            entry_start_time=start,
            entry_end_time=end,
        )
        results[name] = backtest.calculate_metrics(trades, curve)
        all_trades[name] = [t.__dict__ for t in trades]
    note = "仅研究筛选：固定参数，70%研究段/30%留出段，边界禁入96根；未执行跨交易所晋级验证，不据此自动改策略。资金费、盘口撮合未建模；成本0.15%档包含每边额外0.05%的滑点压力。结构ATR模式触及1R即退出。"
    result = {
        "job": job,
        "metrics": results,
        "trades": all_trades,
        "source": "binance_usdm",
        "start_utc": str(bars[0]["time"]),
        "end_utc": str(bars[-1]["time"]),
        "holdout_start_utc": str(bars[split]["time"]),
        "bars": len(bars),
        "note": note,
        "config_revision": manifest["settings"]["revision"],
        "frozen_settings": manifest["settings"],
        "input_sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "completed_at": time.time(),
    }
    target = folder / "results" / f"{job['id']}.json"
    target.parent.mkdir(exist_ok=True)
    atomic_json(target, json.loads(json.dumps(result, default=str)))
    m = results["holdout"]
    label = "结构ATR" if job["mode"] == "structure_atr" else "1R后移动"
    stats = f"留出{int(m['total_trades'])}笔_胜率{m['win_rate']*100:.1f}%_均益{m['expectancy']*100:.2f}%_回撤{abs(m['max_drawdown'])*100:.2f}%"
    filename = f"{job['id']}_{job['symbol']}_{job['interval']}_{label}_成本{job['fee']*100:.2f}%_{stats}.html"
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(v))}</td>"
            for v in [
                name,
                int(values["total_trades"]),
                f"{values['win_rate']*100:.1f}%",
                f"{values['expectancy']*100:.3f}%",
                f"{values['total_return']*100:.2f}%",
                f"{values['max_drawdown']*100:.2f}%",
            ]
        )
        + "</tr>"
        for name, values in results.items()
    )
    content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(filename[:-5])}</title><style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:20px;background:#10141b;color:#eef}}table{{border-collapse:collapse;width:100%}}td,th{{padding:12px;border-bottom:1px solid #556;text-align:left}}pre{{white-space:pre-wrap}}a{{color:#8ee}}</style><h1>{job['symbol']} · {job['interval']} · {label}</h1><p>Binance USDT 合约 | 成本每边 {job['fee']*100:.2f}% | {len(bars)} 根K线</p><p>UTC：{bars[0]['time']} 至 {bars[-1]['time']}</p><p>{note}</p><table><tr><th>区段</th><th>交易数</th><th>胜率</th><th>平均收益</th><th>复合回测收益</th><th>回测回撤</th></tr>{rows}</table><p>零交易时，比例为占位值，不代表策略有效。逐笔复合回测曲线不是账户净值。多重比较结果仅作筛选。</p><p>版本 {result['config_revision']} | 输入校验 {result['input_sha256']}</p><details><summary>完整参数与逐笔交易</summary><pre>{html.escape(json.dumps(result,ensure_ascii=False,default=str,indent=2))}</pre></details></html>"""
    report = folder / "reports" / filename
    report.parent.mkdir(exist_ok=True)
    report.write_text(content)
    atomic_json(folder / "results" / f"{job['id']}-delivery.json", {"report": str(report), "uploaded": False})


def upload_pending(folder, state):
    # Existing VPS outbox announces each new HTML report once. Filenames carry the
    # key holdout metrics, so no separate sender or credential copy is required.
    for path in sorted((folder / "results").glob("*-delivery.json")):
        d = json.loads(path.read_text())
        if d["uploaded"]:
            continue
        report = Path(d["report"])
        remote = f"/opt/crypto-project/reports/overnight-{folder.name}"
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "crypto-vps", f"mkdir -p {remote}"],
            check=True,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        temp = f"{remote}/{report.stem}.upload"
        import shlex

        with report.open("rb") as stream:
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "crypto-vps",
                    f'cat > {shlex.quote(temp)} && mv {shlex.quote(temp)} {shlex.quote(remote+"/"+report.name)}',
                ],
                stdin=stream,
                check=True,
                timeout=40,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        d["uploaded"] = True
        d["uploaded_at"] = time.time()
        atomic_json(path, d)
        state["uploaded"] += 1


def supervise(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    deadline = manifest["deadline"]
    state = {
        "pid": os.getpid(),
        "status": "running",
        "completed": 0,
        "failed": 0,
        "uploaded": 0,
        "total": len(manifest["jobs"]),
        "deadline": deadline,
    }
    child = None

    def stop(*_):
        nonlocal deadline
        deadline = 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    subprocess.Popen(
        [sys.executable, __file__, "--guard", str(folder)],
        cwd=ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log = (folder / "health.jsonl").open("a", buffering=1)
    psutil.cpu_percent()
    try:
        for job in manifest["jobs"]:
            if deadline_reached(time.time(), deadline):
                break
            state.update(job=job, status="waiting", worker_pid=None)
            while time.time() < deadline:
                health = health_snapshot()
                reason = pause_reason(health)
                state.update(health=health, pause_reason=reason, updated_at=time.time())
                atomic_json(folder / "status.json", state)
                log.write(json.dumps(health) + "\n")
                if not reason:
                    break
                time.sleep(min(10, max(0, deadline - time.time())))
            if time.time() >= deadline:
                break
            env = dict(
                os.environ,
                OMP_NUM_THREADS="1",
                OPENBLAS_NUM_THREADS="1",
                MKL_NUM_THREADS="1",
                NUMEXPR_NUM_THREADS="1",
                VECLIB_MAXIMUM_THREADS="1",
                MARKET_DATA_SOURCE="binance_usdm",
            )
            jobfile = folder / "job.json"
            atomic_json(jobfile, job)
            with (folder / f"worker-{job['id']}.log").open("w") as out:
                child = subprocess.Popen(
                    [sys.executable, __file__, "--worker", str(folder)],
                    cwd=ROOT,
                    env=env,
                    start_new_session=True,
                    stdout=out,
                    stderr=out,
                )
                state.update(status="running", worker_pid=child.pid)
                began = time.time()
                last_health = 0
                paused = False
                rest_until = 0
                while child.poll() is None and time.time() < deadline:
                    now = time.time()
                    if now - last_health >= 5:
                        health = health_snapshot()
                        reason = pause_reason(health)
                        try:
                            health["worker_rss_gb"] = psutil.Process(child.pid).memory_info().rss / 2**30
                        except psutil.NoSuchProcess:
                            break
                        if health["worker_rss_gb"] > 2:
                            reason = "单任务内存超过2GB"
                            rest_until = float("inf")
                        if health["thermal"] == 3:
                            reason = "系统热压力严重"
                            deadline = 0
                        if now - began > 1200:
                            reason = "连续任务超过20分钟"
                            rest_until = float("inf")
                        state.update(
                            health=health, pause_reason=reason, status="paused" if reason else "running", updated_at=now
                        )
                        atomic_json(folder / "status.json", state)
                        log.write(json.dumps(health) + "\n")
                        last_health = now
                        if reason:
                            rest_until = max(rest_until, now + 60)
                    if rest_until == float("inf"):
                        break
                    if now < rest_until:
                        if not paused:
                            os.killpg(child.pid, signal.SIGSTOP)
                            paused = True
                        time.sleep(0.5)
                        continue
                    if paused:
                        os.killpg(child.pid, signal.SIGCONT)
                        paused = False
                    # Limit this single-thread worker to ~80% of one core.
                    time.sleep(0.8)
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGSTOP)
                        paused = True
                        time.sleep(0.2)
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGCONT)
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                if child.returncode == 0:
                    state["completed"] += 1
                else:
                    state["failed"] += 1
                child = None
            state.update(worker_pid=None, status="cooldown", updated_at=time.time())
            try:
                upload_pending(folder, state)
                state["upload_error"] = None
            except Exception as exc:
                state["upload_error"] = type(exc).__name__
            atomic_json(folder / "status.json", state)
            rest = 300 if time.time() - began > 1200 else 60
            until = min(deadline, time.time() + rest)
            while time.time() < until:
                state["updated_at"] = time.time()
                atomic_json(folder / "status.json", state)
                time.sleep(min(1, until - time.time()))
        state.update(
            status="finished",
            finished_at=time.time(),
            worker_pid=None,
            stop_reason="07:00截止或人工停止" if time.time() >= deadline else "预设实验已完成",
        )
        try:
            upload_pending(folder, state)
        except Exception as exc:
            state["upload_error"] = type(exc).__name__
        atomic_json(folder / "status.json", state)
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGCONT)
            os.killpg(child.pid, signal.SIGKILL)
        caffeinate.terminate()
        log.close()


def guard(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    deadline = manifest["deadline"]
    while True:
        time.sleep(3)
        try:
            state = json.loads((folder / "status.json").read_text())
        except (OSError, ValueError):
            if time.time() >= deadline:
                return
            continue
        if state.get("status") == "finished":
            return
        if time.time() < deadline and time.time() - state.get("updated_at", time.time()) < 120:
            continue
        worker_pid = state.get("worker_pid")
        if worker_pid:
            try:
                proc = psutil.Process(worker_pid)
                if "--worker" in proc.cmdline() and str(folder) in proc.cmdline():
                    os.killpg(worker_pid, signal.SIGCONT)
                    os.killpg(worker_pid, signal.SIGKILL)
            except (psutil.NoSuchProcess, ProcessLookupError):
                pass
        if time.time() >= deadline:
            try:
                proc = psutil.Process(state["pid"])
                if "--run" in proc.cmdline() and str(folder) in proc.cmdline():
                    proc.terminate()
            except psutil.NoSuchProcess:
                pass
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--guard", type=Path)
    parser.add_argument("--upload-only", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, json.loads((args.worker / "job.json").read_text()))
    elif args.run:
        supervise(args.run)
    elif args.guard:
        guard(args.guard)
    elif args.upload_only:
        state = json.loads((args.upload_only / "status.json").read_text())
        upload_pending(args.upload_only, state)
        atomic_json(args.upload_only / "status.json", state)
