"""Single-writer SQLite evidence journal and rebuildable state indexes."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import fcntl
import time
import zlib

from .config import canonical, digest, require

COUNTERS = ('raw_messages','closed_bars','bbo_health_samples','price_events','flow_pass','flow_fail',
            'flow_unknown','tvp_pass','joint_pass','on_time_events','late_events','reference_captured','reference_missing')
BAR_FIELDS = ('symbol','time_ms','close_time_ms','open','high','low','close','base_volume','quote_volume',
              'trades','taker_buy_quote_volume','taker_buy_base_volume','flow_status')


class Store:
    def __init__(self, directory, max_bytes=4*1024**3, min_free_bytes=2*1024**3):
        self.directory = Path(directory).resolve(); self.directory.mkdir(parents=True, exist_ok=True)
        self.writer_lock=(self.directory/'.writer.lock').open('a+')
        try:fcntl.flock(self.writer_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.writer_lock.close();raise ValueError('STATE_SINGLE_WRITER_REQUIRED')
        self.max_bytes, self.min_free_bytes = max_bytes, min_free_bytes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.directory/'jindan.sqlite3'), timeout=30, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA cache_size=-2048'); self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,record TEXT NOT NULL,previous_sha TEXT NOT NULL,record_sha TEXT NOT NULL,stored_at_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS raw_payloads(sha TEXT PRIMARY KEY,payload BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS bars(symbol TEXT NOT NULL,time_ms INTEGER NOT NULL,version_sha TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(symbol,time_ms));
        CREATE TABLE IF NOT EXISTS bar_versions(symbol TEXT NOT NULL,time_ms INTEGER NOT NULL,version_sha TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(symbol,time_ms,version_sha));
        CREATE TABLE IF NOT EXISTS availability(symbol TEXT NOT NULL,time_ms INTEGER NOT NULL,available_ms INTEGER NOT NULL,PRIMARY KEY(symbol,time_ms));
        CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS checkpoints(name TEXT PRIMARY KEY,state TEXT NOT NULL,state_sha TEXT NOT NULL,journal_seq INTEGER NOT NULL,journal_sha TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(transition_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,confirmed_ms INTEGER NOT NULL,record TEXT NOT NULL,reference TEXT);
        CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS errors(code TEXT PRIMARY KEY,at_ms INTEGER NOT NULL,count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gaps(symbol TEXT NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(symbol,start_ms));
        ''')
        for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
            self.db.execute('INSERT OR IGNORE INTO counters VALUES(?,(SELECT COUNT(*) FROM bars WHERE symbol=?))',('closed_bars:'+symbol,symbol))
        self.db.commit()
        if not self.verify_journal():
            self.db.close();self.writer_lock.close();raise ValueError('JOURNAL_INVALID')

    def _append(self, kind, record):
        previous = self.db.execute('SELECT seq,record_sha FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
        sequence, head = (previous[0]+1, previous[1]) if previous else (1, '0'*64)
        body = canonical(record)
        hashed = hashlib.sha256((str(sequence)+'|'+kind+'|'+head+'|'+body).encode()).hexdigest()
        at = time.time_ns()//1000000
        self.db.execute('INSERT INTO journal VALUES(?,?,?,?,?,?)',(sequence,kind,body,head,hashed,at))
        return dict(sequence=sequence, record_sha=hashed, durable_at_ms=at)

    def _inc(self, name, amount=1):
        self.db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value',(name,amount))

    def record(self, kind, record, payload=None, counter=None):
        with self.lock, self.db:
            if payload is not None:
                raw=canonical(payload).encode(); raw_sha=hashlib.sha256(raw).hexdigest()
                self.db.execute('INSERT OR IGNORE INTO raw_payloads VALUES(?,?)',(raw_sha,zlib.compress(raw,6)))
                record=dict(record,source_payload_sha=raw_sha)
                self._inc('raw_messages')
            result=self._append(kind,record)
            if counter:self._inc(counter)
        result['durable_at_ms']=time.time_ns()//1000000
        return result

    def accept_bar(self, bar, payload):
        semantic={k:bar.get(k) for k in BAR_FIELDS};version=digest(semantic)
        with self.lock, self.db:
            raw=canonical(payload).encode();raw_sha=hashlib.sha256(raw).hexdigest()
            row=self.db.execute('SELECT version_sha FROM bars WHERE symbol=? AND time_ms=?',(bar['symbol'],bar['time_ms'])).fetchone()
            if row and row[0]==version:
                self._inc('duplicate_bars')
                return dict(status='DUPLICATE',version_sha=version)
            self.db.execute('INSERT OR IGNORE INTO raw_payloads VALUES(?,?)',(raw_sha,zlib.compress(raw,6)))
            value=dict(bar,version_sha=version,source_payload_sha=raw_sha)
            text=canonical(value)
            self.db.execute('INSERT OR IGNORE INTO bar_versions VALUES(?,?,?,?)',(bar['symbol'],bar['time_ms'],version,text))
            self._inc('raw_messages')
            if row:
                self._inc('revision_count');self._append('BAR_CONFLICT',dict(symbol=bar['symbol'],time_ms=bar['time_ms'],first_version_sha=row[0],conflicting_version_sha=version,source_payload_sha=raw_sha))
                self._error('BAR_CONFLICT',bar['local_receive_at_ms'])
                return dict(status='CONFLICT',version_sha=version)
            self.db.execute('INSERT INTO bars VALUES(?,?,?,?)',(bar['symbol'],bar['time_ms'],version,text))
            self._inc('closed_bars');self._inc('closed_bars:'+bar['symbol'])
            commit=self._append('BAR_ACCEPTED',dict(symbol=bar['symbol'],time_ms=bar['time_ms'],version_sha=version,source_payload_sha=raw_sha,receive=bar.get('receive')))
        commit.update(status='NEW',version_sha=version,durable_at_ms=time.time_ns()//1000000)
        return commit

    def get_bar(self,symbol,timestamp):
        with self.lock:
            row=self.db.execute('SELECT record FROM bars WHERE symbol=? AND time_ms=?',(symbol,timestamp)).fetchone()
        return self._decode_bar(row[0]) if row else None

    def _decode_bar(self,text):
        value=json.loads(text)
        require(digest({k:value.get(k) for k in BAR_FIELDS})==value.get('version_sha'),'BAR_MARKET_HASH_INVALID')
        proof=self.db.execute('SELECT available_ms FROM availability WHERE symbol=? AND time_ms=?',(value['symbol'],value['time_ms'])).fetchone()
        value['availability_proven']=proof is not None
        if proof:value['available_at_ms']=max(value['available_at_ms'],proof[0])
        return value

    def mark_available(self,symbol,timestamp,available_ms):
        with self.lock,self.db:
            inserted=self.db.execute('INSERT OR IGNORE INTO availability VALUES(?,?,?)',(symbol,timestamp,available_ms)).rowcount
            if inserted:self._append('BAR_AVAILABLE',dict(symbol=symbol,time_ms=timestamp,available_at_ms=available_ms))

    def bars_from(self,symbol,timestamp,limit=180):
        with self.lock:
            rows=self.db.execute('SELECT record FROM bars WHERE symbol=? AND time_ms>=? ORDER BY time_ms LIMIT ?',(symbol,timestamp,limit)).fetchall()
        with self.lock:return [self._decode_bar(r[0]) for r in rows]

    def checkpoint(self,name,state):
        value=canonical(state)
        with self.lock,self.db:
            head=self.db.execute('SELECT seq,record_sha FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
            seq,hashed=head if head else (0,'0'*64)
            self.db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,state_sha=excluded.state_sha,journal_seq=excluded.journal_seq,journal_sha=excluded.journal_sha',
                            (name,value,hashlib.sha256(value.encode()).hexdigest(),seq,hashed))

    def load_checkpoint(self,name):
        with self.lock:
            row=self.db.execute('SELECT state,state_sha,journal_seq,journal_sha FROM checkpoints WHERE name=?',(name,)).fetchone()
            if not row:return None
            require(hashlib.sha256(row[0].encode()).hexdigest()==row[1],'CHECKPOINT_HASH_INVALID')
            actual=self.db.execute('SELECT record_sha FROM journal WHERE seq=?',(row[2],)).fetchone()
            require(row[2]==0 or actual and actual[0]==row[3],'CHECKPOINT_JOURNAL_INVALID')
        return json.loads(row[0])

    def bind_activation(self,activation):
        with self.lock,self.db:
            row=self.db.execute('SELECT value FROM meta WHERE name="activation_sha"').fetchone()
            require(not row or row[0]==activation.document_sha,'ACTIVATION_MISMATCH')
            if not row:
                self.db.execute('INSERT INTO meta VALUES("activation_sha",?)',(activation.document_sha,))
                self._append('ACTIVATION',activation.raw)

    def add_event(self,event):
        with self.lock,self.db:
            row=self.db.execute('SELECT record FROM events WHERE event_id=?',(event['event_id'],)).fetchone()
            if row:
                prior=json.loads(row[0])
                for field in ('symbol','side','measurement_minutes','start_time_ms','confirmed_time_ms','stop_price','origin_target_price','flow_status','tvp_status','joint_status'):
                    require(prior.get(field)==event.get(field),'EVENT_IDENTITY_CONFLICT')
                return False
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,NULL)',(event['event_id'],event['symbol'],event['confirmed_time_ms'],canonical(event)))
            self._append('PRICE_PARENT_CONFIRMED',event);self._inc('price_events')
            status=event['flow_status']
            self._inc('flow_pass' if status=='PASS' else 'flow_fail' if status=='FAIL_THRESHOLD' else 'flow_unknown')
            for key,field in [('tvp_pass','tvp_status'),('joint_pass','joint_status')]:
                if event[field]=='PASS':self._inc(key)
            if event['on_time_status']!='PENDING_COMMIT':
                self._inc('on_time_events' if event['on_time_status']=='ON_TIME' else 'late_events')
        return True

    def event(self,event_id):
        with self.lock:
            row=self.db.execute('SELECT record FROM events WHERE event_id=?',(event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def uncommitted_events(self):
        with self.lock:
            rows=self.db.execute("SELECT record FROM events WHERE json_extract(record,'$.on_time_status')='PENDING_COMMIT' ORDER BY confirmed_ms LIMIT 1000").fetchall()
        return [json.loads(r[0]) for r in rows]

    def events_without_batch(self):
        with self.lock:
            rows=self.db.execute("SELECT record FROM events WHERE event_id NOT IN (SELECT substr(batch_id,10) FROM batches WHERE batch_id LIKE 'thinning:%') ORDER BY confirmed_ms LIMIT 1000").fetchall()
        return [json.loads(r[0]) for r in rows]

    def record_batch_once(self,batch_id,kind,records):
        with self.lock,self.db:
            inserted=self.db.execute('INSERT OR IGNORE INTO batches VALUES(?)',(batch_id,)).rowcount
            if not inserted:return False
            for record in records:self._append(kind,record)
        return True

    def reference(self,event_id,record):
        with self.lock,self.db:
            old=self.db.execute('SELECT reference FROM events WHERE event_id=?',(event_id,)).fetchone()
            if not old or old[0] is not None:return False
            self.db.execute('UPDATE events SET reference=? WHERE event_id=?',(canonical(record),event_id))
            self._append('EVENT_REFERENCE',dict(event_id=event_id,**record))
            self._inc('reference_captured' if record['reference_status']=='CAPTURED' else 'reference_missing')
        return True

    def commit_event_receipt(self,event_id,stamp,available_ms):
        with self.lock,self.db:
            row=self.db.execute('SELECT record FROM events WHERE event_id=?',(event_id,)).fetchone()
            require(row is not None,'Missing parent for commit receipt')
            value=json.loads(row[0])
            if value.get('on_time_status')!='PENDING_COMMIT':return value
            committed=max(stamp.estimated_ms,available_ms)
            uncertainty=stamp.uncertainty_ms
            if uncertainty is None or uncertainty>100 or not value.get('dependency_clock_known',False):status='CLOCK_UNCERTAIN'
            elif committed+max(uncertainty,value.get('dependency_clock_uncertainty_ms',0))>value['entry_time_ms']:status='LATE_DEPENDENCY'
            elif value.get('backfilled'):status='BACKFILLED'
            else:status='ON_TIME'
            value.update(decision_committed_at_ms=committed,on_time_status=status,decision_clock=stamp.record())
            self._append('DECISION_COMMIT_RECEIPT',dict(event_id=event_id,decision_committed_at_ms=committed,on_time_status=status,clock=stamp.record()))
            self.db.execute('UPDATE events SET record=? WHERE event_id=?',(canonical(value),event_id))
            self._inc('on_time_events' if status=='ON_TIME' else 'late_events')
            return value

    def observer(self,record):
        transition=digest({k:record[k] for k in ('observer_id','status','time_ms')})
        with self.lock,self.db:
            found=self.db.execute('INSERT OR IGNORE INTO observations VALUES(?)',(transition,)).rowcount
            if found:self._append('OBSERVER_STATE',record)

    def history(self,minutes,confirmed,available_cutoff):
        with self.lock:
            rows=self.db.execute('SELECT record FROM events WHERE confirmed_ms>=? AND confirmed_ms<? ORDER BY confirmed_ms,event_id',
                                 (confirmed-90*86400000,confirmed)).fetchall()
        result=[]
        for row in rows:
            value=json.loads(row[0])
            if value['measurement_minutes']==minutes and value.get('decision_committed_at_ms') is not None and value['decision_committed_at_ms']<=available_cutoff:
                result.append(value)
        return result

    def unavailable_prior_parents(self,minutes,confirmed,cutoff):
        with self.lock:
            rows=self.db.execute('SELECT record FROM events WHERE confirmed_ms>=? AND confirmed_ms<?',(confirmed-90*86400000,confirmed)).fetchall()
        return sum(1 for row in rows for value in [json.loads(row[0])]
                   if value['measurement_minutes']==minutes and
                   (value.get('decision_committed_at_ms') is None or value['decision_committed_at_ms']>cutoff))

    def recent_events(self,limit=50):
        with self.lock:
            rows=self.db.execute('SELECT record,reference FROM events ORDER BY confirmed_ms DESC,event_id LIMIT ?',(limit,)).fetchall()
        return [(json.loads(r[0]),json.loads(r[1]) if r[1] else None) for r in rows]

    def pending_events(self,now):
        with self.lock:
            rows=self.db.execute('SELECT record FROM events WHERE reference IS NULL AND confirmed_ms<=? ORDER BY confirmed_ms LIMIT 200',(now-60000,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def counts(self):
        with self.lock:actual=dict(self.db.execute('SELECT name,value FROM counters'))
        return {k:actual.get(k,0) for k in COUNTERS+('gap_count','revision_count','duplicate_bars')}

    def counter(self,name):
        with self.lock:
            row=self.db.execute('SELECT value FROM counters WHERE name=?',(name,)).fetchone()
        return row[0] if row else 0

    def _error(self,code,at):
        self.db.execute('INSERT INTO errors VALUES(?,?,1) ON CONFLICT(code) DO UPDATE SET at_ms=excluded.at_ms,count=count+1',(code,int(at)))

    def error(self,code,at):
        with self.lock,self.db:self._error(code,at);self._append('HEALTH_ERROR',dict(code=code,at_ms=at))

    def errors(self):
        with self.lock:return [dict(code=c,at_ms=t,count=n) for c,t,n in self.db.execute('SELECT code,at_ms,count FROM errors ORDER BY at_ms DESC LIMIT 20')]

    def gap(self,symbol,start,end):
        with self.lock,self.db:
            exists=self.db.execute('SELECT 1 FROM gaps WHERE symbol=? AND start_ms=?',(symbol,start)).fetchone()
            if exists:
                self.db.execute('UPDATE gaps SET end_ms=MAX(end_ms,?) WHERE symbol=? AND start_ms=? AND resolved=0',(end,symbol,start))
            if not exists:
                self.db.execute('INSERT INTO gaps VALUES(?,?,?,0)',(symbol,start,end));self._inc('gap_count')
                self._append('SOURCE_GAP',dict(symbol=symbol,start_ms=start,end_ms=end));self._error('SOURCE_GAP',int(time.time()*1000))

    def resolve_gaps(self,symbol,through):
        with self.lock,self.db:self.db.execute('UPDATE gaps SET resolved=1 WHERE symbol=? AND end_ms<=?',(symbol,through))

    def unresolved_gaps(self):
        with self.lock:return [dict(symbol=s,start_ms=a,end_ms=b) for s,a,b in self.db.execute('SELECT symbol,start_ms,end_ms FROM gaps WHERE resolved=0 ORDER BY start_ms LIMIT 100')]

    def symbol_counts(self,symbol):
        with self.lock:
            count=self.counter('closed_bars:'+symbol)
            row=self.db.execute('SELECT time_ms FROM bars WHERE symbol=? ORDER BY time_ms DESC LIMIT 1',(symbol,)).fetchone()
        return dict(closed_bars=count,last_closed_bar_ms=row[0] if row else None)

    def verify_journal(self):
        previous='0'*64;expected=1
        for seq,kind,record,prior,actual in self.db.execute('SELECT seq,kind,record,previous_sha,record_sha FROM journal ORDER BY seq'):
            if seq!=expected or prior!=previous:return False
            hashed=hashlib.sha256((str(seq)+'|'+kind+'|'+prior+'|'+record).encode()).hexdigest()
            if hashed!=actual:return False
            previous=actual;expected+=1
        return True

    def budget(self):
        used=sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
        free=shutil.disk_usage(self.directory).free
        return dict(used_bytes=used,limit_bytes=self.max_bytes,free_bytes=free,min_free_bytes=self.min_free_bytes,
                    ok=used<self.max_bytes and free>=self.min_free_bytes)

    def close(self):
        with self.lock:
            self.db.close();self.writer_lock.close()
