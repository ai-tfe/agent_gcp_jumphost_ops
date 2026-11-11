"""GCP Jump Host Automation Agent — Executes pre-staged PowerShell scripts on Jump Host 1 and Ansible playbooks on Jump Host 2 without custom MCP servers."""

import json
import logging
import os
import time
import uuid
from typing import Any, List, Optional

import google.auth
import google.auth.transport.requests
import requests
from google.adk.agents.llm_agent import Agent
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

logger = logging.getLogger(__name__)

MODEL = os.getenv("ADK_MODEL", "gemini-2.5-flash")

# Default Demo Environment (can be overridden per tool call)
DEFAULT_TARGET_PROJECT = os.getenv("DEMO_TARGET_PROJECT", "project-f625f262-ce1a-467a-a2c")
DEFAULT_TARGET_ZONE = os.getenv("DEMO_TARGET_ZONE", "us-east4-a")
DEFAULT_ANSIBLE_JUMPHOST = os.getenv("ANSIBLE_JUMPHOST_VM", "geap-linux-ansible-jumphost")
DEFAULT_POWERSHELL_JUMPHOST = os.getenv("POWERSHELL_JUMPHOST_VM", "geap-windows-ps-jumphost")

# Managed Google Cloud MCP Endpoints (routed via Agent Egress Gateway)
COMPUTE_MCP_URL = os.getenv("COMPUTE_MCP_URL", "https://compute.googleapis.com/mcp")
LOGGING_MCP_URL = os.getenv("LOGGING_MCP_URL", "https://logging.googleapis.com/mcp")
KNOWLEDGE_MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "https://developerknowledge.googleapis.com/mcp")

PRE_STAGED_CATALOG = {
    "ansible_jumphost": {
        "vm_name": "geap-linux-ansible-jumphost",
        "internal_ip": "10.200.22.10",
        "playbook_directory": "/opt/ansible/playbooks",
        "inventory_path": "/opt/ansible/inventory/hosts.ini",
        "default_target_linux_vm": "geap-private-http (10.200.21.2)",
        "playbooks": [
            {
                "name": "linux_health_check.yml",
                "description": "Collects hostname, kernel, uptime/load, memory, disk usage, and top 5 CPU processes on target Linux VMs.",
            },
            {
                "name": "linux_service_status.yml",
                "description": "Inspects listening TCP ports (ss -tulpn) and verifies the GEAP HTTP service on target Linux VMs.",
            },
            {
                "name": "linux_disk_cleanup.yml",
                "description": "Cleans apt cache and vacuums systemd journal logs older than 3 days on target Linux VMs.",
            },
        ],
    },
    "powershell_jumphost": {
        "vm_name": "geap-windows-ps-jumphost",
        "internal_ip": "10.200.22.11",
        "scripts_directory": "/opt/powershell/scripts (and C:\\automation\\scripts)",
        "default_target_windows_vm": "geap-windows-target (10.200.21.10)",
        "scripts": [
            {
                "name": "windows_health_check.ps1",
                "description": "Runs remote PowerShell Invoke-Command against target VM (10.200.21.10) to collect OS, uptime, disk space, and top 5 CPU processes.",
            },
            {
                "name": "windows_service_status.ps1",
                "description": "Runs remote PowerShell Invoke-Command against target VM (10.200.21.10) to inspect service states.",
            },
            {
                "name": "windows_temp_cleanup.ps1",
                "description": "Runs remote PowerShell Invoke-Command against target VM (10.200.21.10) to clean temporary files and report disk space before/after.",
            },
        ],
    },
}


