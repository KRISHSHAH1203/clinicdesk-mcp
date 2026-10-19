# ClinicDesk MCP

An MCP (Model Context Protocol) server that lets an AI agent work with a clinic scheduling backend: find patients, list open slots, book and cancel appointments. Built so an agent can be trusted with it, which means every call is authorized, scoped to one clinic, validated, idempotent and audited.

**Stack:** Python 3.10+, MCP Python SDK (FastMCP), Pydantic, SQLite, pytest, GitHub Actions.

## What the server exposes

| Kind | Name | Scope needed |
|---|---|---|
| Tool | `search_patients(name_prefix, limit)` | `patients:read` |
| Tool | `list_open_slots(provider?, on_date?, limit)` | `slots:read` |
| Tool | `book_appointment(patient_id, slot_id, idempotency_key)` | `appointments:write` |
| Tool | `cancel_appointment(appointment_id)` | `appointments:write` |
| Tool | `get_appointment(appointment_id)` | `appointments:read` |
| Resource | `clinicdesk://policy` | booking rules the agent reads first |
| Prompt | `book_for_patient(patient_name, preferred_date)` | a safe step-by-step booking flow |

Roles: `front_desk` (everything), `clinician` (read only), `agent` (slots and appointments, but **not** the patient directory).

## Run it

```bash
pip install -e ".[dev]"
pytest -q                      # 15 tests, ~87% coverage, through a real MCP client session

export CLINICDESK_SECRET=change-me
export CLINICDESK_TOKEN=$(python -m clinicdesk.token_cli agent-1 clinic-a agent)
CLINICDESK_SEED=1 python -m clinicdesk     # stdio MCP server
```

Claude Desktop / any MCP client config:

```json
{ "mcpServers": { "clinicdesk": {
    "command": "python", "args": ["-m", "clinicdesk"],
    "env": { "CLINICDESK_SECRET": "change-me", "CLINICDESK_TOKEN": "<token>", "CLINICDESK_SEED": "1" } } } }
```

A LangGraph agent can load the same tools through `langchain-mcp-adapters`.

## Design decisions (what to say in an interview)

**1. The model is not the security boundary.** An LLM can be prompt-injected into calling any tool with any argument, so the server never trusts the caller's intent. Every tool verifies a signed token, checks a scope, and filters every query by the token's tenant. A clinic-a agent asking for clinic-b's slot gets "not found", the same answer as a slot that doesn't exist, so IDs can't be probed.

**2. Idempotent writes.** Agents retry. `book_appointment` takes an `idempotency_key`; a repeat returns the original appointment with `replayed: true` instead of booking twice. A `UNIQUE(tenant, idempotency_key)` constraint backs it up. Cancel is idempotent too.

**3. Transactions.** Booking runs under `BEGIN IMMEDIATE`: check the key, check the patient and slot, mark the slot booked, insert the appointment, all or nothing. Two agents can't book the same slot.

**4. Validation at the edge.** Tool arguments are Pydantic-typed (date pattern, ID ranges, key length), so malformed calls are rejected before any code runs, and the JSON schema the client sees documents the limits.

**5. RBAC plus ABAC.** Roles decide which tools you can call (RBAC). On top of that, an attribute rule (ABAC) uses the `provider` claim in a clinician's token, so a clinician can open only their own appointments. Someone else's appointment looks like it doesn't exist.

**6. Least data.** `search_patients` returns only the last 4 phone digits, enough to confirm identity, not enough to leak it. The `agent` role can't search patients at all.

**7. Audit log.** Every call, including denials, conflicts and not-founds, is written with tenant, subject, tool and outcome, so you can reconstruct what an agent did.

**8. Guidance for the model.** The `clinicdesk://policy` resource and the `book_for_patient` prompt tell the agent how to behave (confirm identity, reuse keys on retry, hand off after two errors). Defence in depth: the rules are written for the model, and enforced in code anyway.

## Ideas to extend

- Streamable HTTP transport with OAuth 2.1 (the MCP auth spec) instead of a static token
- Postgres with row-level security as a second tenant guard
- An evaluation set of agent transcripts that must never double-book or cross tenants
