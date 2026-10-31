"""Synthetic fixtures only; no app import, service, or production database."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("runtime_configuration"),
                             "Explicit separated-runtime configuration is not implemented")
        from runtime_configuration import RuntimeConfiguration, RuntimeConfigurationError, preflight
        self.Config, self.Error, self.preflight = RuntimeConfiguration, RuntimeConfigurationError, preflight
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "releases/main"
        self.source.mkdir(parents=True)
        (self.source / "assets").mkdir()
        (self.source / "migrations").mkdir()
        self.root = self.source.parent / "runtime/staging/unit"
        (self.root / "config").mkdir(parents=True)
        self.path = self.root / "config/runtime.json"
        self.value = dict(version=1, mode="staging", root=str(self.root), database="staging.db",
                          admin_port=29880, parent_port=29881, extraction_port=29892,
                          namespace="aaie_staging_unit_")
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.value))
        return self.path

    def load(self):
        return self.Config.load(self.path, source_root=self.source)

    def test_explicit_roots_are_separate_and_loading_does_not_create_database(self):
        c = self.load()
        self.assertEqual(c.source_root, self.source)
        self.assertEqual(c.root, self.root)
        self.assertEqual(c.db, self.root / "database/staging.db")
        self.assertFalse(c.db.exists())

    def test_preflight_reports_missing_resources_without_writes(self):
        before = sorted(str(p) for p in self.base.rglob("*"))
        result = self.preflight(self.load())
        self.assertFalse(result["ready"])
        self.assertIn("database_missing", result["errors"])
        self.assertIn("field_key_missing", result["errors"])
        self.assertEqual(before, sorted(str(p) for p in self.base.rglob("*")))

    def test_staging_cannot_select_production_root(self):
        self.value["root"] = str(self.source.parent / "runtime/production")
        self.write()
        with self.assertRaises(self.Error): self.load()

    def test_source_directory_cannot_be_data_directory(self):
        self.value["root"] = str(self.source)
        self.write()
        with self.assertRaises(self.Error): self.load()

    def test_relative_or_traversal_database_names_are_rejected(self):
        for name in ["../production.db", "/tmp/db.sqlite", "a/b.db", "", "a.db-wal"]:
            with self.subTest(name=name):
                self.value["database"] = name; self.write()
                with self.assertRaises(self.Error): self.load()

    def test_staging_cannot_use_production_or_colliding_ports(self):
        for fields in [dict(admin_port=8780), dict(extraction_port=8792),
                       dict(parent_port=29880), dict(extraction_port=29893),
                       dict(admin_port=True), dict(admin_port=65530)]:
            with self.subTest(fields=fields):
                original=self.value.copy(); self.value.update(fields); self.write()
                with self.assertRaises(self.Error): self.load()
                self.value=original

    def test_unknown_configuration_keys_are_rejected(self):
        self.value["ignore_isolation"] = True; self.write()
        with self.assertRaises(self.Error): self.load()

    def test_symlink_config_rejected(self):
        link = self.root / "config/alias.json"; link.symlink_to(self.path)
        with self.assertRaises(self.Error):
            self.Config.load(link, source_root=self.source)

    def test_missing_database_remains_missing_after_failed_preflight(self):
        c=self.load(); self.preflight(c)
        self.assertFalse(c.db.parent.exists())

    def test_production_requires_exact_dedicated_root_and_namespace(self):
        self.value.update(mode="production",root=str(self.source.parent/"runtime/production"),
                          admin_port=8780,parent_port=8781,extraction_port=8792,
                          namespace="aaie_production_")
        prod=Path(self.value["root"]); (prod/"config").mkdir(parents=True)
        self.path=prod/"config/runtime.json"; self.write()
        c=self.load()
        self.assertEqual(c.mode,"production")
        self.assertEqual(c.root,prod)

    def test_invalid_secret_is_not_exposed_by_preflight(self):
        (self.root/"config/field_encryption.key").write_text("DO_NOT_ECHO_SECRET")
        result=self.preflight(self.load())
        self.assertIn("field_key_invalid",result["errors"])
        self.assertNotIn("DO_NOT_ECHO_SECRET",json.dumps(result))

    def test_database_alias_is_rejected(self):
        (self.root/"database").mkdir()
        outside=self.base/"outside.db"; sqlite3.connect(outside).close()
        (self.root/"database/staging.db").symlink_to(outside)
        with self.assertRaises(self.Error): self.load()


    def test_config_changed_after_load_is_rejected(self):
        c=self.load()
        self.value["parent_port"]=29981; self.write()
        with self.assertRaises(self.Error): self.preflight(c)

    def test_database_wal_alias_is_rejected_before_sqlite(self):
        (self.root/"database").mkdir()
        outside=self.base/"outside"; outside.write_text("sentinel")
        (self.root/"database/staging.db-wal").symlink_to(outside)
        with self.assertRaises(self.Error): self.load()

    def test_database_hardlink_is_rejected(self):
        import os
        (self.root/"database").mkdir()
        outside=self.base/"outside.db"; sqlite3.connect(outside).close()
        try:os.link(outside,self.root/"database/staging.db")
        except OSError:self.skipTest("Filesystem does not support hardlinks")
        with self.assertRaises(self.Error):self.load()

    def test_corrupt_database_is_reported_without_mutation(self):
        (self.root/"database").mkdir()
        db=self.root/"database/staging.db"; db.write_bytes(b"corrupt database")
        result=self.preflight(self.load())
        self.assertTrue(any(s.startswith("database_verification_failed:") for s in result["errors"]))
        self.assertEqual(db.read_bytes(),b"corrupt database")

    def test_port_conflict_is_reported(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1",0))
            self.value["admin_port"]=s.getsockname()[1]
            if self.value["admin_port"]>65522:self.skipTest("High ephemeral port")
            self.write()
            result=self.preflight(self.load(),check_ports=True)
            self.assertIn("port_unavailable:"+str(self.value["admin_port"]),result["errors"])

    def test_invalid_mode_types_are_controlled_errors(self):
        for mode in ([],{},None,True):
            self.value["mode"]=mode;self.write()
            with self.assertRaises(self.Error):self.load()

    def test_managed_bootstrap_requires_native_sandbox(self):
        from staging_environment import ManagedRuntime, IsolationError
        with self.assertRaises(IsolationError):
            ManagedRuntime.from_configuration(self.load()).assert_sandbox()


    def test_map_helper_uses_explicit_runtime_and_keeps_legacy_guard(self):
        from unittest.mock import patch, MagicMock
        import school_map_tiles
        args=["school_map_tiles.py","--root",str(self.root/"map_tile_service"),
              "--port","29893","--runtime-config",str(self.path)]
        server=MagicMock()
        with patch.object(school_map_tiles,"__file__",str(self.source/"school_map_tiles.py")), \
             patch("sys.argv",args),patch.object(school_map_tiles,"make_server",return_value=server) as make:
            school_map_tiles.main()
            make.assert_called_once_with(self.root/"map_tile_service",29893)
        with patch.object(school_map_tiles,"__file__",str(self.source/"school_map_tiles.py")), \
             patch("sys.argv",args[:-2]),patch("sys.stderr"):
            with self.assertRaises(SystemExit):school_map_tiles.main()

if __name__ == "__main__":
    unittest.main()
