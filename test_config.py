from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from config import Config


class ConfigTests(unittest.TestCase):
    def test_max_admin_id_is_included_in_admin_ids(self) -> None:
        with patch.dict(os.environ, {'ADMIN_IDS':'1,2','MAX_ADMIN_ID':'185607445'}, clear=True):
            config=Config.from_env()
        self.assertEqual(config.admin_ids, frozenset({'1','2','185607445'}))

    def test_manager_ids_are_telegram_recipients(self) -> None:
        with patch.dict(os.environ, {'MANAGER_IDS':'8608404966,7727079839'}, clear=True):
            config=Config.from_env()
        self.assertEqual(config.manager_ids, frozenset({'8608404966','7727079839'}))


if __name__=='__main__': unittest.main()
