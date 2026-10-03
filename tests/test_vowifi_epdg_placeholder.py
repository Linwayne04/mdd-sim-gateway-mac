import socket
import unittest
from unittest.mock import patch

from control.app import vowifi_support


def _addrinfo(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]


class EpdgPlaceholderDetectionTests(unittest.TestCase):
    def test_all_loopback_answers_are_a_placeholder(self):
        with patch.object(socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            self.assertEqual(vowifi_support.lookup("epdg.example"), "placeholder")

    def test_mixed_loopback_and_real_answer_counts_as_ok(self):
        with patch.object(socket, "getaddrinfo",
                          return_value=_addrinfo("127.0.0.1", "203.0.113.7")):
            self.assertEqual(vowifi_support.lookup("epdg.example"), "ok")

    def test_ipv6_loopback_only_is_a_placeholder(self):
        with patch.object(socket, "getaddrinfo", return_value=_addrinfo("::1")):
            self.assertEqual(vowifi_support.lookup("epdg.example"), "placeholder")

    def test_placeholder_assessment_is_unsupported(self):
        out = vowifi_support.assess("001", "01", "epdg.example", dns="placeholder")
        self.assertEqual(out["status"], vowifi_support.UNSUPPORTED)
        self.assertEqual(out["source"], "dns")
        self.assertIn("placeholder", out["reason"])


if __name__ == "__main__":
    unittest.main()
