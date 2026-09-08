"use client";
import {useEffect,useState} from 'react';
import {fetchWithTimeout} from './api-client.mjs';
const METRICS:Record<string,string>={total_trades:'样本笔数',win_rate:'胜率',expectancy:'单笔平均收益',max_drawdown:'回测最大回撤',total_return:'逐笔收益合计'};
type Run={run_id:string;symbol:string;interval:string;saved_at:number;config_revision:string;market_data_source:string;error?:string};
export default function ResearchHistory(){
 const [runs,setRuns]=useState<Run[]>([]),[message,setMessage]=useState('读取实验归档…'),[selected,setSelected]=useState<{metrics?:Record<string,number>;params?:Record<string,unknown>;symbol?:string;started_at?:string;ended_at?:string}|null>(null);
 async function load(){try{const res=await fetchWithTimeout('/api/research-runs');if(!res.ok)throw Error('归档读取失败');const data=await res.json() as {runs:Run[]};setRuns(data.runs);setMessage(data.runs.length?'':'尚无已保存实验；完成回测后自动归档。');}catch(e){setMessage(String(e));}}
 useEffect(()=>{const id=window.setTimeout(()=>void load(),0);return()=>window.clearTimeout(id);},[]);
 async function open(id:string){try{const res=await fetchWithTimeout(`/api/research-runs/${id}`);if(!res.ok)throw Error('实验读取失败');setSelected(await res.json() as {metrics:Record<string,number>});}catch(e){setMessage(String(e));}}
 return <section className="content-panel"><div className="section-heading"><h2>已保存实验</h2><button className="secondary-action" onClick={load}>刷新实验</button></div><p role="status">{message}</p>{runs.map(r=><button className="report-row" key={r.run_id} onClick={()=>void open(r.run_id)}>{r.error||`${r.symbol} · ${r.interval} · ${new Date(r.saved_at*1000).toLocaleString('zh-CN')} · ${r.market_data_source} · ${r.config_revision}`}</button>)}{selected!==null&&<section><h3>{selected.symbol} 实验结果</h3><p>{selected.started_at} — {selected.ended_at}（UTC）</p><div className="stat-grid">{Object.entries(selected.metrics||{}).filter(([key])=>key in METRICS).map(([key,value])=><div key={key}><span>{METRICS[key]}</span><strong>{key==='total_trades'?value:!selected.metrics?.total_trades?'暂无样本':`${(value*100).toFixed(2)}%`}</strong></div>)}</div></section>}{selected!==null&&<details><summary>实验原始结果与配置</summary><pre>{JSON.stringify(selected,null,2)}</pre></details>}</section>;
}
