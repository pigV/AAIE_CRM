"""Run the sync/native domain suite inside the existing task OS guard.

Real shared-DB HTTP simulations are intentionally run separately and serially.
"""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
separate={'test_business_change_http','test_business_change_audit_readonly',
          'test_business_change_migrate_tool','test_business_change_schema'}
modules=['tests.'+p.stem for p in sorted((ROOT/'tests').glob('test_business_change_*.py'))
         if p.stem not in separate]
modules += ['tests.'+name for name in (
    'test_application_withdrawal','test_parent_reapplication','test_parent_form_secure_module',
    'test_parent_form_intake_type','test_student_lifecycle_case_integration','test_transfer_v2_creation',
    'test_transfer_v2_admissions','test_transfer_v2_branches_documents','test_transfer_v2_authoritative_business_records',
    'test_transfer_v2_projection','test_transfer_v2_dashboard','test_transfer_completion_readiness',
    'test_transfer_completion_choices','test_student_transfer_summary','test_progression_contract_gate',
    'test_progression_contract_service','test_progression_completion_readiness',
    'test_student_progression_summary','test_student_management_summary','test_student_status_visa_fallback')]
modules.append('tests.test_progression_case_management.ProgressionServiceTests')
raise SystemExit(subprocess.call([sys.executable,'-B',str(ROOT/'tests/system_business_sync_isolation.py'),
    '--data-cwd',*modules],cwd=ROOT))
