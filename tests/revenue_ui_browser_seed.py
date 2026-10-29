#!/usr/bin/env python3
"""Seed synthetic Revenue UI data into one explicit isolated database."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def seed(database_path: Path) -> None:
    path = database_path.resolve()
    if path == (ROOT / "database/aaie_crm.db").resolve():
        raise AssertionError("seed must run before the isolated runtime copy is installed")
    if not path.is_relative_to(ROOT / "SAFEBOX") or not path.is_file() or path.is_symlink():
        raise AssertionError("seed target must be a regular SAFEBOX database")

    from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
    from revenue.repository import RevenueRepository
    from revenue.services import RevenueService
    from revenue.ui_actions import RevenueUIActions
    from security import password_hash

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute(
            "UPDATE users SET password_hash=? WHERE username LIKE 'revenue-e2e-%'",
            (password_hash("RevenueUiE2E!2026"),),
        )
        connection.execute(
            "UPDATE revenue_module_state SET status='active',cutover_at=?,activated_by=900001,updated_at=? WHERE id=1",
            ("2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
        )
        for student_id, first_name in (("UI-FINANCE-001", "Existing"), ("UI-FINANCE-002", "New")):
            connection.execute(
                """INSERT INTO students(
                       student_id,passport_last_name,passport_first_name,current_status,
                       nas_status,created_date,updated_at,form_data_json,application_stream
                   ) VALUES(?,?,?,'Active','pending',?,?,?,'school_education')""",
                (student_id, "Test", first_name, "2026-08-29", "2026-08-29T00:00:00Z", "{}"),
            )
        connection.execute(
            """INSERT INTO schools(
                   id,school_name,sector_key,school_type,state,basic_info_json,
                   library_structure_json,dynamic_tags_json,zone_data_json,
                   academic_info_json,data_quality_status,created_at,updated_at
               ) VALUES(990010,'AAIE UI Test College','private','Independent','VIC',
                        '{}','{}','[]','{}','{}','Verified',?,?)""",
            ("2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
        )
        cycles = (
            (990090, "ui-cycle-prior", "UI-FINANCE-001", 1, "closed", "withdrawn", "2025-01-01", "2025-08-01"),
            (990100, "ui-cycle-current", "UI-FINANCE-001", 2, "active", None, "2026-08-29", None),
            (990200, "ui-cycle-new", "UI-FINANCE-002", 1, "active", None, "2026-08-29", None),
        )
        for cycle_id, uid, student_id, sequence, status, outcome, opened, closed in cycles:
            connection.execute(
                """INSERT INTO application_cycles(
                       id,cycle_uid,student_id,cycle_type,sequence_no,status,outcome,
                       opened_at,closed_at,created_by_user_id,created_by_name,created_at,
                       updated_by_user_id,updated_by_name,updated_at,education_stream
                   ) VALUES(?,?,?,'initial',?,?,?,?,?,900001,'E2E Admin',?,900001,'E2E Admin',?,'school_education')""",
                (cycle_id, uid, student_id, sequence, status, outcome, opened, closed,
                 f"{opened}T00:00:00Z", f"{opened}T00:00:00Z"),
            )
        connection.execute(
            """INSERT INTO contracts(
                   id,student_id,contract_uid,is_standalone_contract,contract_category,
                   contract_type,contract_title,contract_status,contract_date,
                   service_scope_json,fully_executed_at,updated_at,application_cycle_id
               ) VALUES(990050,'UI-FINANCE-001','ui-prior-contract',0,'student_service',
                        'school_application','UI Prior Contract','FullyExecuted','2025-01-02',
                        '[]','2025-01-03T00:00:00Z','2025-01-03T00:00:00Z',990090)"""
        )
        connection.execute(
            """INSERT INTO private_school_applications(
                   id,application_cycle_id,student_id,school_id,school_name_snapshot,
                   status,submitted_at,submitted_by_user_id,submitted_by_name,
                   created_by_user_id,created_at,updated_at
               ) VALUES(990060,990090,'UI-FINANCE-001',990010,'AAIE UI Test College',
                        'withdrawn','2025-01-04',900001,'E2E Admin',900001,
                        '2025-01-04T00:00:00Z','2025-01-05T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO private_school_applications(
                   id,application_cycle_id,student_id,school_id,school_name_snapshot,
                   status,submitted_at,submitted_by_user_id,submitted_by_name,
                   created_by_user_id,created_at,updated_at
               ) VALUES(990061,990100,'UI-FINANCE-001',990010,'AAIE UI Test College',
                        'offer_received','2026-08-18',900001,'E2E Admin',900001,
                        '2026-08-18T00:00:00Z','2026-08-20T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO offer_received_events(
                   id,application_cycle_id,student_id,source_kind,private_application_id,school_id,
                   school_name_snapshot,external_offer_key,idempotency_key,received_at,
                   recorded_by_user_id,recorded_by_name,created_at
               ) VALUES(990300,990100,'UI-FINANCE-001','private',990061,990010,
                        'AAIE UI Test College','ui-offer-1','ui-offer-idem',
                        '2026-08-20',900001,'E2E Admin','2026-08-20T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO cycle_final_acceptances(
                   id,application_cycle_id,student_id,offer_received_event_id,school_id,
                   school_name_snapshot,selected_at,selected_by_user_id,selected_by_name,
                   acceptance_status,created_at,updated_at
               ) VALUES(990301,990100,'UI-FINANCE-001',990300,990010,
                        'AAIE UI Test College','2026-08-21',900001,'E2E Admin','active',
                        '2026-08-21T00:00:00Z','2026-08-21T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO coe_oshc(
                   id,student_id,document_kind,school_name,coe_start_date,created_at,
                   application_cycle_id
               ) VALUES(990302,'UI-FINANCE-001','COE','AAIE UI Test College',
                        '2027-02-01','2026-08-22T00:00:00Z',990100)"""
        )
        connection.execute(
            """INSERT INTO student_school_enrolments(
                   id,student_id,school_id,school_name_snapshot,enrolment_role,start_date,
                   status,created_by,created_at,updated_by,updated_at,application_cycle_id,
                   year_level_snapshot
               ) VALUES(990303,'UI-FINANCE-001',990010,'AAIE UI Test College','current',
                        '2027-02-01','active',900001,'2026-08-22T00:00:00Z',900001,
                        '2026-08-22T00:00:00Z',990100,'Year 10')"""
        )
        connection.commit()

        service = RevenueService(RevenueRepository(connection))
        existing_case = service.open_case(990100, 900001, "ui-case-existing")
        service.open_case(990200, 900001, "ui-case-new")
        component = service.link_enrolment_component(
            existing_case["uid"], "school", "school_enrolment", 990303, 900001,
            "ui-component", final_acceptance_id=990301, school_enrolment_id=990303,
            coe_oshc_id=990302,
        )
        service.confirm_commencement(component["uid"], "2027-02-01", 900001, "ui-commencement")
        service.record_tuition(
            component["uid"], "2027", "tuition", 2_500_000, "official",
            900001, "ui-tuition",
        )
        account = service.create_billing_account("family", "UI Revenue Family", 900001, "ui-account")
        connection.execute(
            """INSERT INTO contracts(
                   id,student_id,contract_uid,is_standalone_contract,contract_category,
                   contract_type,contract_title,contract_status,contract_date,
                   service_scope_json,fully_executed_at,updated_at,application_cycle_id
               ) VALUES(990070,'UI-FINANCE-001','ui-current-contract',0,'student_service',
                        'school_application','UI Current Contract','FullyExecuted','2026-08-29',
                        '[]','2026-08-29T00:00:00Z','2026-08-29T00:00:00Z',990100)"""
        )
        fee = service.create_fee_items(
            existing_case["uid"], account["uid"],
            [{"milestone_code": "application", "description": "申请服务费",
              "net_cents": 240_000, "gst_cents": 0,
              "contract_id": 990070, "application_cycle_id": 990100}],
            900001, "ui-fee", "ui-fee-rule",
        )[0]
        client_actions = RevenueUIActions(
            connection, path.parent / "revenue_documents", 900001, "Admin"
        )
        client_actions.record_client_payment({
            "fee_item_uid": fee["uid"],
            "payment_method_code": "au_company_account",
            "actual_currency_code": "AUD",
            "actual_amount_cents": "132000",
            "allocated_base_cents": "120000",
            "received_date": "2026-08-30",
            "external_reference": "UI-PAYMENT-001",
            "idempotency_key": "ui-client-payment",
        })
        provider = service.create_revenue_entity("school", "AAIE UI Test College", 900001, "ui-provider")
        master = service.create_revenue_entity("master_agent", "AAIE UI Master Agent", 900001, "ui-master")
        contract_document = service.register_document(
            "commission_contract", "/isolated/ui-direct-contract.pdf", "a" * 64,
            "application/pdf", 100, "upload", 900001, "ui-contract-document",
        )
        contract = service.create_contract_version(
            provider["uid"], contract_document["uid"], "direct_commission",
            "2026-01-01", 900001, "Admin", "ui-contract",
        )
        parse_run = service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion("commission_rate_basis_points", "1000", 1, "1", "10%", 10000)
            ]), 900001, "Admin", "ui-parse",
        )
        service.review_extraction(
            parse_run["extractions"][0]["uid"], "1000", True,
            900001, "Admin", "ui-review",
        )
        contract = service.activate_contract_version(
            contract["uid"], 900001, "Admin", "ui-contract-activate"
        )
        rule = service.activate_commission_rule(
            contract["uid"], {
                "fee_components": ["tuition"],
                "instalments": [{"instalment_no": 1, "academic_period": "2027", "rate_basis_points": 1000}],
            }, 900001, "Admin", "ui-commission-rule",
        )
        line = service.calculate_commission(
            existing_case["uid"], component["uid"], provider["uid"], rule["uid"],
            900001, "Admin", "ui-commission",
        )
        service.activate_master_agent_share_rule(
            master["uid"], provider["uid"], 2000, "2026-01-01",
            900001, "Admin", "ui-share-rule",
        )
        statement_document = service.register_document(
            "master_agent_statement", "/isolated/ui-statement.csv", "b" * 64,
            "text/csv", 100, "imported", 900001, "ui-statement-document",
        )
        service.import_statement(
            master["uid"], provider["uid"], statement_document["uid"], "UI-ST-001",
            [{"external_reference": line["uid"], "student_reference": "UI-FINANCE-001",
              "student_name_text": "Existing Test", "program_text": "Year 10",
              "gross_commission_cents": 275_000}],
            900001, "Admin", "ui-statement",
        )
        connection.execute(
            """INSERT INTO revenue_exceptions(
                   uid,exception_code,severity,aggregate_type,aggregate_uid,message,status,opened_at
               ) VALUES('ui-exception','STATEMENT_REVIEW','warning','commercial_case',?,
                        '请核对一级代理对账单金额','open','2026-08-29T10:00:00Z')""",
            (existing_case["uid"],),
        )
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AssertionError("synthetic Revenue UI seed introduced foreign-key violations")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    seed(args.database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
