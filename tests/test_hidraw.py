import json
import os
import unittest

from mousectl.core.hidraw import parse_feature_lengths

DESCRIPTORS = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "descriptors.json")))


def parse(hexstr):
    return parse_feature_lengths(bytes.fromhex(hexstr.replace(" ", "")))


class FeatureLengths(unittest.TestCase):
    def test_real_x11_dongle(self):
        # Config interface of an Attack Shark X11 dongle, read from /sys.
        d = DESCRIPTORS["attackshark-x11-dongle"]
        got = parse_feature_lengths(bytes.fromhex(d["descriptor"]))
        self.assertEqual(got, {int(k): v for k, v in d["lengths"].items()})

    def test_no_report_id_is_report_zero(self):
        self.assertEqual(parse("75 08 95 40 b1 02"), {0: 64})

    def test_ignores_input_and_output_items(self):
        self.assertEqual(parse("85 01 75 08 95 04 81 02 91 02"), {})

    def test_feature_items_of_one_report_add_up(self):
        # 1 bit x 3 + 5 bits padding = one byte, then 16 bits x 2 = 4 bytes
        self.assertEqual(parse("85 07 75 01 95 03 b1 02 75 05 95 01 b1 01 75 10 95 02 b1 02"), {7: 1 + 4})

    def test_bits_round_up_to_bytes(self):
        self.assertEqual(parse("85 01 75 01 95 03 b1 02"), {1: 1})

    def test_globals_persist_across_collections(self):
        self.assertEqual(parse("75 08 95 05 a1 01 85 02 b1 02 c0 a1 01 85 03 b1 02 c0"), {2: 5, 3: 5})

    def test_four_byte_data(self):
        # bSize 3 means 4 data bytes
        self.assertEqual(parse("85 01 75 08 97 06 00 00 00 b1 02"), {1: 6})

    def test_pop_restores_size_and_count(self):
        # Push saves size 8 / count 3; inside, 16 x 2 for report 1; after Pop report 2 is 8 x 3 again.
        self.assertEqual(parse("75 08 95 03 a4 75 10 95 02 85 01 b1 02 b4 85 02 b1 02"), {1: 4, 2: 3})

    def test_pop_restores_report_id(self):
        # Report ID is a global item too, so Pop brings back report 5.
        self.assertEqual(parse("85 05 75 08 95 02 a4 85 06 95 09 b1 02 b4 b1 02"), {6: 9, 5: 2})

    def test_pop_without_push_is_ignored(self):
        self.assertEqual(parse("85 01 75 08 95 02 b4 b1 02"), {1: 2})

    def test_long_item_is_skipped_whole(self):
        # 0xFE, bDataSize 2, bLongItemTag, 2 data bytes: 5 bytes in all.
        self.assertEqual(parse("fe 02 10 aa bb 85 01 75 08 95 03 b1 02"), {1: 3})

    def test_truncated_long_item_header(self):
        self.assertEqual(parse("85 01 75 08 95 03 b1 02 fe"), {1: 3})

    def test_truncated_long_item_data(self):
        self.assertEqual(parse("85 01 75 08 95 03 b1 02 fe 08 10 aa"), {1: 3})

    def test_truncated_short_item_is_ignored(self):
        # The last Feature item claims a data byte that isn't there.
        self.assertEqual(parse("85 01 75 08 95 03 b1 02 95 07 b1"), {1: 3})

    def test_empty(self):
        self.assertEqual(parse(""), {})


if __name__ == "__main__":
    unittest.main()
