#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import python_env_bootstrap as bootstrap


class PythonEnvironmentBootstrapTests(unittest.TestCase):
    def test_does_not_inject_binary_packages_from_a_different_python_minor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            incompatible = root / "lib/python99.1/site-packages"
            incompatible.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"STOCK_SYSTEM_VENV": str(root)}):
                result = bootstrap.bootstrap_openclaw_venv()
            self.assertEqual(result["injected_site_packages"], [])
            self.assertNotIn(str(incompatible), sys.path)

    def test_injects_only_the_site_packages_matching_the_running_interpreter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            compatible = root / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            compatible.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"STOCK_SYSTEM_VENV": str(root)}):
                result = bootstrap.bootstrap_openclaw_venv()
            try:
                self.assertEqual(result["injected_site_packages"], [str(compatible)])
                self.assertIn(str(compatible), sys.path)
            finally:
                while str(compatible) in sys.path:
                    sys.path.remove(str(compatible))


if __name__ == "__main__":
    unittest.main()
