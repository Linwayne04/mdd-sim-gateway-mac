import unittest
from unittest.mock import patch

from control.app import main


class EsimEngineRunningConflictTests(unittest.TestCase):
    def test_guard_409_is_structured_with_instance_id(self):
        inst = {"id": 7, "reader": "Reader-1"}
        with patch.object(main, "_find_running_by_reader", return_value=inst):
            with self.assertRaises(main.HTTPException) as ctx:
                main._esim_guard_engine("Reader-1")
        exc = ctx.exception
        self.assertEqual(exc.status_code, 409)
        self.assertEqual(exc.detail["code"], "engine_running")
        self.assertEqual(exc.detail["instance_id"], "7")
        self.assertIn("7", exc.detail["message"])

    def test_guard_passes_when_no_line_holds_the_reader(self):
        with patch.object(main, "_find_running_by_reader", return_value=None):
            main._esim_guard_engine("Reader-1")  # must not raise


if __name__ == "__main__":
    unittest.main()
