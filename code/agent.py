import asyncio
import asyncio as _asyncio

import time as _time
from observability.observability_wrapper import (
    trace_agent, trace_step, trace_step_sync, trace_model_call, trace_tool_call,
)
from config import settings as _obs_settings

import logging as _obs_startup_log
from contextlib import asynccontextmanager
from observability.instrumentation import initialize_tracer

_obs_startup_logger = _obs_startup_log.getLogger(__name__)

from modules.guardrails.content_safety_decorator import with_content_safety

GUARDRAILS_CONFIG = {
    'content_safety_enabled': True,
    'runtime_enabled': True,
    'content_safety_severity_threshold': 3,
    'check_toxicity': True,
    'check_jailbreak': True,
    'check_pii_input': False,
    'check_credentials_output': True,
    'check_output': True,
    'check_toxic_code_output': True,
    'sanitize_pii': False
}

import logging
import json
import uuid
import re
from typing import Optional, Dict, Any, List, Union
from pathlib import Path

import requests
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from config import Config

# =========================
# Constants
# =========================

SYSTEM_PROMPT = (
    "You are a professional automation agent specializing in RFQ review and CRM quote creation as part of a multi-step pipeline. "
    "Your responsibilities include:\n\n"
    "- Ensuring process restartability by checking for agent_run_id and retrieving parent outputs as needed.\n\n"
    "- Creating audit entries for each run and maintaining accurate status updates.\n\n"
    "- Reading and validating RFQ JSON data, extracting header and line-level details.\n\n"
    "- Orchestrating Process 1 (contact validation and enrichment) and Process 2 (BU category and resource assignment) in parallel, waiting for both to complete.\n\n"
    "- Managing Human-in-the-Loop (HITL) escalations for missing data or failed operations, and retrieving HITL tasks from the database.\n\n"
    "- Identifying the primary BU category and assigning the correct Inside Sales Engineer resource based on business logic and API responses.\n\n"
    "- Deriving the Ultimate Destination field using a fallback chain from RFQ, end user, or account country.\n\n"
    "- Mapping and validating all required fields for CRM quote creation, removing unwanted prefixes from email subjects, and using static configuration values where required.\n\n"
    "- Handling CRM quote creation, sending email notifications via Microsoft Graph API, and managing failures with appropriate HITL escalation.\n\n"
    "- Appending all generated/derived fields and the quote number to the input JSON, uploading the final JSON to Azure Blob Storage, and updating the agent_run_id status and output_ref in the database.\n\n"
    "Output all responses in a clear, structured format. If required information is missing or an operation fails, escalate to HITL and provide a user-friendly error message.\n\n"
    "If you cannot complete a step due to missing data or system errors, clearly indicate the failure reason and suggest next steps."
)
OUTPUT_FORMAT = "All outputs must be in structured JSON format, including status, data fields, error codes (if any), and escalation actions taken."
FALLBACK_RESPONSE = "Required information could not be found or an operation failed. The issue has been escalated to Human-in-the-Loop (HITL) for manual intervention. Please review the HITL task list for further action."

VALIDATION_CONFIG_PATH = Config.VALIDATION_CONFIG_PATH or str(Path(__file__).parent / "validation_config.json")

# =========================
# Logging
# =========================

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

# =========================
# Input/Output Models
# =========================

class RFQAgentRequest(BaseModel):
    agent_run_id: Optional[str] = Field(None, description="Agent run ID for restartability")
    pipeline_run_id: str = Field(..., description="Pipeline run ID")
    rfq_json: Optional[dict] = Field(None, description="RFQ JSON data extracted from parent or input")
    # Accept additional fields for extensibility

    @field_validator("pipeline_run_id")
    @classmethod
    def validate_pipeline_run_id(cls, v):
        if not v or not str(v).strip():
            raise ValueError("pipeline_run_id is required and cannot be empty.")
        return v.strip()

    @model_validator(mode="after")
    def validate_payload(self):
        if not self.agent_run_id and not self.rfq_json:
            raise ValueError("Either agent_run_id or rfq_json must be provided.")
        return self

