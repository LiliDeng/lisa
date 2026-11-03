# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import importlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    selector = importlib.import_module("ai_select_cases")
    plans = importlib.import_module("validation_plan")
    runner = importlib.import_module("run_selected_cases")
finally:
    sys.path.remove(str(SCRIPTS))

ROOT = SCRIPTS.parents[1]
OPENVMM = {
    "verify_openvmm_upstream_vmm_tests": (
        "lisa/microsoft/testsuites/openvmm/openvmm_tests.py"
    ),
    "verify_openvmm_guest_boot": "lisa/microsoft/testsuites/openvmm/openvmm.py",
    "verify_openvmm_restart_via_platform": (
        "lisa/microsoft/testsuites/openvmm/openvmm.py"
    ),
    "verify_openvmm_stop_start_in_platform": (
        "lisa/microsoft/testsuites/openvmm/openvmm.py"
    ),
}


class ManualValidationTestCase(TestCase):
    def test_real_openvmm_metadata_produces_actionable_recommendations(self) -> None:
        with patch.object(plans, "reviewed_images") as reviewed:
            plan = plans.build_validation_plan(ROOT, OPENVMM, list(OPENVMM), {}, {}, [])
        reviewed.assert_not_called()
        self.assertEqual("manual_required", plan["status"])
        self.assertEqual([], plan["groups"])
        recommendations = plan["recommendations"]
        metadata = plans.load_metadata(ROOT, OPENVMM)
        self.assertEqual(
            {case.full_name for case in metadata.values()},
            {item["case"] for item in recommendations},
        )
        self.assertTrue(all(item["required"] for item in recommendations))
        self.assertTrue(
            all(item["execution_status"] == "not_executed" for item in recommendations)
        )
        upstream = next(
            item for item in recommendations if "upstream_vmm" in item["case"]
        )
        self.assertIn("/dev/mshv", upstream["requirements"])
        self.assertIn("Intel", upstream["requirements"])
        self.assertIn("OpenVmmGuestNode", recommendations[1]["requirements"])

    def test_known_dedicated_suites_and_metadata_platform_gate(self) -> None:
        metadata = plans.load_metadata(ROOT, OPENVMM)
        case = metadata["verify_openvmm_guest_boot"]
        for folder, platform in plans.NON_AZURE_SUITES.items():
            with self.subTest(folder=folder):
                self.assertEqual(
                    platform,
                    plans.manual_requirements(
                        f"lisa/microsoft/testsuites/{folder}/tests.py", case
                    )[0],
                )
        with patch.object(plans, "_is_platform_compatible", return_value=False):
            self.assertEqual(
                "Non-Azure platform",
                plans.manual_requirements(
                    "lisa/microsoft/testsuites/new/tests.py", case
                )[0],
            )

    def test_mixed_platform_plan_preserves_every_case_as_deferred(self) -> None:
        files = dict(OPENVMM)
        files["verify_cpu_count"] = "lisa/microsoft/testsuites/core/cpu.py"
        plan = plans.build_validation_plan(
            ROOT, files, list(files), {"verify_cpu_count": "Changed CPU helper"}, {}, []
        )
        self.assertEqual("manual_required", plan["status"])
        self.assertEqual([], plan["groups"])
        cpu = next(
            item
            for item in plan["recommendations"]
            if item["case"].endswith("verify_cpu_count")
        )
        self.assertEqual("Azure", cpu["platform"])
        self.assertEqual("Changed CPU helper", cpu["reason"])
        self.assertIn("deferred", cpu["requirements"])

    def test_azure_only_selection_and_comment_only_behavior_are_unchanged(self) -> None:
        files = {"verify_cpu_count": "lisa/microsoft/testsuites/core/cpu.py"}
        image = "canonical 0001-com-ubuntu-server-jammy 22_04-lts-gen2 latest"
        plan = plans.build_validation_plan(ROOT, files, list(files), {}, {}, [image])
        self.assertEqual("selected", plan["status"])
        self.assertEqual(["CPU.verify_cpu_count"], plan["groups"][0]["cases"])
        self.assertNotIn("recommendations", plan)
        self.assertEqual(
            "not_required",
            plans.build_validation_plan(ROOT, {}, [], {}, {}, [])["status"],
        )

    def test_manual_runner_refuses_before_azure_or_deployment(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "plan.json"
            path.write_text(
                json.dumps({"status": "manual_required", "groups": []}),
                encoding="utf-8",
            )
            with patch.object(runner, "azure_json") as azure:
                with self.assertRaisesRegex(ValueError, "NOT EXECUTED"):
                    runner.run_plan(path, "unused", "unused")
            azure.assert_not_called()

    def test_selector_outputs_no_executable_cases_and_explicit_recommendations(
        self,
    ) -> None:
        inventory = [
            (path, "openvmm", name, 5, (), ()) for name, path in OPENVMM.items()
        ]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "selection.yml"
            github_output = root / "github-output"
            summary = root / "summary"
            with patch.dict(
                "os.environ",
                {
                    "PR_VALIDATION_IMAGES": "[]",
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            ), patch.object(selector, "find_repo_root", return_value=ROOT):
                selector.write_outputs(
                    list(OPENVMM), inventory, "", str(output), list(OPENVMM)
                )
            self.assertEqual([], yaml.safe_load(output.read_text())["testcase"])
            self.assertIn("case_count=0", github_output.read_text())
            self.assertIn("selection_status=manual_required", github_output.read_text())
            self.assertIn("NOT EXECUTED", summary.read_text())
            self.assertIn("/dev/mshv", summary.read_text())
            self.assertEqual(
                "manual_required",
                json.loads(output.with_suffix(".json").read_text())["status"],
            )

    def test_pipeline_publishes_manual_plan_and_disables_azure_execution(self) -> None:
        pipeline = yaml.safe_load(
            (ROOT / ".azuredevops" / "azure-pipelines.yml").read_text(encoding="utf-8")
        )
        steps = pipeline["jobs"][0]["steps"]
        read_step = next(
            step for step in steps if step.get("displayName") == "Read AI Selection"
        )
        code = read_step["script"].split("python <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        compile(code, "Read AI Selection", "exec")
        plan = plans.build_validation_plan(ROOT, OPENVMM, list(OPENVMM), {}, {}, [])
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ai_selection").mkdir()
            (root / "ai_selection" / "ai_selected_cases.json").write_text(
                json.dumps(plan), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("variable=CASE_COUNT]0", result.stdout)
            self.assertIn("variable=SELECTION_STATUS]manual_required", result.stdout)
            self.assertIn("variable=RECOMMENDED_CASE_COUNT]4", result.stdout)
            self.assertIn("/dev/mshv", result.stdout)
            self.assertIn(
                "NOT EXECUTED",
                (root / "ai_selection" / "manual-recommendations.md").read_text(),
            )
        for step in steps:
            if step.get("task") in ("AzureCLI@2", "PublishTestResults@2"):
                self.assertIn("gt(variables['CASE_COUNT'], 0)", step["condition"])
        gate = next(
            step
            for step in steps
            if step.get("displayName") == "Gate dedicated-platform validation"
        )
        self.assertIn(
            "eq(variables['SELECTION_STATUS'], 'manual_required')", gate["condition"]
        )
        self.assertIn("exit 1", gate["script"])
        self.assertLess(
            steps.index(gate),
            next(
                index
                for index, step in enumerate(steps)
                if step.get("task") == "GitHubComment@0"
            ),
        )
