"""At-least-once local projection worker with leases and atomic SQL output.

Callbacks receive current source state on the same writer transaction, not an
old event delta. They must be local, bounded projection updates. External IO,
OCR, mail, invoices and payments keep their separately authorized workflows.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
import json
import time
import uuid

from .commands import TransactionView
from .contracts import Subject
from .schema import require_schema
from .versions import version_of

KEY_WHERE='event_seq=? AND handler_name=? AND handler_version=? AND subject_kind=? AND subject_id=?'


class LeaseLost(RuntimeError):pass


@dataclass(frozen=True)
class Delivery:
    event_seq: int
    handler_name: str
    handler_version: str
    subject: Subject
    event_subject_version: int
    lease_token: str
    attempts: int

    @property
    def key(self):return (self.event_seq,self.handler_name,self.handler_version,self.subject.kind,self.subject.identifier)


def _dicts(cursor):
    keys=[x[0] for x in cursor.description]
    return [dict(zip(keys,row)) for row in cursor.fetchall()]


def _begin(conn):
    if conn.in_transaction:raise ValueError('worker_requires_own_transaction')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise ValueError('foreign_keys_required')
    require_schema(conn)
    conn.execute('BEGIN IMMEDIATE')


def _stamp(now):return datetime.fromtimestamp(now/1000,timezone.utc).isoformat()


def queue_status(conn,*,can_read):
    rows=_dicts(conn.execute('''SELECT event_seq,handler_name,handler_version,subject_kind,subject_id,
        state,attempts,last_error_code,processed_subject_version,updated_at FROM business_change_deliveries
        ORDER BY event_seq,handler_name,subject_kind,subject_id'''))
    return tuple(x for x in rows if can_read(Subject(x['subject_kind'],x['subject_id'])))


class Processor:
    def __init__(self,callbacks,*,clock_ms=None,lease_ms=30000,max_attempts=8):
        if type(lease_ms) is not int or lease_ms<=0 or type(max_attempts) is not int or max_attempts<1:
            raise ValueError('invalid_worker_limits')
        if any(not isinstance(k,tuple) or len(k)!=2 or not callable(v) for k,v in callbacks.items()):
            raise ValueError('invalid_worker_callbacks')
        self.callbacks=dict(callbacks)
        self.clock=clock_ms or (lambda:int(time.time()*1000))
        self.lease_ms=lease_ms;self.max_attempts=max_attempts

    def _dependencies_done(self,conn,encoded):
        dependencies=json.loads(encoded)
        if not isinstance(dependencies,list):raise ValueError('invalid_dependencies')
        for key in dependencies:
            if not isinstance(key,list) or len(key)!=5:raise ValueError('invalid_dependencies')
            row=conn.execute('SELECT state,processed_subject_version FROM business_change_deliveries WHERE '+KEY_WHERE,key).fetchone()
            required=conn.execute('SELECT subject_version FROM business_change_impacts WHERE event_seq=? AND subject_kind=? AND subject_id=?',(key[0],key[3],key[4])).fetchone()
            if not row or not required or row[0] not in ('done','superseded') or (row[1] or 0)<required[0]:return False
            current=version_of(conn,Subject(key[3],key[4]))
            fresh=conn.execute('''SELECT 1 FROM business_change_deliveries WHERE handler_name=? AND handler_version=?
                AND subject_kind=? AND subject_id=? AND state IN ('done','superseded')
                AND processed_subject_version>=? LIMIT 1''',(key[1],key[2],key[3],key[4],current)).fetchone()
            if not fresh:return False
        return True

    def claim(self,conn):
        _begin(conn)
        try:
            now=self.clock()
            rows=_dicts(conn.execute('''SELECT delivery.*,impact.subject_version FROM business_change_deliveries delivery
              JOIN business_change_impacts impact USING(event_seq,subject_kind,subject_id)
              WHERE attempts<? AND ((state IN ('pending','failed') AND next_attempt_ms<=?)
                OR (state='processing' AND lease_until_ms<=?))
              ORDER BY event_seq,handler_name,subject_kind,subject_id''',(self.max_attempts,now,now)))
            for row in rows:
                if (row['handler_name'],row['handler_version']) not in self.callbacks:continue
                if not self._dependencies_done(conn,row['dependency_keys_json']):continue
                subject=Subject(row['subject_kind'],row['subject_id']);token=uuid.uuid4().hex
                key=(row['event_seq'],row['handler_name'],row['handler_version'],subject.kind,subject.identifier)
                conn.execute('''UPDATE business_change_deliveries SET state='processing',attempts=attempts+1,
                    lease_token=?,lease_until_ms=?,updated_at=? WHERE '''+KEY_WHERE,(token,now+self.lease_ms,_stamp(now),*key))
                conn.commit()
                return Delivery(row['event_seq'],row['handler_name'],row['handler_version'],subject,row['subject_version'],token,row['attempts']+1)
            conn.commit();return None
        except BaseException:
            conn.rollback();raise

    def _owned(self,conn,delivery):
        row=conn.execute('SELECT state,lease_token,lease_until_ms FROM business_change_deliveries WHERE '+KEY_WHERE,delivery.key).fetchone()
        if not row or row[0]!='processing' or row[1]!=delivery.lease_token or row[2]<=self.clock():
            raise LeaseLost('delivery_lease_not_owned')

    def _finish(self,conn,delivery,state,revision):
        self._owned(conn,delivery)
        updated=conn.execute('''UPDATE business_change_deliveries SET state=?,lease_token=NULL,lease_until_ms=NULL,
            processed_subject_version=?,last_error_code=NULL,updated_at=? WHERE '''+KEY_WHERE+" AND state='processing' AND lease_token=?",
            (state,revision,_stamp(self.clock()),*delivery.key,delivery.lease_token))
        if updated.rowcount!=1:raise LeaseLost('delivery_lease_changed')

    def _failed(self,conn,delivery):
        _begin(conn)
        try:
            self._owned(conn,delivery)
            now=self.clock();delay=min(60000,1000*2**min(delivery.attempts-1,10))
            conn.execute('''UPDATE business_change_deliveries SET state='failed',lease_token=NULL,lease_until_ms=NULL,
              last_error_code='handler_failed',next_attempt_ms=?,updated_at=? WHERE '''+KEY_WHERE+' AND lease_token=?',
              (now+delay,_stamp(now),*delivery.key,delivery.lease_token))
            conn.commit()
        except BaseException:
            conn.rollback();raise

    def process(self,conn,delivery):
        _begin(conn)
        try:
            self._owned(conn,delivery)
            dependencies=conn.execute('SELECT dependency_keys_json FROM business_change_deliveries WHERE '+KEY_WHERE,delivery.key).fetchone()[0]
            if not self._dependencies_done(conn,dependencies):
                conn.execute('''UPDATE business_change_deliveries SET state='pending',lease_token=NULL,lease_until_ms=NULL,
                    attempts=max(0,attempts-1),updated_at=? WHERE '''+KEY_WHERE+" AND state='processing' AND lease_token=?",
                    (_stamp(self.clock()),*delivery.key,delivery.lease_token))
                conn.commit();return False
            current=version_of(conn,delivery.subject)
            prior=conn.execute('''SELECT 1 FROM business_change_deliveries WHERE handler_name=? AND handler_version=?
                AND subject_kind=? AND subject_id=? AND state IN ('done','superseded')
                AND processed_subject_version>=? LIMIT 1''',
                (delivery.handler_name,delivery.handler_version,delivery.subject.kind,delivery.subject.identifier,current)).fetchone()
            if prior:self._finish(conn,delivery,'superseded',current)
            else:
                # The writer lock fixes the source version throughout recompute
                # and completion. A queued v1 event may therefore produce v2.
                callback=self.callbacks[(delivery.handler_name,delivery.handler_version)]
                callback(TransactionView(conn,'worker'),delivery.subject,current)
                if version_of(conn,delivery.subject)!=current:raise ValueError('projection_changed_source_version')
                self._finish(conn,delivery,'done',current)
            conn.commit();return True
        except LeaseLost:
            conn.rollback();raise
        except Exception:
            conn.rollback();self._failed(conn,delivery);return False
        except BaseException:
            conn.rollback();raise

    def run_one(self,conn):
        delivery=self.claim(conn)
        if delivery is None:return False
        self.process(conn,delivery)
        return True