class RFQAgentResponse(BaseModel):
    success: bool = Field(..., description="Indicates if the agent run was successful")
    data: Optional[dict] = Field(None, description="Structured output data")
    error: Optional[str] = Field(None, description="Error message if any")
    error_code: Optional[str] = Field(None, description="Error code if any")
    escalation: Optional[dict] = Field(None, description="HITL escalation details if any")

# =========================
# Utility: LLM Output Sanitizer
# =========================

import re as _re

_FENCE_RE = _re.compile(r"```(?:\w+)?\s*\n(.*?)```", _re.DOTALL)
_LONE_FENCE_START_RE = _re.compile(r"^```\w*$")
_WRAPPER_RE = _re.compile(
    r"^(?:"
    r"Here(?:'s| is)(?: the)? (?:the |your |a )?(?:code|solution|implementation|result|explanation|answer)[^:]*:\s*"
    r"|Sure[!,.]?\s*"
    r"|Certainly[!,.]?\s*"
    r"|Below is [^:]*:\s*"
    r")",
    _re.IGNORECASE,
)
_SIGNOFF_RE = _re.compile(
    r"^(?:Let me know|Feel free|Hope this|This code|Note:|Happy coding|If you)",
    _re.IGNORECASE,
)
_BLANK_COLLAPSE_RE = _re.compile(r"\n{3,}")

def _strip_fences(text: str, content_type: str) -> str:
    """Extract content from Markdown code fences."""
    fence_matches = _FENCE_RE.findall(text)
    if fence_matches:
        if content_type == "code":
            return "\n\n".join(block.strip() for block in fence_matches)
        for match in fence_matches:
            fenced_block = _FENCE_RE.search(text)
            if fenced_block:
                text = text[:fenced_block.start()] + match.strip() + text[fenced_block.end():]
        return text
    lines = text.splitlines()
    if lines and _LONE_FENCE_START_RE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()

def _strip_trailing_signoffs(text: str) -> str:
    """Remove conversational sign-off lines from the end of code output."""
    lines = text.splitlines()
    while lines and _SIGNOFF_RE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).rstrip()

@with_content_safety(config=GUARDRAILS_CONFIG)
def sanitize_llm_output(raw: str, content_type: str = "code") -> str:
    """
    Generic post-processor that cleans common LLM output artefacts.
    Args:
        raw: Raw text returned by the LLM.
        content_type: 'code' | 'text' | 'markdown'.
    Returns:
        Cleaned string ready for validation, formatting, or direct return.
    """
    if not raw:
        return ""
    text = _strip_fences(raw.strip(), content_type)
    text = _WRAPPER_RE.sub("", text, count=1).strip()
    if content_type == "code":
        text = _strip_trailing_signoffs(text)
    return _BLANK_COLLAPSE_RE.sub("\n\n", text).strip()

# =========================
# Service Base Class
# =========================

class BaseService:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    def log_info(self, msg: str, **kwargs):
        self.logger.info(msg, extra=kwargs)

    def log_error(self, msg: str, **kwargs):
        self.logger.error(msg, extra=kwargs)

# =========================
# Service Implementations
# =========================

class AuditService(BaseService):
    def create_audit_entry(self, input_payload: dict, pipeline_run_id: str, agent_name: str) -> str:
        """
        Creates an audit entry in SQL DB and returns agent_run_id.
        """
        # Placeholder: Replace with actual DB logic
        try:
            # Simulate DB insert and return a UUID
            agent_run_id = str(uuid.uuid4())
            self.log_info(f"Audit entry created: agent_run_id={agent_run_id}, pipeline_run_id={pipeline_run_id}, agent_name={agent_name}")
            return agent_run_id
        except Exception as e:
            self.log_error(f"Failed to create audit entry: {e}")
            raise

    def update_audit_entry(self, agent_run_id: str, status: str, output_ref: Optional[str]) -> bool:
        """
        Updates audit entry with status and output_ref.
        """
        try:
            # Simulate DB update
            self.log_info(f"Audit entry updated: agent_run_id={agent_run_id}, status={status}, output_ref={output_ref}")
            return True
        except Exception as e:
            self.log_error(f"Failed to update audit entry: {e}")
            return False

