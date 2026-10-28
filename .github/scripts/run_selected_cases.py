# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Resolve reviewed images, execute validation groups, and enforce JUnit coverage."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree

from validation_plan import reviewed_images


def azure_json(arguments: List[str]) -> Any:
    result = subprocess.run(
        ["az", *arguments, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Azure CLI failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def resolve_image(image: str, location: str) -> str:
    parts = image.split()
    if len(parts) != 4:
        raise ValueError(f"Invalid Marketplace image: {image!r}")
    resolved = azure_json(
        ["vm", "image", "show", "--location", location, "--urn", ":".join(parts)]
    )
    version = resolved.get("name", "")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Image did not resolve to a concrete version: {image!r}")
    return " ".join(parts[:3] + [version])


def verify_results(directory: Path, expected: List[str]) -> None:
    files = list(directory.rglob("lisa.junit.xml"))
    if not files:
        raise ValueError(
            f"No JUnit results found under {directory}; "
            "execution coverage cannot be verified."
        )
    cases = [
        case for path in files for case in ElementTree.parse(path).iter("testcase")
    ]
    missing: List[str] = []
    for name in expected:
        suite, method = name.rsplit(".", 1)
        matches = [
            case
            for case in cases
            if case.get("classname") == name
            or (
                case.get("classname") == suite
                and re.sub(r" \([^()]*\)$", "", case.get("name", "")) == method
            )
        ]
        if not matches or any(
            case.find("skipped") is not None
            or case.find("failure") is not None
            or case.find("error") is not None
            for case in matches
        ):
            missing.append(name)
    if missing:
        raise ValueError(f"Cases missing, skipped, attempted, or failed: {missing}")
    if any(
        case.find("failure") is not None or case.find("error") is not None
        for case in cases
    ):
        raise ValueError("JUnit contains failed cases.")


def case_pattern(full_names: List[str]) -> str:
    # LISA Criteria.name matches method names, not suite-qualified names.
    # The selector rejects ambiguous method names before producing a plan.
    return (
        "(?:" + "|".join(re.escape(name.rsplit(".", 1)[1]) for name in full_names) + ")"
    )


def run_plan(plan_path: Path, runbook: str, location: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan["status"] == "not_required" and not plan["groups"]:
        print(plan["reason"])
        return
    if plan["status"] != "selected" or not plan["groups"]:
        raise ValueError("Invalid or empty validation plan.")
    catalog = reviewed_images(repo_root)
    groups = plan["groups"]
    for group in groups:
        if group["marketplace_image"] not in catalog:
            raise ValueError("Validation image is not in the reviewed catalog.")
        if not group["cases"] or any(
            not isinstance(name, str) or not re.fullmatch(r"\w+\.\w+", name)
            for name in group["cases"]
        ):
            raise ValueError("Invalid or empty validation case list.")
        if not set(group["required_cases"]).issubset(group["cases"]):
            raise ValueError("Required cases are missing from the execution group.")
    # Resolve every target before deploying any guests. Never replace an unavailable
    # target with a different distro or leave "latest" unresolved in the run record.
    images = [resolve_image(group["marketplace_image"], location) for group in groups]
    account = azure_json(["account", "show"])
    results: List[Dict[str, str]] = []
    report_path = plan_path.with_name("pr_validation_results.json")
    for index, (group, image) in enumerate(zip(groups, images)):
        # LISA resolves relative log paths under its own runtime root.
        directory = (Path("runtime") / "pr-validation" / f"group-{index}").resolve()
        if directory.exists():
            raise ValueError(f"Refusing to reuse stale results directory: {directory}")
        directory.mkdir(parents=True)
        result = {"image": image, "status": "running", "details": ""}
        results.append(result)
        report_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        try:
            token = azure_json(
                [
                    "account",
                    "get-access-token",
                    "--resource",
                    "https://management.azure.com/",
                ]
            )
            environment = os.environ.copy()
            environment["S_LISA_azure_arm_access_token"] = token["accessToken"]
            pattern = case_pattern(group["cases"])
            command = [
                "lisa",
                "--log_path",
                str(directory),
                "-r",
                runbook,
                "-v",
                f"subscription_id:{account['id']}",
                "-v",
                f"location:{location}",
                "-v",
                f"marketplace_image:{image}",
                "-v",
                f"test_case_name:{pattern}",
                "-v",
                "enable_tests:False",
                "-v",
                "auth_type:token",
            ]
            print(f"Running group {index}: {image}; cases={group['cases']}", flush=True)
            completed = subprocess.run(command, env=environment, check=False)
            if completed.returncode:
                raise RuntimeError(f"LISA exited with code {completed.returncode}.")
            verify_results(directory, group["cases"])
        except (ValueError, RuntimeError, OSError, ElementTree.ParseError) as error:
            result.update(status="failed", details=str(error))
            print(f"Validation group {index} failed: {error}", flush=True)
        else:
            result["status"] = "passed"
        report_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if any(result["status"] != "passed" for result in results):
        raise RuntimeError(f"Validation incomplete or failed; see {report_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default="ai_selected_cases.json", type=Path)
    parser.add_argument("--runbook", required=True)
    parser.add_argument("--location", required=True)
    args = parser.parse_args()
    run_plan(args.plan, args.runbook, args.location)
