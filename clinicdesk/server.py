"""ClinicDesk MCP server: exposes a clinic scheduling backend to AI agents over the
Model Context Protocol, with RBAC, tenant isolation, idempotent writes and an audit log."""
from __future__ import annotations

import os
from typing import Annotated, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .auth import AuthError, Caller, can_view_appointment, verify_token
from .store import Conflict, NotFound, Store

POLICY = """ClinicDesk booking policy (read before acting):
1. Always confirm the patient's identity (name + last 4 digits of phone) before booking.
2. Never book without an explicit time the patient agreed to.
3. Re-use the same idempotency_key when retrying a booking; never invent a new one on retry.
4. If a tool returns an error twice, stop and hand off to front-desk staff.
"""


def build_server(store: Store, secret: str, token_provider: Callable[[], str]) -> FastMCP:
    mcp = FastMCP("clinicdesk")

    def guarded(tool: str, scope: str, detail: dict, fn: Callable[[Caller], dict | list]):
        try:
            caller = verify_token(secret, token_provider())
        except AuthError as exc:
            raise ToolError(f"unauthorized: {exc}") from exc
        try:
            caller.require(scope)
            result = fn(caller)
        except AuthError as exc:
            store.audit(caller.tenant, caller.subject, tool, "denied", detail)
            raise ToolError(f"forbidden: {exc}") from exc
        except NotFound as exc:
            store.audit(caller.tenant, caller.subject, tool, "not_found", detail)
            raise ToolError(str(exc)) from exc
        except Conflict as exc:
            store.audit(caller.tenant, caller.subject, tool, "conflict", detail)
            raise ToolError(str(exc)) from exc
        store.audit(caller.tenant, caller.subject, tool, "ok", detail)
        return result

    @mcp.tool()
    def search_patients(
        name_prefix: Annotated[str, Field(min_length=2, max_length=60, description="Start of the patient's name")],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> list[dict]:
        """Find patients in the caller's clinic by name prefix. Returns only the last 4 phone digits."""
        return guarded("search_patients", "patients:read", {"prefix": name_prefix},
                       lambda c: store.search_patients(c.tenant, name_prefix, limit))

    @mcp.tool()
    def list_open_slots(
        provider: Annotated[str | None, Field(max_length=60)] = None,
        on_date: Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> list[dict]:
        """List unbooked appointment slots, optionally filtered by provider and date."""
        return guarded("list_open_slots", "slots:read", {"provider": provider, "date": on_date},
                       lambda c: store.open_slots(c.tenant, provider, on_date, limit))

    @mcp.tool()
    def book_appointment(
        patient_id: Annotated[int, Field(ge=1)],
        slot_id: Annotated[int, Field(ge=1)],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=64,
                                              description="Reuse the same key when retrying")],
    ) -> dict:
        """Book a slot for a patient. Safe to retry: the same idempotency_key never books twice."""
        def run(c: Caller) -> dict:
            appt, created = store.book(c.tenant, patient_id, slot_id, idempotency_key)
            return {**appt, "replayed": not created}
        return guarded("book_appointment", "appointments:write",
                       {"patient_id": patient_id, "slot_id": slot_id, "key": idempotency_key}, run)

    @mcp.tool()
    def cancel_appointment(appointment_id: Annotated[int, Field(ge=1)]) -> dict:
        """Cancel an appointment and free its slot. Cancelling twice is a no-op."""
        return guarded("cancel_appointment", "appointments:write", {"appointment_id": appointment_id},
                       lambda c: store.cancel(c.tenant, appointment_id))

    @mcp.tool()
    def get_appointment(appointment_id: Annotated[int, Field(ge=1)]) -> dict:
        """Get one appointment in the caller's clinic."""
        def run(c: Caller) -> dict:
            appt = store.get_appointment(c.tenant, appointment_id)
            if not can_view_appointment(c, appt):
                raise NotFound(f"appointment {appointment_id} not found")  # don't reveal it exists
            return appt
        return guarded("get_appointment", "appointments:read", {"appointment_id": appointment_id}, run)

    @mcp.resource("clinicdesk://policy")
    def policy() -> str:
        """Booking rules the agent must follow."""
        return POLICY

    @mcp.prompt()
    def book_for_patient(patient_name: str, preferred_date: str) -> str:
        """A step-by-step prompt for booking a patient safely."""
        return (f"Read clinicdesk://policy first. Find '{patient_name}' with search_patients and confirm "
                f"identity. List open slots on {preferred_date}, offer at most three, and only call "
                f"book_appointment after the patient picks one. Use one idempotency_key per booking.")

    return mcp


def main() -> None:
    secret = os.environ["CLINICDESK_SECRET"]
    store = Store(os.environ.get("CLINICDESK_DB", "clinicdesk.db"))
    if os.environ.get("CLINICDESK_SEED") == "1":
        from .seed import seed
        seed(store)
    build_server(store, secret, lambda: os.environ.get("CLINICDESK_TOKEN", "")).run()


if __name__ == "__main__":
    main()
