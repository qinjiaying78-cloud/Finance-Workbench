"""Validation and stock deltas for returns linked to posted original documents."""
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import HTTPException


def return_rows(conn, book):
    return conn.execute("""SELECT a.payload_json,v.status FROM app_records a LEFT JOIN vouchers v
        ON v.account_set_id=a.account_set_id AND v.legacy_id=json_extract(a.payload_json,'$.voucherId')
        WHERE a.account_set_id=? AND a.store_name='Workpapers' AND a.record_id LIKE 'fw-return:%'""", (book,)).fetchall()


def active_returns(conn, book):
    return [(json.loads(row['payload_json']), row['status']) for row in return_rows(conn, book)
            if row['status'] not in (None, 'VOIDED')]


def stock_adjustments(conn, book, account, sku, warehouse, day):
    qty = value = 0
    for row, status in active_returns(conn, book):
        if (row['inventoryAccount'],row['sku'].upper(),row['warehouse']) != (account,sku.upper(),warehouse):
            continue
        if status != 'POSTED' or row['date'] > day:
            raise HTTPException(409, '该仓库商品已有待过账或更晚的退货单')
        sign = 1 if row['kind']=='SALE_RETURN' else -1
        qty += sign * row['qtyMilli']
        value += sign * row['stockCostCents']
    return qty, value


def proportional(total, before, count, original):
    rounded = lambda n: (2 * total * n + original) // (2 * original)
    return rounded(before + count) - rounded(before)


def validate_return(conn, book, row, models, stock_check):
    source = conn.execute('''SELECT d.*,v.status FROM business_documents d JOIN vouchers v
        ON v.account_set_id=d.account_set_id AND v.legacy_id=d.voucher_legacy_id
        WHERE d.account_set_id=? AND d.legacy_id=?''', (book, row.get('sourceDocId'))).fetchone()
    model = next((item for item in models if item.legacy_id==row.get('voucherId')), None)
    if not source or source['status']!='POSTED' or source['doc_type'] not in {'SALE','PURCHASE'} or not model:
        raise HTTPException(409, '退货须引用已记账的原购销单和本次退货凭证')
    sale = source['doc_type']=='SALE'
    account = source['inventory_account_code']
    party = source['partner_name']
    kind = 'SALE_RETURN' if sale else 'PURCHASE_RETURN'
    qty = row.get('qtyMilli')
    day = row.get('date','')
    try:
        date.fromisoformat(day)
    except (ValueError, TypeError):
        raise HTTPException(422, '退货日期无效')
    earlier = active_returns(conn, book)
    before = sum(r['qtyMilli'] for r, _ in earlier if r['sourceDocId']==source['legacy_id'])
    original = source['quantity_milli']
    if type(qty) is not int or qty <= 0 or before + qty > original or day < source['doc_date']:
        raise HTTPException(409, '退货数量超过原单剩余数量，或日期早于原业务')
    if row.get('qty') != qty/1000 or model.voucher_date.isoformat()!=day or row.get('period')!=day[:7] or model.source!=row.get('docNo'):
        raise HTTPException(422, '退货与凭证日期、数量或单号不一致')
    if not str(row.get('reason','')).strip() or (source['invoice_legacy_id'] or source['tax_cents']) and not str(row.get('creditNote','')).strip():
        raise HTTPException(422, '请填写退货原因及适用的红字发票或税额调整依据')
    net = proportional(source['net_cents'], before, qty, original)
    tax = proportional(source['tax_cents'], before, qty, original)
    cost = proportional(source['cost_cents'] if sale else source['stock_cost_cents'], before, qty, original)
    if any(row.get(k)!=v for k,v in dict(kind=kind,inventoryAccount=account,sku=source['sku'],warehouse=source['warehouse'],party=party,netCents=net,taxCents=tax,grossCents=net+tax,stockCostCents=cost).items()):
        raise HTTPException(422, '退货的商品、仓库、往来单位或金额与原单不符')
    if any(old['docNo']==row['docNo'] or row.get('creditNote') and old.get('creditNote')==row['creditNote'] for old,_ in earlier):
        raise HTTPException(409, '退货单号或红字发票依据已使用')
    stock_check(conn,book,day,source['sku'],account,source['warehouse'],qty,cost,row['id'],kind)
    expected = ({('6001',''):net,('2221',''):tax,('1122',party):-net-tax,(account,''):cost,('6401',''):-cost}
                if sale else {('2202',party):net+tax,(account,''):-cost,('2221',''):-tax if source['deductible'] else 0})
    actual = defaultdict(int)
    for entry in model.entries:
        aux = getattr(entry,'aux_name',None) or ''
        if entry.account_code in ('1122','2202') and entry.contact_id:
            contact=conn.execute('SELECT name FROM contacts WHERE account_set_id=? AND id=?',(book,entry.contact_id)).fetchone()
            aux=contact['name'] if contact else aux
        actual[(entry.account_code,aux)] += int((entry.debit_amount-entry.credit_amount)*100)
    if {k:v for k,v in actual.items() if v}!={k:v for k,v in expected.items() if v}:
        raise HTTPException(422, '退货分录与原单计算不一致')


