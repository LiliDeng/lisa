# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest import TestCase
from unittest.mock import patch

import yaml

from lisa import schema
from lisa.operating_system import Suse, Ubuntu
from lisa.testselector import select_testcases
from lisa.testsuite import TestCaseMetadata, TestSuiteMetadata, simple_requirement

_ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / ".github" / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_PLAN = _script("validation_plan")
_SELECT = _script("ai_select_cases")
_RUN = _script("run_selected_cases")
_UBUNTU = "canonical 0001-com-ubuntu-server-jammy 22_04-lts-gen2 latest"
_SUSE = "suse sles-15-sp6 gen2 latest"
_SUSE_ARM = "suse sles-15-sp6-arm64 gen2 latest"


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def _case(name: str, os_name: str = "", maturity: str = "stable") -> TestCaseMetadata:
    classes = {"suse": Suse, "ubuntu": Ubuntu}
    requirement = simple_requirement(
        supported_os=[classes[os_name]] if os_name else None
    )
    case = TestCaseMetadata(
        description="selector fixture", requirement=requirement, maturity=maturity
    )
    case.name = name
    case.full_name = f"SelectorFixture.{name}"
    case.suite = TestSuiteMetadata(
        area="core",
        category="functional",
        description="fixture",
        name="SelectorFixture",
    )
    return case


