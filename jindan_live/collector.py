"""Public ingestion, bounded causal detection and engineering-only snapshots."""
import asyncio
from collections import deque
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import time

from .config import (Activation,ReceiveStamp,SYMBOLS,MARKET_WS,BBO_WS,M,DAY,canonical,digest,iso,require,validate_public_ws)
from .detector import IncrementalDetector
from .storage import Store


def number(value):
    if value is None or isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError):return None
    return value if math.isfinite(value) else None


def normalize_kline(payload,stamp,backfilled=False):
    raw=payload.get('data',payload)
    require(raw.get('e')=='kline','Expected public kline')
    k=raw.get('k',{})
    if k.get('x') is not True:return None
    symbol=raw.get('s');require(symbol in SYMBOLS and k.get('s')==symbol and k.get('i')=='1m','Registered USD-M 1m symbol required')
    require(raw.get('st',1)==1,'Only USD-M linear observations')
    start=k.get('t');end=k.get('T')
    require(type(start) is int and type(end) is int and start>=0 and start%M==0 and end==start+M-1,'Invalid closed UTC minute clock')
    if raw.get('E') is not None:
        require(type(raw['E']) is int and raw['E']<=stamp.estimated_ms+(stamp.uncertainty_ms or 0),'Future exchange publication rejected')
    closed_bound=stamp.estimated_ms-(stamp.uncertainty_ms or 0)
    require(start+M<=closed_bound,'Future/unproven closed bar rejected')
    values={name:number(k.get(field)) for name,field in [('open','o'),('high','h'),('low','l'),('close','c')]}
    require(all(x is not None and x>0 for x in values.values()) and values['low']<=min(values['open'],values['close'])
            <=max(values['open'],values['close'])<=values['high'],'Invalid positive OHLC')
    base,quote,buybase,buy=map(number,(k.get('v'),k.get('q'),k.get('V'),k.get('Q')))
    trades=k.get('n');trades=trades if type(trades) is int and trades>=0 else None
    totals_valid=base is not None and quote is not None and base>=0 and quote>=0 and trades is not None
    required_fields=('v','q','n','V','Q')
    missing=any(field not in k or k[field] is None for field in required_fields)
    invalid_provided=any(field in k and k[field] is not None and (number(k[field]) is None or number(k[field])<0) for field in required_fields)
    invalid_provided=invalid_provided or ('n' in k and k['n'] is not None and type(k['n']) is not int)
    if invalid_provided:status='INVALID'
    elif missing or not totals_valid or buybase is None or buy is None:status='UNKNOWN_MISSING'
    elif (buybase<0 or buy<0 or buybase>base+max(1e-8,1e-10*base) or buy>quote+max(1e-8,1e-10*quote)
          or ((base==0)!=(quote==0)) or (trades==0 and (base!=0 or quote!=0)) or (quote==0 and buy!=0)):
        status='INVALID'
    else:status='VALID'
    result=dict(values,symbol=symbol,time_ms=start,close_time_ms=start+M,base_volume=base if base is not None and base>=0 else None,
                quote_volume=quote if quote is not None and quote>=0 else None,trades=trades,
                taker_buy_base_volume=buybase if status=='VALID' else None,taker_buy_quote_volume=buy if status=='VALID' else None,
                flow_status=status,is_closed=True,exchange_event_time_ms=raw.get('E'),
                local_receive_at_ms=stamp.utc_ms,available_at_ms=stamp.estimated_ms,
                clock_uncertainty_ms=stamp.uncertainty_ms,receive=stamp.record(),backfilled=backfilled,
                source_version='PUBLIC_REST_BACKFILL' if backfilled else 'PUBLIC_WS_CLOSED_1M')
    return result


