"""Shared product and service directory for the school invoice composer."""

import json

from revenue.repository import new_uid


SAVED = "school_invoice_service_saved"
AGGREGATE = "school_invoice_service"


def list_invoice_services(connection):
    rows = connection.execute(
        """SELECT a.after_json FROM revenue_audit_events a
            WHERE a.action=? AND a.aggregate_type=?
              AND a.id=(SELECT max(b.id) FROM revenue_audit_events b
                         WHERE b.action=a.action AND b.aggregate_type=a.aggregate_type
                           AND b.aggregate_uid=a.aggregate_uid)""",
        (SAVED, AGGREGATE),
    )
    return sorted((json.loads(row[0]) for row in rows), key=lambda item: item["name"].casefold())


def save_invoice_service(repository, actor, form):
    name = str(form.get("service_name") or "").strip()
    description = str(form.get("service_description") or "").strip()
    uid = str(form.get("service_uid") or "")
    revision = str(form.get("service_revision") or "")
    key = str(form.get("idempotency_key") or "")
    if not name or len(name) > 100 or len(description) > 300 or not key:
        raise ValueError("请填写产品／服务名称，名称最多 100 字，常用说明最多 300 字。")
    payload = dict(uid=uid, revision=revision, name=name, description=description)

    def operation():
        catalogue = list_invoice_services(repository.connection)
        current = next((item for item in catalogue if item["uid"] == uid), None)
        if uid and (not current or current["revision"] != revision):
            raise ValueError("该项目已更新，请重新打开目录再编辑。")
        if any(item["uid"] != uid and item["name"].casefold() == name.casefold() for item in catalogue):
            raise ValueError("目录中已有同名产品／服务。")
        return dict(uid=uid or new_uid("invoice-service"), revision=new_uid("service-revision"),
                    name=name, description=description)

    return repository.command(idempotency_key=f"invoice-service:{key}", command_type=SAVED,
                              aggregate_type=AGGREGATE, payload=payload, actor_user_id=actor, operation=operation)
