
import pytest
import asyncio
import types
from unittest.mock import patch, MagicMock, AsyncMock

import agent

@pytest.mark.asyncio
async def test_RFQAgent_run_with_valid_new_input():
    """Functional: RFQAgent.run processes new input, creates audit, runs both processes, creates CRM quote, sends notification, returns success."""
    input_payload = {
        "pipeline_run_id": "PR123",
        "rfq_json": {
            "contact_email": "test@example.com",
            "email_subject": "RE: RFQ for Widget",
            "rfq_received_date": "2024-06-01",
            "line_items": [{"bu_category": "BU1"}, {"bu_category": "BU2"}]
        }
    }
    with patch.object(agent.AuditService, "create_audit_entry", return_value="AR123") as mock_audit, \
         patch.object(agent.BlobStorageService, "download", return_value={"simulated": "parent_output"}) as mock_download, \
         patch.object(agent.BlobStorageService, "upload", return_value=True) as mock_upload, \
         patch.object(agent.NotificationService, "send_email", return_value=True) as mock_notify:
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.run(input_payload)
        assert result["success"] is True
        data = result["data"]
        assert data["agent_run_id"] == "AR123"
        assert data["crm_quote_number"] is not None
        assert data["output_blob_path"] is not None
        assert isinstance(data["final_json"], dict)
        assert "crm_quote_number" in data["final_json"]
        assert "crm_fields" in data["final_json"]
        assert "process1" in data["final_json"]
        assert "process2" in data["final_json"]

@pytest.mark.asyncio
async def test_RFQAgent_run_with_restart_agent_run_id_present():
    """Functional: RFQAgent.run resumes from parent output when agent_run_id is provided."""
    input_payload = {
        "agent_run_id": "AR456",
        "pipeline_run_id": "PR456"
    }
    parent_output = {
        "contact_email": "parent@example.com",
        "email_subject": "FW: RFQ",
        "rfq_received_date": "2024-06-02",
        "line_items": [{"bu_category": "BU3"}]
    }
    with patch.object(agent.BlobStorageService, "download", return_value=parent_output) as mock_download, \
         patch.object(agent.AuditService, "update_audit_entry", return_value=True):
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.run(input_payload)
        assert result["success"] is True
        data = result["data"]
        assert data["agent_run_id"] == "AR456"
        assert data["crm_quote_number"] is not None
        assert data["output_blob_path"] is not None
        assert isinstance(data["final_json"], dict)
        assert data["final_json"]["contact_email"] == "parent@example.com"

@pytest.mark.asyncio
async def test_RFQAgent_run_with_missing_contact_email_triggers_HITL():
    """Functional: Missing contact_email in rfq_json triggers HITL escalation in Process1Service."""
    input_payload = {
        "pipeline_run_id": "PR789",
        "rfq_json": {
            "email_subject": "RFQ",
            "line_items": [{"bu_category": "BU1"}]
        }
    }
    with patch.object(agent.AuditService, "create_audit_entry", return_value="AR789"), \
         patch.object(agent.HITLService, "create_task", return_value="HITL123"), \
         patch.object(agent.HITLService, "get_tasks", return_value=[{"hitl_task_id": "HITL123"}]), \
         patch.object(agent.AuditService, "update_audit_entry", return_value=True):
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.run(input_payload)
        assert result["success"] is False
        assert result["error_code"] == "MISSING_CONTACT_EMAIL"
        escalation = result["escalation"]
        assert escalation["process1"]["hitl_task_id"] == "HITL123"

@pytest.mark.asyncio
async def test_RFQAgent_run_with_missing_bu_category_triggers_HITL():
    """Functional: Missing bu_category in line_items triggers HITL escalation in Process2Service."""
    input_payload = {
        "pipeline_run_id": "PR321",
        "rfq_json": {
            "contact_email": "test@example.com",
            "email_subject": "RFQ",
            "line_items": [{}]
        }
    }
    with patch.object(agent.AuditService, "create_audit_entry", return_value="AR321"), \
         patch.object(agent.HITLService, "create_task", return_value="HITL456"), \
         patch.object(agent.HITLService, "get_tasks", return_value=[{"hitl_task_id": "HITL456"}]), \
         patch.object(agent.AuditService, "update_audit_entry", return_value=True):
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.run(input_payload)
        assert result["success"] is False
        assert result["error_code"] == "MISSING_ISE_CONTACT"
        escalation = result["escalation"]
        assert escalation["process2"]["hitl_task_id"] == "HITL456"

