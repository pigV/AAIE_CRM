from pathlib import Path
from dataclasses import replace
from html.parser import HTMLParser
import unittest

from student_lifecycle.transfer_v2_models import (
    TransferV2Branch,
    TransferV2Eligibility,
    TransferV2Overview,
)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def visible_text(html):
    parser = _VisibleText()
    parser.feed(html)
    return " ".join(parser.parts)
from student_lifecycle.transfer_v2_projection import TransferV2Projection
from transfer_v2_views import (
    _school_type_label,
    render_transfer_v2_creation_form,
    render_transfer_v2_detail,
)


class TransferV2UiTests(unittest.TestCase):
    def test_transfer_grid_can_shrink_inside_mobile_viewport(self):
        css = (Path(__file__).resolve().parents[1] / "assets/transfer-v2.css").read_text()
        self.assertIn("grid-template-columns: minmax(0, 1fr)", css)
        self.assertIn(".transfer-v2-workspace > *,", css)
        self.assertIn(".transfer-v2-legacy > * { min-width: 0; }", css)
        self.assertIn(".transfer-v2-legacy-payments", css)

    def test_school_type_labels_are_chinese_and_fail_closed(self):
        self.assertEqual("中学", _school_type_label("Secondary"))
        self.assertEqual("小学与中学", _school_type_label("Pri/Sec"))
        self.assertEqual("公立学校", _school_type_label("Government"))
        self.assertEqual("类型待确认", _school_type_label("internal-new-kind"))

    def overview(self, *, legacy=False):
        return TransferV2Overview(
            student_id="S1",cycle_id=201,case_id=101,
            source_label=None if legacy else "aaie_existing",
            lifecycle_stage="legacy_unconfirmed" if legacy else "offer_accepted",
            lifecycle_label=(
                "Legacy Transfer · Lifecycle unconfirmed" if legacy else "Offer已接受"
            ),
            is_legacy=legacy,current_school_name="Current College",
            current_year_level="Year 8",application_year_level="Year 9",
            transfer_year=2027,transfer_term="Term 1",
            branches=(
                TransferV2Branch(301,"branch-301",20,"Target Grammar","VIC","Melbourne","Independent","accepted",1),
                TransferV2Branch(302,"branch-302",None,"Manual Target High","NSW","Sydney","Government","offer",2),
            ),
            current_school_id=10,
        )

    def projection(self, *, stage="offer_accepted", accepted=True):
        return TransferV2Projection(
            cycle_id=201,cycle_uid="cycle-201",student_id="S1",case_id=101,
            cycle_status="active",cycle_outcome=None,stage_key=stage,stage_order=50,
            stage_label="Offer已接受",blocker="等待确认付款",next_action="确认付款",
            current_branch_id=301 if accepted else None,
            current_school_name="Target Grammar" if accepted else None,
            current_acceptance_id=401 if accepted else None,
            current_offer_event_id=501 if accepted else None,
            payment_id=None,coe_confirmation_id=None,is_legacy=False,
            ready_to_complete=False,
        )

    def test_creation_form_shows_contract_gate_and_six_business_fields(self):
        blocked = render_transfer_v2_creation_form(
            student_id="S1",csrf="csrf-token",
            eligibility=TransferV2Eligibility(False,None,"contract_required","请先完成转学合同"),
            current_school={"id":10,"name":"Current College"},
            current_year_level="Year 8",
            school_choices=({"id":20,"name":"Target Grammar"},),
        )
        self.assertIn("请先完成转学合同", blocked)
        self.assertIn("disabled", blocked)
        self.assertIn("建立 / 完成转学合同", blocked)
        self.assertIn(
            'href="/students/S1?tab=contracts#contracts-section"', blocked
        )
        self.assertNotIn('href="/students/S1/contracts"', blocked)
        for field in (
            "current_school_id","current_year_level","application_year_level",
            "transfer_year","transfer_term","target_school_id","current_school_name",
            "target_school_name",
        ):
            self.assertIn(f'name="{field}"', blocked)
        self.assertIn("data-transfer-v2-school-selector", blocked)
        self.assertIn("data-transfer-v2-school-library-result", blocked)
        self.assertEqual(2, blocked.count('role="listbox"'))
        self.assertEqual(2, blocked.count('aria-autocomplete="list"'))
        self.assertEqual(2, blocked.count('aria-expanded="false"'))
        self.assertEqual(2, blocked.count('data-transfer-v2-school-suggestions'))
        self.assertIn("学校库搜不到时手动输入", blocked)
        self.assertIn("data-transfer-v2-target-list", blocked)
        self.assertIn("data-transfer-v2-add-target", blocked)
        self.assertIn('<select name="current_year_level" required>', blocked)
        self.assertIn('<select name="application_year_level" required>', blocked)
        self.assertIn('<select name="transfer_term" required>', blocked)
        for year in range(1, 13):
            self.assertEqual(2, blocked.count(f'value="Year {year}"'))
        for term in range(1, 5):
            self.assertEqual(1, blocked.count(f'value="Term {term}"'))

        ready = render_transfer_v2_creation_form(
            student_id="S1",csrf="csrf-token",
            eligibility=TransferV2Eligibility(True,101,"ready","转学合同：已完成"),
            current_school={"id":10,"name":"Current College"},
            current_year_level="Year 8",
            school_choices=({"id":20,"name":"Target Grammar"},),
        )
        self.assertIn("转学合同：已完成", ready)
        self.assertIn('name="source_contract_id" value="101"', ready)
        self.assertNotIn('data-transfer-v2-create disabled', ready)

    def test_staff_can_save_source_without_contract_and_sees_role_handoff(self):
        """Source verification is a Staff action; contract generation is not."""
        html = render_transfer_v2_creation_form(
            student_id="S1", csrf="csrf-token",
            eligibility=TransferV2Eligibility(
                False, None, "contract_required", "等待转学合同签署"
            ),
            current_school={"id": 10, "name": "Current College"},
            current_year_level="Year 8",
            school_choices=({"id": 20, "name": "Target Grammar"},),
            source_options={
                "existing": ({
                    "current_enrolment_id": 80,
                    "ordinary_relationship_cycle_id": 50,
                    "ordinary_relationship_contract_id": 99,
                    "private_school_application_id": None,
                    "public_application_group_id": None,
                    "current_school_name": "Current College",
                    "proof_label": "历史普通申请关系",
                },),
            },
            can_manage_contract=False,
        )
        source_form = html.split('data-transfer-source-evidence', 1)[1].split(
            '</form>', 1
        )[0]
        self.assertIn('name="source_contract_id" value=""', source_form)
        self.assertIn('name="source_action" value="confirm_source"', source_form)
        self.assertIn('type="submit">核验并保存来源依据', source_form)
        self.assertNotIn('disabled', source_form)
        self.assertIn('由 Admin/Manager 生成并完成转学合同', html)
        self.assertNotIn('?tab=contracts#contracts-section', html)

    def test_transfer_page_uses_exact_transfer_role_gate_for_contract_management_link(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        start = source.index("    def student_transfer_case_create(")
        end = source.index("\n    def ", start + 5)
        route = source[start:end]
        self.assertIn(
            "can_manage_contract=can_manage_transfer_contract(self.user)", route
        )
        self.assertNotIn(
            "can_manage_contract=can_generate_contract(self.user)", route
        )

    def test_source_save_success_notice_is_visible(self):
        html = render_transfer_v2_creation_form(
            student_id="S1", csrf="csrf-token",
            eligibility=TransferV2Eligibility(
                False, 101, "contract_required", "等待转学合同签署"
            ),
            current_school={"id": 10, "name": "Current College"},
            current_year_level="Year 8", school_choices=(),
            source_notice="来源依据已保存，请由 Admin/Manager 完成转学合同。",
            can_manage_contract=False,
        )
        self.assertIn('class="notice success" role="status"', html)
        self.assertIn('来源依据已保存', visible_text(html))

    def test_acceptance_and_payment_are_distinct_ui_stages(self):
        acceptance = self.projection(stage="acceptance_required", accepted=False)
        acceptance = replace(
            acceptance,
            stage_label="等待选择已接受学校",
            blocker="请选择一个有效录取通知",
            next_action="选择当前接受的 Offer",
        )
        payment = self.projection(stage="payment_required", accepted=True)
        payment = replace(
            payment,
            stage_label="等待付款确认",
            blocker="等待已接受学校分支的付款",
            next_action="确认已接受学校分支的付款",
        )
        acceptance_html = render_transfer_v2_detail(
            self.overview(), acceptance, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
        )
        payment_html = render_transfer_v2_detail(
            self.overview(), payment, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
        )
        self.assertIn("等待选择已接受学校", visible_text(acceptance_html))
        self.assertIn("选择当前接受的 Offer", visible_text(acceptance_html))
        self.assertIn("等待付款确认", visible_text(payment_html))
        self.assertIn("确认已接受学校分支的付款", visible_text(payment_html))

    def test_new_cycle_acceptance_without_payment_hides_coe_confirmation(self):
        projection = replace(
            self.projection(stage="payment_required", accepted=True),
            stage_label="等待付款确认",
            blocker="付款尚未确认",
            next_action="确认已接受学校分支的付款",
            payment_id=None,
        )
        html = render_transfer_v2_detail(
            self.overview(), projection, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            coe_records=({"id": 701, "file_path": "/test/coe.pdf", "branch_id": 301},),
        )
        self.assertIn("等待付款确认", visible_text(html))
        self.assertNotIn('value="confirm_coe"', html)
        self.assertNotIn("确认已取得入学确认", visible_text(html))

    def test_new_cycle_confirmed_payment_exposes_coe_confirmation(self):
        projection = replace(
            self.projection(stage="coe_update", accepted=True),
            stage_label="更新 COE", blocker="等待确认入学确认文件",
            next_action="更新并确认入学确认文件", payment_id=601,
        )
        html = render_transfer_v2_detail(
            self.overview(), projection, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            payments_by_branch={301: ({
                "id": 601, "payment_category": "Offer Deposit",
                "amount": 1200, "currency": "AUD", "paid_date": "2026-09-17",
                "confirmed_at": "2026-09-17T01:00:00",
            },)},
            coe_records=({"id": 701, "file_path": "/test/coe.pdf", "branch_id": 301},),
        )
        self.assertIn('value="confirm_coe"', html)
        self.assertIn("确认已取得入学确认", visible_text(html))

    def test_new_cycle_switch_acceptance_only_resets_payment(self):
        html = render_transfer_v2_detail(
            self.overview(), self.projection(), csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            offers_by_branch={
                302: ({"id": 503, "state": "ACTIVE", "offer_status": "received"},),
            },
        )
        self.assertIn('name="payment_action" value="reset"', html)
        self.assertNotIn('value="inherit"', html)
        self.assertIn("切换后必须重新确认新学校分支的付款", visible_text(html))

    def test_legacy_switch_acceptance_retains_explicit_compatibility_choice(self):
        projection = replace(self.projection(), is_legacy=True)
        html = render_transfer_v2_detail(
            self.overview(), projection, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            offers_by_branch={
                302: ({"id": 503, "state": "ACTIVE", "offer_status": "received"},),
            },
        )
        self.assertIn('name="payment_action"', html)
        self.assertIn('value="reset"', html)
        self.assertIn('value="inherit"', html)

    def test_new_cycle_completion_mandates_enrolment_transition(self):
        projection = replace(
            self.projection(stage="pending_completion", accepted=True),
            stage_label="待结束", blocker="", next_action="确认结束转学",
            payment_id=601, coe_confirmation_id=701,
        )
        html = render_transfer_v2_detail(
            self.overview(), projection, csrf="csrf-token", can_edit=True,
            documents_by_branch={},
        )
        action_form = html.split('name="action" value="complete_transfer"', 1)[1]
        action_form = action_form.split('</form>', 1)[0]
        self.assertIn(
            'type="hidden" name="update_current_school" value="1"', action_form
        )
        transition_text = visible_text(action_form)
        self.assertIn("旧当前在读转为历史", transition_text)
        self.assertIn("已接受学校转为新当前在读", transition_text)
        self.assertNotIn('name="update_current_school" required', action_form)
        self.assertNotIn('<option value="0">否</option>', action_form)

    def test_overview_has_next_action_branch_colors_accepted_school_and_tabs(self):
        html = render_transfer_v2_detail(
            self.overview(),replace(self.projection(), payment_id=601),
            csrf="csrf-token",can_edit=True,
            documents_by_branch={},
            school_choices=({"id":30,"name":"Another Target High"},),
            offers_by_branch={
                301: ({"id":502,"received_at":"2026-08-20","state":"ACTIVE",
                       "school_name_snapshot":"Target Grammar","is_latest":1},)
            },
            payments_by_branch={
                301: ({"id":601,"payment_category":"Offer Deposit","amount":1000,
                       "currency":"AUD","paid_date":"2026-08-21",
                       "confirmed_at":"2026-08-21T10:00:00"},)
            },
            coe_records=({"id":701,"file_path":"/nas/S1/coe.pdf","branch_id":301},),
        )
        self.assertIn('id="transfer-v2-overview"', html)
        self.assertIn("下一步", html)
        self.assertIn("确认付款", html)
        self.assertIn("最终选定学校", html)
        self.assertIn("Target Grammar", html)
        self.assertIn("transfer-v2-branch--accepted", html)
        self.assertIn("transfer-v2-branch--offer", html)
        for label in ("概览","目标学校","文件","录取通知","付款","入学确认","历史记录","签证","学生保险","住宿","接机"):
            self.assertIn(label, html)
        self.assertNotIn('value="change_origin"', html)
        self.assertNotIn('value="confirm_payment"', html)
        self.assertIn("付款记录 #601", html)
        self.assertIn("唯一创建和上传入口：学生资料页的付款管理", html)
        self.assertIn('value="confirm_coe"', html)
        self.assertIn('value="invalidate_offer"', html)
        self.assertIn('value="request_school_library"', html)
        self.assertIn("+ 添加目标学校", html)
        self.assertIn('value="add_target_school"', html)
        self.assertIn('name="target_school_name"', html)
        self.assertIn('name="target_school_id"', html)
        self.assertIn('data-transfer-v2-school-suggestions', html)
        self.assertIn('/assets/transfer-v2-ui.js?v=8', html)
        self.assertIn('/assets/lifecycle-workspace.css?v=1', html)
        self.assertIn('/assets/lifecycle-workspace.js?v=1', html)
        self.assertIn('value="502"', html)
        self.assertIn('value="701"', html)

    def test_create_and_add_target_share_one_school_selector_contract(self):
        school_choices = ({
            "id": 30,
            "name": "Another Target High",
            "state": "VIC",
            "region": "Eastern Melbourne",
            "school_type": "Government",
        },)
        create_html = render_transfer_v2_creation_form(
            student_id="S1", csrf="csrf-token",
            eligibility=TransferV2Eligibility(True, 101, "ready", "ready"),
            current_school={"id": 10, "name": "Current College"},
            current_year_level="Year 8", school_choices=school_choices,
        )
        detail_html = render_transfer_v2_detail(
            self.overview(), self.projection(), csrf="csrf-token", can_edit=True,
            documents_by_branch={}, school_choices=school_choices,
        )
        for marker in (
            'data-transfer-v2-school-selector',
            'data-transfer-v2-school-name',
            'data-transfer-v2-school-library-result',
            'data-transfer-v2-school-suggestions',
            'data-transfer-v2-school-state',
            'data-transfer-v2-school-region',
            'data-transfer-v2-school-type',
            'data-school-id="30"',
        ):
            self.assertIn(marker, create_html)
            self.assertIn(marker, detail_html)
        self.assertIn('/assets/transfer-v2-ui.js?v=8', create_html)
        self.assertIn('/assets/transfer-v2-ui.js?v=8', detail_html)

    def test_missing_document_override_is_explicit_and_complete_branch_is_direct(self):
        def requirement(uid, label, document_type, satisfied):
            return type("Requirement", (), {
                "requirement_uid": uid,
                "label": label,
                "document_type": document_type,
                "satisfied": satisfied,
            })()

        overview = self.overview()
        overview = replace(
            overview,
            branches=tuple(
                replace(branch, current_state="preparing")
                for branch in overview.branches
            ),
        )
        html = render_transfer_v2_detail(
            overview, self.projection(), csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            checklists_by_branch={
                301: (
                    requirement("passport", "护照", "Passport", False),
                    requirement("transcript", "最新成绩单", "Transcript", False),
                ),
                302: (requirement("birth", "出生证明", "Birth Certificate", True),),
            },
        )
        self.assertIn("以下材料仍待补", html)
        self.assertIn("护照", html)
        self.assertIn("最新成绩单", html)
        self.assertIn("仍有材料待补，继续申请", html)
        self.assertIn('data-transfer-v2-missing-override', html)
        self.assertIn('data-transfer-v2-confirm-message=', html)
        self.assertIn('name="allow_missing" value="1"', html)
        self.assertIn('name="new_state" value="application_submitted"', html)
        self.assertIn("备注（可选）", html)
        self.assertIn(">进入学校申请</button>", html)
        self.assertNotIn("确认带缺项推进", html)

    def test_unaccepted_does_not_show_school_and_legacy_is_read_only(self):
        unaccepted = self.projection(stage="offer_received", accepted=False)
        html = render_transfer_v2_detail(
            self.overview(),unaccepted,csrf="csrf-token",can_edit=True,
            documents_by_branch={},
        )
        self.assertNotIn("最终选定学校", html)
        legacy_html = render_transfer_v2_detail(
            self.overview(legacy=True),None,csrf="csrf-token",can_edit=True,
            documents_by_branch={},
        )
        self.assertIn("历史转学记录 · 办理状态待确认", legacy_html)
        self.assertIn("只读兼容", legacy_html)
        self.assertNotIn("data-transfer-v2-action", legacy_html)

    def test_current_files_render_above_existing_files(self):
        class Item:
            def __init__(self, name, source):
                self.document_id=1
                self.document_type=name
                self.document_date="2026-08-20"
                self.uploaded_at="2026-08-21"
                self.file_path=f"/nas/S1/{name}.pdf"
                self.source_cycle_label=source
                self.binding_id=11
                self.branch_id=301
                self.validity="valid"

        class Sections:
            current=(Item("CurrentTranscript","Current Transfer #2"),)
            existing=(Item("Passport","Initial Cycle"),)

        html = render_transfer_v2_detail(
            self.overview(),self.projection(),csrf="csrf-token",can_edit=True,
            documents_by_branch={301:Sections()},
            checklists_by_branch={301:(type("Requirement",(),{
                "document_type":"CurrentTranscript","requirement_id":81,
                "label":"最新成绩单","adopted_binding_id":None,
            })(),)},
            available_documents=({"id":901,"document_type":"Passport",
                                  "file_path":"/nas/S1/passport.pdf"},),
        )
        current_index = html.index("本次转学更新文件")
        existing_index = html.index("已有文件")
        self.assertLess(current_index, existing_index)
        self.assertIn("transfer-v2-files--current", html)
        self.assertIn("transfer-v2-files--existing", html)
        self.assertIn("本次转学 #2", html)
        self.assertIn("初始申请记录", html)
        self.assertIn('value="bind_document"', html)
        self.assertIn('value="adopt_document"', html)
        self.assertIn('value="invalidate_binding"', html)

    def test_completed_summary_is_explicit(self):
        completed = self.projection(stage="transfer_completed")
        completed = TransferV2Projection(**{
            **completed.__dict__,"cycle_status":"closed","cycle_outcome":"completed",
            "stage_order":100,"stage_label":"Transfer Completed","blocker":"",
            "next_action":None,"payment_id":601,"coe_confirmation_id":701,
        })
        html = render_transfer_v2_detail(
            self.overview(),completed,csrf="csrf-token",can_edit=False,
            documents_by_branch={},
        )
        self.assertIn("转学完成", html)
        self.assertIn("最终学校", html)
        self.assertIn("Year 9", html)
        self.assertIn("2027 · Term 1", html)
        self.assertIn("付款记录 #601 · 已确认", html)
        self.assertIn("入学确认记录 #701 · 已确认", html)
        self.assertNotIn('value="confirm_payment"', html)
        self.assertNotIn('value="confirm_coe"', html)

    def test_transfer_workspace_uses_shared_chinese_presentation_without_raw_internal_terms(self):
        html = render_transfer_v2_detail(
            self.overview(), self.projection(), csrf="csrf-token", can_edit=True,
            documents_by_branch={},
            history_events=({"event_type": "branch_transition", "occurred_at": "2026-08-20", "actor_user_id": 7},),
        )
        text = visible_text(html)
        self.assertIn('data-lifecycle-workspace', html)
        self.assertIn('class="lifecycle-workspace__header"', html)
        self.assertNotIn('class="lifecycle-stepper"', html)
        self.assertIn('class="lifecycle-tabs"', html)
        self.assertIn('class="lifecycle-next-action"', html)
        self.assertIn('data-lifecycle-tab="overview"', html)
        self.assertIn('data-lifecycle-panel="branches"', html)
        for label in ("转学办理", "最终选定学校", "录取通知版本记录", "业务历史"):
            self.assertIn(label, text)
        for forbidden in (
            "Branch", "Overview", "History", "materials_preparation",
            "application_in_progress", "resolver", "projection", "canonical", "UID",
        ):
            self.assertNotIn(forbidden, text)

    def test_transfer_create_is_chinese_shared_workspace_but_preserves_post_values(self):
        html = render_transfer_v2_creation_form(
            student_id="S1", csrf="csrf-token",
            eligibility=TransferV2Eligibility(True, 101, "ready", "转学合同：已完成"),
            current_school={"id": 10, "name": "Current College"},
            current_year_level="Year 8",
            school_choices=({"id": 20, "name": "Target Grammar"},),
        )
        text = visible_text(html)
        self.assertIn('data-lifecycle-workspace', html)
        self.assertIn("建立转学记录", text)
        self.assertIn("创建转学记录", text)
        self.assertNotIn("Transfer V2", text)
        self.assertNotIn("Cycle", text)
        self.assertNotIn('name="source_label"', html)
        self.assertIn("来源待核验", text)
        self.assertIn('name="source_parent_form_token"', html)
        self.assertIn('name="source_existing_evidence"', html)


if __name__ == "__main__":
    unittest.main()
