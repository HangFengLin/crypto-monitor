"use client";
import { useEffect, useState } from "react";
import { fetchWithTimeout } from "./api-client.mjs";
import { createRefreshLoop } from "./refresh-loop.mjs";
type Status={automatic:{enabled?:boolean;running?:boolean;pending?:number;error?:string;last_delivery?:{at?:number;ok?:boolean;error?:string}}};
export default function FeishuNotifications(){
 const [status,setStatus]=useState<Status|null>(null),[error,setError]=useState('');
 useEffect(()=>{let active=true;async function load(signal:AbortSignal){try{const response=await fetchWithTimeout('/api/notifications/status',{signal});if(!response.ok)throw Error('通知状态读取失败');const data=await response.json() as Status;if(active&&!signal.aborted){setStatus(data);setError('');}}catch(e){if(active&&!signal.aborted)setError(String(e));}}const refresh=createRefreshLoop(load,{intervalMs:10000});void refresh.refresh();return()=>{active=false;refresh.stop();};},[]);
 const automatic=status?.automatic;
 return <section className="content-panel"><div className="section-heading"><h2>通知状态</h2></div>{error?<p role="alert">{error}</p>:automatic?<><p>飞书推送：{automatic.enabled?(automatic.running?'运行中':'需要关注'):'已关闭'} · 待发送 {automatic.pending??0} 条</p>{automatic.last_delivery?.at&&<p>最近发送：{new Date(automatic.last_delivery.at*1000).toLocaleString('zh-CN')} · {automatic.last_delivery.ok?'接口已接受':'发送失败'}</p>}{(automatic.error||automatic.last_delivery?.error)&&<p role="alert">{automatic.error||automatic.last_delivery?.error}</p>}</>:<p>正在读取通知状态…</p>}</section>;
}