class BlobStorageService(BaseService):
    def download(self, blob_path: str, file_type: str) -> Union[bytes, dict]:
        """
        Downloads file from Azure Blob Storage.
        """
        try:
            # Placeholder: Simulate blob download
            # In real implementation, use azure-storage-blob SDK
            self.log_info(f"Downloading from blob: {blob_path}, file_type={file_type}")
            # Simulate JSON file
            if file_type == "json":
                return {"simulated": "parent_output"}
            return b""
        except Exception as e:
            self.log_error(f"Blob download failed: {e}")
            raise

    def upload(self, blob_path: str, data: dict) -> bool:
        """
        Uploads file to Azure Blob Storage.
        """
        try:
            # Placeholder: Simulate blob upload
            self.log_info(f"Uploading to blob: {blob_path}")
            return True
        except Exception as e:
            self.log_error(f"Blob upload failed: {e}")
            return False

class HITLService(BaseService):
    @with_content_safety(config=GUARDRAILS_CONFIG)
    def create_task(self, reason_code: str, context: dict) -> str:
        """
        Creates HITL task in SQL DB.
        """
        try:
            hitl_task_id = str(uuid.uuid4())
            self.log_info(f"HITL task created: reason_code={reason_code}, context={context}, hitl_task_id={hitl_task_id}")
            return hitl_task_id
        except Exception as e:
            self.log_error(f"Failed to create HITL task: {e}")
            raise

    @with_content_safety(config=GUARDRAILS_CONFIG)
    def get_tasks(self, agent_run_id: str, pipeline_run_id: str) -> List[dict]:
        """
        Retrieves HITL tasks for the current run.
        """
        try:
            # Placeholder: Simulate DB query
            self.log_info(f"Retrieving HITL tasks: agent_run_id={agent_run_id}, pipeline_run_id={pipeline_run_id}")
            return []
        except Exception as e:
            self.log_error(f"Failed to retrieve HITL tasks: {e}")
            return []

class CRMService(BaseService):
    def get_contact(self, contact_email: str) -> dict:
        """
        Retrieves contact details from CRM.
        """
        try:
            # Placeholder: Simulate CRM API call
            self.log_info(f"CRM get_contact called: contact_email={contact_email}")
            return {"party_id": "P123", "account": {"country": "Germany"}, "accountPartyId": "A456"}
        except Exception as e:
            self.log_error(f"CRM get_contact failed: {e}")
            raise

    def create_quote(self, quote_fields: dict) -> dict:
        """
        Creates CRM quote with mapped fields.
        """
        try:
            # Placeholder: Simulate CRM quote creation
            self.log_info(f"CRM create_quote called: fields={quote_fields}")
            return {"status": "success", "quote_number": "Q789"}
        except Exception as e:
            self.log_error(f"CRM create_quote failed: {e}")
            return {"status": "failure", "error": str(e)}

class TerritoryService(BaseService):
    def get_territory(self, primary_bu_category: str) -> dict:
        """
        Retrieves territory/resource assignment from Territory API.
        """
        try:
            # Placeholder: Simulate Territory API call
            self.log_info(f"Territory get_territory called: primary_bu_category={primary_bu_category}")
            return {
                "resources": [
                    {"resource_id": "ISE123", "FunctionCode_Meaning": "Inside Sales Engineer", "TerritoryOwnerFlag": True}
                ]
            }
        except Exception as e:
            self.log_error(f"Territory get_territory failed: {e}")
            raise

class NotificationService(BaseService):
    def send_email(self, eml_file: bytes, recipient_mailbox: str) -> bool:
        """
        Sends email notification with EML attachment via Microsoft Graph API.
        """
        try:
            self.log_info(f"Notification sent to {recipient_mailbox}")
            return True
        except Exception as e:
            self.log_error(f"Notification send failed: {e}")
            return False

# =========================
# Process Services
# =========================

