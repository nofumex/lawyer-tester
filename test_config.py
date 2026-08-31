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


if __name__=='__main__': unittest.main()
