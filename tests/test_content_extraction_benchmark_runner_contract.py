import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.content_extraction_real_benchmark import main


class BenchmarkRunnerContractTests(unittest.TestCase):
    def test_benchmark_executes_the_production_runner_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "source.mp4"
            video.write_bytes(b"video")
            output = root / "output"
            calls = []

            class Runner:
                def run(self, run_id):
                    calls.append(("runner", run_id))
                    return SimpleNamespace(
                        status="completed", current_stage="completed",
                        accurate_status="AUTO_COMPLETE", accounting={}, error_code="",
                    )

            def runner_builder(repository, workspace, environ, workflow):
                calls.append(("builder", Path(workspace), environ["BILLING_MODE"], workflow))
                return Runner()

            exit_code = main(
                ["--video", str(video), "--output-root", str(output)],
                runner_builder=runner_builder,
                local_workflow_builder=lambda *_args, **_kwargs: "workflow",
            )

            report = json.loads((output / "benchmark-result.json").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual([item[0] for item in calls], ["builder", "runner"])
            self.assertEqual(report["execution_path"], "ServicePipelineRunner")
            self.assertEqual(report["pipeline"]["current_stage"], "completed")
            self.assertFalse(report["cloud_execution"])
            self.assertEqual(report["estimated_cost"], 0)


if __name__ == "__main__":
    unittest.main()
