import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "run_factor_factory_pipeline.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_factor_factory_pipeline", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PipelineQlibHookTests(unittest.TestCase):
    def test_pipeline_appends_qlib_sidecar_stage_by_default(self):
        module = load_module()
        root = Path("/factor_factory")
        specs = module.build_stage_specs(root, "/usr/bin/python3")

        self.assertEqual(specs[-1]["name"], "qlib_sidecar_round")
        self.assertIn("command", specs[-1])
        self.assertEqual(
            specs[-1]["command"][0],
            str(root / "qlib_lab" / "venv" / "bin" / "python"),
        )
        self.assertIn(str(root / "scripts" / "qlib_adapter" / "run_qlib_sidecar_round.py"), specs[-1]["command"])

    def test_pipeline_threads_overlay_manifest_into_audit_and_qlib_sidecar_stages(self):
        module = load_module()
        root = Path("/factor_factory")
        overlay_manifest = root / "outputs/evolution/parallel_lines/run_x/formal_overlay_contract_inputs.json"

        specs = module.build_stage_specs(root, "/usr/bin/python3", overlay_manifest=overlay_manifest)
        audit_spec = next(spec for spec in specs if spec["name"] == "run_factor_audit")
        qlib_spec = next(spec for spec in specs if spec["name"] == "qlib_sidecar_round")

        self.assertIn("--overlay-manifest", audit_spec["command"])
        self.assertIn(str(overlay_manifest), audit_spec["command"])
        self.assertIn("--overlay-manifest", qlib_spec["command"])
        self.assertIn(str(overlay_manifest), qlib_spec["command"])

    def test_pipeline_can_skip_qlib_sidecar_for_emergency_runs(self):
        module = load_module()
        specs = module.build_stage_specs(Path("/factor_factory"), "/usr/bin/python3", include_qlib=False)

        self.assertNotIn("qlib_sidecar_round", [spec["name"] for spec in specs])


if __name__ == "__main__":
    unittest.main()
