"""Random frame loss/jitter followed by capture-time pairing and two live RTSP feeds.

The ZIP dataset remains untouched. Capture timestamps are synthetic, derived from
the configured source FPS. All timing comparisons use integer microseconds.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import random
import re
import secrets
import sys
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

SECOND = 1_000_000
ROOT = Path(__file__).resolve().parent
STREAMS = ("rgb", "ir")


@dataclass(frozen=True)
class Frame:
    stream: str
    frame_id: int
    capture_us: int
    arrival_us: int
    path: str = ""


@dataclass(frozen=True)
class Pair:
    rgb: Frame
    ir: Frame
    emitted_us: int

    @property
    def gap_us(self) -> int:
        return abs(self.rgb.capture_us - self.ir.capture_us)


class PairSynchronizer:
    """Consume the nearest currently arrived counterpart, once, without rewinding.

    This deliberately favors live latency over a globally optimal assignment.
    A later arrival cannot change a previously emitted pair. Buffer TTL measures
    time waiting AFTER arrival; it is independent of the capture-time tolerance.
    """

    def __init__(
        self,
        max_gap_us: int = 2 * SECOND,
        buffer_ttl_us: int = 8 * SECOND,
        buffer_limit: int = 120,
        on_reject: Callable[[Frame, str, int], None] | None = None,
    ):
        if not 0 <= max_gap_us <= 2 * SECOND:
            raise ValueError("capture-time tolerance must be between 0 and 2 seconds")
        if buffer_ttl_us < 0 or buffer_limit <= 0:
            raise ValueError("buffer TTL must be nonnegative and limit must be positive")
        self.max_gap_us = max_gap_us
        self.buffer_ttl_us = buffer_ttl_us
        self.buffer_limit = buffer_limit
        self.buffers: dict[str, list[Frame]] = {name: [] for name in STREAMS}
        self.last_capture = {name: -1 for name in STREAMS}
        self.stats = {name: 0 for name in ("stale", "expired", "overflow", "unmatched")}
        self.on_reject = on_reject
        self.now_us = 0

    def _reject(self, frame: Frame, reason: str, now_us: int) -> None:
        self.stats[reason] += 1
        if self.on_reject:
            self.on_reject(frame, reason, now_us)

    def expire(self, now_us: int) -> None:
        self.now_us = now_us
        for stream in STREAMS:
            kept = []
            for frame in self.buffers[stream]:
                if now_us - frame.arrival_us > self.buffer_ttl_us:
                    self._reject(frame, "expired", now_us)
                else:
                    kept.append(frame)
            self.buffers[stream] = kept

    def push(self, frame: Frame, now_us: int) -> list[Pair]:
        if frame.stream not in STREAMS:
            raise ValueError("unknown stream")
        if frame.arrival_us > now_us:
            raise ValueError("cannot pair a frame before it arrives")
        self.expire(now_us)
        if frame.capture_us <= self.last_capture[frame.stream]:
            self._reject(frame, "stale", now_us)
            return []
        if now_us - frame.arrival_us > self.buffer_ttl_us:
            self._reject(frame, "expired", now_us)
            return []

        other = "ir" if frame.stream == "rgb" else "rgb"
        candidates = self.buffers[other]
        best = min(candidates, key=lambda f: (abs(f.capture_us - frame.capture_us), f.capture_us), default=None)
        if best is not None and abs(best.capture_us - frame.capture_us) <= self.max_gap_us:
            candidates.remove(best)
            pair = Pair(frame, best, now_us) if frame.stream == "rgb" else Pair(best, frame, now_us)
            # Keep the invariant here, at the boundary every output passes through.
            if pair.gap_us > self.max_gap_us:
                raise RuntimeError("capture-time pairing invariant was violated")
            for stream in STREAMS:
                self.last_capture[stream] = getattr(pair, stream).capture_us
                kept = []
                for old in self.buffers[stream]:
                    if old.capture_us <= self.last_capture[stream]:
                        self._reject(old, "stale", now_us)
                    else:
                        kept.append(old)
                self.buffers[stream] = kept
            return [pair]

        self.buffers[frame.stream].append(frame)
        self.buffers[frame.stream].sort(key=lambda f: (f.capture_us, f.frame_id))
        if len(self.buffers[frame.stream]) > self.buffer_limit:
            self._reject(self.buffers[frame.stream].pop(0), "overflow", now_us)
        return []

    def flush(self) -> None:
        for stream in STREAMS:
            for frame in self.buffers[stream]:
                self._reject(frame, "unmatched", self.now_us)
            self.buffers[stream] = []


@dataclass
class Config:
    sequence: str = ""
    fps_rgb: float = 15.0
    fps_ir: float = 15.0
    drop_rgb: float = 0.1
    drop_ir: float = 0.1
    rgb_delay_min_ms: float = 20.0
    rgb_delay_max_ms: float = 350.0
    ir_delay_min_ms: float = 20.0
    ir_delay_max_ms: float = 350.0
    spike_probability: float = 0.05
    spike_max_ms: float = 3500.0
    ir_offset_s: float = 0.0
    max_gap_s: float = 2.0
    buffer_ttl_s: float = 8.0
    buffer_limit: int = 120
    seed: int | None = None
    frames: int = 0
    loop: bool = True

    def validate(self) -> None:
        def bound(name: str, low: float, high: float) -> None:
            value = getattr(self, name)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")

        for name in ("fps_rgb", "fps_ir"):
            bound(name, 0.1, 120)
        for name in ("drop_rgb", "drop_ir", "spike_probability"):
            bound(name, 0, 1)
        for stream in STREAMS:
            for suffix in ("min", "max"):
                bound(f"{stream}_delay_{suffix}_ms", 0, 60000)
            if getattr(self, f"{stream}_delay_min_ms") > getattr(self, f"{stream}_delay_max_ms"):
                raise ValueError(f"{stream}: delay minimum cannot exceed maximum")
        bound("spike_max_ms", 0, 60000)
        bound("ir_offset_s", 0, 60)
        bound("max_gap_s", 0, 2)
        bound("buffer_ttl_s", 0, 120)
        if not isinstance(self.buffer_limit, int) or not 1 <= self.buffer_limit <= 10000:
            raise ValueError("buffer_limit must be an integer between 1 and 10000")
        if not isinstance(self.frames, int) or self.frames < 0:
            raise ValueError("frames must be a nonnegative integer")
        if self.seed is not None and not isinstance(self.seed, int):
            raise ValueError("seed must be an integer or omitted")


class ZipDataset:
    """Index ZIP member names only; decompress just the selected output images."""

    def __init__(self, archive: Path):
        self.archive = archive.resolve()
        self.zip = zipfile.ZipFile(self.archive)
        self.sequences: dict[str, dict[str, list[str]]] = {}
        for info in self.zip.infolist():
            parts = info.filename.split("/")
            if len(parts) < 3 or parts[-2] not in STREAMS or Path(parts[-1]).suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            sequence = "/".join(parts[:-2])
            self.sequences.setdefault(sequence, {name: [] for name in STREAMS})[parts[-2]].append(info.filename)
        self.sequences = {name: data for name, data in self.sequences.items() if all(data[s] for s in STREAMS)}
        for data in self.sequences.values():
            for frames in data.values():
                frames.sort(key=lambda name: tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", name)))
        if not self.sequences:
            self.close()
            raise ValueError("ZIP must contain a sequence with both rgb/ and ir/ image folders")

    def read(self, frame: Frame) -> bytes:
        return self.zip.read(frame.path)

    def close(self) -> None:
        self.zip.close()


class EventLog:
    fields = ("event", "sim_time_s", "stream", "frame_id", "capture_s", "arrival_s", "delay_s", "rgb_frame_id", "ir_frame_id", "gap_s", "reason")

    def __init__(self, path: Path | None):
        self.file = None
        self.writer = None
        self.rows = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.file = path.open("w", encoding="utf-8-sig", newline="")
            self.writer = csv.DictWriter(self.file, fieldnames=self.fields)
            self.writer.writeheader()

    def record(self, event: str, now_us: int, frame: Frame | None = None, pair: Pair | None = None, reason: str = "") -> None:
        if self.writer is None:
            return
        row = {"event": event, "sim_time_s": f"{now_us / SECOND:.6f}", "reason": reason}
        if frame:
            row.update(stream=frame.stream, frame_id=frame.frame_id, capture_s=f"{frame.capture_us / SECOND:.6f}")
            if event != "drop":
                row.update(arrival_s=f"{frame.arrival_us / SECOND:.6f}", delay_s=f"{(frame.arrival_us - frame.capture_us) / SECOND:.6f}")
        if pair:
            row.update(rgb_frame_id=pair.rgb.frame_id, ir_frame_id=pair.ir.frame_id, gap_s=f"{pair.gap_us / SECOND:.6f}")
        self.writer.writerow(row)
        self.rows += 1
        if self.rows % 100 == 0:
            self.file.flush()

    def close(self) -> None:
        if self.file:
            self.file.close()


class Simulation:
    """Capture -> independent per-frame loss/delay -> arrival heap -> pairing."""

    def __init__(self, dataset: ZipDataset, config: Config, log: EventLog):
        config.validate()
        if config.sequence not in dataset.sequences:
            raise ValueError(f"unknown sequence: {config.sequence}")
        self.dataset, self.config, self.log = dataset, config, log
        self.seed = config.seed if config.seed is not None else secrets.randbits(48)
        self.randomizers = {stream: random.Random(self.seed ^ salt) for stream, salt in (("rgb", 0x12345), ("ir", 0xABCDE))}
        self.sync = PairSynchronizer(
            round(config.max_gap_s * SECOND), round(config.buffer_ttl_s * SECOND), config.buffer_limit,
            lambda f, reason, now: log.record("reject", now, frame=f, reason=reason),
        )
        self.stats = {f"{event}_{stream}": 0 for event in ("captured", "dropped", "received") for stream in STREAMS}
        self.stats.update(pairs=0, last_gap_s=None, max_observed_gap_s=0.0)
        self.now_us = 0

    def summary(self) -> dict:
        return {**self.stats, **self.sync.stats, "seed": self.seed, "simulation_time_s": self.now_us / SECOND,
                "buffered_rgb": len(self.sync.buffers["rgb"]), "buffered_ir": len(self.sync.buffers["ir"])}

    def run(self, on_pair: Callable[[Pair], None] | None = None, realtime: bool = False,
            stop: threading.Event | None = None, on_progress: Callable[[dict], None] | None = None) -> dict:
        config = self.config
        source = self.dataset.sequences[config.sequence]
        next_ids = {stream: 0 for stream in STREAMS}
        fps = {"rgb": config.fps_rgb, "ir": config.fps_ir}
        offset = {"rgb": 0, "ir": round(config.ir_offset_s * SECOND)}
        limits = {s: (config.frames or math.inf) if config.loop else min(config.frames or math.inf, len(source[s])) for s in STREAMS}
        deliveries: list[tuple[int, int, Frame]] = []
        serial = 0
        started = time.monotonic()
        next_progress = 0
        stop = stop or threading.Event()

        try:
            while not stop.is_set():
                capture_times = {s: offset[s] + round(next_ids[s] * SECOND / fps[s]) if next_ids[s] < limits[s] else math.inf for s in STREAMS}
                stream = min(STREAMS, key=lambda s: capture_times[s])
                next_capture = capture_times[stream]
                next_arrival = deliveries[0][0] if deliveries else math.inf
                event_time = min(next_capture, next_arrival)
                if math.isinf(event_time):
                    break
                self.now_us = int(event_time)
                if realtime:
                    remaining = started + event_time / SECOND - time.monotonic()
                    if remaining > 0 and stop.wait(remaining):
                        break
                self.sync.expire(self.now_us)

                if next_arrival <= next_capture:
                    _, _, incoming = heapq.heappop(deliveries)
                    self.stats[f"received_{incoming.stream}"] += 1
                    self.log.record("arrival", self.now_us, frame=incoming)
                    for pair in self.sync.push(incoming, self.now_us):
                        self.stats["pairs"] += 1
                        self.stats["last_gap_s"] = pair.gap_us / SECOND
                        self.stats["max_observed_gap_s"] = max(self.stats["max_observed_gap_s"], pair.gap_us / SECOND)
                        self.log.record("pair", self.now_us, pair=pair)
                        if on_pair:
                            on_pair(pair)
                else:
                    frame_id = next_ids[stream]
                    next_ids[stream] += 1
                    rng = self.randomizers[stream]
                    path = source[stream][frame_id % len(source[stream])]
                    frame = Frame(stream, frame_id, self.now_us, self.now_us, path)
                    self.stats[f"captured_{stream}"] += 1
                    self.log.record("capture", self.now_us, frame=frame)
                    if rng.random() < getattr(config, f"drop_{stream}"):
                        self.stats[f"dropped_{stream}"] += 1
                        self.log.record("drop", self.now_us, frame=frame, reason="random_frame_loss")
                    else:
                        delay_ms = rng.uniform(getattr(config, f"{stream}_delay_min_ms"), getattr(config, f"{stream}_delay_max_ms"))
                        if rng.random() < config.spike_probability:
                            # The spike amount, too, is independently random per frame.
                            delay_ms += rng.uniform(0, config.spike_max_ms)
                        frame = Frame(stream, frame_id, self.now_us, self.now_us + round(delay_ms * 1000), path)
                        serial += 1
                        heapq.heappush(deliveries, (frame.arrival_us, serial, frame))

                if on_progress and self.now_us >= next_progress:
                    on_progress(self.summary())
                    next_progress = self.now_us + 2 * SECOND
        finally:
            self.sync.flush()
        return self.summary()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Random RGB/thermal drone simulator: two real RTSP sources, <=2s capture-time pairing.")
    p.add_argument("--archive", type=Path, default=ROOT / "train_ST_001.zip")
    p.add_argument("--sequence", default="")
    p.add_argument("--list-sequences", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--rtsp", action="store_true", help="publish two live RTSP streams (default)")
    mode.add_argument("--headless", action="store_true", help="fast simulation without media servers")
    p.add_argument("--host", default="0.0.0.0", help="RTSP bind address")
    p.add_argument("--port", type=int, default=8554)
    p.add_argument("--ffmpeg", type=Path, default=ROOT / "tools" / "ffmpeg.exe")
    p.add_argument("--mediamtx", type=Path, default=ROOT / "tools" / "mediamtx.exe")
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--frames", type=int, default=None, help="captured frames per stream; 0=infinite (headless default=300)")
    p.add_argument("--no-loop", action="store_true", help="stop at dataset end, without resetting capture timestamps")
    p.add_argument("--rgb-fps", type=float, default=15)
    p.add_argument("--ir-fps", type=float, default=15)
    p.add_argument("--rgb-drop", type=float, default=0.1)
    p.add_argument("--ir-drop", type=float, default=0.1)
    p.add_argument("--rgb-delay-ms", nargs=2, type=float, default=(20, 350), metavar=("MIN", "MAX"))
    p.add_argument("--ir-delay-ms", nargs=2, type=float, default=(20, 350), metavar=("MIN", "MAX"))
    p.add_argument("--spike-probability", type=float, default=0.05)
    p.add_argument("--spike-max-ms", type=float, default=3500)
    p.add_argument("--ir-offset-s", type=float, default=0)
    p.add_argument("--max-gap-s", type=float, default=2)
    p.add_argument("--buffer-ttl-s", type=float, default=8)
    p.add_argument("--buffer-limit", type=int, default=120)
    p.add_argument("--seed", type=int, default=None, help="optional reproducible seed; omitted=OS entropy each run")
    p.add_argument("--log", type=Path, default=ROOT / "results" / "events.csv")
    p.add_argument("--no-log", action="store_true")
    return p


def main() -> int:
    # Windows redirected streams can otherwise default to cp1252, including
    # when this CLI is launched from a UTF-8 PowerShell session.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = parser()
    args = p.parse_args()
    dataset = None
    log = None
    output = None
    simulation = None
    try:
        dataset = ZipDataset(args.archive)
        if args.list_sequences:
            for name, data in sorted(dataset.sequences.items()):
                print(f"{name}: RGB={len(data['rgb'])}, IR={len(data['ir'])}")
            return 0
        sequence = args.sequence or sorted(dataset.sequences)[0]
        frames = args.frames if args.frames is not None else (300 if args.headless else 0)
        if args.headless and frames == 0 and not args.no_loop:
            raise ValueError("headless requires --frames >0 or --no-loop to finish")
        config = Config(
            sequence=sequence, fps_rgb=args.rgb_fps, fps_ir=args.ir_fps,
            drop_rgb=args.rgb_drop, drop_ir=args.ir_drop,
            rgb_delay_min_ms=args.rgb_delay_ms[0], rgb_delay_max_ms=args.rgb_delay_ms[1],
            ir_delay_min_ms=args.ir_delay_ms[0], ir_delay_max_ms=args.ir_delay_ms[1],
            spike_probability=args.spike_probability, spike_max_ms=args.spike_max_ms,
            ir_offset_s=args.ir_offset_s, max_gap_s=args.max_gap_s,
            buffer_ttl_s=args.buffer_ttl_s, buffer_limit=args.buffer_limit,
            seed=args.seed, frames=frames, loop=not args.no_loop,
        )
        config.validate()
        if not 1 <= args.port <= 65535 or args.width < 2 or args.width % 2:
            raise ValueError("port must be 1..65535 and output width must be a positive even integer")
        log = EventLog(None if args.no_log else args.log)
        simulation = Simulation(dataset, config, log)
        info = {"archive": str(dataset.archive), "config": asdict(config), "effective_seed": simulation.seed,
                "timestamp_origin": "synthetic common-clock timestamps, sorted image ordinal / source FPS"}
        info_path = ROOT / "results" / "run_info.json"
        info_path.parent.mkdir(parents=True, exist_ok=True)
        info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Sequence: {sequence}; random seed: {simulation.seed}; capture gap limit: {config.max_gap_s}s", flush=True)
        print("Each frame independently samples random loss and random delay; capture timestamps are synthetic.", flush=True)

        callback = None
        if not args.headless:
            from rtsp_output import RtspOutput

            if not args.ffmpeg.is_file() or not args.mediamtx.is_file():
                raise ValueError("streaming tools missing; run .\\setup_streaming.ps1 first")
            output = RtspOutput(args.ffmpeg.resolve(), args.mediamtx.resolve(), ROOT, args.host, args.port, args.width, min(config.fps_rgb, config.fps_ir))
            output.start()

            def publish(pair: Pair) -> None:
                output.publish(pair, dataset.read(pair.rgb), dataset.read(pair.ir))

            callback = publish
            print(f"RTSP RGB:     rtsp://<THIS-PC-LAN-IP>:{args.port}/rgb", flush=True)
            print(f"RTSP thermal: rtsp://<THIS-PC-LAN-IP>:{args.port}/thermal", flush=True)
            print("Press Ctrl+C to stop both streams and their media server.", flush=True)

        def progress(stats: dict) -> None:
            print(f"t={stats['simulation_time_s']:.1f}s pairs={stats['pairs']} "
                  f"random-drop(rgb/ir)={stats['dropped_rgb']}/{stats['dropped_ir']} "
                  f"max-capture-gap={stats['max_observed_gap_s']:.3f}s", flush=True)

        summary = simulation.run(callback, realtime=not args.headless, on_progress=progress if not args.headless else None)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    finally:
        if output:
            output.close()
        if log:
            log.close()
        if dataset:
            dataset.close()
        if simulation:
            summary_path = ROOT / "results" / "summary.json"
            summary_path.write_text(json.dumps(simulation.summary(), indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
