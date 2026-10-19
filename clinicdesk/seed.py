from .store import Store


def seed(store: Store) -> None:
    if store.db.execute("SELECT COUNT(*) FROM patients").fetchone()[0]:
        return
    store.add_patient("clinic-a", "Asha Mehta", "9876501234")
    store.add_patient("clinic-a", "Arjun Rao", "9876505678")
    store.add_patient("clinic-b", "Asha Verma", "9123409999")
    for t in ("09:00", "09:30", "10:00"):
        store.add_slot("clinic-a", "dr-iyer", f"2026-11-02T{t}:00+05:30")
    store.add_slot("clinic-b", "dr-khan", "2026-11-02T09:00:00+05:30")
