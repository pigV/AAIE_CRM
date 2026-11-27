"""Audited historical collection review; never reprice using today's fee rules."""
from datetime import date
import json
import sqlite3
from contextlib import contextmanager
from revenue.repository import new_uid, canonical_json
from revenue.services import RevenueService
from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.client_payments import CLIENT_PAYMENT_METHODS
from revenue.access_policy import HISTORICAL_RECEIPT_MANAGER_ROLES
TABLE = 'revenue_historical_receipts'
KINDS = {'client_service': '院校申请服务费', 'student_visa': '独立学生签证服务费'}

def available(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None

def reviews(c, cycle_id=None, student_id=None):
    if not available(c):
        return []
    where = []
    args = []
    if cycle_id is not None:
        where.append('application_cycle_id=?')
        args.append(cycle_id)
    if student_id:
        where.append('student_id=?')
        args.append(student_id)
    return [dict(r) for r in c.execute('SELECT * FROM ' + TABLE + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC', args)]

def fee_kind(milestone):
    from revenue.visa_fee_policy import VISA_FEE_KINDS
    if milestone in VISA_FEE_KINDS:
        return 'student_visa' if VISA_FEE_KINDS[milestone] == 'student_visa' else None
    return 'client_service'

def blocks_repricing(c, cycle_id, kind, visa_case_id=None):
    if kind is None:
        return False
    for record in reviews(c, cycle_id):
        if record['service_kind'] != kind or record['status'] == 'cancelled':
            continue
        if kind != 'student_visa' or visa_case_id is None or record['visa_case_id'] is None or record['visa_case_id'] == visa_case_id:
            return True
    return False

def can_view_evidence(c, document_uid):
    if not available(c):
        return False
    rows = c.execute('SELECT h.student_id FROM revenue_historical_receipt_evidence e JOIN revenue_historical_receipts h ON h.id=e.historical_receipt_id JOIN revenue_documents d ON d.id=e.document_id WHERE d.uid=?', (document_uid,)).fetchall()
    return bool(rows) and all(RevenueOperationalVisibility(c).is_student_visible(r[0]) for r in rows)

def summary(r):
    if r['status'] == 'cancelled':
        return '已取消核对'
    if r['status'] == 'draft':
        return '已确认收款 · 资料待补（待核实）' if r['claimed_received'] else '收款待核实'
    if r['agreed_aud_cents'] is None or r['settled_aud_cents'] is None:
        return '已收款 · AUD 待核对'
    return '历史已收清' if r['settled_aud_cents'] >= r['agreed_aud_cents'] else '历史部分收款'

class HistoricalReceiptService:

    def __init__(self, repository, actor_user_id, actor_role, *, actor_has_revenue_access=False):
        self.repo = repository
        self.c = repository.connection
        self.actor = int(actor_user_id)
        self.role = actor_role
        self.actor_has_revenue_access = actor_has_revenue_access is True

    def _authorize(self, *, cancel=False):
        manager = self.role in HISTORICAL_RECEIPT_MANAGER_ROLES and self.actor_has_revenue_access and not cancel
        if self.role != 'Admin' and not manager:
            raise PermissionError('历史收款须由管理员或已授权经理核对')
        if not self.c.execute('SELECT 1 FROM users WHERE id=?', (self.actor,)).fetchone():
            raise PermissionError('操作人不存在')
        cols = {r[1] for r in self.c.execute('PRAGMA table_info(users)')}
        if {'role_id', 'is_active'} <= cols:
            not_deleted = " AND u.deleted_at IS NULL" if 'deleted_at' in cols else ''
            row = self.c.execute("SELECT r.name FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=? AND u.is_active=1" + not_deleted, (self.actor,)).fetchone()
            if row and row[0] != self.role:
                row = None
            if not row:
                raise PermissionError('操作人权限已变化')
        if manager:
            grant = self.c.execute("SELECT 1 FROM user_module_permissions WHERE user_id=? AND module_key='revenue_commission' AND can_access=1", (self.actor,)).fetchone()
            if not grant:
                raise PermissionError('收入与佣金权限已撤销')

    def _cycle(self, sid, cid):
        cy = self.c.execute('SELECT * FROM application_cycles WHERE id=?', (cid,)).fetchone()
        if not cy or cy['student_id'] != sid or (not RevenueOperationalVisibility(self.c).is_student_visible(sid)):
            raise ValueError('学生与周期不一致或已不可见')
        return cy

    def _row(self, uid):
        row = self.c.execute('SELECT * FROM ' + TABLE + ' WHERE uid=?', (uid,)).fetchone()
        if row is None:
            raise ValueError('历史核对记录不存在')
        self._cycle(row['student_id'], row['application_cycle_id'])
        return dict(row)

    @contextmanager
    def _transaction(self):
        if self.c.in_transaction:
            with self.repo.transaction():
                yield
        else:
            self.c.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.c.rollback()
                raise
            else:
                self.c.commit()

    @staticmethod
    def _integer(v, name, zero=False):
        if v is None or v == '':
            return None
        if isinstance(v, bool) or not isinstance(v, int) or v < (0 if zero else 1) or (v > 10 ** 12):
            raise ValueError(name + '金额无效')
        return v

    def _validate(self, data):
        sid = str(data.get('student_id') or '').strip()
        try:
            cid = int(data['application_cycle_id'])
        except (KeyError, ValueError, TypeError):
            raise ValueError('请选择申请周期')
        self._cycle(sid, cid)
        kind = data.get('service_kind')
        if kind not in KINDS:
            raise ValueError('请选择收费类别')
        note = str(data.get('source_note') or '').strip()
        if not 10 <= len(note) <= 2000:
            raise ValueError('核对来源说明须为10至2000字')
        state = data.get('contract_state')
        if state not in {'documented', 'historical_missing', 'not_required'}:
            raise ValueError('请选择历史合同情况')
        claimed = data.get('claimed_received')
        if claimed not in (True, False, 0, 1):
            raise ValueError('请选择收款确认状态')
        currency = data.get('currency_code') or None
        if currency not in (None, 'AUD', 'CNY'):
            raise ValueError('仅支持AUD或CNY原币')
        paid = str(data.get('received_date') or '').strip() or None
        if paid:
            try:
                valid = date.fromisoformat(paid)
            except ValueError:
                raise ValueError('实际收款日期无效')
            if valid > date.fromisoformat(self.repo.now()[:10]):
                raise ValueError('收款日期不能在未来')
        visa = data.get('visa_case_id') or None
        if visa is not None:
            try:
                visa = int(visa)
            except (TypeError, ValueError):
                raise ValueError('签证案件无效')
            row = self.c.execute('SELECT * FROM visa_cases WHERE id=?', (visa,)).fetchone()
            if kind != 'student_visa' or not row or row['application_cycle_id'] != cid or ('student_id' in row.keys() and row['student_id'] != sid):
                raise ValueError('签证案件归属不一致')
        return dict(student_id=sid, application_cycle_id=cid, service_kind=kind, visa_case_id=visa, claimed_received=int(claimed), agreed_aud_cents=self._integer(data.get('agreed_aud_cents'), '历史议定', True), actual_cents=self._integer(data.get('actual_cents'), '实际到账'), currency_code=currency, settled_aud_cents=self._integer(data.get('settled_aud_cents'), '已结算AUD', True), received_date=paid, method_code=data.get('method_code') or None, contract_state=state, source_note=note)

    def save(self, data, key):
        self._authorize()
        payload = self._validate(data)
        try:
            expected = int(data.get('expected_revision', 0))
        except (TypeError, ValueError):
            raise ValueError('核对版本无效')
        request = {**payload, 'expected_revision': expected, 'review_uid': str(data.get('review_uid') or '')}

        def operation():
            prior = self.c.execute('SELECT * FROM ' + TABLE + ' WHERE application_cycle_id=? AND service_kind=?', (payload['application_cycle_id'], payload['service_kind'])).fetchone()
            if prior and (prior['status'] == 'confirmed' or prior['revision'] != expected or request['review_uid'] != prior['uid']):
                raise ValueError('记录已确认或版本变化，请刷新')
            if not prior and expected != 0:
                raise ValueError('核对版本已变化')
            now = self.repo.now()
            fields = list(payload)
            if prior:
                self.c.execute('UPDATE ' + TABLE + ' SET ' + ','.join((f + '=?' for f in fields)) + ",status='draft',revision=revision+1,updated_by=?,updated_at=? WHERE id=?", [*payload.values(), self.actor, now, prior['id']])
                uid = prior['uid']
            else:
                uid = new_uid('historical')
                self.c.execute('INSERT INTO ' + TABLE + '(uid,' + ','.join(fields) + ',status,revision,created_by,created_at,updated_by,updated_at) VALUES(' + ','.join(('?' for _ in range(len(fields) + 7))) + ')', [uid, *payload.values(), 'draft', 1, self.actor, now, self.actor, now])
            return self._row(uid)
        with self._transaction():
            return self.repo.command(idempotency_key=key, command_type='historical_collection_review_saved', aggregate_type='historical_collection', payload=request, actor_user_id=self.actor, operation=operation)

    def _no_overlap(self, row):
        from revenue.visa_fee_policy import signed_student_visa_package, fully_executed_contracts
        case = self.c.execute('SELECT * FROM revenue_commercial_cases WHERE application_cycle_id=?', (row['application_cycle_id'],)).fetchone()
        if case and case['student_id'] != row['student_id']:
            raise ValueError('财务档案的学生归属不一致')
        if case:
            fees = self.c.execute("SELECT f.milestone_code,s.canonical_json FROM revenue_fee_items f JOIN revenue_rule_snapshots s ON s.id=f.rule_snapshot_id WHERE f.commercial_case_id=? AND f.status<>'cancelled'", (case['id'],))
            for fee in fees:
                if fee_kind(fee[0]) != row['service_kind']:
                    continue
                if row['service_kind'] == 'student_visa':
                    try:
                        items = [item for item in json.loads(fee[1])['items'] if item.get('milestone_code') == fee[0]]
                        identities = {item.get('visa_case_id') for item in items}
                    except (TypeError, ValueError, KeyError):
                        raise ValueError('已有签证收费归属待核实')
                    if identities and None not in identities and row['visa_case_id'] not in identities:
                        continue
                raise ValueError('已有同项正式收费或套餐记录，请先对账，不能重复登记历史收款')
        if row['service_kind'] == 'student_visa':
            contracts = self.c.execute('SELECT * FROM contracts WHERE student_id=? AND application_cycle_id=?', (row['student_id'], row['application_cycle_id'])).fetchall()
            if len(fully_executed_contracts(contracts)) > 1:
                raise ValueError('多份有效已签合同的签证覆盖尚待核实')
            if signed_student_visa_package(contracts, row['student_id']):
                raise ValueError('已有签证套餐覆盖，请先核对，不能重复登记900')

    def confirm(self, uid, revision, key, *, acknowledged=False):
        self._authorize()
        if acknowledged is not True:
            raise ValueError('请明确确认历史金额、币种、日期及核对来源')

        def operation():
            row = self._row(uid)
            if row['status'] != 'draft' or row['revision'] != int(revision):
                raise ValueError('核对版本已变化或已确认')
            cy = self._cycle(row['student_id'], row['application_cycle_id'])
            self._validate(row)
            if not row['claimed_received'] or not row['actual_cents'] or (not row['currency_code']) or (not row['received_date']):
                raise ValueError('金额、币种、日期或收款确认尚不完整，不能正式登记')
            method = CLIENT_PAYMENT_METHODS.get(row['method_code'])
            if not method or method[0] != row['currency_code']:
                raise ValueError('付款方式与原币不一致')
            if row['service_kind'] == 'student_visa':
                from revenue.visa_fee_policy import visa_subclass
                visa = self.c.execute('SELECT * FROM visa_cases WHERE id=?', (row['visa_case_id'],)).fetchone() if row['visa_case_id'] is not None else None
                if not visa or visa_subclass(dict(visa)) != '500':
                    raise ValueError('请核实独立500学生签证案件，不能关联陪读或未知签证类别')
                if row['contract_state'] == 'not_required' and cy['education_stream'] != 'higher_education':
                    raise ValueError('大学免申请服务费的教育类别尚待核实')
            self._no_overlap(row)
            if row['settled_aud_cents'] is not None and row['agreed_aud_cents'] is None:
                raise ValueError('AUD分配缺少原议定金额依据')
            if row['agreed_aud_cents'] is not None and row['settled_aud_cents'] is not None and (row['settled_aud_cents'] > row['agreed_aud_cents']):
                raise ValueError('AUD结算超过历史议定金额')
            if row['currency_code'] == 'AUD' and row['settled_aud_cents'] is not None and (row['settled_aud_cents'] > row['actual_cents']):
                raise ValueError('AUD结算超过实际到账')
            if row['currency_code'] == 'CNY' and row['settled_aud_cents'] is not None:
                raise ValueError('人民币历史到账本轮不做AUD分配，请单独核实换算依据')
            now = self.repo.now()
            service = RevenueService(self.repo)
            existing = self.c.execute('SELECT * FROM revenue_commercial_cases WHERE application_cycle_id=?', (row['application_cycle_id'],)).fetchone()
            case = dict(existing) if existing else service.open_case(row['application_cycle_id'], self.actor, 'historical-case:' + str(row['application_cycle_id']))
            if not existing and cy['status'] != 'active':
                self.c.execute("UPDATE revenue_commercial_cases SET status='closed',closed_at=? WHERE id=?", (now, case['id']))
            method_row = self.c.execute('SELECT id FROM revenue_payment_methods WHERE method_code=?', (row['method_code'],)).fetchone()
            payment_uid = new_uid('historical-payment')
            self.c.execute("INSERT INTO revenue_payments(uid,payment_direction,counterparty_kind,payment_method_id,external_reference,received_date,amount_cents,allocated_cents,currency_code,status,confirmed_by,confirmed_at) VALUES(?,'received','client',?,?,?,?,0,?,'confirmed',?,?)", (payment_uid, method_row[0] if method_row else None, row['uid'], row['received_date'], row['actual_cents'], row['currency_code'], self.actor, now))
            payment = self.c.execute('SELECT id FROM revenue_payments WHERE uid=?', (payment_uid,)).fetchone()[0]
            for account, debit, credit in [('Bank', row['actual_cents'], 0), ('HistoricalClientFunds', 0, row['actual_cents'])]:
                self.c.execute("INSERT INTO revenue_ledger_entries(uid,account_code,event_type,source_kind,source_uid,commercial_case_id,debit_cents,credit_cents,currency_code,occurred_at,created_by) VALUES(?,?,'received','historical_client_payment',?,?,?,?,?,?,?)", (new_uid('ledger'), account, payment_uid, case['id'], debit, credit, row['currency_code'], row['received_date'], self.actor))
            self.c.execute('UPDATE ' + TABLE + " SET status='confirmed',revision=revision+1,payment_id=?,commercial_case_id=?,updated_by=?,updated_at=? WHERE id=?", (payment, case['id'], self.actor, now, row['id']))
            return self._row(uid)
        with self._transaction():
            return self.repo.command(idempotency_key=key, command_type='historical_collection_confirmed', aggregate_type='historical_collection', payload={'uid': uid, 'revision': int(revision), 'acknowledged': True}, actor_user_id=self.actor, operation=operation)

    def cancel(self, uid, revision, key):
        self._authorize(cancel=True)

        def operation():
            row = self._row(uid)
            if row['status'] != 'draft' or row['revision'] != int(revision):
                raise ValueError('只能取消当前待核实记录')
            self.c.execute('UPDATE ' + TABLE + " SET status='cancelled',revision=revision+1,updated_by=?,updated_at=? WHERE id=?", (self.actor, self.repo.now(), row['id']))
            return self._row(uid)
        with self._transaction():
            return self.repo.command(idempotency_key=key, command_type='historical_collection_cancelled', aggregate_type='historical_collection', payload={'uid': uid, 'revision': int(revision)}, actor_user_id=self.actor, operation=operation)

    def attach(self, uid, document_uid, kind, key):
        self._authorize()
        if kind not in {'payment_evidence', 'external_receipt'}:
            raise ValueError('凭证类型无效')

        def operation():
            row = self._row(uid)
            if row['status'] == 'cancelled':
                raise ValueError('已取消核对不能补传凭证，请先重新核对')
            doc = self.repo.require_by_uid('revenue_documents', document_uid)
            if doc['document_kind'] != 'historical_' + kind or doc['mime_type'] not in {'application/pdf', 'image/png', 'image/jpeg'}:
                raise ValueError('只能关联本入口登记的历史凭证')
            prior = self.c.execute('SELECT h.student_id FROM revenue_historical_receipt_evidence e JOIN ' + TABLE + ' h ON h.id=e.historical_receipt_id WHERE e.document_id=?', (doc['id'],)).fetchall()
            if any((p[0] != row['student_id'] for p in prior)):
                raise ValueError('凭证属于其他学生')
            self.c.execute('INSERT INTO revenue_historical_receipt_evidence(uid,historical_receipt_id,document_id,evidence_kind,created_by,created_at) VALUES(?,?,?,?,?,?)', (new_uid('historical-evidence'), row['id'], doc['id'], kind, self.actor, self.repo.now()))
            return {'uid': uid, 'document_uid': document_uid, 'kind': kind}
        with self._transaction():
            return self.repo.command(idempotency_key=key, command_type='historical_collection_evidence_added', aggregate_type='historical_collection', payload={'uid': uid, 'document_uid': document_uid, 'kind': kind}, actor_user_id=self.actor, operation=operation)