class ValidationPlanTestCase(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def build(
        self,
        cases: Dict[str, TestCaseMetadata],
        targets: List[str],
        context: str = "",
        explicit: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        with patch.object(_PLAN, "load_metadata", return_value=cases), patch.object(
            _PLAN, "reviewed_images", return_value=[_UBUNTU, _SUSE, _SUSE_ARM]
        ), patch.object(
            _PLAN, "changed_os_context", return_value=(set(targets), context)
        ):
            plan: Dict[str, Any] = _PLAN.build_validation_plan(
                self.root,
                {name: "fixture.py" for name in cases},
                list(cases),
                {name: "Direct modification" for name in cases},
                {},
                explicit or [],
            )
            return plan

    def test_suse_change_never_defaults_to_ubuntu(self) -> None:
        plan = self.build({"verify_common": _case("verify_common")}, ["Suse"])
        self.assertEqual([_SUSE], [g["marketplace_image"] for g in plan["groups"]])
        self.assertEqual(
            ["SelectorFixture.verify_common"], plan["groups"][0]["required_cases"]
        )

    def test_mixed_os_cases_get_separate_compatible_groups(self) -> None:
        plan = self.build(
            {
                "verify_suse": _case("verify_suse", "suse"),
                "verify_ubuntu": _case("verify_ubuntu", "ubuntu"),
            },
            ["Suse", "Ubuntu"],
        )
        by_image = {
            group["marketplace_image"]: group["cases"] for group in plan["groups"]
        }
        self.assertEqual(["SelectorFixture.verify_suse"], by_image[_SUSE])
        self.assertEqual(["SelectorFixture.verify_ubuntu"], by_image[_UBUNTU])

    def test_os_allowlist_selects_compatible_image_without_diff_signal(self) -> None:
        plan = self.build({"verify_suse": _case("verify_suse", "suse")}, [])
        self.assertEqual(_SUSE, plan["groups"][0]["marketplace_image"])

    def test_incompatible_target_is_a_coverage_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "affected OS Suse"):
            self.build({"verify_ubuntu": _case("verify_ubuntu", "ubuntu")}, ["Suse"])

    def test_unreviewed_distro_cannot_fall_back_to_ubuntu(self) -> None:
        with self.assertRaisesRegex(ValueError, "CoreOs"):
            self.build({"verify_common": _case("verify_common")}, ["CoreOs"])

    def test_arm_suse_image_is_selected(self) -> None:
        plan = self.build(
            {"verify_common": _case("verify_common")}, ["Suse"], "aarch64"
        )
        self.assertEqual(_SUSE_ARM, plan["groups"][0]["marketplace_image"])

    def test_missing_arm_gen1_image_does_not_ignore_generation(self) -> None:
        with self.assertRaisesRegex(ValueError, "architecture/generation"):
            self.build(
                {"verify_common": _case("verify_common")}, ["Suse"], "arm64 gen1"
            )

    def test_version_branch_requires_explicit_reviewed_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "Version-specific"):
            self.build(
                {"verify_common": _case("verify_common")},
                ["Suse"],
                "node.os.information.version >= parse_version('15.0')",
            )
        plan = self.build(
            {"verify_common": _case("verify_common")},
            ["Suse"],
            "parse_version('15.0')",
            [_SUSE],
        )
        self.assertEqual(_SUSE, plan["groups"][0]["marketplace_image"])

    def test_explicit_image_must_cover_changed_os(self) -> None:
        with self.assertRaisesRegex(ValueError, "affected OS Suse"):
            self.build(
                {"verify_common": _case("verify_common")}, ["Suse"], explicit=[_UBUNTU]
            )

    def test_explicit_images_all_run(self) -> None:
        plan = self.build(
            {"verify_common": _case("verify_common")}, [], explicit=[_UBUNTU, _SUSE]
        )
        self.assertEqual(2, len(plan["groups"]))

    def test_catalog_is_repository_reviewed_list(self) -> None:
        images = _PLAN.reviewed_images(_ROOT)
        self.assertIn(_SUSE, images)
        self.assertIn("suse sles-12-sp5 gen2 latest", images)
        self.assertTrue(all(len(image.split()) == 4 for image in images))
        suse = [image for image in images if _PLAN._matches_os(image, "Suse")]
        self.assertEqual(_SUSE_ARM, suse[0])
        self.assertEqual(_SUSE, next(image for image in suse if "arm64" not in image))

    def test_debian_change_does_not_count_ubuntu_as_debian(self) -> None:
        self.assertFalse(_PLAN._matches_os(_UBUNTU, "Debian"))
        self.assertTrue(_PLAN._matches_os("debian debian-12 12-gen2 latest", "Debian"))

    def test_class_context_ignores_unrelated_os(self) -> None:
        path = self.root / "lisa" / "operating_system.py"
        path.parent.mkdir()
        path.write_text(
            "class Suse:\n    def update(self):\n        command = 'zypper update'\n"
            "class Ubuntu:\n    def update(self):\n        command = 'apt upgrade'\n",
            encoding="utf-8",
        )
        names, _ = _PLAN.changed_os_context(
            self.root, {"lisa/operating_system.py": {3}}
        )
        self.assertEqual({"Suse"}, names)

    def test_enclosing_os_branch_is_detected(self) -> None:
        path = self.root / "tool.py"
        path.write_text(
            "from lisa.operating_system import Suse, Ubuntu\n"
            "def install(node):\n"
            "    if isinstance(node.os, Suse):\n"
            "        node.execute('zypper install tool')\n",
            encoding="utf-8",
        )
        names, _ = _PLAN.changed_os_context(self.root, {"tool.py": {4}})
        self.assertEqual({"Suse"}, names)

    def test_elif_does_not_select_preceding_ubuntu_branch(self) -> None:
        path = self.root / "tool.py"
        path.write_text(
            "def install(node):\n"
            "    if isinstance(node.os, Ubuntu):\n"
            "        node.execute('apt install tool')\n"
            "    elif isinstance(node.os, operating_system.Suse):\n"
            "        node.execute('zypper install tool')\n",
            encoding="utf-8",
        )
        names, _ = _PLAN.changed_os_context(self.root, {"tool.py": {5}})
        self.assertEqual({"Suse"}, names)

    def test_else_branch_requires_explicit_images(self) -> None:
        path = self.root / "tool.py"
        path.write_text(
            "def install(node):\n"
            "    if isinstance(node.os, Ubuntu):\n"
            "        node.execute('apt install tool')\n"
            "    else:\n"
            "        node.execute('other install tool')\n",
            encoding="utf-8",
        )
        names, context = _PLAN.changed_os_context(self.root, {"tool.py": {5}})
        self.assertEqual(set(), names)
        with self.assertRaisesRegex(ValueError, "else OS branch"):
            _PLAN._image_candidates([_UBUNTU, _SUSE], context, [])


