"""Approval rules for the first Toorak invoice in each mail pilot release."""

from revenue.school_invoice_reminders import EMAIL


REHEARSAL_RECIPIENT = "zhang0723@gmail.com"
SENDER = "accounts@aaie.com.hk"
FINANCE_COPY_ADDRESS = "accounts@aaie.com.hk"


def payer_mail_addresses(raw, *, simulation):
    if not isinstance(raw, str) or len(raw) > 2048 or any(c in raw for c in "\r\n"):
        raise ValueError("请输入有效的收票邮箱，用逗号分隔。")
    addresses = []
    seen = set()
    for part in raw.split(","):
        address = part.strip()
        lower = address.casefold()
        if (len(address) > 254 or not EMAIL.fullmatch(address)
                or lower.endswith(".test") != simulation
                or lower in {FINANCE_COPY_ADDRESS, REHEARSAL_RECIPIENT.casefold()}):
            raise ValueError("收票邮箱不符合当前发信配置，请逐个核对。")
        if lower not in seen:
            addresses.append(address)
            seen.add(lower)
    return addresses


def payer_copy_addresses(recipient, cc, bcc, *, simulation):
    """The finance copy is mandatory. Bcc is used only in the SMTP envelope."""
    seen = {FINANCE_COPY_ADDRESS, *map(str.casefold, payer_mail_addresses(recipient, simulation=simulation))}
    result = []
    for raw in (cc, bcc):
        addresses = []
        for address in payer_mail_addresses(raw, simulation=simulation) if raw else ():
            if address.casefold() not in seen:
                addresses.append(address)
                seen.add(address.casefold())
        result.append(addresses)
    return tuple(result)


def release_values(current, stage, recipient, evidence, approval, claims, simulation):
    mode = "candidate" if simulation else "production"
    values = dict(transport=mode, stage=stage, enabled=False, verified=False, recipient="",
                  delivery_recipient="", evidence="", approval="", claims=[])
    if stage == "off":
        return values
    if stage not in {"verified","first"}:
        raise ValueError("请选择有效的邮件试点阶段。")
    recipient, evidence, approval = (str(value or "").strip() for value in (recipient,evidence,approval))
    if (len(recipient)>254 or not EMAIL.fullmatch(recipient) or not 1<=len(evidence)<=200
            or (not simulation and (recipient.lower() in {REHEARSAL_RECIPIENT, FINANCE_COPY_ADDRESS}
                                     or recipient.lower().endswith(".test")))):
        raise ValueError("请核对学校财务邮箱，并填写可追溯的核对依据。")
    values.update(verified=True,recipient=recipient,evidence=evidence,
                  delivery_recipient=REHEARSAL_RECIPIENT if simulation else recipient)
    if stage == "first":
        if (current.get("transport")!=mode or current.get("stage")!="verified"
                or current.get("recipient")!=recipient or current.get("evidence")!=evidence
                or not 1<=len(approval)<=200 or approval==evidence
                or not 1<=len(claims)<=100 or len(set(claims))!=len(claims)
                or any(not isinstance(uid,str) or not uid.startswith("claim-") for uid in claims)):
            raise ValueError("请先保存已核对的邮箱，再填写单独批准记录并选择一张发票的申领。")
        values.update(enabled=True,approval=approval,claims=sorted(claims))
    return values
