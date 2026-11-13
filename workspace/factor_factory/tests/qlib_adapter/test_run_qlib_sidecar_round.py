import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "qlib_adapter"
    / "run_qlib_sidecar_round.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("run_qlib_sidecar_round", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunQlibSidecarRoundTests(unittest.TestCase):
    def test_build_steps_orders_export_before_recording(self):
        module = load_module()
        steps = module.build_steps(Path("/root"), Path("/venv/bin/python"), "round-a")
        names = [step["name"] for step in steps]
        self.assertEqual(
            names,
            [
                "export_panel_to_qlib",
                "build_qlib_dataset_config",
                "run_qlib_factor_arena",
                "sync_qlib_results_to_factor_factory",
                "record_factor_factory_round",
            ],
        )
        self.assertIn("--round-name", steps[-1]["cmd"])
        self.assertIn("round-a", steps[-1]["cmd"])

    def test_build_steps_passes_overlay_manifest_to_recorder(self):
        module = load_module()
        overlay_manifest = Path("/root/outputs/evolution/parallel_lines/run_x/formal_overlay_contract_inputs.json")

        steps = module.build_steps(Path("/root"), Path("/venv/bin/python"), "round-a", overlay_manifest=overlay_manifest)

        self.assertIn("--overlay-manifest", steps[-1]["cmd"])
        self.assertIn(str(overlay_manifest), steps[-1]["cmd"])


if __name__ == "__main__":
    unittest.main()