class Process1Service(BaseService):
    def __init__(self, crm_service: CRMService, hitl_service: HITLService):
        super().__init__()
        self.crm_service = crm_service
        self.hitl_service = hitl_service

    def execute(self, rfq_data: dict) -> dict:
        """
        Validates contact email, calls CRM Contact API, escalates to HITL if missing.
        """
        try:
            contact_email = rfq_data.get("contact_email") or rfq_data.get("ContactEmail")
            if not contact_email:
                hitl_id = self.hitl_service.create_task("MISSING_CONTACT_EMAIL", {"rfq_data": rfq_data})
                return {
                    "status": "escalated",
                    "error_code": "MISSING_CONTACT_EMAIL",
                    "hitl_task_id": hitl_id,
                    "message": "Contact email missing. Escalated to HITL."
                }
            contact_info = self.crm_service.get_contact(contact_email)
            return {
                "status": "success",
                "contact_info": contact_info
            }
        except Exception as e:
            self.log_error(f"Process1 execution failed: {e}")
            hitl_id = self.hitl_service.create_task("MISSING_CONTACT_EMAIL", {"rfq_data": rfq_data, "error": str(e)})
            return {
                "status": "escalated",
                "error_code": "MISSING_CONTACT_EMAIL",
                "hitl_task_id": hitl_id,
                "message": f"Contact validation failed: {e}"
            }

class Process2Service(BaseService):
    def __init__(self, territory_service: TerritoryService, hitl_service: HITLService):
        super().__init__()
        self.territory_service = territory_service
        self.hitl_service = hitl_service

    def execute(self, rfq_data: dict, process1_output: dict) -> dict:
        """
        Identifies primary BU, calls Territory API, assigns resource, escalates to HITL if not found.
        """
        try:
            # Identify all BU categories
            line_items = rfq_data.get("line_items", [])
            bu_counts = {}
            for item in line_items:
                bu = item.get("bu_category")
                if bu:
                    bu_counts[bu] = bu_counts.get(bu, 0) + 1
            if not bu_counts:
                hitl_id = self.hitl_service.create_task("MISSING_ISE_CONTACT", {"rfq_data": rfq_data})
                return {
                    "status": "escalated",
                    "error_code": "MISSING_ISE_CONTACT",
                    "hitl_task_id": hitl_id,
                    "message": "No BU categories found. Escalated to HITL."
                }
            primary_bu = max(bu_counts, key=bu_counts.get)
            territory_info = self.territory_service.get_territory(primary_bu)
            ise_resource = None
            for res in territory_info.get("resources", []):
                if res.get("FunctionCode_Meaning") == "Inside Sales Engineer":
                    if res.get("TerritoryOwnerFlag"):
                        ise_resource = res
                        break
                    elif not ise_resource:
                        ise_resource = res
            if not ise_resource:
                hitl_id = self.hitl_service.create_task("MISSING_ISE_CONTACT", {"rfq_data": rfq_data})
                return {
                    "status": "escalated",
                    "error_code": "MISSING_ISE_CONTACT",
                    "hitl_task_id": hitl_id,
                    "message": "No Inside Sales Engineer found. Escalated to HITL."
                }
            return {
                "status": "success",
                "primary_bu_category": primary_bu,
                "all_bu_categories": list(bu_counts.keys()),
                "resource_id": ise_resource.get("resource_id"),
                "territory_info": territory_info
            }
        except Exception as e:
            self.log_error(f"Process2 execution failed: {e}")
            hitl_id = self.hitl_service.create_task("MISSING_ISE_CONTACT", {"rfq_data": rfq_data, "error": str(e)})
            return {
                "status": "escalated",
                "error_code": "MISSING_ISE_CONTACT",
                "hitl_task_id": hitl_id,
                "message": f"Resource assignment failed: {e}"
            }

# =========================
# Main Agent Implementation
# =========================