def _gcp_agent_identity_headers(_readonly_context=None) -> dict[str, str]:
    """Attach AGENT_IDENTITY OAuth Bearer token and resolved string project ID header."""
    headers: dict[str, str] = {}
    try:
        credentials, default_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        if credentials.token:
            headers["Authorization"] = f"Bearer {credentials.token}"
        raw_proj = (
            os.getenv("GOOGLE_CLOUD_QUOTA_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or default_project
            or "svcprjprd"
        )
        quota_project = "svcprjprd" if str(raw_proj).isdigit() else str(raw_proj)
        headers["X-Goog-User-Project"] = quota_project
    except Exception as exc:
        logger.warning("Failed to refresh AGENT_IDENTITY credentials: %s", exc)
    return headers


def _dispatch_jumphost_metadata_command(
    *,
    project_id: str,
    zone: str,
    jumphost_vm: str,
    metadata_key: str,
    command_payload: dict[str, Any],
    wait_seconds: int = 35,
) -> dict[str, Any]:
    """Dispatches a command payload to a Jump Host VM via Compute Engine HTTPS API and polls Cloud Logging for stdout/stderr."""
    headers = _gcp_agent_identity_headers()
    headers["Content-Type"] = "application/json"

    inst_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{project_id}"
        f"/zones/{zone}/instances/{jumphost_vm}"
    )
    inst_resp = requests.get(inst_url, headers=headers, timeout=20)
    inst_resp.raise_for_status()
    inst_data = inst_resp.json()
    metadata = inst_data.get("metadata", {})
    fingerprint = metadata.get("fingerprint", "")
    items = [
        item for item in metadata.get("items", []) if item.get("key") != metadata_key
    ]
    items.append({"key": metadata_key, "value": json.dumps(command_payload)})

    set_meta_url = f"{inst_url}/setMetadata"
    set_resp = requests.post(
        set_meta_url,
        headers=headers,
        json={"fingerprint": fingerprint, "items": items},
        timeout=20,
    )
    set_resp.raise_for_status()

    run_id = command_payload["run_id"]
    log_url = "https://logging.googleapis.com/v2/entries:list"
    log_filter = (
        f'logName="projects/{project_id}/logs/geap-jumphost-execution" '
        f'AND jsonPayload.run_id="{run_id}"'
    )

    deadline = time.time() + max(10, min(wait_seconds, 90))
    while time.time() < deadline:
        time.sleep(3)
        try:
            l_resp = requests.post(
                log_url,
                headers=headers,
                json={
                    "resourceNames": [f"projects/{project_id}"],
                    "filter": log_filter,
                    "orderBy": "timestamp desc",
                    "pageSize": 1,
                },
                timeout=15,
            )
            if l_resp.status_code == 200:
                entries = l_resp.json().get("entries", [])
                if entries:
                    return {
                        "status": "COMPLETED",
                        "project_id": project_id,
                        "zone": zone,
                        "jumphost_vm": jumphost_vm,
                        "execution_result": entries[0].get("jsonPayload", {}),
                    }
        except Exception as exc:
            logger.warning("Polling Cloud Logging for run_id=%s failed: %s", run_id, exc)

    return {
        "status": "DISPATCHED_PENDING_LOG",
        "run_id": run_id,
        "project_id": project_id,
        "zone": zone,
        "jumphost_vm": jumphost_vm,
        "note": (
            f"Command was dispatched to {jumphost_vm}. Query Cloud Logging with filter: "
            f'logName="projects/{project_id}/logs/geap-jumphost-execution" AND jsonPayload.run_id="{run_id}"'
        ),
    }


def list_jumphost_automation_catalog() -> dict[str, Any]:
    """Lists the available Jump Host VMs, target VMs, pre-staged Ansible playbooks, and pre-staged PowerShell scripts."""
    return {
        "project_id": DEFAULT_TARGET_PROJECT,
        "zone": DEFAULT_TARGET_ZONE,
        "catalog": PRE_STAGED_CATALOG,
    }


def execute_ansible_playbook_from_jumphost(
    playbook_name: str = "linux_health_check.yml",
    target_hosts: str = "geap-private-http",
    jumphost_vm: str = DEFAULT_ANSIBLE_JUMPHOST,
    project_id: str = DEFAULT_TARGET_PROJECT,
    zone: str = DEFAULT_TARGET_ZONE,
) -> dict[str, Any]:
    """Executes a pre-staged Ansible playbook from `/opt/ansible/playbooks/` on the Linux Ansible Jump Host (`geap-linux-ansible-jumphost`) against target Linux VMs (such as `geap-private-http`).

    Args:
        playbook_name: Pre-staged playbook file name in `/opt/ansible/playbooks/` (e.g., `linux_health_check.yml`, `linux_service_status.yml`, `linux_disk_cleanup.yml`).
        target_hosts: Target Linux host or inventory group in `/opt/ansible/inventory/hosts.ini` (default: `geap-private-http`).
        jumphost_vm: Name of the Ansible Jump Host VM (default: `geap-linux-ansible-jumphost`).
        project_id: GCP project ID where the jump host resides (default: `project-f625f262-ce1a-467a-a2c`).
        zone: GCP zone where the jump host resides (default: `us-east4-a`).
    """
    run_id = f"ansible-{uuid.uuid4().hex[:10]}"
    payload = {
        "run_id": run_id,
        "playbook": playbook_name,
        "target_hosts": target_hosts,
    }
    return _dispatch_jumphost_metadata_command(
        project_id=project_id,
        zone=zone,
        jumphost_vm=jumphost_vm,
        metadata_key="geap-ansible-cmd",
        command_payload=payload,
        wait_seconds=40,
    )


def execute_powershell_script_from_jumphost(
    script_name: str = "windows_health_check.ps1",
    target_vm: str = "10.200.21.10",
    script_args: str = "",
    jumphost_vm: str = DEFAULT_POWERSHELL_JUMPHOST,
    project_id: str = DEFAULT_TARGET_PROJECT,
    zone: str = DEFAULT_TARGET_ZONE,
) -> dict[str, Any]:
    """Executes a pre-staged PowerShell (`.ps1`) script from the PowerShell Jump Host (`geap-windows-ps-jumphost`) against remote target VMs (`geap-windows-target` / `10.200.21.10`).

    Args:
        script_name: Pre-staged PowerShell script name (e.g., `windows_health_check.ps1`, `windows_service_status.ps1`, `windows_temp_cleanup.ps1`).
        target_vm: Remote target VM IP or hostname (default: `10.200.21.10` for `geap-windows-target`).
        script_args: Optional extra arguments to pass to the PowerShell script (e.g., `-ServiceName ssh,cron`).
        jumphost_vm: Name of the PowerShell Jump Host VM (default: `geap-windows-ps-jumphost`).
        project_id: GCP project ID where the jump host resides (default: `project-f625f262-ce1a-467a-a2c`).
        zone: GCP zone where the jump host resides (default: `us-east4-a`).
    """
    run_id = f"ps-{uuid.uuid4().hex[:10]}"
    payload = {
        "run_id": run_id,
        "script_name": script_name,
        "target_vm": target_vm,
        "script_args": script_args,
    }
    return _dispatch_jumphost_metadata_command(
        project_id=project_id,
        zone=zone,
        jumphost_vm=jumphost_vm,
        metadata_key="geap-ps-cmd",
        command_payload=payload,
        wait_seconds=40,
    )