class RequiredSelectionTestCase(TestCase):
    def test_private_os_update_maps_through_public_wrapper_and_test_helper(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            os_path = root / "lisa" / "operating_system.py"
            os_path.parent.mkdir()
            os_path.write_text(
                "class Posix:\n"
                "    def update_packages(self):\n"
                "        self._update_packages()\n"
                "class Suse(Posix):\n"
                "    def _update_packages(self):\n"
                "        self.execute('zypper update')\n",
                encoding="utf-8",
            )
            (root / "suite.py").write_text(
                "class Suite:\n"
                "    def _upgrade(self, node):\n"
                "        node.os.update_packages('')\n"
                "    @TestCaseMetadata(priority=2)\n"
                "    def verify_os_update(self, node):\n"
                "        self._upgrade(node)\n",
                encoding="utf-8",
            )
            inventory = [("suite.py", "core", "verify_os_update", 2, (), ())]
            obligations = _SELECT.get_os_method_obligations(
                root, {"lisa/operating_system.py": {6}}, inventory
            )
            self.assertEqual({"update_packages": ["verify_os_update"]}, obligations)
            required, reasons = _SELECT.ensure_required_coverage(
                [], [], inventory, (), (), obligations
            )
            self.assertEqual(["verify_os_update"], required)
            self.assertIn(
                "Changed OS method update_packages", reasons["verify_os_update"]
            )

    def test_feature_and_tool_obligations_survive_ai_omission(self) -> None:
        inventory = [
            ("a.py", "core", "verify_easy", 0, ("Disk",), ()),
            ("b.py", "core", "stress_required", 3, ("Gpu",), ("Hwclock",)),
        ]
        required, reasons = _SELECT.ensure_required_coverage(
            ["verify_easy"], [], inventory, ("Disk", "Gpu"), ("Hwclock",)
        )
        self.assertEqual(["verify_easy", "stress_required"], required)
        self.assertIn("Changed tool Hwclock", reasons["stress_required"])

    def test_direct_stress_case_is_required(self) -> None:
        required, _ = _SELECT.ensure_required_coverage(
            [], ["stress_direct"], [], (), ()
        )
        self.assertEqual(["stress_direct"], required)

    def test_missing_mapping_is_explicit_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "manual validation"):
            _SELECT.ensure_required_coverage([], [], [], ("Gpu",), ())

    def test_metadata_change_is_detected_as_direct_test_change(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "suite.py"
            path.write_text(
                "class Suite:\n"
                "    @TestCaseMetadata(priority=3)\n"
                "    def stress_direct(self):\n"
                "        pass\n",
                encoding="utf-8",
            )
            cases, shared = _SELECT._get_test_method_changes(
                Path(directory), "suite.py", {2}
            )
            self.assertEqual(["stress_direct"], cases)
            self.assertFalse(shared)

    def test_deletion_marks_new_file_anchor(self) -> None:
        diff = "--- a/tool.py\n+++ b/tool.py\n@@ -3,1 +3,0 @@\n-    old()\n"
        self.assertEqual({"tool.py": {3}}, _SELECT._extract_changed_new_lines(diff))

    def test_multiple_changed_files_are_accumulated(self) -> None:
        inventory = [
            (
                "lisa/microsoft/testsuites/core/first.py",
                "core",
                "verify_first",
                1,
                (),
                (),
            ),
            (
                "lisa/microsoft/testsuites/core/second.py",
                "core",
                "verify_second",
                1,
                (),
                (),
            ),
        ]
        with patch.object(
            _SELECT, "_changed_lines_for_file", return_value={1}
        ), patch.object(
            _SELECT,
            "_get_test_method_changes",
            side_effect=[(["verify_first"], False), (["verify_second"], False)],
        ):
            result = _SELECT.get_testsuite_related_cases(
                inventory, "\n".join(item[0] for item in inventory)
            )
        self.assertEqual(["verify_first", "verify_second"], result)

    def test_no_code_changes_produce_explicit_not_required_plan(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "selected.yml"
            with patch.dict(
                os.environ,
                {
                    "PR_CHANGED_FILES": "README.rst",
                    "PR_DIFF": "documentation",
                    "PR_DIFF_FILE": "",
                    "RUNBOOK_OUTPUT": str(output),
                    "PR_VALIDATION_IMAGES": "[]",
                    "GITHUB_OUTPUT": "",
                    "GITHUB_STEP_SUMMARY": "",
                },
            ):
                _SELECT.main()
            plan = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual("not_required", plan["status"])
            self.assertEqual([], plan["groups"])


class ValidationExecutionTestCase(TestCase):
    def test_execution_filter_matches_actual_lisa_method_names_and_preview(
        self,
    ) -> None:
        case = _case("verify_case", "suse", maturity="preview")
        selected = select_testcases(
            filters=[
                schema.TestCase(
                    criteria=schema.Criteria(
                        name=_RUN.case_pattern([case.full_name]),
                        maturity=["stable", "preview"],
                    )
                )
            ],
            init_cases=[case],
            target_os=Suse,
            target_platforms=["azure"],
        )
        self.assertEqual(
            [case.full_name], [item.metadata.full_name for item in selected]
        )

    def test_unavailable_image_prevents_all_guest_runs(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "selected",
                        "groups": [
                            {
                                "marketplace_image": _SUSE,
                                "cases": ["Suite.verify_case"],
                                "required_cases": ["Suite.verify_case"],
                                "reasons": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                _RUN,
                "resolve_image",
                side_effect=RuntimeError("Marketplace image unavailable"),
            ), patch.object(_RUN.subprocess, "run") as process:
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    _RUN.run_plan(path, "runbook.yml", "westus3")
                process.assert_not_called()

    def test_image_latest_resolves_to_concrete_version(self) -> None:
        with patch.object(_RUN, "azure_json", return_value={"name": "2026.10.28"}):
            self.assertEqual(
                "suse sles-15-sp6 gen2 2026.10.28",
                _RUN.resolve_image(_SUSE, "westus3"),
            )

    def test_unresolved_image_is_error(self) -> None:
        with patch.object(_RUN, "azure_json", return_value={"name": "latest"}):
            with self.assertRaisesRegex(ValueError, "concrete version"):
                _RUN.resolve_image(_SUSE, "westus3")

    def test_junit_requires_pass_for_each_planned_case(self) -> None:
        for result in ("", "<skipped/>", "<failure/>", "<error/>"):
            with self.subTest(result=result), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "lisa.junit.xml").write_text(
                    '<testsuites><testsuite><testcase classname="Suite.verify_case" '
                    f'name="verify_case (1)">{result}</testcase>'
                    "</testsuite></testsuites>",
                    encoding="utf-8",
                )
                if result:
                    with self.assertRaisesRegex(ValueError, "skipped, attempted"):
                        _RUN.verify_results(root, ["Suite.verify_case"])
                else:
                    _RUN.verify_results(root, ["Suite.verify_case"])
                with self.assertRaisesRegex(ValueError, "missing"):
                    _RUN.verify_results(root, ["Suite.other_case"])

    def test_missing_junit_is_error(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No JUnit"):
                _RUN.verify_results(Path(directory), ["Suite.verify_case"])

    def test_runner_continues_other_groups_but_fails_incomplete_validation(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "status": "selected",
                        "groups": [
                            {
                                "marketplace_image": image,
                                "cases": ["Suite.verify_case"],
                                "required_cases": ["Suite.verify_case"],
                                "reasons": {},
                            }
                            for image in [_UBUNTU, _SUSE]
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with _working_directory(root), patch.object(
                _RUN, "resolve_image", side_effect=lambda image, _: image
            ), patch.object(
                _RUN,
                "azure_json",
                return_value={"id": "test-subscription", "accessToken": "test-token"},
            ), patch.object(
                _RUN.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as process, patch.object(
                _RUN,
                "verify_results",
                side_effect=[ValueError("test was skipped"), None],
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    _RUN.run_plan(plan, "runbook.yml", "westus3")
            self.assertEqual(2, process.call_count)
            self.assertNotIn("test-token", str(process.call_args.args[0]))
            results = json.loads(
                (root / "pr_validation_results.json").read_text(encoding="utf-8")
            )
            self.assertEqual(["failed", "passed"], [item["status"] for item in results])

    def test_pipeline_preserves_pool_timeout_and_runs_group_plan(self) -> None:
        config = yaml.safe_load(
            (_ROOT / ".azuredevops" / "azure-pipelines.yml").read_text(encoding="utf-8")
        )
        self.assertEqual({"name": "LSG_1ES_Pool_Test"}, config["pool"])
        job = config["jobs"][0]
        self.assertEqual(0, job["timeoutInMinutes"])
        azure_step = next(
            step for step in job["steps"] if step.get("task") == "AzureCLI@2"
        )
        self.assertIn("run_selected_cases.py", azure_step["inputs"]["inlineScript"])
        self.assertIn("gt(variables['CASE_COUNT'], 0)", azure_step["condition"])