class CollectorCore:
    def __init__(self,store,activation):
        self.store,self.activation=store,activation;store.bind_activation(activation)
        self.detectors={};self.processed={s:activation.warmup_start_ms-M for s in SYMBOLS}
        self.last_receives={s:None for s in SYMBOLS};self.quotes={s:deque(maxlen=512) for s in SYMBOLS}
        self.health_minute={s:None for s in SYMBOLS};self.quote_lost_until={s:None for s in SYMBOLS};self.quote_overflows={s:0 for s in SYMBOLS};self.connections={'market':False,'bbo':False}
        self.quality_halt=False;self.disk_halt=False;self.stopped=False
        checkpoint=store.load_checkpoint('runtime')
        self.quote_cache_reset_at_ms=time.time_ns()//1000000
        self.restored_quote_context=bool(checkpoint or store.recent_events(1))
        if checkpoint:
            require(checkpoint['activation_sha']==activation.document_sha,'Checkpoint activation mismatch')
            self.processed=checkpoint['processed'];self.last_receives=checkpoint['last_receives'];self.quality_halt=checkpoint.get('quality_halt',False)
            for key,value in checkpoint['detectors'].items():
                symbol,minutes=key.split(':');self.detectors[key]=IncrementalDetector(symbol,int(minutes),value)

    def save(self):
        self.store.checkpoint('runtime',dict(activation_sha=self.activation.document_sha,processed=self.processed,
            last_receives=self.last_receives,quality_halt=self.quality_halt,detectors={k:d.snapshot() for k,d in self.detectors.items()}))

    def _computed(self,stamp):
        return max(stamp.estimated_ms,int(time.time()*1000+stamp.offset_ms))

    def ingest_kline(self,payload,stamp,backfilled=False):
        require(not self.disk_halt,'DISK_BUDGET_HALT')
        body=payload.get('data',payload);opened=body.get('k',{}).get('t')
        if body.get('k',{}).get('x') is not True:return 'UNFINISHED'
        self.activation.check_bar_scope(opened)
        bar=normalize_kline(payload,stamp,backfilled)
        self.last_receives[bar['symbol']]=stamp.utc_ms
        accepted=self.store.accept_bar(bar,payload)
        if accepted['status']=='CONFLICT':
            self.quality_halt=True;self.save();return 'CONFLICT'
        if accepted['status']=='NEW':
            self.store.mark_available(bar['symbol'],bar['time_ms'],max(bar['available_at_ms'],self._computed(stamp)))
        if bar['time_ms']>=self.activation.warmup_start_ms and not self.quality_halt:
            self.drain(bar['symbol'],stamp)
        return accepted['status']

    def drain(self,symbol,stamp):
        expected=self.processed[symbol]+M
        for bar in self.store.bars_from(symbol,expected,180):
            if bar['time_ms']!=expected:
                self.store.gap(symbol,expected,bar['time_ms']);break
            computed=self._computed(stamp)
            if not bar.get('availability_proven'):
                self.store.mark_available(symbol,bar['time_ms'],computed)
                bar=self.store.get_bar(symbol,bar['time_ms'])
            for minutes in (15,60):
                key=symbol+':'+str(minutes)
                detector=self.detectors.setdefault(key,IncrementalDetector(symbol,minutes))
                result=detector.ingest_minute(bar,computed_ms=computed)
                for observer in result['observers']:
                    self.store.observer(dict(observer,processed_at_ms=computed))
                for event in result['events']:
                    finished=self._computed(stamp)
                    event['feature_calculated_at_ms']=finished
                    event['available_time_ms']=max(event['available_time_ms'],finished)
                    event['v3_features']['available_time_ms']=event['available_time_ms']
                    event.update(decision_committed_at_ms=None,on_time_status='PENDING_COMMIT',
                                 reference_status='PENDING',observation_activation_id=self.activation.activation_id)
                    self.store.add_event(event)
                    receipt_stamp=ReceiveStamp(max(stamp.utc_ms,int(time.time()*1000)),time.monotonic_ns(),stamp.offset_ms,stamp.uncertainty_ms)
                    prior=self.store.event(event['event_id'])
                    committed=self.store.commit_event_receipt(event['event_id'],receipt_stamp,prior['available_time_ms'])
                    self._thinning(committed,committed['decision_committed_at_ms'])
            self.processed[symbol]=bar['time_ms'];expected+=M
            self.store.resolve_gaps(symbol,expected);self.save()

    def recover(self,stamp):
        for event in self.store.uncommitted_events():
            self.store.commit_event_receipt(event['event_id'],stamp,max(event['available_time_ms'],stamp.estimated_ms))
        for event in self.store.events_without_batch():
            if event.get('decision_committed_at_ms') is not None:self._thinning(event,event['decision_committed_at_ms'])
        for symbol in SYMBOLS:
            if not self.quality_halt:self.drain(symbol,stamp)

    def _thinning(self,event,cutoff):
        history=self.store.history(event['measurement_minutes'],event['confirmed_time_ms'],cutoff)
        unknown=sum(h['flow_status'] not in ('PASS','FAIL_THRESHOLD') or h['tvp_status'] not in ('PASS','FAIL_THRESHOLD') for h in history)
        unknown+=self.store.unavailable_prior_parents(event['measurement_minutes'],event['confirmed_time_ms'],cutoff)
        required_through=event['confirmed_time_ms']-event['measurement_minutes']*M-M
        coverage=all(self.processed[s]>=required_through for s in SYMBOLS)
        complete=(event['confirmed_time_ms']>=self.activation.warmup_start_ms+96*DAY and coverage and not self.store.unresolved_gaps() and unknown==0)
        records=[]
        for package in ('E2','ORIGIN'):
            kernel='V3__M'+str(event['measurement_minutes'])+'__'+package
            for arm,field in [('TVP_RECOVERY','tvp_status'),('DIVERGENCE_RECOVERY','flow_status'),('TVP_DIVERGENCE_RECOVERY','joint_status')]:
                target=sum(h[field]=='PASS' for h in history)
                rate=(target/len(history) if history else 1.) if complete else None
                for seed in (610031,610032,610033):
                    uniform=int(hashlib.sha256(('SPECIAL_EVENT_V3|'+kernel+'|'+event['event_id']+'|'+str(seed)).encode()).hexdigest()[:16],16)/2**64
                    records.append(dict(event_id=event['event_id'],kernel_id=kernel,target_arm=arm,seed=seed,
                        prior_window_start_ms=event['confirmed_time_ms']-90*DAY,prior_window_end_ms=event['confirmed_time_ms'],
                        prior_price_count=len(history),prior_target_count=target,unknown_prior_event_count=unknown,
                        history_complete=complete,rate_p=rate,uniform_u=uniform,selected=uniform<rate if rate is not None else None,
                        decision_information_cutoff_ms=cutoff,target_qualification_complete=unknown==0,coverage_watermarks=dict(self.processed)))
        self.store.record_batch_once('thinning:'+event['event_id'],'CAUSAL_THINNING',records)

    def ensure_gap_coverage(self,stamp):
        if stamp.estimated_ms<self.activation.warmup_start_ms+M+15000:return
        until=(stamp.estimated_ms-15000)//M*M
        for symbol in SYMBOLS:
            expected=self.processed[symbol]+M
            if expected<until and self.store.get_bar(symbol,expected) is None:self.store.gap(symbol,expected,until)

    def ingest_bbo(self,payload,stamp):
        raw=payload.get('data',payload);symbol=raw.get('s')
        require(raw.get('e')=='bookTicker' and symbol in SYMBOLS and raw.get('st',1)==1,'Unregistered public BBO')
        bid,ask,bid_size,ask_size=map(number,(raw.get('b'),raw.get('a'),raw.get('B'),raw.get('A')))
        require(all(x is not None and x>0 for x in (bid,ask)) and bid<=ask and
                all(x is not None and x>=0 for x in (bid_size,ask_size)),'Invalid BBO')
        require(type(raw.get('E')) is int,'BBO source event time required')
        self.activation.check_bar_scope(raw['E'])
        require(raw['E']<=stamp.estimated_ms+(stamp.uncertainty_ms or 0),'Future BBO source rejected')
        value=dict(symbol=symbol,bid=bid,ask=ask,bid_size=bid_size,ask_size=ask_size,
                   quote_exchange_time_ms=raw['E'],quote_receive_time_ms=stamp.utc_ms,source_sequence=raw.get('u'),
                   receive=stamp.record(),payload=copy.deepcopy(raw))
        if len(self.quotes[symbol])==self.quotes[symbol].maxlen:
            self.quote_lost_until[symbol]=self.quotes[symbol][0]['receive']['estimated_utc_ms'];self.quote_overflows[symbol]+=1
        self.quotes[symbol].append(value);self.last_receives[symbol]=stamp.utc_ms
        return value

    def reference_for(self,event,stamp):
        deadline=event['entry_time_ms'];candidates=[]
        for quote in list(self.quotes[event['symbol']]):
            receive=quote['receive'];u=receive['offset_uncertainty_ms']
            if u is None or u>100:continue
            received=receive['estimated_utc_ms'];upper=received+u
            source_age=deadline-quote['quote_exchange_time_ms'];cache_age=deadline-received
            if upper<=deadline and 0<=cache_age<=1000 and 0<=source_age-u and source_age+u<=1000:
                candidates.append(quote)
        value=dict(scheduled_at_ms=deadline,snapshot_selected_at_ms=stamp.estimated_ms,
                   capture_delay_ms=max(0,stamp.estimated_ms-deadline),reference_status='NO_REFERENCE')
        if not candidates:
            lost=self.quote_lost_until[event['symbol']]
            reset_utc=self.quote_cache_reset_at_ms+stamp.offset_ms
            restart_loss=(self.restored_quote_context and
                          (stamp.uncertainty_ms is None or reset_utc+stamp.uncertainty_ms>=deadline-1000))
            if restart_loss:
                value.update(reference_status='QUOTE_HISTORY_INCOMPLETE',history_reason='RESTART_BUFFER_LOSS')
            elif lost is not None and lost>=deadline-1000:
                value.update(reference_status='QUOTE_HISTORY_INCOMPLETE',history_reason='BUFFER_OVERFLOW')
            return value
        chosen=max(candidates,key=lambda q:q['receive']['estimated_utc_ms'])
        value.update({k:v for k,v in chosen.items() if k!='payload'})
        value['reference_status']='CAPTURED';value['source_payload']=chosen['payload']
        return value

    def tick(self,stamp):
        for event in self.store.pending_events(stamp.estimated_ms):
            if stamp.estimated_ms>event['expires_time_ms']:
                ref=dict(reference_status='MISSED_DEADLINE',scheduled_at_ms=event['entry_time_ms'],snapshot_selected_at_ms=stamp.estimated_ms)
            else:ref=self.reference_for(event,stamp)
            self.store.reference(event['event_id'],ref)
        for symbol in SYMBOLS:
            minute=stamp.estimated_ms//M
            if self.health_minute[symbol]==minute or not self.quotes[symbol]:continue
            quote=self.quotes[symbol][-1];self.health_minute[symbol]=minute
            self.store.record('BBO_HEALTH',dict(sampled_at_ms=stamp.estimated_ms,**{k:v for k,v in quote.items() if k!='payload'}),
                              payload=quote['payload'],counter='bbo_health_samples')

    def check_budget(self,stamp):
        budget=self.store.budget()
        if not budget['ok']:
            if not self.disk_halt:
                self.disk_halt=True;self.store.error('DISK_BUDGET_HALT',stamp.utc_ms)
            return False
        return not self.disk_halt

    def status(self,stamp):
        counts=self.store.counts();gaps=self.store.unresolved_gaps()
        phase=self.activation.phase(stamp.estimated_ms)
        per={s:dict(self.store.symbol_counts(s),last_receive_ms=self.last_receives[s],processed_bar_ms=self.processed[s] if self.processed[s]>=self.activation.warmup_start_ms else None) for s in SYMBOLS}
        received=[v for v in self.last_receives.values() if v is not None];closed=[p['last_closed_bar_ms'] for p in per.values() if p['last_closed_bar_ms'] is not None]
        events=[]
        fields=('event_id','symbol','measurement_minutes','side','initial_direction','start_time_ms','confirmed_time_ms','available_time_ms',
                'decision_committed_at_ms','flow_status','tvp_status','joint_status','on_time_status','research_only')
        for event,ref in self.store.recent_events():
            item={k:event.get(k) for k in fields};item['reference_status']=ref['reference_status'] if ref else 'PENDING';events.append(item)
        halted=self.quality_halt or self.disk_halt
        operational=('STOPPED' if self.stopped else 'HALTED' if halted else 'WAITING' if phase=='WAITING_CAPTURE' else
                     'RUNNING' if all(self.connections.values()) and not gaps else 'DEGRADED')
        return dict(schema_version=1,display_name='金丹v1.0',version='1.0.0',mode='observation_only',updated_at=stamp.utc_ms/1000,
                    phase='ENGINEERING_HALT' if halted else phase,calendar_phase=phase,operational_status=operational,
                    activation_id=self.activation.activation_id,activation_sha256=self.activation.document_sha,**self.activation.dates(),
                    counts={k:counts[k] for k in counts if k not in ('gap_count','revision_count')},per_symbol=per,
                    last_closed_bar_ms=max(closed) if closed else None,last_receive_ms=max(received) if received else None,
                    errors=self.store.errors(),gap_count=counts['gap_count'],unresolved_gap_count=len(gaps),revision_count=counts['revision_count'],
                    clock=stamp.record(),events=events,storage=self.store.budget(),reconnect_count=self.store.counter('reconnect_count'),
                    quote_buffer_overflow_count=sum(self.quote_overflows.values()),
                    capabilities=dict(raw_closed_1m=True,actual_receive_timestamps=True,incremental_price_flow_events=True,causal_history=True,
                        journal_checkpoint_recovery=True,bbo_reference_observation=True,e2_labels=False,origin_labels=False,
                        funding_cashflow_labels=False,formal_scoring=False,order_execution=False),
                    readiness_gaps=['E2_LABELS_NOT_IMPLEMENTED','ORIGIN_LABELS_NOT_IMPLEMENTED','FUNDING_LABELS_NOT_IMPLEMENTED','SCORE_PREFLIGHT_REQUIRED'],
                    boundaries=dict(orders_enabled=False,final_period_access=False,trading_integration=False,account_access=False,
                        prospective_final_confirmation=False,performance_evaluated=False,synthetic_history_used=False))

    def write_snapshots(self,stamp):
        status=self.status(stamp)
        for name,value in [('status.json',status),('events.json',dict(schema_version=1,updated_at=status['updated_at'],events=status['events']))]:
            target=self.store.directory/name;tmp=target.with_suffix('.json.tmp')
            with tmp.open('w') as stream:
                json.dump(value,stream,ensure_ascii=False,allow_nan=False,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,target)
        return status


