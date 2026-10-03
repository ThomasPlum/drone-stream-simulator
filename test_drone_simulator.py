"""Contract tests for RGB/infrared capture-time synchronization.

Run with: python -m unittest -v test_drone_simulator
"""

import random
import unittest
from unittest.mock import patch

from drone_stream_simulator import Config, EventLog, Frame, Pair, PairSynchronizer, Simulation


SECOND = 1_000_000


def frame(stream, frame_id, capture_us, arrival_us=None):
    """Create a frame with an explicit common capture/arrival time scale."""
    if arrival_us is None:
        arrival_us = capture_us
    return Frame(
        stream=stream,
        frame_id=frame_id,
        capture_us=capture_us,
        arrival_us=arrival_us,
        path="",
    )


class PairTests(unittest.TestCase):
    def test_gap_is_absolute_capture_time_difference(self):
        rgb = frame("rgb", 1, 5 * SECOND, 9 * SECOND)
        ir = frame("ir", 2, 3 * SECOND, 10 * SECOND)
        self.assertEqual(Pair(rgb, ir, 10 * SECOND).gap_us, 2 * SECOND)


class PairSynchronizerTests(unittest.TestCase):
    def assert_buffers_empty(self, synchronizer):
        self.assertEqual(synchronizer.buffers["rgb"], [])
        self.assertEqual(synchronizer.buffers["ir"], [])

    def test_exact_two_second_gap_is_accepted(self):
        synchronizer = PairSynchronizer()
        rgb = frame("rgb", 1, 0, 0)
        ir = frame("ir", 1, 2 * SECOND, 2 * SECOND)
        self.assertEqual(synchronizer.push(rgb, 0), [])

        pairs = synchronizer.push(ir, 2 * SECOND)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].rgb, rgb)
        self.assertEqual(pairs[0].ir, ir)
        self.assertEqual(pairs[0].gap_us, 2 * SECOND)
        self.assertEqual(pairs[0].emitted_us, 2 * SECOND)
        self.assert_buffers_empty(synchronizer)

    def test_two_seconds_plus_one_microsecond_does_not_pair(self):
        synchronizer = PairSynchronizer()
        synchronizer.push(frame("rgb", 1, 0, 0), 0)

        pairs = synchronizer.push(
            frame("ir", 1, 2 * SECOND + 1), 2 * SECOND + 1
        )

        self.assertEqual(pairs, [])
        self.assertEqual(len(synchronizer.buffers["rgb"]), 1)
        self.assertEqual(len(synchronizer.buffers["ir"]), 1)
        synchronizer.flush()
        self.assertEqual(synchronizer.stats["unmatched"], 2)
        self.assert_buffers_empty(synchronizer)

    def test_three_second_network_delay_does_not_reject_synchronized_frames(self):
        synchronizer = PairSynchronizer()
        rgb = frame("rgb", 1, 0, 0)
        ir = frame("ir", 1, 0, 3 * SECOND)
        synchronizer.push(rgb, 0)

        pairs = synchronizer.push(ir, 3 * SECOND)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].gap_us, 0)
        self.assertEqual(pairs[0].emitted_us, 3 * SECOND)
        self.assertEqual(synchronizer.stats["expired"], 0)

    def test_nearest_currently_arrived_frame_is_selected(self):
        synchronizer = PairSynchronizer()
        candidates = [
            frame("rgb", 1, 0, 3 * SECOND),
            frame("rgb", 2, SECOND, 3 * SECOND + 1),
            frame("rgb", 3, 2 * SECOND, 3 * SECOND + 2),
        ]
        for candidate in candidates:
            self.assertEqual(
                synchronizer.push(candidate, candidate.arrival_us), []
            )
        infrared = frame("ir", 1, 1_900_000, 3 * SECOND + 3)

        pairs = synchronizer.push(infrared, infrared.arrival_us)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].rgb.frame_id, 3)
        self.assertEqual(pairs[0].gap_us, 100_000)
        self.assertEqual(synchronizer.stats["stale"], 2)
        self.assert_buffers_empty(synchronizer)

    def test_a_future_better_match_cannot_replace_an_emitted_pair(self):
        synchronizer = PairSynchronizer()
        rgb = frame("rgb", 1, SECOND, 3 * SECOND)
        ir = frame("ir", 1, 1_500_000, 3 * SECOND + 1)
        self.assertEqual(synchronizer.push(rgb, rgb.arrival_us), [])
        pairs = synchronizer.push(ir, ir.arrival_us)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].gap_us, 500_000)

        # This closer candidate arrives only after the first pair was emitted.
        later = frame("ir", 2, SECOND, 4 * SECOND)
        self.assertEqual(synchronizer.push(later, later.arrival_us), [])
        self.assertEqual(pairs[0].ir.frame_id, 1)
        self.assertEqual(synchronizer.stats["stale"], 1)
        self.assert_buffers_empty(synchronizer)

    def test_out_of_order_frames_cannot_rewind_or_reuse_output(self):
        synchronizer = PairSynchronizer()
        arrivals = [
            frame("rgb", 1, 100, 100),
            frame("ir", 1, 100, 100),
            frame("rgb", 3, 300, 300),
            frame("rgb", 2, 200, 301),
            frame("ir", 3, 310, 310),
            frame("ir", 2, 210, 311),
            frame("rgb", 4, 400, 400),
            frame("ir", 4, 400, 400),
        ]
        pairs = []
        for incoming in arrivals:
            pairs.extend(synchronizer.push(incoming, incoming.arrival_us))

        self.assertEqual(len(pairs), 3)
        for stream in ("rgb", "ir"):
            output_frames = [getattr(pair, stream) for pair in pairs]
            captures = [output.capture_us for output in output_frames]
            self.assertTrue(
                all(earlier < later for earlier, later in zip(captures, captures[1:]))
            )
            identifiers = [output.frame_id for output in output_frames]
            self.assertEqual(len(identifiers), len(set(identifiers)))
            self.assertEqual(synchronizer.last_capture[stream], captures[-1])
        self.assertEqual(synchronizer.stats["stale"], 2)
        self.assertTrue(all(pair.gap_us <= 2 * SECOND for pair in pairs))
        self.assert_buffers_empty(synchronizer)

    def test_capture_equal_to_last_output_is_stale(self):
        synchronizer = PairSynchronizer()
        synchronizer.push(frame("rgb", 1, SECOND, SECOND), SECOND)
        self.assertEqual(
            len(synchronizer.push(frame("ir", 1, SECOND), SECOND)), 1
        )

        for stream in ("rgb", "ir"):
            duplicate_time = frame(stream, 2, SECOND, 2 * SECOND)
            self.assertEqual(
                synchronizer.push(duplicate_time, duplicate_time.arrival_us), []
            )

        self.assertEqual(synchronizer.stats["stale"], 2)
        self.assert_buffers_empty(synchronizer)

    def test_seeded_drops_and_reordering_preserve_output_invariants(self):
        randomizer = random.Random(4271)
        synchronizer = PairSynchronizer(
            buffer_ttl_us=1_200_000, buffer_limit=7
        )
        arrivals = []
        for stream, period in (("rgb", 200_000), ("ir", 220_000)):
            for frame_id in range(60):
                capture_us = frame_id * period
                delay_us = randomizer.randrange(0, 4 * SECOND + 1)
                if randomizer.random() >= 0.3:
                    arrivals.append(
                        frame(stream, frame_id, capture_us, capture_us + delay_us)
                    )
        arrivals.sort(key=lambda incoming: incoming.arrival_us)
        pairs = []
        last_emitted = {"rgb": None, "ir": None}

        for incoming in arrivals:
            emitted = synchronizer.push(incoming, incoming.arrival_us)
            for pair in emitted:
                self.assertLessEqual(pair.gap_us, 2 * SECOND)
                self.assertGreaterEqual(pair.emitted_us, pair.rgb.arrival_us)
                self.assertGreaterEqual(pair.emitted_us, pair.ir.arrival_us)
                for stream in ("rgb", "ir"):
                    capture_us = getattr(pair, stream).capture_us
                    if last_emitted[stream] is not None:
                        self.assertGreater(capture_us, last_emitted[stream])
                    last_emitted[stream] = capture_us
            pairs.extend(emitted)
            for stream in ("rgb", "ir"):
                buffered = synchronizer.buffers[stream]
                self.assertLessEqual(len(buffered), 7)
                for pending in buffered:
                    self.assertLessEqual(
                        incoming.arrival_us - pending.arrival_us, 1_200_000
                    )
                    if last_emitted[stream] is not None:
                        self.assertGreater(
                            pending.capture_us, last_emitted[stream]
                        )

        self.assertGreater(len(pairs), 0)
        for stream in ("rgb", "ir"):
            identifiers = [getattr(pair, stream).frame_id for pair in pairs]
            self.assertEqual(len(identifiers), len(set(identifiers)))
        synchronizer.flush()
        self.assert_buffers_empty(synchronizer)

    def test_ttl_keeps_exact_boundary_and_expires_one_microsecond_later(self):
        synchronizer = PairSynchronizer(buffer_ttl_us=SECOND)
        synchronizer.push(frame("rgb", 1, 0, 0), 0)

        synchronizer.expire(SECOND)
        self.assertEqual(len(synchronizer.buffers["rgb"]), 1)
        self.assertEqual(synchronizer.stats["expired"], 0)

        synchronizer.expire(SECOND + 1)
        self.assert_buffers_empty(synchronizer)
        self.assertEqual(synchronizer.stats["expired"], 1)

    def test_ttl_measures_wait_since_arrival_not_age_since_capture(self):
        synchronizer = PairSynchronizer(buffer_ttl_us=SECOND)
        delayed = frame("rgb", 1, 0, 10 * SECOND)
        synchronizer.push(delayed, delayed.arrival_us)

        synchronizer.expire(11 * SECOND)
        self.assertEqual(synchronizer.buffers["rgb"], [delayed])
        self.assertEqual(synchronizer.stats["expired"], 0)

        synchronizer.expire(11 * SECOND + 1)
        self.assert_buffers_empty(synchronizer)
        self.assertEqual(synchronizer.stats["expired"], 1)

    def test_push_expires_waiting_frames_before_matching(self):
        synchronizer = PairSynchronizer(buffer_ttl_us=SECOND)
        synchronizer.push(frame("rgb", 1, 0, 0), 0)

        pairs = synchronizer.push(
            frame("ir", 1, 0, SECOND + 1), SECOND + 1
        )

        self.assertEqual(pairs, [])
        self.assertEqual(synchronizer.stats["expired"], 1)
        self.assertEqual(len(synchronizer.buffers["ir"]), 1)

    def test_buffer_limit_removes_earliest_capture_for_each_stream(self):
        for stream in ("rgb", "ir"):
            with self.subTest(stream=stream):
                synchronizer = PairSynchronizer(buffer_limit=2)
                arrivals = [
                    frame(stream, 3, 3 * SECOND, 4 * SECOND),
                    frame(stream, 1, SECOND, 4 * SECOND + 1),
                    frame(stream, 2, 2 * SECOND, 4 * SECOND + 2),
                ]
                for incoming in arrivals:
                    self.assertEqual(
                        synchronizer.push(incoming, incoming.arrival_us), []
                    )
                    self.assertLessEqual(len(synchronizer.buffers[stream]), 2)

                retained = synchronizer.buffers[stream]
                self.assertEqual(
                    sorted(item.capture_us for item in retained),
                    [2 * SECOND, 3 * SECOND],
                )
                self.assertEqual(synchronizer.stats["overflow"], 1)

    def test_flush_clears_unmatched_frames_and_is_idempotent(self):
        synchronizer = PairSynchronizer()
        synchronizer.push(frame("rgb", 1, 0, 0), 0)
        synchronizer.push(frame("ir", 1, 3 * SECOND), 3 * SECOND)

        synchronizer.flush()
        self.assert_buffers_empty(synchronizer)
        self.assertEqual(synchronizer.stats["unmatched"], 2)

        synchronizer.flush()
        self.assertEqual(synchronizer.stats["unmatched"], 2)
        self.assert_buffers_empty(synchronizer)

    def test_zero_gap_and_zero_ttl_allow_same_instant_pairing(self):
        synchronizer = PairSynchronizer(max_gap_us=0, buffer_ttl_us=0)
        synchronizer.push(frame("rgb", 1, SECOND), SECOND)

        pairs = synchronizer.push(frame("ir", 1, SECOND), SECOND)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].gap_us, 0)
        self.assertEqual(synchronizer.stats["expired"], 0)

    def test_invalid_bounds_are_rejected(self):
        invalid_settings = [
            {"max_gap_us": -1},
            {"max_gap_us": 2 * SECOND + 1},
            {"buffer_ttl_us": -1},
            {"buffer_limit": 0},
            {"buffer_limit": -1},
        ]
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    PairSynchronizer(**settings)


