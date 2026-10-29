"""Transfer-only pricing and signing preconditions from canonical source evidence."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from . import transfer_source_authority as source

TRANSFER = 'TransferApplicationContract'

def money(value):
    try:
        result = Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: invalid contract amount')
    if not result.is_finite() or result < 0:
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: invalid contract amount')
    return result

def commercials(connection, student_id, contract_id, *, payment_method, discount='0', source_authority=None):
    authority = source_authority if source_authority is not None else source.resolve_for_contract(connection, student_id, contract_id)
    classification = authority['source_classification']
    if classification not in ('aaie_existing', 'new_transfer'):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: unresolved source')
    from revenue.transfer_pricing import resolve_transfer_base_fee
    from revenue.client_fee_rules import MissingFeeRule, AmbiguousFeeRule
    # The saved source timestamp is stable across reopen/retry; no 'today' drift.
    try:
        rule = resolve_transfer_base_fee(connection, student_id=student_id,
            transfer_contract_id=contract_id, source_engagement_date=str(authority['created_at'])[:10], source_authority=authority)
    except (MissingFeeRule, AmbiguousFeeRule) as exc:
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: active Transfer fee rule requires resolution') from exc
    base = Decimal(rule.net_cents) / 100
    reduction = money(discount or '0')
    if reduction > base:
        raise source.SourceAuthorityError('Contract discount exceeds verified base fee')
    net = base - reduction
    gst = (net * Decimal('.10')).quantize(Decimal('.01')) if payment_method == '澳洲公户' else Decimal('0')
    return {'authority': authority, 'fee_rule':rule.snapshot(), 'base_fee':format(base,'.2f'),
            'discount':format(reduction,'.2f'), 'gst':format(gst,'.2f'),
            'final_fee':format(net+gst,'.2f')}

def trusted_binding(authority):
    return {**{key:authority[key] for key in ('transfer_contract_id','student_id','source_classification','rule_version')},
            'authority_id':authority['id']}

def require_signable(connection, contract, snapshot):
    contract = dict(contract)
    if contract.get('contract_type') != TRANSFER:
        return None
    resolved = source.resolve_for_contract(connection, contract['student_id'], contract['id'])
    proof = snapshot.get('transfer_fee_authority') or {}
    expected = trusted_binding(resolved)
    if any(proof.get(key) != value for key,value in expected.items()):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: regenerate contract from verified source')
    calculated = commercials(connection, contract['student_id'], contract['id'],
        payment_method=contract.get('service_fee_currency'), discount=contract.get('discount_amount'))
    if money(contract.get('consulting_and_visa_service_fee')) != money(calculated['base_fee']):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: contract base fee conflicts with source')
    # Published authority explicitly validates approved GST-free exceptions itself.
    if snapshot.get('generation_authority') == 'PublishedContractTemplateV2':
        from contract_generation_authority import validate_transfer_fee_authority
        try:
            validate_transfer_fee_authority(snapshot)
        except RuntimeError as exc:
            raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: invalid frozen Transfer fee') from exc
        commercial = snapshot['commercial']
        if (contract.get('service_fee_currency') != commercial.get('payment_method')
                or money(contract.get('discount_amount') or '0') != money(commercial.get('discount_amount') or '0')):
            raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: payment or discount differs from frozen contract')
        final = commercial['final_fee_amount']
    else:
        final = calculated['final_fee']
    if money(contract.get('final_fee_amount')) != money(final):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: contract total conflicts with source')
    if not contract.get('contract_file_path'):
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: contract document has not been generated')
    return resolved


def fee_terms(payload):
    """Commercial terms retained when the legacy template replaces section three."""
    base = money(payload.get('consulting_and_visa_service_fee'))
    discount = money(payload.get('discount_amount') or '0')
    total = money(payload.get('final_fee_amount'))
    net = base-discount
    gst = total-net
    if net < 0 or gst < 0:
        raise source.SourceAuthorityError('Transfer commercial amounts are inconsistent')
    return '\n'.join((f'基础服务费（不含 GST）：AUD {base:.2f}',
        f'优惠金额：AUD {discount:.2f}', f'折后服务费（不含 GST）：AUD {net:.2f}',
        f'GST：AUD {gst:.2f}', f'最终合同总额：AUD {total:.2f}',
        '支付方式：'+str(payload.get('service_fee_currency') or '')))


def validate_fee_text(text, contract):
    """A signable Transfer artifact must visibly contain its exact fee amounts."""
    import re
    if dict(contract).get('contract_type') != TRANSFER:
        return
    # Preserve line boundaries so duplicate authoritative rows remain visible.
    normalized = re.sub(r'[,，\t\f\v \u00a0\u3000\r]', '', str(text))
    labels = {
        'consulting_and_visa_service_fee': r'(?:基础服务费|标准服务费)',
        'final_fee_amount': r'(?:最终合同总额|最终应付总额|合同总额|最终费用|最终金额)',
    }
    for key, label in labels.items():
        pattern = (r'(?<![\u3400-\u9fffA-Za-z0-9_])' + label
            + r'(?:[（(][^）)\n]*[）)])?[:：]?\n?(?:AUD|\$)([0-9]+(?:\.[0-9]{1,2})?)(?![0-9.])')
        amounts = re.findall(pattern, normalized, flags=re.IGNORECASE)
        if len(amounts) != 1 or money(amounts[0]) != money(contract[key]):
            raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: generated Transfer contract does not display consistent verified fees')


def validate_pdf_data(data, contract):
    if dict(contract).get('contract_type') != TRANSFER:
        return
    import io
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    except Exception as exc:
        raise source.SourceAuthorityError('SOURCE VERIFICATION HOLD: signed Transfer artifact is not a readable PDF') from exc
    validate_fee_text(text, contract)
