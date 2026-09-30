import unittest
from unittest.mock import patch

from control.app import main


def _log_block(source, code, cseq=102):
    """One Asterisk 'Received SIP response' block as engine.logs returns it."""
    return (
        f"<--- Received SIP response - {code} {'OK' if code < 300 else 'Error'} --- "
        f"from {source} --->\n"
        f"SIP/2.0 {code} {'OK' if code < 300 else 'Method Not Allowed'}\n"
        f"Via: SIP/2.0/UDP 172.17.0.3:5060\n"
        f"CSeq: {cseq} MESSAGE\n"
        f"Content-Length:  0\n\n"
    )


class SmsResultSkipsSoftphoneWsResponseTests(unittest.TestCase):
    def test_softphone_ws_405_does_not_overwrite_positive_outcome(self):
        raw = _log_block("UDP:10.0.0.1:5060", 202) + _log_block("WS:127.0.0.1:8088", 405)
        with patch.object(main.engine, "logs", return_value=raw):
            self.assertEqual(main.detect_sms_result("1"), {"ok": True, "code": 202, "reason": "OK"})

    def test_softphone_ws_405_alone_is_not_a_carrier_verdict(self):
        raw = _log_block("WS:127.0.0.1:8088", 405)
        with patch.object(main.engine, "logs", return_value=raw):
            self.assertEqual(main.detect_sms_result("1"), {"ok": None})

    def test_real_carrier_rejection_from_udp_peer_is_kept(self):
        raw = _log_block("UDP:10.0.0.1:5060", 403)
        with patch.object(main.engine, "logs", return_value=raw):
            result = main.detect_sms_result("1")
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], 403)


if __name__ == "__main__":
    unittest.main()