class RFQAgent(BaseService):
    def __init__(self):
        super().__init__()
        self.audit_service = AuditService()
        self.blob_service = BlobStorageService()
        self.hitl_service = HITLService()
        self.crm_service = CRMService()
        self.territory_service = TerritoryService()
        self.notification_service = NotificationService()
        self.process1_service = Process1Service(self.crm_service, self.hitl_service)
        self.process2_service = Process2Service(self.territory_service, self.hitl_service)

    @trace_agent(agent_name=_obs_settings.AGENT_NAME, project_name=_obs_settings.PROJECT_NAME)
    @with_content_safety(config=GUARDRAILS_CONFIG)
    async def run(self, input_payload: dict) -> dict:
        """
        Entry point for agent execution. Handles input validation, restartability, audit logging,
        parallel process orchestration, aggregation, and output packaging.
        """
        async with trace_step(
            "agent_run", step_type="process",
            decision_summary="RFQAgent main orchestration",
            output_fn=lambda r: f"success={r.get('success', False)}"
        ) as step:
            try:
                # Step 1: Restartability or Audit Entry
                agent_run_id = input_payload.get("agent_run_id")
                pipeline_run_id = input_payload.get("pipeline_run_id")
                rfq_json = input_payload.get("rfq_json")
                parent_output = None

                if agent_run_id:
                    # Resume from parent output
                    parent_blob_path = self._get_parent_output_ref(agent_run_id, pipeline_run_id)
                    if not parent_blob_path:
                        return {
                            "success": False,
                            "error": "Parent output_ref not found for restart.",
                            "error_code": "RESTART_FAILED"
                        }
                    parent_output = self.blob_service.download(parent_blob_path, "json")
                    rfq_json = parent_output
                else:
                    # New run: create audit entry
                    try:
                        agent_run_id = self.audit_service.create_audit_entry(
                            input_payload, pipeline_run_id, "RFQ Review"
                        )
                    except Exception as e:
                        return {
                            "success": False,
                            "error": f"Audit entry creation failed: {e}",
                            "error_code": "AUDIT_CREATION_FAILED"
                        }

                if not rfq_json:
                    return {
                        "success": False,
                        "error": "RFQ JSON data missing.",
                        "error_code": "MISSING_RFQ_JSON"
                    }

                # Step 2: Parallel Process Execution
                process1_task = asyncio.create_task(self._run_process1(rfq_json))
                process2_task = asyncio.create_task(self._run_process2(rfq_json, process1_task))
                process1_result, process2_result = await asyncio.gather(process1_task, process2_task)

                # Step 3: HITL Escalation Check
                escalation = None
                if process1_result.get("status") == "escalated" or process2_result.get("status") == "escalated":
                    escalation = {
                        "process1": process1_result if process1_result.get("status") == "escalated" else None,
                        "process2": process2_result if process2_result.get("status") == "escalated" else None,
                        "hitl_tasks": self.hitl_service.get_tasks(agent_run_id, pipeline_run_id)
                    }
                    self.audit_service.update_audit_entry(agent_run_id, "ESCALATED", None)
                    return {
                        "success": False,
                        "error": "Escalated to HITL due to missing data or assignment failure.",
                        "error_code": process1_result.get("error_code") or process2_result.get("error_code"),
                        "escalation": escalation
                    }

                # Step 4: Field Mapping and Transformation
                try:
                    mapped_fields = self._map_crm_fields(
                        rfq_json,
                        process1_result,
                        process2_result
                    )
                except Exception as e:
                    hitl_id = self.hitl_service.create_task("QUOTE_CREATION_FAILED", {"rfq_json": rfq_json, "error": str(e)})
                    self.audit_service.update_audit_entry(agent_run_id, "ESCALATED", None)
                    return {
                        "success": False,
                        "error": f"Field mapping failed: {e}",
                        "error_code": "QUOTE_CREATION_FAILED",
                        "escalation": {"hitl_task_id": hitl_id}
                    }

                # Step 5: CRM Quote Creation
                crm_quote_resp = self.crm_service.create_quote(mapped_fields)
                if crm_quote_resp.get("status") != "success":
                    hitl_id = self.hitl_service.create_task("QUOTE_CREATION_FAILED", {"crm_quote_resp": crm_quote_resp})
                    self.audit_service.update_audit_entry(agent_run_id, "ESCALATED", None)
                    return {
                        "success": False,
                        "error": "CRM quote creation failed.",
                        "error_code": "QUOTE_CREATION_FAILED",
                        "escalation": {"hitl_task_id": hitl_id}
                    }

                # Step 6: Notification
                try:
                    eml_blob_path = rfq_json.get("eml_blob_path", "rfq_email.eml")
                    eml_file = self.blob_service.download(eml_blob_path, "eml")
                    self.notification_service.send_email(eml_file, "wcc_mailbox@example.com")
                except Exception as e:
                    self.log_error(f"Notification failed: {e}")

                # Step 7: Final Output Packaging
                final_json = dict(rfq_json)
                final_json.update({
                    "crm_quote_number": crm_quote_resp.get("quote_number"),
                    "crm_fields": mapped_fields,
                    "process1": process1_result,
                    "process2": process2_result
                })
                output_blob_path = f"rfq_outputs/{agent_run_id}.json"
                self.blob_service.upload(output_blob_path, final_json)
                self.audit_service.update_audit_entry(agent_run_id, "COMPLETED", output_blob_path)

                return {
                    "success": True,
                    "data": {
                        "agent_run_id": agent_run_id,
                        "crm_quote_number": crm_quote_resp.get("quote_number"),
                        "output_blob_path": output_blob_path,
                        "final_json": final_json
                    }
                }
            except Exception as e:
                self.log_error(f"Agent run failed: {e}")
                hitl_id = self.hitl_service.create_task("GENERAL_FAILURE", {"input_payload": input_payload, "error": str(e)})
                return {
                    "success": False,
                    "error": f"Agent run failed: {e}",
                    "error_code": "GENERAL_FAILURE",
                    "escalation": {"hitl_task_id": hitl_id}
                }

    async def resume(self, agent_run_id: str, pipeline_run_id: str) -> dict:
        """
        Handles agent restartability by fetching parent output from Blob Storage and resuming processing.
        """
        async with trace_step(
            "agent_resume", step_type="process",
            decision_summary="RFQAgent resume orchestration",
            output_fn=lambda r: f"success={r.get('success', False)}"
        ) as step:
            try:
                parent_blob_path = self._get_parent_output_ref(agent_run_id, pipeline_run_id)
                if not parent_blob_path:
                    return {
                        "success": False,
                        "error": "Parent output_ref not found for restart.",
                        "error_code": "RESTART_FAILED"
                    }
                parent_output = self.blob_service.download(parent_blob_path, "json")
                return await self.run({
                    "agent_run_id": agent_run_id,
                    "pipeline_run_id": pipeline_run_id,
                    "rfq_json": parent_output
                })
            except Exception as e:
                self.log_error(f"Resume failed: {e}")
                return {
                    "success": False,
                    "error": f"Resume failed: {e}",
                    "error_code": "RESTART_FAILED"
                }

    async def _run_process1(self, rfq_json: dict) -> dict:
        return self.process1_service.execute(rfq_json)

    async def _run_process2(self, rfq_json: dict, process1_task) -> dict:
        process1_result = await process1_task
        return self.process2_service.execute(rfq_json, process1_result)

    def _get_parent_output_ref(self, agent_run_id: str, pipeline_run_id: str) -> Optional[str]:
        """
        Simulate fetching output_ref from DB for parent agent run.
        """
        # Placeholder: In real implementation, query DB for output_ref
        # For demo, return a static blob path
        return f"parent_outputs/{agent_run_id}.json"

    def _map_crm_fields(self, rfq_json: dict, process1_result: dict, process2_result: dict) -> dict:
        """
        Map and transform fields for CRM quote creation.
        """
        # Static config values
        KIND_OF_BUSINESS = "3.1"
        TIER = "0"
        LOCATION_ID = "300000136312807"

        # Ultimate Destination logic
        ultimate_destination = (
            rfq_json.get("ultimate_destination")
            or rfq_json.get("end_user_country")
            or process1_result.get("contact_info", {}).get("account", {}).get("country")
            or ""
        )

        # Customer Reference (email subject) with prefix removal
        subject = rfq_json.get("email_subject", "")
        subject = re.sub(r"^(RE:|FW:|\[EXTERNAL\]|\[EXT\])\s*", "", subject, flags=re.IGNORECASE)

        # Primary BU category
        primary_bu = process2_result.get("primary_bu_category", "")

        # All BU categories as comma separated
        all_bu_categories = process2_result.get("all_bu_categories", [])
        business_category = ",".join(sorted(set(all_bu_categories)))

        # Owner (ISE resource)
        owner_id = process2_result.get("resource_id", "")

        # AssociatedContact_Id_c and Purchaser_Id_c from contact_info
        contact_info = process1_result.get("contact_info", {})
        associated_contact_id = contact_info.get("party_id", "")
        purchaser_id = contact_info.get("accountPartyId", "")

        # RecordName: unique uuid with -TTA suffix
        record_name = f"{str(uuid.uuid4())}-TTA"

        # RFQ received date
        rfq_received_date = rfq_json.get("rfq_received_date") or rfq_json.get("rfqReceivedDate_c", "")

        return {
            "RecordName": record_name,
            "rfqReceivedDate_c": rfq_received_date,
            "CustomerReference_c": subject,
            "UltimateDestination_c": ultimate_destination,
            "KindOfBusiness_c": KIND_OF_BUSINESS,
            "Tier_c": TIER,
            "PrimaryBusinessCategory_c": primary_bu,
            "BusinessCategory_c": business_category,
            "Location_Id_c": LOCATION_ID,
            "Owner_Id_c": owner_id,
            "AssociatedContact_Id_c": associated_contact_id,
            "Purchaser_Id_c": purchaser_id
        }

