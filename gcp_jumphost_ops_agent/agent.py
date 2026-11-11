"""GCP Jump Host Automation Agent — Executes pre-staged PowerShell scripts on Jump Host 1 and Ansible playbooks on Jump Host 2 without custom MCP servers."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
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


def _load_inventory() -> dict[str, Any]:
    """Parse packaged inventory.txt into jump hosts and target hostnames/IPs."""
    path = Path(__file__).with_name("inventory.txt")
    inventory: dict[str, Any] = {
        "path": str(path),
        "groups": {},
        "hosts": {},
    }
    if not path.is_file():
        inventory["error"] = f"inventory.txt not found at {path}"
        return inventory
    current_group = "ungrouped"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].strip()
            inventory["groups"].setdefault(current_group, [])
            continue
        parts = line.split()
        name = parts[0]
        ip = ""
        for part in parts[1:]:
            if part.startswith("ansible_host="):
                ip = part.split("=", 1)[1]
            elif not ip:
                ip = part
        host = {"name": name, "ip": ip, "group": current_group}
        inventory["groups"].setdefault(current_group, []).append(host)
        inventory["hosts"][name] = host
        if ip:
            inventory["hosts"][ip] = host
    return inventory


INVENTORY = _load_inventory()


def _resolve_target(name_or_ip: str, *, prefer_ip: bool) -> str:
    """Map inventory hostname to IP (Windows remoting) or keep Ansible inventory name."""
    raw = (name_or_ip or "").strip()
    if not raw:
        return raw
    host = INVENTORY.get("hosts", {}).get(raw)
    if not host:
        return raw
    if prefer_ip:
        return host.get("ip") or host.get("name") or raw
    return host.get("name") or raw


def _http_error_payload(exc: requests.HTTPError, *, context: dict[str, Any]) -> dict[str, Any]:
    resp = exc.response
    body = ""
    if resp is not None:
        try:
            body = resp.text[:2000]
        except Exception:
            body = ""
    return {
        "status": "FAILED",
        "error_type": "HTTPError",
        "http_status": getattr(resp, "status_code", None),
        "url": getattr(resp, "url", None),
        "error": str(exc),
        "response_body": body,
        **context,
    }


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
    context = {
        "project_id": project_id,
        "zone": zone,
        "jumphost_vm": jumphost_vm,
        "run_id": command_payload.get("run_id"),
        "command_payload": command_payload,
    }

    inst_url = (
        f"https://compute.googleapis.com/compute/v1/projects/{project_id}"
        f"/zones/{zone}/instances/{jumphost_vm}"
    )
    try:
        inst_resp = requests.get(inst_url, headers=headers, timeout=20)
        inst_resp.raise_for_status()
    except requests.HTTPError as exc:
        return _http_error_payload(exc, context=context)
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__, "error": str(exc), **context}
    inst_data = inst_resp.json()
    metadata = inst_data.get("metadata", {})
    fingerprint = metadata.get("fingerprint", "")
    items = [
        item for item in metadata.get("items", []) if item.get("key") != metadata_key
    ]
    items.append({"key": metadata_key, "value": json.dumps(command_payload)})

    set_meta_url = f"{inst_url}/setMetadata"
    try:
        set_resp = requests.post(
            set_meta_url,
            headers=headers,
            json={"fingerprint": fingerprint, "items": items},
            timeout=20,
        )
        set_resp.raise_for_status()
        set_data = set_resp.json() if set_resp.content else {}
        op_name = set_data.get("name")
        if op_name:
            op_url = (
                f"https://compute.googleapis.com/compute/v1/projects/{project_id}"
                f"/zones/{zone}/operations/{op_name}"
            )
            op_deadline = time.time() + 15
            while time.time() < op_deadline:
                time.sleep(1)
                try:
                    op_resp = requests.get(op_url, headers=headers, timeout=10)
                    if op_resp.status_code == 200:
                        op_data = op_resp.json()
                        if op_data.get("status") == "DONE":
                            if op_data.get("error"):
                                return {
                                    "status": "FAILED",
                                    "error_type": "GCEOperationError",
                                    "error": op_data.get("error"),
                                    **context,
                                }
                            break
                except Exception:
                    pass
    except requests.HTTPError as exc:
        return _http_error_payload(exc, context=context)
    except Exception as exc:
        return {"status": "FAILED", "error_type": type(exc).__name__, "error": str(exc), **context}

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
    """Lists jump hosts, inventory.txt hostnames/IPs, example playbooks, and example PowerShell scripts."""
    return {
        "project_id": DEFAULT_TARGET_PROJECT,
        "zone": DEFAULT_TARGET_ZONE,
        "inventory": INVENTORY,
        "catalog": PRE_STAGED_CATALOG,
        "note": (
            "Any playbook under /opt/ansible/playbooks on the Linux jump host and any .ps1 "
            "under /opt/powershell/scripts (or C:\\automation\\scripts) on the Windows jump host can be executed. "
            "Resolve target hostnames from inventory.txt; do not ask the user for an IP when the name is listed."
        ),
    }


def list_jumphost_inventory() -> dict[str, Any]:
    """Returns hostnames and IPs from packaged inventory.txt (Linux/Windows jump hosts and target VMs)."""
    return {
        "project_id": DEFAULT_TARGET_PROJECT,
        "zone": DEFAULT_TARGET_ZONE,
        "inventory": INVENTORY,
    }


def execute_ansible_playbook_from_jumphost(
    playbook_name: str = "linux_health_check.yml",
    target_hosts: str = "geap-private-http",
    jumphost_vm: str = DEFAULT_ANSIBLE_JUMPHOST,
    project_id: str = DEFAULT_TARGET_PROJECT,
    zone: str = DEFAULT_TARGET_ZONE,
) -> dict[str, Any]:
    """Executes any Ansible playbook file present under `/opt/ansible/playbooks/` on the Linux Ansible Jump Host against inventory hostnames.

    Args:
        playbook_name: Playbook file name on the jump host (any `.yml` in `/opt/ansible/playbooks/`, not limited to the sample catalog).
        target_hosts: Hostname or group from inventory.txt / `/opt/ansible/inventory/hosts.ini` (default: `geap-private-http`).
        jumphost_vm: Name of the Ansible Jump Host VM (default: `geap-linux-ansible-jumphost`).
        project_id: GCP project ID where the jump host resides (default: `project-f625f262-ce1a-467a-a2c`).
        zone: GCP zone where the jump host resides (default: `us-east4-a`).
    """
    resolved_hosts = _resolve_target(target_hosts, prefer_ip=False)
    run_id = f"ansible-{uuid.uuid4().hex[:10]}"
    payload = {
        "run_id": run_id,
        "playbook": playbook_name,
        "target_hosts": resolved_hosts,
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
    target_vm: str = "geap-windows-target",
    script_args: str = "",
    jumphost_vm: str = DEFAULT_POWERSHELL_JUMPHOST,
    project_id: str = DEFAULT_TARGET_PROJECT,
    zone: str = DEFAULT_TARGET_ZONE,
) -> dict[str, Any]:
    """Executes any PowerShell `.ps1` present on the PowerShell Jump Host against a target hostname from inventory.txt.

    Args:
        script_name: PowerShell script file name on the jump host (any `.ps1` in `/opt/powershell/scripts` or `C:\\automation\\scripts`, not limited to the sample catalog).
        target_vm: Target hostname or IP from inventory.txt (hostname is resolved to IP). Default: `geap-windows-target`.
        script_args: Optional extra arguments to pass to the PowerShell script (e.g., `-ServiceName wuauserv`).
        jumphost_vm: Name of the PowerShell Jump Host VM (default: `geap-windows-ps-jumphost`).
        project_id: GCP project ID where the jump host resides (default: `project-f625f262-ce1a-467a-a2c`).
        zone: GCP zone where the jump host resides (default: `us-east4-a`).
    """
    resolved_target = _resolve_target(target_vm, prefer_ip=True)
    run_id = f"ps-{uuid.uuid4().hex[:10]}"
    payload = {
        "run_id": run_id,
        "script_name": script_name,
        "target_vm": resolved_target,
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
        "Google Cloud Jump Host Automation Agent that executes PowerShell scripts from "
        "`geap-windows-ps-jumphost` and Ansible playbooks from `geap-linux-ansible-jumphost` "
        "against inventory.txt target VMs in `project-f625f262-ce1a-467a-a2c` (`us-east4-a`)."
    ),
    instruction=(
        "You are the Google Cloud Jump Host Automation Specialist (`gcp_jumphost_ops_agent`).\n"
        "Project `project-f625f262-ce1a-467a-a2c`, zone `us-east4-a`.\n"
        "ALWAYS call `list_jumphost_inventory` (or catalog) first. Hostnames and IPs come from inventory.txt. "
        "Never ask the user for an IP or hostname when it is already in inventory. "
        "Never ask which sample script to use when the user already named a playbook or .ps1.\n"
        "You MAY execute ANY playbook file on the Linux jump host (`/opt/ansible/playbooks/`) and ANY .ps1 "
        "on the Windows jump host (`/opt/powershell/scripts` or `C:\\automation\\scripts`). "
        "The sample catalog is examples only, not a whitelist.\n"
        "Windows remoting: pass the inventory hostname (e.g. geap-windows-target); the tool resolves it to IP.\n"
        "Linux Ansible: pass the inventory hostname (e.g. geap-private-http).\n"
        "After every execute_* call, reply in the Playground with a clean, human-friendly summary for a non-technical reader, followed by an expandable or structured details section:\n"
        "1. Executive Summary (in plain language):\n"
        "   - Overall Status: Success / Failure\n"
        "   - Target Machine & Jump Host used\n"
        "   - Key Findings (e.g. Uptime, Disk space available/used percentage, Health condition, or Service states like 'wuauserv: stopped', 'LanmanWorkstation: stopped')\n"
        "   - Top CPU Consumers formatted as a clean bulleted list or markdown table (Name, CPU%, Memory)\n"
        "2. Actionable Takeaway / Recommendations (e.g., 'Disk usage is healthy at 26%, no action needed' or 'Service X is stopped')\n"
        "3. Technical Execution Details (collapsible or clearly labeled at the bottom: run_id, return_code, duration_sec, command, raw stdout/stderr if needed for auditing)\n"
        "- If status is FAILED: Clearly explain in plain language what went wrong, quoting the HTTP status and error.\n"
        "- If status is DISPATCHED_PENDING_LOG: Explain that the command was successfully sent to the jump host and is currently executing, giving the run ID and Cloud Logging filter.\n"
        "Do not dump raw unformatted JSON as the primary response."
    ),
    tools=[
        list_jumphost_inventory,
        list_jumphost_automation_catalog,
        execute_ansible_playbook_from_jumphost,
        execute_powershell_script_from_jumphost,
        compute_mcp_toolset,
        logging_mcp_toolset,
        knowledge_mcp_toolset,
    ],
)