class BestEffortMcpToolset(McpToolset):
    """Loads filtered MCP tools with AGENT_IDENTITY auth and keeps the agent running if egress denies a server."""

    def __init__(
        self,
        *,
        server_name: str,
        url: str,
        tool_name_prefix: str,
        tool_filter: Optional[List[str]] = None,
    ):
        self._server_name = server_name
        super().__init__(
            connection_params=StreamableHTTPConnectionParams(
                url=url,
                timeout=30.0,
            ),
            tool_filter=tool_filter,
            tool_name_prefix=tool_name_prefix,
            errlog=None,
            header_provider=_gcp_agent_identity_headers,
        )

    async def get_tools(self, readonly_context=None):
        try:
            return await super().get_tools(readonly_context)
        except Exception as exc:
            logger.warning("MCP server %s unavailable: %s", self._server_name, exc)
            return []


compute_mcp_toolset = BestEffortMcpToolset(
    server_name="google-compute-engine",
    url=COMPUTE_MCP_URL,
    tool_name_prefix="gce_",
    tool_filter=[
        "list_instances",
        "get_instance_basic_info",
    ],
)

logging_mcp_toolset = BestEffortMcpToolset(
    server_name="google-cloud-logging",
    url=LOGGING_MCP_URL,
    tool_name_prefix="logging_",
    tool_filter=[
        "list_log_entries",
        "list_log_names",
    ],
)

knowledge_mcp_toolset = BestEffortMcpToolset(
    server_name="google-developer-knowledge",
    url=KNOWLEDGE_MCP_URL,
    tool_name_prefix="knowledge_",
    tool_filter=[
        "search_documents",
        "get_documents",
    ],
)


root_agent = Agent(
    model=MODEL,
    name="gcp_jumphost_ops_agent",
    description=(
        "Google Cloud Jump Host Automation Agent that executes pre-staged PowerShell scripts from "
        "`geap-windows-ps-jumphost` and pre-staged Ansible playbooks from `geap-linux-ansible-jumphost` "
        "against remote target VMs in `project-f625f262-ce1a-467a-a2c` (`us-east4-a`) without custom MCP servers."
    ),
    instruction=(
        "You are the Google Cloud Jump Host Automation Specialist (`gcp_jumphost_ops_agent`).\n"
        "You manage a 4-VM Jump Host architecture in project `project-f625f262-ce1a-467a-a2c` (zone `us-east4-a`):\n"
        "1. Linux Ansible Jump Host: `geap-linux-ansible-jumphost` (`10.200.22.10`) -> runs `ansible-playbook` from `/opt/ansible/playbooks/` "
        "against target Linux VM `geap-private-http` (`10.200.21.2`).\n"
        "   - Pre-staged playbooks: `linux_health_check.yml`, `linux_service_status.yml`, `linux_disk_cleanup.yml`.\n"
        "2. PowerShell Jump Host: `geap-windows-ps-jumphost` (`10.200.22.11`) -> runs PowerShell (`pwsh` / `.ps1`) scripts from `/opt/powershell/scripts/` "
        "using `Invoke-Command` against target VM `geap-windows-target` (`10.200.21.10`).\n"
        "   - Pre-staged scripts: `windows_health_check.ps1`, `windows_service_status.ps1`, `windows_temp_cleanup.ps1`.\n\n"
        "WORKFLOW:\n"
        "- Use `list_jumphost_automation_catalog` to inspect available jump hosts, target VMs, playbooks, and PowerShell scripts.\n"
        "- Use `execute_ansible_playbook_from_jumphost` to run Ansible playbooks on `geap-linux-ansible-jumphost` targeting `geap-private-http`.\n"
        "- Use `execute_powershell_script_from_jumphost` to run PowerShell `.ps1` scripts on `geap-windows-ps-jumphost` targeting `10.200.21.10` (`geap-windows-target`).\n"
        "- Always report the exact `run_id`, `command`, `return_code`, and `stdout` returned from the jump host execution."
    ),
    tools=[
        list_jumphost_automation_catalog,
        execute_ansible_playbook_from_jumphost,
        execute_powershell_script_from_jumphost,
        compute_mcp_toolset,
        logging_mcp_toolset,
        knowledge_mcp_toolset,
    ],
)