# =========================
# FastAPI App & Endpoints
# =========================

@asynccontextmanager
async def _obs_lifespan(application):
    """Initialise observability on startup, clean up on shutdown."""
    try:
        _obs_startup_logger.info('')
        _obs_startup_logger.info('========== Agent Configuration Summary ==========')
        _obs_startup_logger.info(f'Environment: {getattr(Config, "ENVIRONMENT", "N/A")}')
        _obs_startup_logger.info(f'Agent: {getattr(Config, "AGENT_NAME", "N/A")}')
        _obs_startup_logger.info(f'Project: {getattr(Config, "PROJECT_NAME", "N/A")}')
        _obs_startup_logger.info(f'LLM Provider: {getattr(Config, "MODEL_PROVIDER", "N/A")}')
        _obs_startup_logger.info(f'LLM Model: {getattr(Config, "LLM_MODEL", "N/A")}')
        _cs_endpoint = getattr(Config, 'AZURE_CONTENT_SAFETY_ENDPOINT', None)
        _cs_key = getattr(Config, 'AZURE_CONTENT_SAFETY_KEY', None)
        if _cs_endpoint and _cs_key:
            _obs_startup_logger.info('Content Safety: Enabled (Azure Content Safety)')
            _obs_startup_logger.info(f'Content Safety Endpoint: {_cs_endpoint}')
        else:
            _obs_startup_logger.info('Content Safety: Not Configured')
        _obs_startup_logger.info('Observability Database: Azure SQL')
        _obs_startup_logger.info(f'Database Server: {getattr(Config, "OBS_AZURE_SQL_SERVER", "N/A")}')
        _obs_startup_logger.info(f'Database Name: {getattr(Config, "OBS_AZURE_SQL_DATABASE", "N/A")}')
        _obs_startup_logger.info('===============================================')
        _obs_startup_logger.info('')
    except Exception as _e:
        _obs_startup_logger.warning('Config summary failed: %s', _e)

    _obs_startup_logger.info('')
    _obs_startup_logger.info('========== Content Safety & Guardrails ==========')
    if GUARDRAILS_CONFIG.get('content_safety_enabled'):
        _obs_startup_logger.info('Content Safety: Enabled')
        _obs_startup_logger.info(f'  - Severity Threshold: {GUARDRAILS_CONFIG.get("content_safety_severity_threshold", "N/A")}')
        _obs_startup_logger.info(f'  - Check Toxicity: {GUARDRAILS_CONFIG.get("check_toxicity", False)}')
        _obs_startup_logger.info(f'  - Check Jailbreak: {GUARDRAILS_CONFIG.get("check_jailbreak", False)}')
        _obs_startup_logger.info(f'  - Check PII Input: {GUARDRAILS_CONFIG.get("check_pii_input", False)}')
        _obs_startup_logger.info(f'  - Check Credentials Output: {GUARDRAILS_CONFIG.get("check_credentials_output", False)}')
    else:
        _obs_startup_logger.info('Content Safety: Disabled')
    _obs_startup_logger.info('===============================================')
    _obs_startup_logger.info('')

    _obs_startup_logger.info('========== Initializing Agent Services ==========')
    # 1. Observability DB schema (imports are inside function — only needed at startup)
    try:
        from observability.database.engine import create_obs_database_engine
        from observability.database.base import ObsBase
        import observability.database.models  # noqa: F401
        _obs_engine = create_obs_database_engine()
        ObsBase.metadata.create_all(bind=_obs_engine, checkfirst=True)
        _obs_startup_logger.info('✓ Observability database connected')
    except Exception as _e:
        _obs_startup_logger.warning('✗ Observability database connection failed (metrics will not be saved)')
    # 2. OpenTelemetry tracer (initialize_tracer is pre-injected at top level)
    try:
        _t = initialize_tracer()
        if _t is not None:
            _obs_startup_logger.info('✓ Telemetry monitoring enabled')
        else:
            _obs_startup_logger.warning('✗ Telemetry monitoring disabled')
    except Exception as _e:
        _obs_startup_logger.warning('✗ Telemetry monitoring failed to initialize')
    _obs_startup_logger.info('=================================================')
    _obs_startup_logger.info('')
    yield

