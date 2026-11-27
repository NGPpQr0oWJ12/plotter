from dataclasses import replace
import unittest
from unittest.mock import Mock, call, patch

from machine import Contour, build_plan
from xiao_job import (
    JobEncodingError, JobTransferError, crc16, encode_job, finalization, prepare_single_page, transfer_job,
)


ENDPOINT = ("192.0.2.10", 1234)


def prepare(*contours, tool=4, units="mm", waste_trim_count=0):
    return encode_job(contours, tool=tool, units=units, width=2400, height=1750,
                      waste_trim_count=waste_trim_count)


class XiaoJobTests(unittest.TestCase):
    def setUp(self):
        self.line = Contour(((0, 0), (10, 20)), False, "Крой")

    def test_open_contour_matches_native_checksum_table_fixture(self):
        job = prepare(self.line)
        self.assertEqual(crc16(b"123456789"), 0x4B37)
        self.assertEqual(job.point_count, 2)
        self.assertEqual(job.chunks[0].payload,
                         b"[0](0,4,0.010,0.010,1.00)(1,4,10.010,20.010,1.00)[2]<\xdf\xc4>\0")

    def test_closed_contour_matches_native_checksum_table_fixture(self):
        shape = Contour(((1, 2), (11, 2), (11, 12)), True, "Крой")
        job = prepare(shape, tool=8)
        self.assertEqual(job.point_count, 4)
        self.assertEqual(job.chunks[0].payload,
                         b"[0](0,8,1.010,2.010,1.00)(1,8,11.010,2.010,1.00)"
                         b"(1,8,11.010,12.010,1.00)(1,8,1.010,2.010,1.00)[4]<\xb2\xb3>\0")

    def test_block_split_keeps_contour_pen_state_and_cumulative_indices(self):
        long = Contour(tuple((x, 1000) for x in range(100, 180)), False, "Крой")
        job = prepare(long, self.line)
        self.assertEqual([chunk.next_index for chunk in job.chunks], [34, 68, 80, 82])
        self.assertTrue(job.chunks[1].payload.startswith(b"[34](1,4,134.010,"))
        self.assertTrue(job.chunks[3].payload.startswith(b"[80](0,4,0.010,"))
        self.assertTrue(all(len(chunk.payload) < 1000 for chunk in job.chunks))
        self.assertEqual(job.vertex(34), ((133.01, 1000.01), True))
        self.assertEqual(job.vertex(35), ((134.01, 1000.01), True))
        self.assertEqual(job.vertex(81), ((0.01, 0.01), False))

    def test_invalid_geometry_units_and_tool_are_rejected(self):
        at_edge = Contour(((0, 0), (2400, 20)), False, "Крой")
        too_small = Contour(((0, 0), (0.0001, 0)), False, "Крой")
        for contours, tool, units in [((self.line,), True, "mm"), ((self.line,), 4, "cm"),
                                      ((at_edge,), 4, "mm"), ((too_small,), 4, "mm")]:
            with self.subTest(contours=contours, tool=tool, units=units), self.assertRaises(JobEncodingError):
                prepare(*contours, tool=tool, units=units)

    def test_page_commit_requires_explicit_distance(self):
        with self.assertRaises(JobEncodingError):
            finalization(paged=True)
        last = finalization(paged=True, page_distance_mm=10.12)
        self.assertEqual((last.packet, last.acknowledgement), (b"end10120)\0", b"endok"))
        intermediate = finalization(paged=True, last_page=False, page_distance_mm=2400)
        self.assertEqual((intermediate.packet, intermediate.acknowledgement), (b"page2400000)\0", b"pageok"))

    def test_single_page_preserves_xy_placement_and_uses_absolute_page_end(self):
        for origin, end in [((0, 0), b"end10120)\0"), ((20, 30), b"end30120)\0")]:
            with self.subTest(origin=origin):
                x, y = origin
                plan = build_plan({"paths": [{"points": [[x, y], [x + 10, y + 20]]}]}, 2400, 1750)
                job, finish = prepare_single_page(plan, tool=4, width=2400, height=1750, page_add_mm=0.12)
                self.assertIn(f"(0,4,{x + 0.01:.3f},{y + 0.01:.3f},1.00)".encode("ascii"), job.chunks[0].payload)
                self.assertEqual(finish.packet, end)
                self.assertEqual(plan.contours[0].points[0], origin)

    def test_single_page_waste_trim_preserves_signed_y_and_vertex_indices(self):
        plan = build_plan({"paths": [{"points": [[0, 0], [10, 20]]}]}, 2400, 1750)
        trim = Contour(((5, -30), (5, 1420)), False, "Обрезка отхода")
        plan = replace(plan, contours=(*plan.contours, trim))
        job, finish = prepare_single_page(plan, tool=4, width=2400, height=1750, page_add_mm=0.12,
                                          waste_trim_count=1)
        self.assertEqual(job.path_end_indices, (2, 4))
        self.assertEqual(job.vertex(2), ((10.01, 20.01), True))
        self.assertEqual(job.vertex(3), ((5.01, -29.99), False))
        self.assertEqual(job.vertex(4), ((5.01, 1420.01), True))
        self.assertEqual(finish.packet, b"end10120)\0")

    def test_only_trailing_waste_trim_contours_can_use_bounded_negative_y(self):
        trim = Contour(((5, -30), (5, 1420)), False, "Обрезка отхода")
        below_limit = Contour(((5, -30.001), (5, 1420)), False, trim.layer)
        negative_x = Contour(((-1, -30), (5, 1420)), False, trim.layer)
        above_limit = Contour(((5, -30), (5, 1750)), False, trim.layer)
        for contours, count in [((trim,), 0), ((trim, self.line), 1), ((below_limit,), 1),
                                ((negative_x,), 1), ((above_limit,), 1), ((trim,), 2)]:
            with self.subTest(contours=contours, count=count), self.assertRaises(JobEncodingError):
                prepare(*contours, waste_trim_count=count)

    def test_single_page_rejects_repeats_feed_and_page_extent_outside_x_limit(self):
        for points, overrides in [([[0, 0], [10, 20]], {"repeats": 2}),
                                  ([[0, 0], [10, 20]], {"feed": 12}),
                                  ([[0, 0], [2399.95, 20]], {})]:
            with self.subTest(points=points, overrides=overrides):
                plan = build_plan({"paths": [{"points": points}], **overrides}, 2400, 1750)
                with self.assertRaises(JobEncodingError):
                    prepare_single_page(plan, tool=4, width=2400, height=1750, page_add_mm=0.12)

    def test_single_page_rejects_nonzero_native_page_offset(self):
        plan = build_plan({"paths": [{"points": [[20, 30], [30, 50]]}]}, 2400, 1750)
        with self.assertRaises(JobEncodingError):
            prepare_single_page(plan, tool=4, width=2400, height=1750, page_add_mm=0.12, page_x_offset_mm=1)

    @patch("xiao_job.sleep")
    def test_transfer_acks_are_bound_to_controller_and_current_index(self, _sleep):
        client = Mock()
        client.recvfrom.side_effect = [
            (b"hpgl1\0", (ENDPOINT[0], 1235)), (b"hpgl1\0", ENDPOINT),
            (b"[0]\0", ENDPOINT), (b"[2]\0", ENDPOINT), (b"endok\0", ENDPOINT),
        ]
        job = prepare(self.line)
        finish = finalization(paged=True, page_distance_mm=10.12)
        self.assertEqual(transfer_job(client, ENDPOINT, job, finish), 2)
        self.assertEqual(client.sendto.call_args_list, [
            call(b"hpgl1\0", ENDPOINT), call(job.chunks[0].payload, ENDPOINT), call(b"end10120)\0", ENDPOINT),
        ])

    @patch("xiao_job.sleep")
    def test_transfer_ack_accepts_padding_but_not_a_different_c_string(self, _sleep):
        client = Mock()
        client.recvfrom.side_effect = [
            (b"hpgl12\0", ENDPOINT), (b"hpgl1\0\0", ENDPOINT),
            (b"[2]extra\0", ENDPOINT), (b"[2]\0\0", ENDPOINT),
            (b"endok\0\0", ENDPOINT),
        ]
        job = prepare(self.line)
        self.assertEqual(transfer_job(client, ENDPOINT, job, finalization(paged=False)), 2)
        self.assertEqual(client.recvfrom.call_count, 5)

    def test_missing_block_ack_leaves_transfer_uncommitted(self):
        client = Mock()
        client.recvfrom.side_effect = [(b"hpgl1\0", ENDPOINT), (b"[99]\0", ENDPOINT), TimeoutError]
        job = prepare(self.line)
        with self.assertRaises(JobTransferError) as result:
            transfer_job(client, ENDPOINT, job, finalization(paged=False))
        self.assertEqual(result.exception.acknowledged_points, 0)
        self.assertFalse(result.exception.commit_sent)
        self.assertEqual(client.sendto.call_count, 2)

    @patch("xiao_job.sleep")
    def test_missing_final_ack_is_ambiguous_and_does_not_retransmit(self, _sleep):
        client = Mock()
        client.recvfrom.side_effect = [(b"hpgl1\0", ENDPOINT), (b"[2]\0", ENDPOINT), TimeoutError]
        job = prepare(self.line)
        with self.assertRaises(JobTransferError) as result:
            transfer_job(client, ENDPOINT, job, finalization(paged=False))
        self.assertEqual(result.exception.acknowledged_points, 2)
        self.assertTrue(result.exception.commit_sent)
        self.assertEqual(client.sendto.call_count, 3)