class FakeDataset:
    """Small source indexes; Simulation must not read/decompress image data."""

    def __init__(self):
        self.sequences = {
            "test": {
                "rgb": ["test/rgb/0.jpg", "test/rgb/1.jpg"],
                "ir": ["test/ir/0.jpg", "test/ir/1.jpg", "test/ir/2.jpg"],
            }
        }


class RecordingEventLog(EventLog):
    def __init__(self):
        super().__init__(None)
        self.events = []

    def record(self, event, now_us, frame=None, pair=None, reason=""):
        self.events.append((event, now_us, frame, pair, reason))


class SimulationTests(unittest.TestCase):
    def config(self, **overrides):
        settings = dict(
            sequence="test", frames=60, seed=724, loop=True,
            drop_rgb=0, drop_ir=0,
            rgb_delay_min_ms=0, rgb_delay_max_ms=0,
            ir_delay_min_ms=0, ir_delay_max_ms=0,
            spike_probability=0, spike_max_ms=0,
        )
        settings.update(overrides)
        return Config(**settings)

    def run_simulation(self, config, log=None):
        simulation = Simulation(FakeDataset(), config, log or EventLog(None))
        pairs = []
        summary = simulation.run(realtime=False, on_pair=pairs.append)
        return summary, pairs

    def test_fixed_seed_reproduces_pairs_and_per_frame_random_events(self):
        first_log, second_log = RecordingEventLog(), RecordingEventLog()
        settings = dict(
            drop_rgb=0.25, drop_ir=0.4,
            rgb_delay_max_ms=900, ir_delay_max_ms=900,
            spike_probability=0.25, spike_max_ms=1500,
        )
        first = self.run_simulation(self.config(**settings), first_log)
        second = self.run_simulation(self.config(**settings), second_log)

        self.assertEqual(first, second)
        self.assertEqual(first_log.events, second_log.events)
        summary, pairs = first
        self.assertGreater(len(pairs), 0)
        for stream in ("rgb", "ir"):
            self.assertEqual(summary[f"captured_{stream}"], 60)
            self.assertGreater(summary[f"dropped_{stream}"], 0)
            self.assertLess(summary[f"dropped_{stream}"], 60)
            self.assertEqual(
                summary[f"received_{stream}"] + summary[f"dropped_{stream}"], 60
            )
            arrivals = [
                event_frame for event, _, event_frame, _, _ in first_log.events
                if event == "arrival" and event_frame.stream == stream
            ]
            delays = [item.arrival_us - item.capture_us for item in arrivals]
            self.assertGreater(len(set(delays)), 1, "delay must vary per frame")
            self.assertTrue(all(0 <= delay <= 2_400_000 for delay in delays))
        rejected = sum(summary[reason] for reason in ("stale", "expired", "overflow", "unmatched"))
        self.assertEqual(
            summary["received_rgb"] + summary["received_ir"],
            2 * summary["pairs"] + rejected,
        )

    def test_all_frames_lost_finishes_without_deliveries_or_pairs(self):
        summary, pairs = self.run_simulation(
            self.config(drop_rgb=1, drop_ir=1,
                        rgb_delay_min_ms=60_000, rgb_delay_max_ms=60_000,
                        ir_delay_min_ms=60_000, ir_delay_max_ms=60_000)
        )

        self.assertEqual(pairs, [])
        self.assertEqual(summary["pairs"], 0)
        for stream in ("rgb", "ir"):
            self.assertEqual(summary[f"captured_{stream}"], 60)
            self.assertEqual(summary[f"dropped_{stream}"], 60)
            self.assertEqual(summary[f"received_{stream}"], 0)
            self.assertEqual(summary[f"buffered_{stream}"], 0)
        self.assertLess(summary["simulation_time_s"], 4)

    def test_zero_loss_delay_pairs_every_frame_and_loop_clock_keeps_advancing(self):
        summary, pairs = self.run_simulation(self.config())
        sources = FakeDataset().sequences["test"]

        self.assertEqual(len(pairs), 60)
        self.assertEqual(summary["pairs"], 60)
        self.assertTrue(all(pair.gap_us == 0 for pair in pairs))
        self.assertTrue(all(pair.rgb.capture_us == pair.ir.capture_us for pair in pairs))
        for stream in ("rgb", "ir"):
            output = [getattr(pair, stream) for pair in pairs]
            self.assertEqual([item.frame_id for item in output], list(range(60)))
            timestamps = [item.capture_us for item in output]
            self.assertTrue(all(before < after for before, after in zip(timestamps, timestamps[1:])))
            self.assertEqual(
                [item.path for item in output],
                [sources[stream][index % len(sources[stream])] for index in range(60)],
            )
            self.assertEqual(summary[f"received_{stream}"], 60)
            self.assertEqual(summary[f"dropped_{stream}"], 0)

    def test_different_fps_and_large_offset_never_emit_over_limit(self):
        for offset, should_pair in ((4.2, True), (20.0, False)):
            with self.subTest(offset=offset):
                summary, pairs = self.run_simulation(
                    self.config(fps_rgb=12, fps_ir=7, ir_offset_s=offset,
                                buffer_ttl_s=120)
                )
                self.assertEqual(bool(pairs), should_pair)
                self.assertLessEqual(summary["max_observed_gap_s"], 2)
                for pair in pairs:
                    self.assertLessEqual(pair.gap_us, 2 * SECOND)
                    for output in (pair.rgb, pair.ir):
                        self.assertLessEqual(output.capture_us, output.arrival_us)
                        self.assertLessEqual(output.arrival_us, pair.emitted_us)
                self.assertEqual(summary["buffered_rgb"], 0)
                self.assertEqual(summary["buffered_ir"], 0)

    def test_omitted_seed_uses_fresh_entropy_and_explicit_seed_bypasses_it(self):
        with patch("drone_stream_simulator.secrets.randbits", side_effect=[111, 222]) as entropy:
            first, _ = self.run_simulation(self.config(seed=None, frames=2))
            second, _ = self.run_simulation(self.config(seed=None, frames=2))
            explicit, _ = self.run_simulation(self.config(seed=333, frames=2))

        self.assertEqual(entropy.call_count, 2)
        self.assertEqual(first["seed"], 111)
        self.assertEqual(second["seed"], 222)
        self.assertEqual(explicit["seed"], 333)


if __name__ == "__main__":
    unittest.main()
