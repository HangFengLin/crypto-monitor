"use client";
import { useEffect, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
type Settings = { revision:string; values:{paper:{enabled:boolean;reward_risk:number;fee_rate:number;stop_mode:string;signal_direction:string}} };
export default function RuntimeSettings() {
  const [settings,setSettings]=useState<Settings|null>(null),[error,setError]=useState('');
  useEffect(()=>{let active=true;async function load(){try{const response=await fetchWithTimeout('/api/settings');if(!response.ok)throw Error('当前配置读取失败');const data=await response.json() as Settings;if(active){setSettings(data);setError('');}}catch(e){if(active)setError(String(e));}}void load();const id=setInterval(load,30000);return()=>{active=false;clearInterval(id);};},[]);
  const paper=settings?.values.paper;
  return <section className="content-panel"><div className="section-heading"><h2>当前运行规则</h2><span>只读</span></div><p>参数由后台维护；需要调整时直接告诉我。</p>{error?<p role="alert">{error}</p>:paper?<><div className="stat-grid"><div><span>新信号记录</span><strong>{paper.enabled?'开启':'暂停'}</strong></div><div><span>目标盈亏比</span><strong>{paper.reward_risk} R</strong></div><div><span>单边手续费</span><strong>{(paper.fee_rate*100).toFixed(2)}%</strong></div><div><span>允许方向</span><strong>{({all:'多空',long:'只做多',short:'只做空'} as Record<string,string>)[paper.signal_direction]||paper.signal_direction}</strong></div></div><p>止损：{paper.stop_mode==='structure_atr'?'结构 ATR':'1R 后移动止损'} · 配置版本 {settings?.revision}</p></>:<p>正在读取运行规则…</p>}</section>;
}
