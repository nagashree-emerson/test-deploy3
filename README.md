# RFQ Review and CRM Quote Automation Agent

Automates RFQ review, CRM quote creation, and HITL escalation with audit logging, parallel process orchestration, and observability. Designed for robust, restartable pipeline execution with Azure/Audit/Blob/CRM integrations and FastAPI endpoints.

---

## Quick Start

### 1. Create a virtual environment:
```
python -m venv .venv
```

### 2. Activate the virtual environment:
- **Windows:**
  ```
  .venv\Scripts\activate
  ```
- **macOS/Linux:**
  ```
  source .venv/bin/activate
  ```

### 3. Install dependencies:
```
pip install -r requirements.txt
```

### 4. Environment setup:
Copy `.env.example` to `.env` and fill in all required values.
```
cp .env.example .env
```

### 5. Running the agent

- Direct execution:
  ```
  python code/agent.py
  ```
- As a FastAPI server:
  ```
  uvicorn code.agent:app --reload --host 0.0.0.0 --port 8000
  ```

---

## Environment Variables

**Agent Identity**
- `AGENT_NAME`
- `AGENT_ID`
- `PROJECT_NAME`
- `PROJECT_ID`
- `SERVICE_NAME`
- `SERVICE_VERSION`

**General**
- `ENVIRONMENT`

**Azure Key Vault**
- `USE_KEY_VAULT`
- `KEY_VAULT_URI`
- `AZURE_USE_DEFAULT_CREDENTIAL`

**Azure Authentication**
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`

**LLM Configuration**
- `MODEL_PROVIDER`
- `LLM_MODEL`
- `LLM_TEMPERATURE`
- `LLM_MAX_TOKENS`
- `LLM_MODELS` (JSON array, optional)

**API Keys / Secrets**
- `OPENAI_API_KEY`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `AZURE_CONTENT_SAFETY_KEY`

**Service Endpoints**
- `AZURE_CONTENT_SAFETY_ENDPOINT`
- `AZURE_SEARCH_ENDPOINT`
- `AZURE_SEARCH_API_KEY`
- `AZURE_SEARCH_INDEX_NAME`

**Observability DB**
- `OBS_DATABASE_TYPE`
- `OBS_AZURE_SQL_SERVER`
- `OBS_AZURE_SQL_DATABASE`
- `OBS_AZURE_SQL_PORT`
- `OBS_AZURE_SQL_USERNAME`
- `OBS_AZURE_SQL_PASSWORD`
- `OBS_AZURE_SQL_SCHEMA`
- `OBS_AZURE_SQL_TRUST_SERVER_CERTIFICATE`

**Agent-Specific**
- `VALIDATION_CONFIG_PATH`
- `VERSION`
- `CONTENT_SAFETY_ENABLED`
- `CONTENT_SAFETY_SEVERITY_THRESHOLD`

---

## API Endpoints

### **GET** `/health`
- **Description:** Health check endpoint.
- **Response:**
  ```
  {
    "status": "ok"
  }
  ```

### **POST** `/run`
- **Description:** Main entrypoint for RFQ Review and CRM Quote Automation Agent.
- **Request body:**
  ```
  {
    "agent_run_id": "string (optional)",
    "pipeline_run_id": "string (required)",
    "rfq_json": { ... } (optional)
  }
  ```
  - Either `agent_run_id` or `rfq_json` must be provided.

- **Response:**
  ```
  {
    "success": true|false,
    "data": {
      "agent_run_id": "string",
      "crm_quote_number": "string",
      "output_blob_path": "string",
      "final_json": { ... }
    } (optional),
    "error": null|string,
    "error_code": null|string,
    "escalation": {
      "process1": { ... } (optional),
      "process2": { ... } (optional),
      "hitl_tasks": [ ... ] (optional),
      "hitl_task_id": "string" (optional)
    } (optional)
  }
  ```

### **POST** `/resume`
- **Description:** Resume agent run for restartability.
- **Request body:**
  ```
  {
    "agent_run_id": "string (required)",
    "pipeline_run_id": "string (required)"
  }
  ```
- **Response:**
  ```
  {
    "success": true|false,
    "data": {
      "agent_run_id": "string",
      "crm_quote_number": "string",
      "output_blob_path": "string",
      "final_json": { ... }
    } (optional),
    "error": null|string,
    "error_code": null|string,
    "escalation": {
      "process1": { ... } (optional),
      "process2": { ... } (optional),
      "hitl_tasks": [ ... ] (optional),
      "hitl_task_id": "string" (optional)
    } (optional)
  }
  ```

---

## Running Tests

### 1. Install test dependencies (if not already installed):
```
pip install pytest pytest-asyncio
```

### 2. Run all tests:
```
pytest tests/
```

### 3. Run a specific test file:
```
pytest tests/test_<module_name>.py
```

### 4. Run tests with verbose output:
```
pytest tests/ -v
```

### 5. Run tests with coverage report:
```
pip install pytest-cov
pytest tests/ --cov=code --cov-report=term-missing
```

---

## Deployment with Docker

### 1. Prerequisites: Ensure Docker is installed and running.

### 2. Environment setup: Copy `.env.example` to `.env` and configure all required environment variables.

### 3. Build the Docker image:
```
docker build -t RFQ Review and CRM Quote Automation Agent -f deploy/Dockerfile .
```

### 4. Run the Docker container:
```
docker run -d --env-file .env -p 8000:8000 --name RFQ Review and CRM Quote Automation Agent RFQ Review and CRM Quote Automation Agent
```

### 5. Verify the container is running:
```
docker ps
```

### 6. View container logs:
```
docker logs RFQ Review and CRM Quote Automation Agent
```

### 7. Stop the container:
```
docker stop RFQ Review and CRM Quote Automation Agent
```

---

## Notes

- All run commands must use the `code/` prefix (e.g., `python code/agent.py`, `uvicorn code.agent:app ...`).
- See `.env.example` for all required and optional environment variables.
- The agent requires access to LLM API keys and (optionally) Azure SQL for observability.
- For production, configure Key Vault and secure credentials as needed.

---

**RFQ Review and CRM Quote Automation Agent** — Automate RFQ review, CRM quote creation, and HITL escalation with robust audit and observability.