def validate_auxiliary_voucher(conn, book, row, models):
    model = next((item for item in models if item.legacy_id==row.get('voucherId')), None)
    if not model or model.voucher_date.isoformat()!=row.get('date') or model.voucher_date.isoformat()[:7]!=row.get('period') or len(model.entries)!=2:
        raise HTTPException(422, '业务底稿与关联凭证不符')
    actual = defaultdict(int)
    for entry in model.entries:
        aux = entry.aux_name or ''
        if entry.contact_id and entry.account_code in ('1122','2202'):
            contact=conn.execute('SELECT name FROM contacts WHERE account_set_id=? AND id=?',(book,entry.contact_id)).fetchone()
            aux=contact['name'] if contact else aux
        actual[(entry.account_code,aux)] += int((entry.debit_amount-entry.credit_amount)*100)
    if row['id'].startswith('fw-refund:'):
        returned = next((item for item,status in active_returns(conn,book) if item['id']==row.get('returnId') and status=='POSTED'), None)
        amount = row.get('amount')
        if not returned or type(amount) is not int or amount<=0 or row['date']<returned['date'] or row.get('party')!=returned['party'] or not row.get('bank') or not row.get('ref'):
            raise HTTPException(422,'退款须关联已过账退货及银行回单')
        refunds=conn.execute("""SELECT COALESCE(SUM(json_extract(a.payload_json,'$.amount')),0) FROM app_records a JOIN vouchers v
            ON v.account_set_id=a.account_set_id AND v.legacy_id=json_extract(a.payload_json,'$.voucherId')
            WHERE a.account_set_id=? AND a.record_id LIKE 'fw-refund:%' AND json_extract(a.payload_json,'$.returnId')=? AND v.status<>'VOIDED'""", (book,returned['id'])).fetchone()[0]
        if refunds+amount>returned['grossCents']:
            raise HTTPException(409,'累计退款超过退货金额')
        sale=returned['kind']=='SALE_RETURN'
        signed=amount if sale else -amount
        subject='1122' if sale else '2202'
        balance=contact_balance(conn,book,subject,returned['party'],model.voucher_date)
        pending=conn.execute("""SELECT COALESCE(SUM(json_extract(a.payload_json,'$.amount')),0) FROM app_records a JOIN vouchers v
            ON v.account_set_id=a.account_set_id AND v.legacy_id=json_extract(a.payload_json,'$.voucherId')
            WHERE a.account_set_id=? AND a.record_id LIKE 'fw-refund:%' AND json_extract(a.payload_json,'$.subject')=?
            AND json_extract(a.payload_json,'$.party')=? AND v.status IN ('PENDING','DRAFT')""", (book,subject,returned['party'])).fetchone()[0]
        if amount>max(0,(-balance if sale else balance)-pending) or row.get('subject')!=subject:
            raise HTTPException(409,'退款金额超过往来反向余额')
        expected={(subject,returned['party']):signed,('1002',row['bank']):-signed}
    else:
        from calendar import monthrange
        code,aux=row.get('code'),row.get('aux')
        already=conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id LIKE 'fw-fx:%' AND json_extract(payload_json,'$.period')=? AND json_extract(payload_json,'$.code')=? AND json_extract(payload_json,'$.aux')=?", (book,row.get('period'),code,aux)).fetchone()
        if already:
            raise HTTPException(409,'本期间该项目已登记调汇')
        carrying=row.get('carrying')
        try:
            foreign,rate=Decimal(row['foreign']),Decimal(row['rate'])
            if not foreign.is_finite() or not rate.is_finite() or foreign<=0 or rate<=0 or type(carrying) is not int or not carrying:
                raise ValueError
            amount=int((foreign*rate*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            translated=amount if carrying>0 else -amount
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise HTTPException(422,'原币、汇率或账面金额无效')
        difference=translated-carrying
        day=model.voucher_date
        if code not in ('1002','1122','2202') or not aux or not row.get('basis') or not difference or row.get('difference')!=difference or day.day!=monthrange(day.year,day.month)[1] or model.source_type!='期末调汇':
            raise HTTPException(422,'汇率计算、期间或调汇项目不一致')
        if code in ('1122','2202'):
            live=contact_balance(conn,book,code,aux,day)
        else:
            opening=conn.execute("SELECT initial_debit-initial_credit FROM accounts WHERE account_set_id=? AND code='1002'", (book,)).fetchone()
            if opening and opening[0]:
                raise HTTPException(409,'银行期初缺少账户分配，不能调汇')
            live=conn.execute("""SELECT COALESCE(SUM(e.debit_amount-e.credit_amount),0) FROM voucher_entries e JOIN vouchers v ON v.account_set_id=e.account_set_id AND v.id=e.voucher_id
                WHERE e.account_set_id=? AND e.account_code='1002' AND e.aux_name=? AND v.status IN ('POSTED','REVERSED') AND v.voucher_date<=?""", (book,aux,day.isoformat())).fetchone()[0]
        if live!=carrying:
            raise HTTPException(409,'人民币账面余额已变化，请刷新重算调汇')
        expected={(code,aux):difference,('6603',''):-difference}
    if dict(actual)!=expected:
        raise HTTPException(422,'业务金额与凭证分录不一致')


def contact_balance(conn, book, code, party, day):
    from .reports import contact_sub_ledger
    contact=conn.execute('SELECT id FROM contacts WHERE account_set_id=? AND name=? AND type=?', (book,party,'CUSTOMER' if code=='1122' else 'SUPPLIER')).fetchone()
    if not contact:
        raise HTTPException(422,'往来单位不存在')
    result=contact_sub_ledger(code,contact['id'],end_date=day,account_set_id=book,conn=conn)
    sign=-1 if result['ending_balance_side']=='贷' else 1
    return sign*int(Decimal(result['ending_balance'])*100)
