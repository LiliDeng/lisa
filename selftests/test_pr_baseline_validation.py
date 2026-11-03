# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "run_selected_cases", SCRIPTS / "run_selected_cases.py"
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
sys.path.remove(str(SCRIPTS))


class BaselineValidationTest(unittest.TestCase):
    def test_comparison_matrix(self) -> None:
        head = {"status": "failed", "signature": "same"}
        for status, signature, verdict in [
            ("passed", "", "suspected_regression"),
            ("failed", "same", "same_failure"),
            ("failed", "other", "different_failure"),
            ("error", "same", "different_failure"),
            ("skipped", "", "inconclusive"),
            ("missing", "", "inconclusive"),
            ("inconsistent", "", "inconclusive"),
        ]:
            with self.subTest(status=status, signature=signature):
                self.assertEqual(
                    runner.compare_outcomes(
                        head, {"status": status, "signature": signature}
                    ),
                    verdict,
                )
        self.assertEqual(
            runner.compare_outcomes(
                {"status": "failed", "signature": ""},
                {"status": "failed", "signature": ""},
            ),
            "different_failure",
        )

    def test_junit_signatures_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = directory / "lisa.junit.xml"
            root = ElementTree.Element("testsuite")
            case = ElementTree.SubElement(
                root, "testcase", classname="CPU", name="verify_count (lisa_0_0)"
            )
            failure = ElementTree.SubElement(case, "failure", message="Expected 2")
            failure.text = 'File "/head/test.py", line 10\nAssertionError: Expected 2'
            ElementTree.ElementTree(root).write(report)
            head = runner.read_outcomes(directory, ["CPU.verify_count"])
            failure.text = 'File "/base/test.py", line 20\nAssertionError: Expected 2'
            ElementTree.ElementTree(root).write(report)
            base = runner.read_outcomes(directory, ["CPU.verify_count"])
            self.assertEqual(head, base)
            failure.set("message", "Expected 3")
            ElementTree.ElementTree(root).write(report)
            changed = runner.read_outcomes(directory, ["CPU.verify_count"])
            self.assertNotEqual(head, changed)
            with self.assertRaisesRegex(ValueError, "unexpected failed"):
                runner.read_outcomes(directory, ["CPU.missing"])
            case.remove(failure)
            ElementTree.SubElement(case, "skipped")
            ElementTree.ElementTree(root).write(report)
            self.assertEqual(
                runner.read_outcomes(directory, ["CPU.verify_count"])[
                    "CPU.verify_count"
                ]["status"],
                "skipped",
            )
            self.assertEqual(
                runner.read_outcomes(directory, ["CPU.missing"])["CPU.missing"][
                    "status"
                ],
                "missing",
            )
            report.write_text("<invalid>")
            with self.assertRaises(ElementTree.ParseError):
                runner.read_outcomes(directory, ["CPU.verify_count"])

    def test_exact_case_pattern(self) -> None:
        import re

        pattern = runner.case_pattern(["CPU.verify_count", "CPU.verify_other"])
        self.assertIsNotNone(re.search(pattern, "verify_count"))
        self.assertIsNone(re.search(pattern, "verify_count_extra"))

    def test_deployment_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = directory / "arm_template_parameters.json"
            report.write_text(
                json.dumps({"nodes": {"value": [{"vm_size": "Standard_D2ds_v5"}]}})
            )
            self.assertEqual(runner.deployment_sizes(directory), ["Standard_D2ds_v5"])
            report.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "value": [
                                {"vm_size": "Standard_D2ds_v5"},
                                {"vm_size": "Standard_D4ds_v5"},
                            ]
                        }
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "heterogeneous"):
                runner.deployment_sizes(directory)

    def test_deployment_signature_ignores_identity_not_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = directory / "arm_template_parameters.json"
            node = {"vm_size": "Standard_D2ds_v5", "name": "head"}
            report.write_text(json.dumps({"nodes": {"value": [node]}}))
            head = runner.deployment_signature(directory)
            node["name"] = "base"
            report.write_text(json.dumps({"nodes": {"value": [node]}}))
            self.assertEqual(head, runner.deployment_signature(directory))
            report.write_text(
                json.dumps({"nodes": {"value": [{**node, "osdisk_size_in_gb": 128}]}})
            )
            self.assertNotEqual(head, runner.deployment_signature(directory))

    def test_prepare_baseline_uses_isolated_install_and_fixed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(runner.subprocess, "run") as run:
                runner.prepare_baseline(root, "a" * 40, root / "baseline")
                self.assertIn("a" * 40, run.call_args_list[0].args[0])
                self.assertIn("--detach", run.call_args_list[0].args[0])
                self.assertEqual(run.call_args_list[1].args[0][1:3], ["-m", "venv"])
                self.assertEqual(
                    run.call_args_list[2].args[0][-3:], ["install", "-e", ".[azure]"]
                )
                self.assertEqual(
                    run.call_args_list[2].kwargs["cwd"], root / "baseline" / "source"
                )

    def test_execute_does_not_discard_failed_junit_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            failed = {"CPU.verify_count": {"status": "failed", "signature": "same"}}
            with patch.object(
                runner, "azure_json", return_value={"accessToken": "test"}
            ):
                with patch.object(
                    runner.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1),
                ) as run:
                    with patch.object(runner, "read_outcomes", return_value=failed):
                        self.assertEqual(
                            runner.execute_group(
                                "lisa",
                                root,
                                root / "head",
                                "runbook.yml",
                                "westus3",
                                "image 1 2 3",
                                ["CPU.verify_count"],
                                "subscription",
                            ),
                            failed,
                        )
                    command = run.call_args.args[0]
                    self.assertIn("keep_environment:no", command)
                    self.assertIn("wait_delete:True", command)
                    self.assertIn("resource_group_name:", command)
                    self.assertEqual(run.call_args.kwargs["cwd"], root)
                    self.assertEqual(
                        run.call_args.kwargs["env"]["PYTHONPATH"], str(root)
                    )

    def test_failure_triggered_orchestration(self) -> None:
        for base_status, expected_status in [
            ("failed", "same_failure"),
            ("passed", "failed"),
            ("skipped", "failed"),
            ("missing", "failed"),
            ("setup_failure", "failed"),
            ("topology_mismatch", "failed"),
            (None, "passed"),
        ]:
            with self.subTest(base_status=base_status):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    scripts = root / ".github" / "scripts"
                    scripts.mkdir(parents=True)
                    plan = root / "plan.json"
                    plan.write_text(
                        json.dumps(
                            {
                                "status": "selected",
                                "groups": [
                                    {
                                        "marketplace_image": "image 1 2 latest",
                                        "cases": [
                                            "CPU.verify_count",
                                            "CPU.verify_other",
                                        ],
                                        "required_cases": ["CPU.verify_count"],
                                    }
                                ],
                            }
                        )
                    )
                    passing = {"status": "passed", "signature": ""}
                    head = {
                        "CPU.verify_count": (
                            passing
                            if base_status is None
                            else {"status": "failed", "signature": "same"}
                        ),
                        "CPU.verify_other": passing,
                    }
                    calls = [head]
                    if base_status:
                        calls.append(
                            {
                                "CPU.verify_count": {
                                    "status": base_status,
                                    "signature": "same"
                                    if base_status == "failed"
                                    else "",
                                }
                            }
                        )
                    with patch.object(
                        runner, "__file__", str(scripts / "runner.py")
                    ), patch.object(
                        runner, "reviewed_images", return_value=["image 1 2 latest"]
                    ), patch.object(
                        runner, "resolve_image", return_value="image 1 2 3"
                    ), patch.object(
                        runner, "azure_json", return_value={"id": "sub"}
                    ), patch.object(
                        runner.subprocess, "check_output", return_value="b" * 40
                    ), patch.object(
                        runner.subprocess, "run"
                    ), patch.object(
                        runner, "deployment_sizes", return_value=["Standard_D2ds_v5"]
                    ), patch.object(
                        runner, "deployment_signature", return_value="configuration"
                    ), patch.object(
                        runner, "prepare_baseline", return_value=root / "venv" / "lisa"
                    ) as prepare, patch.object(
                        runner, "execute_group", side_effect=calls
                    ) as execute:
                        if expected_status == "failed":
                            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                                if base_status == "setup_failure":
                                    prepare.side_effect = RuntimeError(
                                        "Baseline installation failed"
                                    )
                                if base_status == "topology_mismatch":
                                    runner.deployment_sizes.side_effect = [
                                        ["Standard_D2ds_v5"],
                                        ["Standard_D4ds_v5"],
                                    ]
                                runner.run_plan(
                                    plan, "runbook.yml", "westus3", "a" * 40
                                )
                        else:
                            runner.run_plan(plan, "runbook.yml", "westus3", "a" * 40)
                        results = json.loads(
                            plan.with_name("pr_validation_results.json").read_text()
                        )
                        self.assertEqual(results[0]["status"], expected_status)
                        self.assertEqual(
                            prepare.call_count, int(base_status is not None)
                        )
                        self.assertEqual(
                            execute.call_count,
                            (
                                1
                                + int(
                                    base_status is not None
                                    and base_status != "setup_failure"
                                )
                            ),
                        )
                        if base_status and base_status != "setup_failure":
                            self.assertEqual(
                                execute.call_args.args[6], ["CPU.verify_count"]
                            )
                            self.assertEqual(
                                execute.call_args.args[4:6],
                                ("westus3", "image 1 2 3"),
                            )


if __name__ == "__main__":
    unittest.main()
