"""Synthetic operational workflow driver for Revenue cases A-E.

The driver invokes the same public orchestrator/service boundaries used by the
existing CRM workflow hooks.  It never writes Production or core authority
records through Revenue.
"""

from __future__ import annotations

from revenue.ai import ContractSuggestion, ManualContractSuggestionProvider
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from revenue.repository import RevenueRepository, new_uid
from revenue.services import RevenueService


class RevenueOperationalWorkflowDriver:
    def __init__(self, connection):
        self.connection = connection
        self.service = RevenueService(RevenueRepository(connection))
        self.orchestrator = RevenueOperationalOrchestrator(connection, 1, "Admin")

    def activate_provider_rule(self, entity_type="school", provider_id=10, rate=1000):
        entity = self.service.create_revenue_entity(
            entity_type, "Operational Provider", 1, f"e2e-entity:{entity_type}:{provider_id}"
        )
        link_column = (
            "school_id" if entity_type == "school" else "higher_education_provider_id"
        )
        self.connection.execute(
            f"""INSERT INTO revenue_entity_provider_links(
                   uid,revenue_entity_id,{link_column},effective_from,created_at
                 ) VALUES(?,?,?,'2026-01-01','2026-08-30')""",
            (new_uid("provider-link"), entity["id"], provider_id),
        )
        document = self.service.register_document(
            "commission_contract", f"/isolated/{entity_type}-{provider_id}.pdf",
            (str(provider_id)[-1:] or "1") * 64, "application/pdf", 100,
            "upload", 1, f"e2e-document:{entity_type}:{provider_id}",
        )
        contract = self.service.create_contract_version(
            entity["uid"], document["uid"], "direct_commission", "2026-01-01",
            1, "Admin", f"e2e-contract:{entity_type}:{provider_id}",
        )
        parse = self.service.create_parse_run(
            contract["uid"], ManualContractSuggestionProvider([
                ContractSuggestion(
                    "commission_rate_basis_points", str(rate), 1, "1",
                    f"{rate / 100}%", 10000,
                )
            ]), 1, "Admin", f"e2e-parse:{entity_type}:{provider_id}",
        )
        self.service.review_extraction(
            parse["extractions"][0]["uid"], str(rate), True, 1, "Admin",
            f"e2e-review:{entity_type}:{provider_id}",
        )
        active = self.service.activate_contract_version(
            contract["uid"], 1, "Admin", f"e2e-activate:{entity_type}:{provider_id}"
        )
        rule = self.service.activate_commission_rule(
            active["uid"], {
                "fee_components": ["tuition"],
                "instalments": [{
                    "instalment_no": 1, "academic_period": "2027",
                    "rate_basis_points": rate,
                }],
            }, 1, "Admin", f"e2e-rule:{entity_type}:{provider_id}",
        )
        return entity, rule