app = FastAPI(
    title="RFQ Review and CRM Quote Automation Agent",
    description="Automates RFQ review, CRM quote creation, and HITL escalation with audit logging and parallel process orchestration.",
    version=Config.SERVICE_VERSION if hasattr(Config, "SERVICE_VERSION") else "1.0.0",
    lifespan=_obs_lifespan
)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}

@app.exception_handler(RequestValidationError)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "error": "Malformed JSON or invalid request.",
            "details": exc.errors(),
            "tips": [
                "Ensure your JSON is valid (check for missing commas, quotes, or brackets).",
                "Field names must match the API schema.",
                "If sending large text, keep it under 50,000 characters."
            ]
        }
    )

@app.exception_handler(ValidationError)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "error": "Malformed JSON or invalid request.",
            "details": exc.errors(),
            "tips": [
                "Ensure your JSON is valid (check for missing commas, quotes, or brackets).",
                "Field names must match the API schema.",
                "If sending large text, keep it under 50,000 characters."
            ]
        }
    )

@app.post("/run", response_model=RFQAgentResponse)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def run_agent(req: RFQAgentRequest):
    """
    Main entrypoint for RFQ Review and CRM Quote Automation Agent.
    """
    agent = RFQAgent()
    try:
        input_payload = req.model_dump()
        result = await agent.run(input_payload)
        # Sanitize output
        if isinstance(result, dict):
            result = json.loads(sanitize_llm_output(json.dumps(result), content_type="code"))
        return result
    except Exception as e:
        logger.error(f"Agent run failed: {e}")
        return {
            "success": False,
            "error": f"Agent run failed: {e}",
            "error_code": "GENERAL_FAILURE"
        }

