from __future__ import annotations

import unittest

from app.notifications import RECIPIENT_ROLES


class NotificationRoutingTests(unittest.TestCase):
    def test_general_and_major_routes_match_confirmed_business_rule(self) -> None:
        self.assertEqual(
            ("safety_officer", "safety_director", "work_area_manager"),
            RECIPIENT_ROLES["general"],
        )
        self.assertEqual(("project_manager", "safety_director"), RECIPIENT_ROLES["major"])


if __name__ == "__main__":
    unittest.main()
