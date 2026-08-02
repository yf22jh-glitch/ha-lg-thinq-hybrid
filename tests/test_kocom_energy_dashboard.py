"""Dashboard safety tests for Kocom and LG energy comparisons."""

from __future__ import annotations

import unittest

from scripts.update_kocom_energy_dashboard import update_dashboard


class KocomEnergyDashboardTests(unittest.TestCase):
    def test_status_and_breakdown_cards_are_repaired(self) -> None:
        status = {
            "type": "custom:mushroom-template-card",
            "entity": "sensor.kocom_eneoji_kocom_energy_usage",
            "primary": "old",
        }
        breakdown = {
            "type": "custom:mushroom-template-card",
            "entity": "sensor.kocom_eneoji_kocom_electricity_usage",
            "primary": "전기 누적",
            "secondary": "이번 달 LG 분류 · old",
        }
        storage = {"data": {"config": {"views": [{"cards": [status, breakdown]}]}}}

        self.assertEqual(update_dashboard(storage), (1, 1))
        self.assertIn("connection_state", status["primary"])
        self.assertIn("자동 재시도 중", status["secondary"])
        self.assertIn("ns.total <= electricity_value", breakdown["secondary"])
        self.assertIn("집계 범위 불일치로 비율 보류", breakdown["secondary"])

        self.assertEqual(update_dashboard(storage), (1, 1))
        self.assertIn("LG 참고 합계", breakdown["secondary"])


if __name__ == "__main__":
    unittest.main()
