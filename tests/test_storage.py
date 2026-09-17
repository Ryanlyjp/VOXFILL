import json
import tempfile
import unittest
from pathlib import Path

from storage import DEFAULT_SETTINGS, Store


class DefaultSettingsTests(unittest.TestCase):
    def test_starts_at_the_sim_only_plans_page(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["target_url"], "https://voxi.co.uk/sim-only-plans")
        self.assertFalse(DEFAULT_SETTINGS["follow_plan_flow"])

    def test_uses_current_checkout_selectors(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["continue_selector"], 'button[aria-label="Continue"]')
        self.assertEqual(DEFAULT_SETTINGS["payment_cancel_selector"], "input#btnCancel")
        self.assertTrue(DEFAULT_SETTINGS["click_cancel"])
        self.assertEqual(DEFAULT_SETTINGS["settlement_wait_minutes"], 0)

    def test_migrates_legacy_checkout_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "settings": {
                            "continue_selector": 'button:has-text("Continue")',
                            "payment_cancel_selector": "#btnCancel",
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = Store(directory).snapshot()["settings"]

        self.assertEqual(settings["continue_selector"], 'button[aria-label="Continue"]')
        self.assertEqual(settings["payment_cancel_selector"], "input#btnCancel")


class ProfileCardTests(unittest.TestCase):
    def test_profile_cards_persist_can_be_renamed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            card = {
                "id": "card-1",
                "name": "资料卡 1",
                "email": "",
                "dob": "25012003",
                "first_name": "Ava",
                "last_name": "Carter",
                "address": "M14 7HX",
                "password": "Pass1!",
                "pin": "1234",
                "memorable_word": "bluebird",
                "phone": "",
            }
            store.add_profile_card(card)

            reopened = Store(directory)
            self.assertEqual(reopened.snapshot()["profile_cards"], [card])
            self.assertTrue(reopened.update_profile_card("card-1", name="已编辑资料"))
            self.assertEqual(reopened.snapshot()["profile_cards"][0]["name"], "已编辑资料")
            self.assertTrue(reopened.delete_profile_card("card-1"))
            self.assertEqual(reopened.snapshot()["profile_cards"], [])

    def test_bulk_delete_preserves_pinned_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add_profile_card({"id": "pinned", "name": "置顶", "pinned": True})
            store.add_profile_card({"id": "normal", "name": "普通", "pinned": False})
            store.add_profile_card({"id": "legacy", "name": "旧卡"})

            self.assertEqual(store.delete_unpinned_profile_cards(), 2)
            self.assertEqual(
                [card["id"] for card in store.snapshot()["profile_cards"]],
                ["pinned"],
            )

    def test_pinning_does_not_change_card_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add_profile_card({"id": "older", "name": "较早资料"})
            store.add_profile_card({"id": "newer", "name": "较新资料"})

            self.assertTrue(store.update_profile_card("older", pinned=True))
            cards = store.snapshot()["profile_cards"]
            self.assertEqual([card["id"] for card in cards], ["newer", "older"])
            self.assertTrue(cards[1]["pinned"])
