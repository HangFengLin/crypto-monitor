"""Bounded-memory incremental mirror of the frozen V3 price/flow mechanism.

Numerical rules stay fixed. Receive/availability evidence is provided by the live
adapter, never inferred as close+5s. All valid PRICE parents survive missing FLOW.
"""
from collections import deque
import copy
import hashlib
import math
import numpy as np
from .config import canonical, require

M=60000
PLAN_ID='SPECIAL_EVENT_V3_DIVERGENCE_RECOVERY_20261004'
FAMILY='IMPULSE_DIVERGENCE_RECOVERY'


def identity(symbol,minutes,direction,start,detected,confirmed=None):
    value=dict(plan_id=PLAN_ID,family=FAMILY,measurement_minutes=minutes,symbol=symbol,
               initial_direction=direction,start_time_ms=start,detected_time_ms=detected)
    if confirmed is not None:value['confirmed_time_ms']=confirmed
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def optional_sum(values):
    return math.fsum(values) if all(x is not None for x in values) else None


class IncrementalDetector:
    def __init__(self,symbol,minutes,state=None):
        require(minutes in (15,60),'Frozen measurement required')
        self.symbol,self.minutes=symbol,minutes
        self.bars=deque(maxlen=160);self.pending=[];self.index=-1;self.last_minute=None
        self.states={-1:None,1:None};self.cooldown_until={-1:-1,1:-1};self.cooldown_states={-1:None,1:None}
        if state:
            require(state['symbol']==symbol and state['minutes']==minutes,'Checkpoint detector identity')
            self.bars.extend(state['bars']);self.pending=state['pending'];self.index=state['index'];self.last_minute=state['last_minute']
            for name in ('states','cooldown_until','cooldown_states'):
                setattr(self,name,{int(k):v for k,v in state[name].items()})

    def snapshot(self):
        return copy.deepcopy(dict(symbol=self.symbol,minutes=self.minutes,bars=list(self.bars),pending=self.pending,
                                  index=self.index,last_minute=self.last_minute,states=self.states,
                                  cooldown_until=self.cooldown_until,cooldown_states=self.cooldown_states))

    def _get(self,index):
        offset=index-self.bars[0]['index']
        require(0<=offset<len(self.bars),'Required frozen dependency absent')
        return self.bars[offset]

    def _range(self,first,last):
        return [self._get(i) for i in range(first,last)]

    def _observer(self,state,status,bar,**extra):
        value=dict(observer_id=state['observer_id'],status=status,symbol=self.symbol,measurement_minutes=self.minutes,
                   initial_direction=state['direction'],start_time_ms=state['start_time_ms'],
                   detected_time_ms=state['detected_time_ms'],time_ms=bar['end'],bar_index=bar['index'],
                   start_bar_index=state['start'],detected_bar_index=state['detected'],atr=state['atr'],
                   volume_baseline=state['baseline'],origin_target_price=state['origin'],detected_close=state['detected_close'],
                   impulse_amplitude=state['impulse'],path_efficiency=state['efficiency'])
        value.update(extra);return value

    def _phase(self,first,last,direction):
        bars=self._range(first,last)
        quote=optional_sum([x for b in bars for x in b['minute_quotes']])
        buy=optional_sum([x for b in bars for x in b['minute_buys']])
        quality=[x for b in bars for x in b['minute_flow_status']]
        if quote is None or buy is None:
            status='INVALID' if 'INVALID' in quality else 'UNKNOWN_MISSING'
        elif quote<=0:status='ZERO_PHASE_Q'
        else:status='VALID'
        imbalance=(2*buy-quote)/quote if status=='VALID' else None
        return dict(start_time_ms=bars[0]['start'],end_exclusive_ms=bars[-1]['end'],minute_rows=len(bars)*self.minutes,
                    Q=quote,B=buy,D=imbalance,directional_D=direction*imbalance if imbalance is not None else None,
                    valid=status=='VALID',status=status)

    def _confirmed(self,state,bar,computed_ms):
        direction=state['direction'];start=state['start'];detected=state['detected'];index=bar['index']
        elapsed=index-detected;confirmation=bar['end']
        phases=dict(impulse=self._phase(start,detected+1,direction),recovery=self._phase(detected+1,index+1,direction))
        bad=[p['status'] for p in phases.values() if not p['valid']]
        if bad:flow_status='INVALID' if 'INVALID' in bad else 'UNKNOWN_MISSING' if 'UNKNOWN_MISSING' in bad else 'ZERO_PHASE_Q'
        else:flow_status='PASS' if phases['recovery']['directional_D']>=.1 else 'FAIL_THRESHOLD'
        q=phases['impulse']['Q'];base=state['baseline']
        rvol=q/(4*base) if q is not None and base is not None and base>0 else None
        if q is None or base is None:tvp_status='UNKNOWN_MISSING'
        else:tvp_status='PASS' if rvol is not None and elapsed<=3 and rvol>=1.5 else 'FAIL_THRESHOLD'
        if flow_status=='PASS' and tvp_status=='PASS':joint='PASS'
        elif flow_status=='FAIL_THRESHOLD' or tvp_status=='FAIL_THRESHOLD':joint='FAIL_THRESHOLD'
        else:joint=flow_status if flow_status!='PASS' else tvp_status
        selection=self._range(start,index+1)
        stop=(max(b['high'] for b in selection)+.25*state['atr'] if direction==1 else
              min(b['low'] for b in selection)-.25*state['atr'])
        dependencies=self._range(max(0,start-120),index+1)
        available=max([computed_ms]+[b['available_at_ms'] for b in dependencies])
        recovery_fraction=direction*(state['detected_close']-bar['close'])/state['impulse']
        event_id=identity(self.symbol,self.minutes,direction,state['start_time_ms'],state['detected_time_ms'],confirmation)
        features=dict(structure_eligible=True,flow_valid=not bad,flow_eligible=flow_status=='PASS',tvp_eligible=tvp_status=='PASS',
                      phase_details=phases,impulse_amplitude=state['impulse'],path_efficiency=state['efficiency'],
                      impulse_rvol=rvol,recovery_fraction=recovery_fraction,elapsed_recovery_bars=elapsed,
                      invalid_reasons=bad,feature_clock_ms=confirmation,available_time_ms=available)
        return dict(event_id=event_id,symbol=self.symbol,family=FAMILY,side=-direction,initial_direction=direction,
                    measurement_minutes=self.minutes,start_time_ms=state['start_time_ms'],confirmed_time_ms=confirmation,
                    available_time_ms=available,entry_time_ms=confirmation+M,expires_time_ms=confirmation+2*M,
                    confirmation_price=bar['close'],stop_price=stop,atr=state['atr'],origin_target_price=state['origin'],
                    v3_features=features,flow_status=flow_status,tvp_status=tvp_status,joint_status=joint,
                    latest_dependency_available_at_ms=max(b['available_at_ms'] for b in dependencies),
                    dependency_clock_uncertainty_ms=max((b.get('clock_uncertainty_ms') or 0 for b in dependencies),default=0),
                    dependency_clock_known=all(b.get('clock_uncertainty_ms') is not None for b in dependencies),
                    backfilled=any(b['backfilled'] for b in selection),research_only=True)

    def ingest_minute(self,minute,computed_ms):
        timestamp=minute['time_ms']
        require(self.last_minute is None or timestamp==self.last_minute+M,'Detector requires contiguous unique minutes')
        self.last_minute=timestamp
        bucket=timestamp//(self.minutes*M)*(self.minutes*M)
        if not self.pending and timestamp!=bucket:
            return dict(events=[],observers=[])
        self.pending.append(copy.deepcopy(minute))
        if len(self.pending)<self.minutes:return dict(events=[],observers=[])
        parts=self.pending;self.pending=[];self.index+=1
        minute_quotes=[p.get('quote_volume') for p in parts]
        quote=(float(np.sum(np.asarray(minute_quotes,dtype=np.float64),dtype=np.float64))
               if all(q is not None for q in minute_quotes) else None)
        previous=self.bars[-1]['close'] if self.bars else parts[0]['close']
        high=max(p['high'] for p in parts);low=min(p['low'] for p in parts)
        bar=dict(index=self.index,start=bucket,end=bucket+self.minutes*M,open=parts[0]['open'],high=high,low=low,
                 close=parts[-1]['close'],quote=quote,TR=max(high-low,abs(high-previous),abs(low-previous)),
                 minute_quotes=[p.get('quote_volume') for p in parts],minute_buys=[p.get('taker_buy_quote_volume') for p in parts],
                 minute_flow_status=[p.get('flow_status','UNKNOWN_MISSING') for p in parts],
                 available_at_ms=max(p['available_at_ms'] for p in parts),backfilled=any(p.get('backfilled',False) for p in parts),
                 clock_uncertainty_ms=(max(p.get('clock_uncertainty_ms',0) or 0 for p in parts)
                                       if all(p.get('clock_uncertainty_ms',0) is not None for p in parts) else None))
        self.bars.append(bar);events=[];observers=[];index=self.index
        for direction in (-1,1):
            state=self.states[direction]
            if state is not None:
                elapsed=index-state['detected'];recovery=direction*(state['detected_close']-bar['close'])
                if recovery>=.35*state['impulse']:
                    event=self._confirmed(state,bar,computed_ms);events.append(event)
                    observers.append(self._observer(state,'CONFIRMED',bar,elapsed_recovery_bars=elapsed,event_id=event['event_id']))
                    self.states[direction]=None;self.cooldown_states[direction]=state;self.cooldown_until[direction]=index+6
                elif elapsed==6:
                    observers.append(self._observer(state,'TIMEOUT',bar,elapsed_recovery_bars=elapsed))
                    self.states[direction]=None;self.cooldown_states[direction]=state;self.cooldown_until[direction]=index+6
                else:observers.append(self._observer(state,'OBSERVING',bar,elapsed_recovery_bars=elapsed))
                continue
            if index<=self.cooldown_until[direction]:
                observers.append(self._observer(self.cooldown_states[direction],'COOLDOWN_SKIP',bar,
                                               remaining_including_current=self.cooldown_until[direction]-index+1));continue
            start=index-3
            if start<120:continue
            origin=self._get(start-1)['close'];impulse=direction*(bar['close']-origin)
            if impulse<=0:continue
            path=math.fsum(abs(self._get(j)['close']-self._get(j-1)['close']) for j in range(start,index+1))
            efficiency=impulse/path if path>0 else 0.
            if efficiency<.6:continue
            atr=float(np.mean(np.asarray([b['TR'] for b in self._range(start-20,start)],dtype=np.float64)))
            if atr<=0 or impulse<2.5*atr:continue
            baseline_quotes=[b['quote'] for b in self._range(start-120,start)]
            if any(q is None for q in baseline_quotes):baseline=None
            else:
                baseline=float(np.median(np.asarray(baseline_quotes,dtype=np.float64)))
            state=dict(observer_id=identity(self.symbol,self.minutes,direction,self._get(start)['start'],bar['end']),
                       direction=direction,start=start,detected=index,atr=atr,baseline=baseline,origin=origin,
                       detected_close=bar['close'],impulse=impulse,efficiency=efficiency,
                       start_time_ms=self._get(start)['start'],detected_time_ms=bar['end'])
            self.states[direction]=state;observers.append(self._observer(state,'DETECTED',bar))
        return dict(events=events,observers=observers)
