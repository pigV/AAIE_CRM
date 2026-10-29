"""Opt-in real-process failure injection; launchd dispatch is simulated."""
import os
import unittest
from tools.production_activation_drill import run_scenario, run_capability_scenario

@unittest.skipUnless(os.environ.get('CRM_RUN_ISOLATED_ACTIVATION_DRILL')=='1', 'requires explicit isolated process drill authorization')
class ActualActivationDrillTests(unittest.TestCase):
    def check_rollback(self, scenario, service='admin'):
        result=run_scenario(scenario,service=service)
        self.assertEqual(result['backend'],'actual_owned_subprocess')
        self.assertEqual(result['controller_status'],'ROLLBACK_PASS')
        self.assertTrue(result['injected_failure_reached'])
        self.assertTrue(result['failed_candidate_removed'])
        self.assertTrue(result['previous_release_healthy'])
        self.assertTrue(result['runtime_authority_matches'])
        self.assertTrue(result['unrelated_process_untouched'])
        self.assertTrue(result['synthetic_database_unchanged'])
        self.assertTrue(result['jobs_cleaned_up'])
        return result
    def test_startup_failure_rolls_back_admin(self):self.check_rollback('startup_failure')
    def test_health_failure_rolls_back_admin(self):self.check_rollback('health_failure')
    def test_stuck_candidate_rolls_back_admin(self):self.check_rollback('stuck')
    def test_late_health_cannot_commit_after_failure(self):self.check_rollback('late_health')
    def test_never_created_process_uses_handshake_no_ack_proof(self):self.check_rollback('no_process')
    def test_actual_pre_ack_gate_exits_without_running_application(self):self.check_rollback('pre_ack_exit')
    def test_actual_git_publisher_database_process_rollback(self):
        result=self.check_rollback('real_git_health_failure')
        actual=result['actual_git_release_plan']
        self.assertNotEqual(actual['previous'],actual['candidate'])
        self.assertEqual(actual['final_head'],actual['previous'])
        self.assertTrue(actual['formal_publisher_used'])
        self.assertTrue(actual['snapshot_restore_result_exists'])
        self.assertTrue(actual['artifacts_match_previous'])
    def test_exited_root_orphan_is_removed(self):self.check_rollback('orphan_exit')
    def test_migration_applied_failure_restores_synthetic_baseline(self):self.check_rollback('migration_failure')
    def test_startup_failure_rolls_back_parent(self):self.check_rollback('startup_failure','parent')

@unittest.skipUnless(os.environ.get('CRM_RUN_ISOLATED_ACTIVATION_DRILL')=='1', 'requires explicit isolated process drill authorization')
class ActualCapabilityBaselineTests(unittest.TestCase):
    def check_baseline(self,fail):
        result=run_capability_scenario(fail_compatible=fail)
        self.assertEqual(result['status'],'ORIGINAL_RECOVERED' if fail else 'CAPABILITY_READY')
        for key in ('same_git_head','database_bytes_unchanged','exact_artifacts','paired_health','sentinel_unchanged','handshake_directory_empty','formal_publisher_used','baseline_initially_had_no_gate','three_mutable_artifacts','fault_reached','owned_processes_cleaned'):self.assertTrue(result[key],key)
        self.assertEqual(result['compatible_baseline_sealed'],not fail)
    def test_same_sha_first_capability_install_actual_processes(self):self.check_baseline(False)
    def test_failed_first_capability_install_recovers_original_processes(self):self.check_baseline(True)


if __name__=='__main__':unittest.main()

@unittest.skipUnless(os.environ.get('CRM_RUN_ISOLATED_ACTIVATION_DRILL')=='1', 'requires explicit isolated process drill authorization')
class ActualPreparationRecoveryTests(unittest.TestCase):
    def check_recovery(self,fail):
        result=run_capability_scenario(preparation_failure='health_failure' if fail else 'healthy')
        r=result['preparation_recovery']
        self.assertEqual(r['status'],'HOLD' if fail else 'ORIGINAL_RECOVERED')
        for key in ('failure_injected','lease_held_at_failure','authority_unchanged','database_unchanged','artifacts_unchanged','sentinel_unchanged','exactly_one_start_per_role','candidate_never_started','no_second_start','final_state_correct'):
            self.assertTrue(r[key],key)
        self.assertTrue(result['owned_processes_cleaned'])
    def test_prepare_failure_restores_actual_ack_baseline_once(self):self.check_recovery(False)
    def test_prepare_failure_recovery_failure_fences_without_second_start(self):self.check_recovery(True)
