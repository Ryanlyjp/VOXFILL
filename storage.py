"""Small JSON-backed persistent state store for the local panel."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS: dict[str, Any] = {
    "target_url": os.getenv("TARGET_URL", "https://voxi.co.uk/sim-only-plans"),
    "follow_plan_flow": False,
    "plan_link_selector": 'a:has-text("See all SIM only")',
    "choose_plan_selector": '[data-test="choose-plan-button"]',
    "continue_selector": 'button[aria-label="Continue"]',
    "email_selector": "#email",
    "first_name_selector": "#firstName",
    "last_name_selector": "#lastName",
    "dob_day_selector": "#dateOfBirth-day",
    "dob_month_selector": "#dateOfBirth-month",
    "dob_year_selector": "#dateOfBirth-year",
    "postcode_selector": "#postcode-input-postcode",
    "postcode_trigger_selector": "#postcode-input-postcode-trigger",
    "address_selector": "#address-select-postcode",
    "password_selector": "#password",
    "pin_selector": "#pin",
    "memorable_word_selector": "#memorableWord",
    "consent_selector": "#consentAgreement",
    "payment_cancel_selector": "input#btnCancel",
    "cookie_reject_selector": "#onetrust-reject-all-handler",
    "proxy_pool": "",
    "proxy_rotation": True,
    "headless": True,
    "debug_mode": False,
    "click_cancel": True,
    "settlement_wait_minutes": 0,
    "timeout_ms": 15000,
    "task_count": 1,
    "concurrency": 1,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lines(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        value = raw.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "state.json"
        self._lock = threading.RLock()
        self._state = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "emails": [],
            "postcodes": [],
            "profile_cards": [],
            "settings": dict(DEFAULT_SETTINGS),
            "task_records": [],
            "runtime": {
                "status": "idle",
                "message": "",
                "email": "",
                "postcode": "",
                "started_at": None,
                "finished_at": None,
                "logs": [],
                "screenshots": [],
                "task_total": 0,
                "task_completed": 0,
                "task_success": 0,
                "task_failed": 0,
                "task_concurrency": 1,
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default()
        state = self._default()
        state.update({k: v for k, v in data.items() if k in state})
        saved_settings = state.get("settings", {})
        state["settings"] = {**DEFAULT_SETTINGS, **saved_settings}
        if saved_settings.get("continue_selector") == 'button:has-text("Continue")':
            state["settings"]["continue_selector"] = DEFAULT_SETTINGS["continue_selector"]
        if saved_settings.get("payment_cancel_selector") == "#btnCancel":
            state["settings"]["payment_cancel_selector"] = DEFAULT_SETTINGS["payment_cancel_selector"]
        state["runtime"] = {**self._default()["runtime"], **state.get("runtime", {})}
        return state

    def _save(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def replace_emails(self, text: str) -> list[dict[str, Any]]:
        with self._lock:
            previous = {item["value"]: item.get("used_at") for item in self._state["emails"]}
            self._state["emails"] = [
                {"value": value, "used_at": previous.get(value)} for value in _lines(text)
            ]
            self._save()
            return self._state["emails"]

    def replace_postcodes(self, text: str) -> list[str]:
        with self._lock:
            self._state["postcodes"] = _lines(text)
            self._save()
            return list(self._state["postcodes"])

    def add_profile_card(self, card: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cards = self._state.setdefault("profile_cards", [])
            cards.insert(0, dict(card))
            self._state["profile_cards"] = cards[:500]
            self._save()
            return dict(card)

    def update_profile_card(self, card_id: str, **values: Any) -> bool:
        with self._lock:
            for card in self._state.get("profile_cards", []):
                if card.get("id") == card_id:
                    card.update(values)
                    self._save()
                    return True
            return False

    def delete_profile_card(self, card_id: str) -> bool:
        with self._lock:
            cards = self._state.get("profile_cards", [])
            kept = [card for card in cards if card.get("id") != card_id]
            if len(kept) == len(cards):
                return False
            self._state["profile_cards"] = kept
            self._save()
            return True

    def delete_unpinned_profile_cards(self) -> int:
        with self._lock:
            cards = self._state.get("profile_cards", [])
            kept = [card for card in cards if card.get("pinned", False)]
            deleted = len(cards) - len(kept)
            if deleted:
                self._state["profile_cards"] = kept
                self._save()
            return deleted

    def claim_next_email(self) -> str | None:
        emails = self.claim_next_emails(1)
        return emails[0] if emails else None

    def claim_next_emails(self, count: int) -> list[str]:
        if count < 1:
            return []
        with self._lock:
            available = [item for item in self._state["emails"] if not item.get("used_at")]
            if len(available) < count:
                return []
            claimed: list[str] = []
            now = _now()
            for item in self._state["emails"]:
                if item.get("used_at"):
                    continue
                item["used_at"] = now
                claimed.append(str(item["value"]))
                if len(claimed) == count:
                    self._save()
                    return claimed
        return []

    def available_email_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._state["emails"] if not item.get("used_at"))

    def reset_email_marks(self) -> None:
        with self._lock:
            for item in self._state["emails"]:
                item["used_at"] = None
            self._save()

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            allowed = set(DEFAULT_SETTINGS)
            self._state["settings"].update({k: v for k, v in values.items() if k in allowed})
            self._save()
            return dict(self._state["settings"])

    def set_runtime(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._state["runtime"].update(values)
            self._save()
            return dict(self._state["runtime"])

    def append_log(self, message: str, level: str = "info") -> dict[str, str]:
        with self._lock:
            item = {"at": _now(), "level": level, "message": message}
            logs = self._state["runtime"].setdefault("logs", [])
            logs.append(item)
            self._state["runtime"]["logs"] = logs[-400:]
            self._state["runtime"]["message"] = message
            self._save()
            return item

    def add_screenshot(self, filename: str) -> None:
        self.add_screenshot_for_task(filename)

    def add_screenshot_for_task(self, filename: str, record_id: str | None = None) -> None:
        with self._lock:
            screenshots = self._state["runtime"].setdefault("screenshots", [])
            screenshots.append({"at": _now(), "filename": filename})
            self._state["runtime"]["screenshots"] = screenshots[-50:]
            if record_id:
                for record in self._state["task_records"]:
                    if record.get("id") == record_id:
                        record.setdefault("screenshots", []).append(filename)
                        break
            self._save()

    def add_task_record(self, record: dict[str, Any]) -> None:
        with self._lock:
            records = self._state.setdefault("task_records", [])
            records.insert(0, dict(record))
            self._state["task_records"] = records[:500]
            self._save()

    def update_task_record(self, record_id: str, **values: Any) -> None:
        with self._lock:
            for record in self._state.get("task_records", []):
                if record.get("id") == record_id:
                    record.update(values)
                    self._save()
                    return

    def complete_task_record(
        self,
        record_id: str,
        status: str,
        message: str,
        finished_at: str,
    ) -> bool:
        with self._lock:
            found = False
            for record in self._state.get("task_records", []):
                if record.get("id") == record_id:
                    if record.get("status") != "running":
                        return False
                    record.update(status=status, message=message, finished_at=finished_at)
                    found = True
                    break
            if not found:
                return False
            runtime = self._state["runtime"]
            runtime["task_completed"] = int(runtime.get("task_completed", 0)) + 1
            if status == "success":
                runtime["task_success"] = int(runtime.get("task_success", 0)) + 1
            else:
                runtime["task_failed"] = int(runtime.get("task_failed", 0)) + 1
            self._save()
            return True

    def delete_task_record(self, record_id: str) -> bool:
        with self._lock:
            records = self._state.get("task_records", [])
            kept = [record for record in records if record.get("id") != record_id]
            if len(kept) == len(records):
                return False
            self._state["task_records"] = kept
            self._save()
            return True

    def clear_task_records(self) -> None:
        with self._lock:
            self._state["task_records"] = []
            self._save()
