"""
This script will process the `grant_access/` and `revoke_access/` requests.

Main logic lives in conda-smithy. This is just a wrapper for admin-requests infra.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from ruamel.yaml import YAML
from typing import List, Dict, Tuple

GH_ORG = os.environ.get("GH_ORG", "conda-forge")

# This is a mapping for the filename in {grant,revoke}_access/<name>.txt
# to the cirun resource configuration
# The schema for resource configuration is:
# {
#    "resource": "<cirun-resource-name>"
#    "policy_args": "List[<policy-args>]"
#}
# policy_args can only contain "pull_request at the moment"
CIRUN_FILENAME_RESOURCE_MAPPING = {
    "cirun-gpu-runner": {
        "resource": "cirun-gpu-runner",
    },
    "cirun-gpu-runner-pr": {
        "resource": "cirun-gpu-runner",
        "policy_args": ["pull_request"]
    }
}


ACCESS_YAML_FILENAME = ".access_control.yml"


def _get_access_control_files(path: str) -> List[str]:
    access_path = Path(path)
    all_files = list(access_path.glob("*"))
    return (
        [f for f in all_files if f.parts[-1] != "example.txt"]
    )


def _get_filename_feedstock_mapping(path: str) -> Dict[str, str]:
    access_files = _get_access_control_files(path)
    file_name_feedstock_mapping = {}
    for file_path in access_files:
        file_name, feedstocks = parse_an_input_file(file_path)
        file_name_feedstock_mapping[file_name] = feedstocks
    return file_name_feedstock_mapping


def parse_an_input_file(file_path: str) -> Tuple[str, List[str]]:
    with open(file_path, "r") as f:
        feedstocks = f.readlines()
        feedstocks = [feedstock.strip() for feedstock in feedstocks]
        file_name = file_path.parts[-1].strip(".txt")
        return file_name, feedstocks


def _process_access_control_requests(path: str, remove: bool = True) -> None:
    print(f"Processing access control requests for {path}")
    grant_access_request = _get_filename_feedstock_mapping(path)
    for filename, feedstocks in grant_access_request.items():
        if filename in CIRUN_FILENAME_RESOURCE_MAPPING:
            resource_mapping = CIRUN_FILENAME_RESOURCE_MAPPING.get(filename)
            resource = resource_mapping.get("resource")
            policy_args = resource_mapping.get("policy_args")
            for feedstock in feedstocks:
                feedstock_repo = f"{feedstock}-feedstock"
                print(f"Processing feedstock for access control: {feedstock_repo}")
                _process_request_for_feedstock(
                    feedstock_repo, resource, remove=remove, policy_args=policy_args
                )
                action = "remove" if remove else "add"
                update_access_yaml(resource, feedstock_repo, action=action)


def process_access_control_requests() -> None:
    """Process access control requests"""
    print("Processing access control request")
    _process_access_control_requests("grant_access", remove=False)
    _process_access_control_requests("revoke_access")


def _process_request_for_feedstock(
        feedstock: str, resource: str, remove: bool, policy_args: List[str]
) -> None:
    feedstock_clone_path = tempfile.mkdtemp()
    assert GH_ORG
    clone_cmd = f"git clone --depth 1 https://github.com/{GH_ORG}/{feedstock}.git {feedstock_clone_path}"
    print(f"Cloning: {clone_cmd}")
    subprocess.run(
        clone_cmd,
        shell=True,
    )

    register_ci_cmd = [
        "conda-smithy register-ci",
        f"--organization {GH_ORG}",
        "--without-azure",
        "--without-travis",
        "--without-circle",
        "--without-appveyor",
        "--without-drone",
        "--without-webservice",
        "--without-anaconda-token",
        f"--feedstock_directory {feedstock_clone_path}",
        f"--cirun-resources {resource}",
    ]

    if policy_args:
        policy_args_param = [f"--cirun-policy-args {arg}" for arg in policy_args]
        register_ci_cmd.extend(policy_args_param)
    if remove:
        register_ci_cmd.append("--remove")

    register_ci_cmd_str = " ".join(register_ci_cmd)
    print(f"RegisterCI command: {register_ci_cmd_str}")
    subprocess.check_call(register_ci_cmd_str,
        shell=True,
    )


def check_if_repo_exists(feedstock_name: str) -> None:
    repo = f"{feedstock_name}-feedstock"
    owner_repo = f"{GH_ORG}/{repo}"
    print(f"Checking if {owner_repo} exists")
    response = requests.get(f"https://api.github.com/repos/{owner_repo}")
    if response.status_code != 200:
        raise ValueError(f"Repository: {owner_repo} not found!")

def _check_for_path(path: str) -> None:
    mapping = _get_filename_feedstock_mapping(path)
    for filename, feedstocks in mapping.items():
        print(f"Checking if {filename} is in {CIRUN_FILENAME_RESOURCE_MAPPING.keys()}")
        assert filename in CIRUN_FILENAME_RESOURCE_MAPPING
        for feedstock in feedstocks:
            check_if_repo_exists(feedstock)
    if not mapping:
        print(f"Nothing to check for: {path}")

def check() -> None:
    """Check requests are valid (resources exist, feedstocks exist)"""
    _check_for_path("grant_access")
    _check_for_path("revoke_access")

def _commit_after_files_removal(push: bool = True) -> None:
    subprocess.check_call(
        "git add .",
        shell=True,
    )

    subprocess.check_call(
            "git commit -am 'Remove access control files'",
            shell=True,
        )
    if push:
        subprocess.check_call(
            "git push",
            shell=True,
        )


def _remove_input_files(dir: str, file_to_keep: str) -> None:
    print("Removing input files")
    directory = Path(dir)
    files_to_keep = [Path(file_to_keep)]
    if not directory.is_dir():
        raise ValueError("The specified path is not a directory.")

    for file in directory.iterdir():
        if file.is_file() and file not in files_to_keep:
            print(f"Removing input file: {file}")
            file.unlink()
    _commit_after_files_removal(push=False)


def update_access_yaml(
        resource: str, feedstock_name: str, action: str, filename: str = ACCESS_YAML_FILENAME
) -> None:
    """
    Manage feedstock in .access_control.yml.

    Parameters:
    - resource: the access control resource (e.g., "travis", "cirun-gpu-small").
    - feedstock_name: the name of the feedstock.
    - action: either "add" or "remove".
    - filename: the name of the YAML file.
    """
    print(f"Updating {filename}")
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)  # Indent settings for 2-space mapping

    with open(filename, 'r') as f:
        content = yaml.load(f)

    # If resource doesn't exist, create it
    if resource not in content['access_control']:
        content['access_control'][resource] = []

    if action == "add":
        # Check if feedstock already exists
        if any(entry["feedstock"] == feedstock_name for entry in content['access_control'][resource]):
            print(f"Feedstock {feedstock_name} already exists under {resource}. Skipping addition.")
            return

        entry = {"feedstock": feedstock_name}
        content['access_control'][resource].append(entry)

    elif action == "remove":
        if not content['access_control'][resource]:
            print(f"No feedstock found under resource {resource}.")
            return
        content['access_control'][resource] = [
            entry for entry in content['access_control'][resource]
            if entry["feedstock"] != feedstock_name
        ]

    else:
        raise ValueError(f"Invalid action {action}. Choose 'add' or 'remove'.")

    with open(filename, 'w') as f:
        yaml.dump(content, f)



def main() -> None:
    """Process requests"""
    check()  # doesn't hurt
    process_access_control_requests()
    _remove_input_files("grant_access/", file_to_keep="grant_access/example.txt")
    _remove_input_files("revoke_access/", file_to_keep="revoke_access/example.txt")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and "check" in sys.argv:
        sys.exit(check())
    sys.exit(main())
