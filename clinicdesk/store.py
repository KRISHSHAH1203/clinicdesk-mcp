"""SQLite storage. Every read and write is scoped to a tenant; bookings are transactional
and idempotent."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_patients_tenant_name ON patients(tenant, name);
CREATE TABLE IF NOT EXISTS slots (
  id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, provider TEXT NOT NULL,
  starts_at TEXT NOT NULL, booked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_slots_tenant_start ON slots(tenant, starts_at);
CREATE TABLE IF NOT EXISTS appointments (
  id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, patient_id INTEGER NOT NULL,
  slot_id INTEGER NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(tenant, idempotency_key));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, tenant TEXT NOT NULL, subject TEXT NOT NULL,
  tool TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    # seed helpers -----------------------------------------------------------
    def add_patient(self, tenant: str, name: str, phone: str) -> int:
        cur = self.db.execute("INSERT INTO patients(tenant,name,phone) VALUES (?,?,?)",
                              (tenant, name, phone))
        return int(cur.lastrowid)

    def add_slot(self, tenant: str, provider: str, starts_at: str) -> int:
        cur = self.db.execute("INSERT INTO slots(tenant,provider,starts_at) VALUES (?,?,?)",
                              (tenant, provider, starts_at))
        return int(cur.lastrowid)

    # reads --------------------------------------------------------------------
    def search_patients(self, tenant: str, name_prefix: str, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, name, phone FROM patients WHERE tenant=? AND name LIKE ? "
            "ORDER BY name LIMIT ?", (tenant, name_prefix.replace("%", "") + "%", limit))
        return [{"id": r["id"], "name": r["name"], "phone_last4": r["phone"][-4:]} for r in rows]

    def open_slots(self, tenant: str, provider: str | None, on_date: str | None,
                   limit: int) -> list[dict]:
        sql = "SELECT id, provider, starts_at FROM slots WHERE tenant=? AND booked=0"
        args: list = [tenant]
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        if on_date:
            sql += " AND substr(starts_at,1,10)=?"
            args.append(on_date)
        sql += " ORDER BY starts_at LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def get_appointment(self, tenant: str, appointment_id: int) -> dict:
        r = self.db.execute(
            "SELECT a.id, a.status, a.patient_id, s.provider, s.starts_at FROM appointments a "
            "JOIN slots s ON s.id=a.slot_id WHERE a.tenant=? AND a.id=?",
            (tenant, appointment_id)).fetchone()
        if r is None:
            raise NotFound(f"appointment {appointment_id} not found")
        return dict(r)

    # writes -------------------------------------------------------------------
    def book(self, tenant: str, patient_id: int, slot_id: int, idempotency_key: str) -> tuple[dict, bool]:
        """Book a slot. Returns (appointment, created). A repeated idempotency key returns the
        original appointment instead of booking twice."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT id FROM appointments WHERE tenant=? AND idempotency_key=?",
                (tenant, idempotency_key)).fetchone()
            if prior:
                self.db.execute("COMMIT")
                return self.get_appointment(tenant, prior["id"]), False
            if not self.db.execute("SELECT 1 FROM patients WHERE tenant=? AND id=?",
                                   (tenant, patient_id)).fetchone():
                raise NotFound(f"patient {patient_id} not found")
            slot = self.db.execute("SELECT booked FROM slots WHERE tenant=? AND id=?",
                                   (tenant, slot_id)).fetchone()
            if slot is None:
                raise NotFound(f"slot {slot_id} not found")
            if slot["booked"]:
                raise Conflict(f"slot {slot_id} is already booked")
            self.db.execute("UPDATE slots SET booked=1 WHERE id=?", (slot_id,))
            cur = self.db.execute(
                "INSERT INTO appointments(tenant,patient_id,slot_id,status,idempotency_key,created_at) "
                "VALUES (?,?,?,'BOOKED',?,?)", (tenant, patient_id, slot_id, idempotency_key, _now()))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get_appointment(tenant, int(cur.lastrowid)), True

    def cancel(self, tenant: str, appointment_id: int) -> dict:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            r = self.db.execute("SELECT slot_id, status FROM appointments WHERE tenant=? AND id=?",
                                (tenant, appointment_id)).fetchone()
            if r is None:
                raise NotFound(f"appointment {appointment_id} not found")
            if r["status"] == "CANCELLED":
                self.db.execute("COMMIT")
                return self.get_appointment(tenant, appointment_id)
            self.db.execute("UPDATE appointments SET status='CANCELLED' WHERE id=?", (appointment_id,))
            self.db.execute("UPDATE slots SET booked=0 WHERE id=?", (r["slot_id"],))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get_appointment(tenant, appointment_id)

    def audit(self, tenant: str, subject: str, tool: str, outcome: str, detail: dict) -> None:
        self.db.execute("INSERT INTO audit_log(at,tenant,subject,tool,outcome,detail) VALUES (?,?,?,?,?,?)",
                        (_now(), tenant, subject, tool, outcome, json.dumps(detail, sort_keys=True)))

    def audit_rows(self, tenant: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT tool, outcome, subject FROM audit_log WHERE tenant=? ORDER BY id", (tenant,))]