@pytest.mark.asyncio
async def test_RFQAgent_resume_with_valid_agent_run_id_and_pipeline_run_id():
    """Integration: RFQAgent.resume fetches parent output and resumes processing."""
    agent_run_id = "AR999"
    pipeline_run_id = "PR999"
    parent_output = {
        "contact_email": "resume@example.com",
        "email_subject": "RFQ",
        "rfq_received_date": "2024-06-03",
        "line_items": [{"bu_category": "BUX"}]
    }
    with patch.object(agent.BlobStorageService, "download", return_value=parent_output), \
         patch.object(agent.AuditService, "update_audit_entry", return_value=True):
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.resume(agent_run_id, pipeline_run_id)
        assert result["success"] is True
        data = result["data"]
        assert data["agent_run_id"] == agent_run_id
        assert data["final_json"]["contact_email"] == "resume@example.com"

@pytest.mark.asyncio
async def test_RFQAgent_run_with_CRM_quote_creation_failure_triggers_HITL():
    """Integration: CRMService.create_quote returns failure, triggers HITL escalation."""
    input_payload = {
        "pipeline_run_id": "PRFAIL",
        "rfq_json": {
            "contact_email": "fail@example.com",
            "email_subject": "RFQ",
            "line_items": [{"bu_category": "BUFAIL"}]
        }
    }
    with patch.object(agent.AuditService, "create_audit_entry", return_value="ARFAIL"), \
         patch.object(agent.CRMService, "create_quote", return_value={"status": "failure"}), \
         patch.object(agent.HITLService, "create_task", return_value="HITLFAIL"), \
         patch.object(agent.AuditService, "update_audit_entry", return_value=True):
        rfq_agent = agent.RFQAgent()
        result = await rfq_agent.run(input_payload)
        assert result["success"] is False
        assert result["error_code"] == "QUOTE_CREATION_FAILED"
        assert result["escalation"]["hitl_task_id"] == "HITLFAIL"

def test_Process1Service_execute_with_valid_contact_email():
    """Unit: Process1Service.execute with valid contact_email triggers CRMService.get_contact and returns success."""
    mock_crm = MagicMock()
    mock_crm.get_contact.return_value = {"party_id": "P1", "account": {"country": "US"}}
    mock_hitl = MagicMock()
    svc = agent.Process1Service(mock_crm, mock_hitl)
    rfq_data = {"contact_email": "foo@bar.com"}
    result = svc.execute(rfq_data)
    assert result["status"] == "success"
    assert result["contact_info"] == {"party_id": "P1", "account": {"country": "US"}}
    mock_crm.get_contact.assert_called_once_with("foo@bar.com")

def test_Process1Service_execute_with_missing_contact_email():
    """Unit: Process1Service.execute with missing contact_email triggers HITLService.create_task and returns escalation."""
    mock_crm = MagicMock()
    mock_hitl = MagicMock()
    mock_hitl.create_task.return_value = "HITL999"
    svc = agent.Process1Service(mock_crm, mock_hitl)
    rfq_data = {"email_subject": "RFQ"}
    result = svc.execute(rfq_data)
    assert result["status"] == "escalated"
    assert result["error_code"] == "MISSING_CONTACT_EMAIL"
    assert result["hitl_task_id"] == "HITL999"
    mock_hitl.create_task.assert_called_once()

def test_Process2Service_execute_with_valid_BU_categories():
    """Unit: Process2Service.execute with valid line_items triggers TerritoryService.get_territory and returns success."""
    mock_territory = MagicMock()
    mock_territory.get_territory.return_value = {
        "resources": [
            {"resource_id": "ISE1", "FunctionCode_Meaning": "Inside Sales Engineer", "TerritoryOwnerFlag": True}
        ]
    }
    mock_hitl = MagicMock()
    svc = agent.Process2Service(mock_territory, mock_hitl)
    rfq_data = {
        "line_items": [
            {"bu_category": "BU1"},
            {"bu_category": "BU1"},
            {"bu_category": "BU2"}
        ]
    }
    process1_output = {"contact_info": {"party_id": "P1"}}
    result = svc.execute(rfq_data, process1_output)
    assert result["status"] == "success"
    assert result["primary_bu_category"] == "BU1"
    assert result["resource_id"] == "ISE1"
    mock_territory.get_territory.assert_called_once_with("BU1")

