# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


class MetadataImportTestCase(unittest.TestCase):
    def run_probe(self, source: str) -> None:
        setup = (
            "import sys\n"
            "from pathlib import Path\n"
            f"root = Path({str(REPO_ROOT)!r})\n"
            "sys.path.insert(0, str(root))\n"
            "sys.path.insert(0, str(root / '.github' / 'scripts'))\n"
            "from validation_plan import load_metadata\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", setup + textwrap.dedent(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selected_suites_with_builtin_extension_imports(self) -> None:
        self.run_probe(
            """
            from ai_select_cases import find_testsuites_dir, list_test_cases
            inventory = list_test_cases(root, find_testsuites_dir(root))
            suite_path = "lisa/microsoft/testsuites/xfstests/xfstesting.py"
            cases = {
                name: path for path, _, name, _, _, _ in inventory
                if path == suite_path
            }
            assert len(cases) == 12, cases
            cases["verify_device_channels_change"] = (
                "lisa/microsoft/testsuites/network/networksettings.py"
            )
            metadata = load_metadata(root, cases)
            assert set(metadata) == set(cases), metadata
            assert metadata["verify_btrfs_nvme_datadisk"].full_name == (
                "Xfstesting.verify_btrfs_nvme_datadisk"
            )
            assert "microsoft.testsuites.xfstests.xfstests" in sys.modules
            assert "microsoft.testsuites.network.common" in sys.modules
            assert "microsoft.testsuites.gpu.gpusuite" not in sys.modules
            assert "lisa.microsoft.testsuites.gpu.gpusuite" not in sys.modules
            """
        )

    def test_missing_case_still_fails(self) -> None:
        self.run_probe(
            """
            try:
                load_metadata(root, {
                    "missing_case": "lisa/microsoft/testsuites/xfstests/xfstesting.py"
                })
            except ValueError as error:
                assert "missing or ambiguous" in str(error), error
            else:
                raise AssertionError("Missing case was accepted")
            """
        )

    def test_foreign_suite_still_fails(self) -> None:
        self.run_probe(
            """
            import types
            name = "lisa.microsoft.testsuites.xfstests.xfstesting"
            module = types.ModuleType(name)
            module.__file__ = str(root.parent / "foreign_xfstesting.py")
            sys.modules[name] = module
            try:
                load_metadata(root, {
                    "verify_btrfs_nvme_datadisk":
                    "lisa/microsoft/testsuites/xfstests/xfstesting.py"
                })
            except ValueError as error:
                assert "does not match the repository" in str(error), error
            else:
                raise AssertionError("Foreign suite was accepted")
            """
        )