@app.post("/resume", response_model=RFQAgentResponse)
@with_content_safety(config=GUARDRAILS_CONFIG)
async def resume_agent(req: RFQAgentRequest):
    """
    Resume agent run for restartability.
    """
    agent = RFQAgent()
    try:
        if not req.agent_run_id or not req.pipeline_run_id:
            return {
                "success": False,
                "error": "agent_run_id and pipeline_run_id are required for resume.",
                "error_code": "RESTART_FAILED"
            }
        result = await agent.resume(req.agent_run_id, req.pipeline_run_id)
        if isinstance(result, dict):
            result = json.loads(sanitize_llm_output(json.dumps(result), content_type="code"))
        return result
    except Exception as e:
        logger.error(f"Agent resume failed: {e}")
        return {
            "success": False,
            "error": f"Agent resume failed: {e}",
            "error_code": "RESTART_FAILED"
        }

async def _run_agent():
    """Entrypoint: runs the agent with observability (trace collection only)."""
    import uvicorn

    # Unified logging config — routes uvicorn, agent, and observability through
    # the same handler so all telemetry appears in a single consistent stream.
    _LOG_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(name)s: %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "agent":          {"handlers": ["default"], "level": "INFO", "propagate": False},
            "__main__":       {"handlers": ["default"], "level": "INFO", "propagate": False},
            "observability": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "config": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "azure":   {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "urllib3": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
    }

    config = uvicorn.Config(
        "agent:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
        log_config=_LOG_CONFIG,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    _asyncio.run(_run_agent())