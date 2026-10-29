import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nas_recovery_supervisor import (
    CANONICAL_MOUNT_POINT,
    CANONICAL_SHARE,
    CANONICAL_STUDENT_ROOT,
    ActionLimiter,
    JsonEventLogger,
    JsonStateStore,
    LiveSystem,
    MountState,
    Observation,
    RecoveryPolicy,
    ServiceHealth,
    Supervisor,
    classify,
)


UTC = timezone.utc


def service_health(service, *, ready, error_code=""):
    port = 8780 if service == "admin" else 8781
    return ServiceHealth.from_payload(
        service,
        {
            "status": "ok",
            "service": service,
            "port": port,
            "nas": {
                "ready": ready,
                "root": CANONICAL_STUDENT_ROOT,
                "readable": ready,
                "writable": ready,
                "error_code": error_code,
                "message": error_code,
            },
        },
    )


def observation(
    *,
    mounted=True,
    root_exists=True,
    root_readable=True,
    admin=None,
    parent=None,
    alternate_mounts=(),
    canonical_share_matches=True,
):
    return Observation(
        mount=MountState(
            canonical_mounted=mounted,
            root_exists=root_exists,
            root_readable=root_readable,
            alternate_mounts=tuple(alternate_mounts),
            canonical_share_matches=canonical_share_matches,
        ),
        admin=admin or service_health("admin", ready=True),
        parent=parent or service_health("parent", ready=True),
    )


class FakeSystem:
    def __init__(self, observations, *, restart_pids=None, reconnect_result=False):
        self.observations = list(observations)
        self.last_observation = self.observations[-1]
        self.restart_pids = restart_pids or {"admin": (100, 101), "parent": (200, 201)}
        self.reconnect_result = reconnect_result
        self.restarts = []
        self.reconnects = []
        self.sleeps = []

    def observe(self):
        if self.observations:
            self.last_observation = self.observations.pop(0)
        return self.last_observation

    def restart_service(self, service):
        self.restarts.append(service)
        return self.restart_pids[service]

    def reconnect(self, share, mount_point):
        self.reconnects.append((share, mount_point))
        return self.reconnect_result

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class RecoveryClassificationTests(unittest.TestCase):
    def test_healthy_selects_no_action(self):
        decision = classify(observation())

        self.assertEqual(decision.state, "healthy")
        self.assertEqual(decision.action, "none")

    def test_admin_process_stale_restarts_admin_only(self):
        decision = classify(
            observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        )

        self.assertEqual(decision.state, "admin_process_stale")
        self.assertEqual(decision.action, "restart_admin")

    def test_parent_process_stale_restarts_parent_only(self):
        decision = classify(
            observation(parent=service_health("parent", ready=False, error_code="write_denied"))
        )

        self.assertEqual(decision.state, "parent_process_stale")
        self.assertEqual(decision.action, "restart_parent")

    def test_mount_lost_is_blocked_without_attempting_reconnect(self):
        decision = classify(
            observation(
                mounted=False,
                root_exists=False,
                root_readable=False,
                admin=service_health("admin", ready=False, error_code="mount_unavailable"),
                parent=service_health("parent", ready=False, error_code="mount_unavailable"),
            )
        )

        self.assertEqual(decision.state, "mount_lost")
        self.assertEqual(decision.action, "human_attention")
        self.assertEqual(decision.reason, "automatic_remount_disabled")
        self.assertEqual(decision.share, "//192.168.1.99/Server")
        self.assertEqual(decision.mount_point, "/Volumes/Server")

    def test_duplicate_or_suffixed_mount_is_blocked(self):
        decision = classify(
            observation(
                mounted=False,
                root_exists=False,
                root_readable=False,
                alternate_mounts=("/Volumes/Server-1",),
                admin=service_health("admin", ready=False, error_code="mount_unavailable"),
                parent=service_health("parent", ready=False, error_code="mount_unavailable"),
            )
        )

        self.assertEqual(decision.state, "mount_conflict")
        self.assertEqual(decision.action, "human_attention")

    def test_wrong_share_at_canonical_mount_point_is_blocked(self):
        decision = classify(observation(canonical_share_matches=False))

        self.assertEqual(decision.state, "mount_source_mismatch")
        self.assertEqual(decision.action, "human_attention")

    def test_service_reporting_noncanonical_student_root_is_blocked(self):
        payload = {
            "status": "ok",
            "service": "admin",
            "port": 8780,
            "nas": {
                "ready": True,
                "root": "/tmp/local-fallback",
                "readable": True,
                "writable": True,
                "error_code": "",
            },
        }
        decision = classify(
            observation(admin=ServiceHealth.from_payload("admin", payload))
        )

        self.assertEqual(decision.state, "storage_root_mismatch")
        self.assertEqual(decision.action, "human_attention")

    def test_both_services_denied_with_mount_present_is_not_auto_restarted(self):
        decision = classify(
            observation(
                admin=service_health("admin", ready=False, error_code="read_denied"),
                parent=service_health("parent", ready=False, error_code="read_denied"),
            )
        )

        self.assertEqual(decision.state, "permission_or_shared_session_failure")
        self.assertEqual(decision.action, "human_attention")

    def test_unavailable_health_endpoint_is_not_treated_as_nas_stale(self):
        admin = ServiceHealth.unavailable("admin", "timeout")
        decision = classify(observation(admin=admin))

        self.assertEqual(decision.state, "service_health_unavailable")
        self.assertEqual(decision.action, "human_attention")


class SupervisorBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = JsonStateStore(root / "state.json")
        self.logger = JsonEventLogger(root / "events.jsonl")
        self.now = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)

    def supervisor(self, system, *, policy=None):
        return Supervisor(
            system=system,
            state_store=self.store,
            logger=self.logger,
            policy=policy
            or RecoveryPolicy(
                wake_grace_delays=(5, 10),
                periodic_confirmation_delays=(3,),
                post_action_delays=(2, 4),
                service_restart_cooldown_seconds=900,
                service_restart_window_seconds=3600,
                service_restart_max_attempts=2,
                resume_gap_seconds=120,
            ),
            now=lambda: self.now,
        )

    def test_wake_grace_allows_nas_to_recover_without_restart(self):
        stale = observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        system = FakeSystem([stale, observation()])

        result = self.supervisor(system).run_once(trigger="wake")

        self.assertEqual(result.status, "recovered_during_grace")
        self.assertEqual(system.restarts, [])
        self.assertEqual(system.sleeps, [5])

    def test_persistent_admin_stale_restarts_admin_and_verifies_nas_ready(self):
        stale = observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        system = FakeSystem([stale, stale, stale, observation()])

        result = self.supervisor(system).run_once(trigger="periodic")

        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.action, "restart_admin")
        self.assertEqual(result.old_pid, 100)
        self.assertEqual(result.new_pid, 101)
        self.assertEqual(system.restarts, ["admin"])
        self.assertNotIn("parent", system.restarts)
        self.assertEqual(system.reconnects, [])

    def test_parent_stale_never_touches_admin_or_8792(self):
        stale = observation(parent=service_health("parent", ready=False, error_code="read_denied"))
        system = FakeSystem([stale, stale, stale, observation()])

        result = self.supervisor(system).run_once(trigger="periodic")

        self.assertEqual(result.status, "recovered")
        self.assertEqual(system.restarts, ["parent"])
        self.assertNotIn("admin", system.restarts)
        self.assertNotIn("content_extraction", system.restarts)

    def test_mount_lost_never_calls_reconnect_or_restarts_services(self):
        lost = observation(
            mounted=False,
            root_exists=False,
            root_readable=False,
            admin=service_health("admin", ready=False, error_code="mount_unavailable"),
            parent=service_health("parent", ready=False, error_code="mount_unavailable"),
        )
        system = FakeSystem([lost, lost], reconnect_result=True)

        result = self.supervisor(system).run_once(trigger="periodic")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failure_reason, "automatic_remount_disabled")
        self.assertEqual(system.reconnects, [])
        self.assertEqual(system.restarts, [])

    def test_restart_loop_protection_persists_across_supervisor_instances(self):
        stale = observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        policy = RecoveryPolicy(
            wake_grace_delays=(),
            periodic_confirmation_delays=(),
            post_action_delays=(),
            service_restart_cooldown_seconds=900,
            service_restart_window_seconds=3600,
            service_restart_max_attempts=2,
            resume_gap_seconds=120,
        )
        first_system = FakeSystem([stale, stale])
        first = self.supervisor(first_system, policy=policy).run_once(trigger="periodic")
        self.assertEqual(first.status, "failed")
        self.assertEqual(first_system.restarts, ["admin"])

        self.now += timedelta(seconds=60)
        second_system = FakeSystem([stale])
        second = self.supervisor(second_system, policy=policy).run_once(trigger="wake")

        self.assertEqual(second.status, "blocked")
        self.assertEqual(second.failure_reason, "restart_cooldown")
        self.assertEqual(second_system.restarts, [])

    def test_corrupt_persistent_state_fails_closed_without_restart(self):
        self.store.path.write_text("{not-json", encoding="utf-8")
        stale = observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        system = FakeSystem([stale])

        result = self.supervisor(system).run_once(trigger="periodic")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.failure_reason, "state_store_unavailable")
        self.assertEqual(system.restarts, [])
        self.assertEqual(system.reconnects, [])

    def test_long_run_gap_is_treated_as_wake_and_deduplicated(self):
        self.store.save({"last_run_at": (self.now - timedelta(minutes=10)).isoformat()})
        stale = observation(admin=service_health("admin", ready=False, error_code="read_denied"))
        system = FakeSystem([stale, observation()])

        result = self.supervisor(system).run_once(trigger="periodic")

        self.assertEqual(result.trigger, "resume_gap")
        self.assertEqual(result.status, "recovered_during_grace")
        self.assertEqual(system.restarts, [])
        saved = self.store.load()
        self.assertEqual(saved["last_run_at"], self.now.isoformat())

    def test_json_log_contains_required_observability_without_credentials(self):
        system = FakeSystem([observation()])
        result = self.supervisor(system).run_once(trigger="periodic")
        self.assertEqual(result.status, "healthy")

        event = json.loads(self.logger.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(
            {
                "timestamp",
                "trigger",
                "wake_event",
                "mount_state",
                "admin_health",
                "parent_health",
                "selected_action",
                "old_pid",
                "new_pid",
                "recovery_result",
                "retry_count",
                "failure_reason",
            }.issubset(event)
        )
        self.assertNotIn("sonnyz", json.dumps(event))
        self.assertNotIn("password", json.dumps(event).lower())

    def test_action_limiter_enforces_window_even_after_cooldown(self):
        limiter = ActionLimiter(self.store)
        first = limiter.reserve(
            "restart_admin", self.now, cooldown_seconds=10, window_seconds=3600, max_attempts=2
        )
        second = limiter.reserve(
            "restart_admin",
            self.now + timedelta(seconds=20),
            cooldown_seconds=10,
            window_seconds=3600,
            max_attempts=2,
        )
        third = limiter.reserve(
            "restart_admin",
            self.now + timedelta(seconds=40),
            cooldown_seconds=10,
            window_seconds=3600,
            max_attempts=2,
        )

        self.assertEqual(first, (True, ""))
        self.assertEqual(second, (True, ""))
        self.assertEqual(third, (False, "restart_window_limit"))


class LiveProbeBoundaryTests(unittest.TestCase):
    def test_mount_probe_accepts_username_qualified_canonical_share(self):
        completed = subprocess.CompletedProcess(
            args=["/sbin/mount"],
            returncode=0,
            stdout=(
                "//sonnyz@192.168.1.99/Server on /Volumes/Server "
                "(smbfs, nodev, nosuid, mounted by faqiang)\n"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "nas_recovery_supervisor.CANONICAL_STUDENT_ROOT", folder
        ), mock.patch(
            "nas_recovery_supervisor.subprocess.run", return_value=completed
        ):
            state = LiveSystem._mount_state()

        self.assertTrue(state.canonical_mounted)
        self.assertTrue(state.canonical_share_matches)
        self.assertEqual(state.alternate_mounts, ())

    def test_mount_probe_surfaces_suffixed_mount_without_treating_it_as_canonical(self):
        completed = subprocess.CompletedProcess(
            args=["/sbin/mount"],
            returncode=0,
            stdout=(
                "//sonnyz@192.168.1.99/Server on /Volumes/Server-1 "
                "(smbfs, nodev, nosuid, mounted by faqiang)\n"
            ),
            stderr="",
        )
        with mock.patch(
            "nas_recovery_supervisor.subprocess.run", return_value=completed
        ):
            state = LiveSystem._mount_state()

        self.assertFalse(state.canonical_mounted)
        self.assertEqual(state.alternate_mounts, ("/Volumes/Server-1",))


if __name__ == "__main__":
    unittest.main()
