import http.server
import builtins
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import notify


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).hits.append({
            "path": self.path,
            "token": self.headers.get("X-MDD-Engine-Token"),
            "payload": json.loads(body or b"{}"),
        })
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class NotifyUrllibFallbackTests(unittest.TestCase):
    def setUp(self):
        _Recorder.hits = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _run(self, argv, env_extra, block_requests=False):
        env = {"MANAGER_URL": self.url, "MANAGER_EVENT_TOKEN": "tok123",
               "MDD_ENV": "/nonexistent", **env_extra}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", argv):
            if block_requests:
                with patch.dict(sys.modules, {"requests": None, "urllib3": None}):
                    notify.main()
            else:
                notify.main()

    def test_posts_via_requests_when_available(self):
        if _requests_missing():
            self.skipTest("requests not installed in this interpreter")
        self._run(["notify.py", "sms_in", "+123", "aGk="], {})
        self.assertEqual(len(_Recorder.hits), 1)
        hit = _Recorder.hits[0]
        self.assertEqual(hit["path"], "/api/engine/event")
        self.assertEqual(hit["token"], "tok123")
        self.assertEqual(hit["payload"]["event"], "sms_in")

    def test_falls_back_to_stdlib_urllib_without_requests(self):
        self._run(["notify.py", "call_in", "+456"], {}, block_requests=True)
        self.assertEqual(len(_Recorder.hits), 1)
        hit = _Recorder.hits[0]
        self.assertEqual(hit["token"], "tok123")
        self.assertEqual(hit["payload"]["event"], "call_in")
        self.assertEqual(hit["payload"]["args"], ["+456"])

    def test_unexpected_import_failure_does_not_escape_to_dialplan(self):
        original_import = builtins.__import__

        def fail_requests(name, *args, **kwargs):
            if name == "requests":
                raise RuntimeError("broken package")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_requests), \
             patch.object(notify, "_warn") as warning:
            self._run(["notify.py", "call_in", "+456"], {})
        warning.assert_called_once()
        self.assertIn("RuntimeError", warning.call_args.args[0])
        self.assertFalse(_Recorder.hits)


def _requests_missing():
    try:
        import requests  # noqa
        return False
    except ImportError:
        return True


if __name__ == "__main__":
    unittest.main()