def test_Process2Service_execute_with_missing_BU_categories():
    """Unit: Process2Service.execute with missing bu_category triggers HITLService.create_task and returns escalation."""
    mock_territory = MagicMock()
    mock_hitl = MagicMock()
    mock_hitl.create_task.return_value = "HITL888"
    svc = agent.Process2Service(mock_territory, mock_hitl)
    rfq_data = {"line_items": [{}]}
    process1_output = {"contact_info": {"party_id": "P1"}}
    result = svc.execute(rfq_data, process1_output)
    assert result["status"] == "escalated"
    assert result["error_code"] == "MISSING_ISE_CONTACT"
    assert result["hitl_task_id"] == "HITL888"
    mock_hitl.create_task.assert_called_once()

def test_AuditService_create_audit_entry_returns_agent_run_id():
    """Unit: AuditService.create_audit_entry returns a valid agent_run_id."""
    svc = agent.AuditService()
    input_payload = {"foo": "bar"}
    pipeline_run_id = "PRID"
    agent_name = "RFQ Review"
    agent_run_id = svc.create_audit_entry(input_payload, pipeline_run_id, agent_name)
    assert agent_run_id is not None
    assert isinstance(agent_run_id, str)

def test_AuditService_update_audit_entry_returns_true():
    """Unit: AuditService.update_audit_entry returns True on successful update."""
    svc = agent.AuditService()
    agent_run_id = "ARID"
    status = "COMPLETED"
    output_ref = "blob/path.json"
    result = svc.update_audit_entry(agent_run_id, status, output_ref)
    assert result is True

def test_BlobStorageService_download_returns_dict_for_json_file_type():
    """Unit: BlobStorageService.download returns dict when file_type is 'json'."""
    svc = agent.BlobStorageService()
    blob_path = "some/path.json"
    result = svc.download(blob_path, "json")
    assert isinstance(result, dict)

def test_BlobStorageService_upload_returns_true():
    """Unit: BlobStorageService.upload returns True on successful upload."""
    svc = agent.BlobStorageService()
    blob_path = "some/path.json"
    data = {"foo": "bar"}
    result = svc.upload(blob_path, data)
    assert result is True

def test_HITLService_create_task_returns_hitl_task_id():
    """Unit: HITLService.create_task returns a valid hitl_task_id."""
    svc = agent.HITLService()
    reason_code = "MISSING_CONTACT_EMAIL"
    context = {"foo": "bar"}
    hitl_task_id = svc.create_task(reason_code, context)
    assert hitl_task_id is not None
    assert isinstance(hitl_task_id, str)

def test_HITLService_get_tasks_returns_empty_list():
    """Unit: HITLService.get_tasks returns empty list when no tasks exist."""
    svc = agent.HITLService()
    agent_run_id = "ARID"
    pipeline_run_id = "PRID"
    tasks = svc.get_tasks(agent_run_id, pipeline_run_id)
    assert isinstance(tasks, list)
    assert len(tasks) == 0

def test_CRMService_get_contact_returns_contact_info_dict():
    """Unit: CRMService.get_contact returns a contact_info dict."""
    svc = agent.CRMService()
    contact_email = "foo@bar.com"
    result = svc.get_contact(contact_email)
    assert isinstance(result, dict)
    assert "party_id" in result
    assert "account" in result

def test_CRMService_create_quote_returns_success_dict():
    """Unit: CRMService.create_quote returns dict with status='success' and quote_number."""
    svc = agent.CRMService()
    quote_fields = {"foo": "bar"}
    result = svc.create_quote(quote_fields)
    assert result["status"] == "success"
    assert result["quote_number"] is not None

def test_TerritoryService_get_territory_returns_resources_dict():
    """Unit: TerritoryService.get_territory returns dict with resources list."""
    svc = agent.TerritoryService()
    primary_bu_category = "BU1"
    result = svc.get_territory(primary_bu_category)
    assert isinstance(result, dict)
    assert "resources" in result
    assert isinstance(result["resources"], list)

def test_NotificationService_send_email_returns_true():
    """Unit: NotificationService.send_email returns True on successful send."""
    svc = agent.NotificationService()
    eml_file = b"test"
    recipient_mailbox = "foo@bar.com"
    result = svc.send_email(eml_file, recipient_mailbox)
    assert result is True