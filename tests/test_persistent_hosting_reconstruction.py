import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PersistentHostingReconstructionTests(unittest.TestCase):
    def test_main_source_reconstructs_all_three_independent_jobs(self):
        admin_parent = load_tool("production_launchd_control")
        extraction = load_tool("content_extraction_launchd_control")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "home"
            production = root / "Volumes/Zhang/AAIE CodeX/CRM"
            production.mkdir(parents=True)
            head = "c" * 40
            service_python = root / "runtime/python3"

            first = admin_parent.install_artifacts(
                source_root=ROOT, home=home, production_root=production,
                expected_heads=(head, "d" * 40), service_python=service_python,
            )
            second = extraction.install_artifacts(
                source_root=ROOT, home=home, production_root=production,
                expected_heads=(head, "d" * 40), volume_root=root / "Volumes/Zhang",
            )

            self.assertEqual(len(first + second), 6)
            labels = {
                plistlib.loads(path.read_bytes())["Label"]
                for path in first + second if path.suffix == ".plist"
            }
            self.assertEqual(labels, {
                "com.aaie.crm.production.admin",
                "com.aaie.crm.production.parent",
                "com.aaie.crm.production.content-extraction",
            })
            for path in first + second:
                self.assertTrue(path.is_file())
                self.assertNotIn("._", path.name)


if __name__ == "__main__":
    unittest.main()
