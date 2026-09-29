import os
import tempfile
import unittest
from unittest.mock import patch

from control.app import config


class PortAllocationTests(unittest.TestCase):
    def test_container_mode_does_not_probe_the_control_network_namespace(self):
        block = config._alloc_ports(0)
        with patch.dict(os.environ, {"MDD_CONTAINER_STACK": "1"}), \
                patch.object(config, "_host_port_free") as probe:
            self.assertTrue(config._block_free(block, set()))
        probe.assert_not_called()

    def test_rtp_collision_rejects_the_whole_block(self):
        block = config._alloc_ports(0)
        occupied = block["rtp_start"] + 1
        with patch.object(config, "_host_port_free",
                          side_effect=lambda port: port != occupied):
            self.assertFalse(config._block_free(block, set()))

    def test_new_blocks_use_the_compact_rtp_span(self):
        block = config._alloc_ports(0)
        self.assertEqual(block["rtp_span"], config.DEFAULT_RTP_SPAN)
        self.assertEqual(config.rtp_span(block), 12)
        self.assertEqual(len(config._block_ports(block)), 16)

    def test_saved_blocks_without_span_keep_legacy_width(self):
        block = {key: value for key, value in config._alloc_ports(0).items()
                 if key != "rtp_span"}
        self.assertEqual(config.rtp_span(block), config.LEGACY_RTP_SPAN)
        self.assertIn(block["rtp_start"] + 59, config._block_ports(block))

    def test_the_softphone_ws_port_is_part_of_the_block(self):
        # On native macOS every line shares loopback, so the browser softphone's WS
        # signalling port must be allocated per line and reserved/probed like the rest.
        self.assertEqual(config._alloc_ports(0)["webrtc"], 8088)
        self.assertEqual(config._alloc_ports(1)["webrtc"], 8098)
        self.assertIn(8098, config._block_ports(config._alloc_ports(1)))
        with patch.object(config, "_host_port_free", lambda port: port != 8098):
            self.assertFalse(config._block_free(config._alloc_ports(1), set()))
        # Legacy blocks saved before per-line WS signalling have no "webrtc" key and
        # reserve nothing for it; their engine port is derived from the line's index.
        saved = {key: value for key, value in config._alloc_ports(0).items()
                 if key != "webrtc"}
        self.assertNotIn(8088, config._block_ports(saved))
        with patch.object(config, "_host_port_free", return_value=True):
            self.assertTrue(config._block_free(saved, set()))
        self.assertEqual(config.instance_port({"ports": saved, "index": 1}, "webrtc"), 8098)

    def test_rendered_instance_has_no_softphone_listen_address(self):
        base = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"listen_addr": "0.0.0.0", "webrtc": {"password": "password"}},
            "ports": config._alloc_ports(0),
        }
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            rendered = config.render_instance_json(base, config.DEFAULTS["settings"])
        self.assertNotIn("listen_addr", rendered["sip"])
        self.assertNotIn("port", rendered["sip"]["webrtc"])

    def test_rendered_rtp_end_matches_the_effective_span(self):
        base = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"webrtc": {"password": "password"}},
        }
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            compact = {**base, "ports": config._alloc_ports(0)}
            rendered = config.render_instance_json(compact, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 11)

            legacy_ports = {key: value for key, value in config._alloc_ports(0).items()
                            if key != "rtp_span"}
            legacy = {**base, "ports": legacy_ports}
            rendered = config.render_instance_json(legacy, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 59)


class RenderedControlPortsTests(unittest.TestCase):
    """instance.json is the single source of truth for the engine's control ports."""

    def render(self, inst):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            return config.render_instance_json(
                {"id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
                 "imei": "123456789012345", "ami_secret": "secret",
                 "sip": {"webrtc": {"password": "password"}}, **inst},
                config.DEFAULTS["settings"])

    def test_emitted_ports_match_the_allocated_block(self):
        rendered = self.render({"ports": config._alloc_ports(0)})
        self.assertEqual(rendered["ami_port"], 5038)
        self.assertEqual(rendered["sip_port"], 5060)
        self.assertEqual(rendered["sip_tls_port"], 5061)
        self.assertEqual(rendered["webrtc_ws_port"], 8088)

    def test_index_offsets_each_port_by_its_stride(self):
        rendered = self.render({"ports": config._alloc_ports(1)})
        self.assertEqual(rendered["ami_port"], 5048)
        self.assertEqual(rendered["sip_port"], 5070)
        self.assertEqual(rendered["sip_tls_port"], 5071)
        self.assertEqual(rendered["webrtc_ws_port"], 8098)

    def test_legacy_block_without_webrtc_derives_the_port_from_the_index(self):
        legacy = {key: value for key, value in config._alloc_ports(2).items()
                  if key != "webrtc"}
        rendered = self.render({"ports": legacy, "index": 2})
        self.assertEqual(rendered["webrtc_ws_port"], 8088 + 20)

    def test_caller_name_defaults_empty_and_flows_when_set(self):
        self.assertEqual(self.render({})["caller_name"], "")
        rendered = self.render({"sip": {"caller_name": "Wayne",
                                        "webrtc": {"password": "password"}}})
        self.assertEqual(rendered["caller_name"], "Wayne")


if __name__ == "__main__":
    unittest.main()
