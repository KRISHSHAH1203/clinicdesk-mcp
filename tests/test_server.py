"""End-to-end tests through a real MCP client session (in memory)."""
import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from clinicdesk.auth import issue_token
from clinicdesk.seed import seed
from clinicdesk.server import build_server
from clinicdesk.store import Store

SECRET = "test-secret"


class Ctx:
    def __init__(self, role="front_desk", tenant="clinic-a", provider=None):
        self.store = Store()
        seed(self.store)
        self.token = issue_token(SECRET, "tester", tenant, role, provider=provider)
        self.server = build_server(self.store, SECRET, lambda: self.token)


async def call(ctx, tool, args):
    async with create_connected_server_and_client_session(ctx.server._mcp_server) as client:
        res = await client.call_tool(tool, args)
        text = res.content[0].text if res.content else ""
        return res.isError, (text if res.isError else _payload(res))


def _payload(res):
    if res.structuredContent is not None:
        sc = res.structuredContent
        return sc.get("result", sc)
    return json.loads(res.content[0].text)


def run(coro):
    return anyio.run(lambda: coro)


@pytest.mark.anyio
async def test_lists_tools_resource_and_prompt():
    ctx = Ctx()
    async with create_connected_server_and_client_session(ctx.server._mcp_server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"search_patients", "list_open_slots", "book_appointment",
                "cancel_appointment", "get_appointment"} <= names
        res = await client.read_resource("clinicdesk://policy")
        assert "idempotency_key" in res.contents[0].text
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert "book_for_patient" in prompts


@pytest.mark.anyio
async def test_search_masks_phone_and_isolates_tenant():
    err, rows = await call(Ctx(), "search_patients", {"name_prefix": "Asha"})
    assert not err
    assert [r["name"] for r in rows] == ["Asha Mehta"]          # clinic-b's Asha is invisible
    assert rows[0]["phone_last4"] == "1234" and "phone" not in rows[0]


@pytest.mark.anyio
async def test_booking_is_idempotent():
    ctx = Ctx()
    args = {"patient_id": 1, "slot_id": 1, "idempotency_key": "retry-key-001"}
    err1, first = await call(ctx, "book_appointment", args)
    err2, second = await call(ctx, "book_appointment", args)
    assert not err1 and not err2
    assert first["id"] == second["id"]
    assert first["replayed"] is False and second["replayed"] is True
    assert ctx.store.db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1


@pytest.mark.anyio
async def test_double_booking_a_slot_conflicts():
    ctx = Ctx()
    await call(ctx, "book_appointment", {"patient_id": 1, "slot_id": 1, "idempotency_key": "key-aaaaaaa1"})
    err, msg = await call(ctx, "book_appointment", {"patient_id": 2, "slot_id": 1, "idempotency_key": "key-bbbbbbb2"})
    assert err and "already booked" in msg


@pytest.mark.anyio
async def test_cross_tenant_ids_are_not_found():
    ctx = Ctx()  # clinic-a caller; slot 4 belongs to clinic-b
    err, msg = await call(ctx, "book_appointment", {"patient_id": 1, "slot_id": 4, "idempotency_key": "key-ccccccc3"})
    assert err and "not found" in msg


@pytest.mark.anyio
async def test_clinician_cannot_write_and_denial_is_audited():
    ctx = Ctx(role="clinician")
    err, msg = await call(ctx, "book_appointment", {"patient_id": 1, "slot_id": 1, "idempotency_key": "key-ddddddd4"})
    assert err and "forbidden" in msg
    assert ctx.store.audit_rows("clinic-a")[-1]["outcome"] == "denied"


@pytest.mark.anyio
async def test_agent_role_cannot_read_patient_directory():
    err, msg = await call(Ctx(role="agent"), "search_patients", {"name_prefix": "As"})
    assert err and "forbidden" in msg


@pytest.mark.anyio
async def test_invalid_token_rejected():
    ctx = Ctx()
    ctx.token = "garbage"
    err, msg = await call(ctx, "list_open_slots", {})
    assert err and "unauthorized" in msg


@pytest.mark.anyio
async def test_input_validation():
    err, _ = await call(Ctx(), "list_open_slots", {"on_date": "02/11/2026"})
    assert err
    err, _ = await call(Ctx(), "book_appointment", {"patient_id": 1, "slot_id": 1, "idempotency_key": "short"})
    assert err


@pytest.mark.anyio
async def test_cancel_frees_slot_and_is_idempotent():
    ctx = Ctx()
    _, appt = await call(ctx, "book_appointment", {"patient_id": 1, "slot_id": 2, "idempotency_key": "key-eeeeeee5"})
    _, slots = await call(ctx, "list_open_slots", {"on_date": "2026-11-02"})
    assert 2 not in [s["id"] for s in slots]
    for _ in range(2):
        err, res = await call(ctx, "cancel_appointment", {"appointment_id": appt["id"]})
        assert not err and res["status"] == "CANCELLED"
    _, slots = await call(ctx, "list_open_slots", {"on_date": "2026-11-02"})
    assert 2 in [s["id"] for s in slots]


@pytest.mark.anyio
async def test_abac_clinician_sees_only_own_appointments():
    ctx = Ctx()
    _, appt = await call(ctx, "book_appointment", {"patient_id": 1, "slot_id": 1, "idempotency_key": "key-fffffff6"})
    ctx.token = issue_token(SECRET, "dr", "clinic-a", "clinician", provider="dr-iyer")
    err, res = await call(ctx, "get_appointment", {"appointment_id": appt["id"]})
    assert not err and res["provider"] == "dr-iyer"
    ctx.token = issue_token(SECRET, "dr2", "clinic-a", "clinician", provider="dr-shah")
    err, msg = await call(ctx, "get_appointment", {"appointment_id": appt["id"]})
    assert err and "not found" in msg