class PublicRuntime:
    """Two public WS readers, clock evidence, bounded recovery and one DB worker."""
    def __init__(self,core):
        from concurrent.futures import ThreadPoolExecutor
        self.core=core;self.stop=asyncio.Event();self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='jindan-journal')
        self.offset_ms=0.;self.uncertainty_ms=None;self.clock_updated_mono=None;self.clock_wall_anchor_ms=None;self.clock_mono_anchor_ns=None
        self.rest_not_before=0.;self.last_error_at={};self.session=None

    def reading(self):
        wall=time.time_ns()//1000000;mono=time.monotonic_ns()
        elapsed=(time.monotonic()-self.clock_updated_mono) if self.clock_updated_mono is not None else None
        discontinuity=(self.clock_wall_anchor_ms is not None and abs((wall-self.clock_wall_anchor_ms)-(mono-self.clock_mono_anchor_ns)/1e6)>100)
        uncertainty=(self.uncertainty_ms+elapsed*.05 if not discontinuity and elapsed is not None and 0<=elapsed<300 and self.uncertainty_ms is not None else None)
        return ReceiveStamp(wall,mono,self.offset_ms,uncertainty)

    async def call(self,func,*args):
        import functools
        return await asyncio.get_running_loop().run_in_executor(self.executor,functools.partial(func,*args))

    async def error(self,code):
        now=time.monotonic()
        if now-self.last_error_at.get(code,-1000)<30:return
        self.last_error_at[code]=now
        await self.call(self.core.store.error,code,self.reading().utc_ms)
        print(canonical(dict(event='collector_health',code=code,at_ms=self.reading().utc_ms)),flush=True)

    async def wait(self,seconds):
        try:await asyncio.wait_for(self.stop.wait(),timeout=seconds)
        except asyncio.TimeoutError:pass

    async def public_json(self,path,params):
        import aiohttp
        from .config import validate_public_rest
        validate_public_rest(path,params,self.core.activation)
        require(time.monotonic()>=self.rest_not_before,'PUBLIC_REST_COOLDOWN')
        started=self.reading()
        async with self.session.get('https://fapi.binance.com'+path,params=params,
                                    timeout=aiohttp.ClientTimeout(total=10),allow_redirects=False) as response:
            if response.status in (418,429):
                try:pause=max(3.,min(300.,float(response.headers.get('Retry-After','60'))))
                except ValueError:pause=60.
                self.rest_not_before=time.monotonic()+pause
                await self.error('RATE_LIMITED');raise ValueError('PUBLIC_REST_COOLDOWN')
            require(response.status==200,'PUBLIC_SOURCE_UNAVAILABLE')
            chunks=[];size=0
            async for chunk in response.content.iter_chunked(65536):
                size+=len(chunk);require(size<=1024*1024,'Public response exceeded bounded input');chunks.append(chunk)
            received=self.reading();value=json.loads(b''.join(chunks))
        return value,started,received

    async def clock_loop(self):
        while not self.stop.is_set():
            try:
                result,begin,end=await self.public_json('/fapi/v1/time',{})
                server=result.get('serverTime');require(type(server) is int,'Public server timestamp absent')
                rtt_ms=(end.monotonic_ns-begin.monotonic_ns)/1e6
                require(0<=rtt_ms<=5000,'Public clock roundtrip invalid')
                midpoint=(begin.utc_ms+end.utc_ms)/2
                self.offset_ms=server-midpoint;self.uncertainty_ms=rtt_ms/2+2.
                self.clock_updated_mono=time.monotonic();self.clock_wall_anchor_ms=end.utc_ms;self.clock_mono_anchor_ns=end.monotonic_ns
                await self.call(self.core.store.record,'CLOCK_CHECK',dict(local_request_at_ms=begin.utc_ms,local_receive_at_ms=end.utc_ms,
                    roundtrip_ms=rtt_ms,utc_offset_estimate_ms=self.offset_ms,offset_uncertainty_ms=self.uncertainty_ms,
                    interpretation='PUBLIC_SERVER_ROUNDTRIP_ESTIMATE_NOT_INDEPENDENT_UTC_CERTIFICATION'),result)
                if self.uncertainty_ms>100:await self.error('CLOCK_UNCERTAIN')
            except asyncio.CancelledError:raise
            except Exception:await self.error('CLOCK_UNCERTAIN')
            await self.wait(60)

    async def stream_loop(self,name,url):
        import aiohttp
        validate_public_ws(url);delay=3.
        while not self.stop.is_set():
            if self.reading().estimated_ms<self.core.activation.capture_start_ms:
                await self.wait(min(30,(self.core.activation.capture_start_ms-self.reading().estimated_ms)/1000));continue
            if self.core.disk_halt:return
            try:
                async with self.session.ws_connect(url,heartbeat=30,autoping=True,max_msg_size=65536,
                                                   receive_timeout=90,timeout=15) as ws:
                    self.core.connections[name]=True;delay=3.
                    print(canonical(dict(event='public_ws_connected',stream=name,at_ms=self.reading().utc_ms)),flush=True)
                    async for message in ws:
                        stamp=self.reading()
                        if self.stop.is_set() or self.core.disk_halt:break
                        if message.type==aiohttp.WSMsgType.TEXT:
                            try:
                                payload=json.loads(message.data);body=payload.get('data',payload)
                                if name=='market':
                                    opened=body.get('k',{}).get('t')
                                    if body.get('k',{}).get('x') is not True:continue
                                    if type(opened) is int and opened<self.core.activation.capture_start_ms:continue
                                    await self.call(self.core.ingest_kline,payload,stamp)
                                else:self.core.ingest_bbo(payload,stamp)
                            except (ValueError,TypeError,KeyError,OverflowError):
                                await self.error('PUBLIC_PAYLOAD_INVALID')
                        elif message.type in (aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE):break
            except asyncio.CancelledError:raise
            except Exception:await self.error('PUBLIC_SOURCE_UNAVAILABLE')
            finally:self.core.connections[name]=False
            if not self.stop.is_set():
                await self.call(self.core.store.record,'WS_RECONNECT',dict(stream=name,at_ms=self.reading().utc_ms),None,'reconnect_count')
                await self.error('WS_DISCONNECTED');await self.wait(delay);delay=min(60,delay*2)

    async def recovery_loop(self):
        while not self.stop.is_set():
            try:
                stamp=self.reading()
                if stamp.estimated_ms>=self.core.activation.warmup_start_ms and not self.core.quality_halt and not self.core.disk_halt:
                    await self.call(self.core.ensure_gap_coverage,stamp)
                    for gap in self.core.store.unresolved_gaps()[:3]:
                        left=gap['start_ms'];right=min(gap['end_ms']-1,left+180*M-1,(stamp.estimated_ms//M)*M-1)
                        if right<left:continue
                        rows,_,received=await self.public_json('/fapi/v1/klines',dict(symbol=gap['symbol'],interval='1m',startTime=left,endTime=right,limit=180))
                        require(isinstance(rows,list) and len(rows)<=180,'Bounded public backfill required')
                        for row in rows:
                            require(isinstance(row,list) and len(row)>=12,'Public backfill schema mismatch')
                            opened=int(row[0]);require(left<=opened<=right and int(row[6])+1<=received.estimated_ms,'Backfill returned out-of-scope or unclosed minute')
                            payload=dict(e='kline',E=None,s=gap['symbol'],k=dict(t=opened,T=int(row[6]),s=gap['symbol'],i='1m',x=True,
                                o=row[1],h=row[2],l=row[3],c=row[4],v=row[5],q=row[7],n=int(row[8]),V=row[9],Q=row[10]))
                            await self.call(self.core.ingest_kline,payload,received,True)
                    await self.call(self.core.recover,self.reading())
            except asyncio.CancelledError:raise
            except Exception:await self.error('RECOVERY_REQUIRED')
            await self.wait(30)

    async def heartbeat_loop(self):
        last_snapshot=0.
        while not self.stop.is_set():
            try:
                stamp=self.reading()
                if not await self.call(self.core.check_budget,stamp):self.stop.set()
                await self.call(self.core.tick,stamp)
                if time.monotonic()-last_snapshot>=5:
                    await self.call(self.core.write_snapshots,stamp);last_snapshot=time.monotonic()
            except asyncio.CancelledError:raise
            except Exception:
                print(canonical(dict(event='collector_health',code='SNAPSHOT_OR_STORAGE_FAILED',at_ms=self.reading().utc_ms)),flush=True)
                self.stop.set()
            await self.wait(.5)

    async def run(self):
        import aiohttp
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):
            try:loop.add_signal_handler(sig,self.stop.set)
            except (NotImplementedError,RuntimeError):pass
        tasks=[]
        try:
            await self.call(self.core.write_snapshots,self.reading())
            if not await self.call(self.core.check_budget,self.reading()):return
            await self.call(self.core.recover,self.reading())
            timeout=aiohttp.ClientTimeout(total=None,sock_connect=10,sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout,trust_env=False,headers={'User-Agent':'JindanPublicObservation/1.0'}) as session:
                self.session=session
                tasks=[asyncio.create_task(self.clock_loop()),asyncio.create_task(self.stream_loop('market',MARKET_WS)),
                       asyncio.create_task(self.stream_loop('bbo',BBO_WS)),asyncio.create_task(self.recovery_loop()),
                       asyncio.create_task(self.heartbeat_loop())]
                await self.stop.wait()
        finally:
            for task in tasks:task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            self.core.stopped=True
            try:
                await self.call(self.core.save)
                await self.call(self.core.write_snapshots,self.reading())
            finally:
                self.executor.shutdown(wait=True,cancel_futures=True)
