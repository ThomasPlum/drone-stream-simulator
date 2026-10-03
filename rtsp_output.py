"""Publish synchronized JPEG pairs as two live H.264 RTSP/TCP streams.

Only this class's own MediaMTX and FFmpeg children are stopped. The caller
supplies already synchronized pairs and calls ``publish`` at the simulated
arrival times; this module does not schedule, duplicate, or repeat frames.

Configuration follows https://mediamtx.org/docs/references/configuration-file.
Timing options follow https://www.ffmpeg.org/ffmpeg-all.html: wall-clock input
timestamps and ``-fps_mode passthrough`` retain variable delivery intervals.
The two independent RTSP clients may have different player buffering; use the
same overlaid PAIR number to verify a pair, rather than assuming two players
render together. Capture-time difference is validated separately from network
delay and is never allowed to exceed two seconds.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


LOGGER = logging.getLogger(__name__)
SECOND = 1_000_000


@dataclass
class _WriteJob:
    data: bytes
    release: threading.Event
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class RtspOutput:
    """Own an RTSP server and two FIFO image encoders.

    ``host`` is 0.0.0.0 for LAN readers, or 127.0.0.1 for local readers. Local
    publishing always uses 127.0.0.1; other source IPs cannot publish. ``fps``
    describes input timing and keyframe cadence, not a constant output rate.
    An optional Pillow overlay annotates copies in memory, leaving ZIP assets
    untouched. A missing Pillow installation only disables that annotation.
    """

    def __init__(
        self,
        ffmpeg_path: Path,
        mediamtx_path: Path,
        workspace: Path,
        host: str = "0.0.0.0",
        port: int = 8554,
        width: int = 960,
        fps: float = 15,
        *,
        overlay: bool = True,
    ) -> None:
        if host == "localhost":
            host = "127.0.0.1"
        if host not in ("0.0.0.0", "127.0.0.1"):
            raise ValueError("RTSP host must be 0.0.0.0 or 127.0.0.1 so local publishing remains available")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("RTSP port must be an integer from 1 to 65535")
        if isinstance(width, bool) or not isinstance(width, int) or width < 2 or width % 2:
            raise ValueError("RTSP output width must be a positive even integer (at least 2)")
        if isinstance(fps, bool) or not math.isfinite(fps) or fps <= 0:
            raise ValueError("RTSP nominal FPS must be a finite positive number")
        self.ffmpeg_path = Path(ffmpeg_path).resolve()
        self.mediamtx_path = Path(mediamtx_path).resolve()
        self.workspace = Path(workspace).resolve()
        self.host = host
        self.port = port
        self.width = width
        self.fps = float(fps)
        self.overlay = overlay
        self.results_dir = self.workspace / "results"
        self.config_path = self.results_dir / "mediamtx.yml"
        self.log_paths = {
            "mediamtx": self.results_dir / "rtsp_mediamtx.log",
            "rgb": self.results_dir / "rtsp_rgb.log",
            "thermal": self.results_dir / "rtsp_thermal.log",
        }
        self._children: list[subprocess.Popen[bytes]] = []
        self._logs: list[BinaryIO] = []
        self._server: subprocess.Popen[bytes] | None = None
        self._encoders: dict[str, subprocess.Popen[bytes]] = {}
        self._queues: dict[str, queue.Queue[_WriteJob]] = {}
        self._writers: list[threading.Thread] = []
        self._lifecycle_lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._pairs_sent = 0
        self._last_capture: dict[str, int] = {}
        self._fonts: dict[int, Any] = {}
        self._write_timeout_s = 10.0
        self._startup_timeout_s = 10.0

    @property
    def urls(self) -> dict[str, str]:
        """Local test URLs; LAN readers replace 127.0.0.1 with this PC's IP."""
        return {
            "rgb": f"rtsp://127.0.0.1:{self.port}/rgb",
            "thermal": f"rtsp://127.0.0.1:{self.port}/thermal",
        }

    def __enter__(self) -> RtspOutput:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _popen_flags() -> dict[str, Any]:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def _port_is_busy(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                return True
        except OSError:
            return False

    def _config(self, include_moq: bool) -> str:
        # Restrict publishing independently of anonymous LAN reading. Only the
        # two named paths exist; no wildcard/default publisher path is exposed.
        moq_line = "moq: false\n" if include_moq else ""
        return (
            "logLevel: info\n"
            "logDestinations: [stdout]\n"
            # A high random drop rate can leave a publisher without any valid
            # pair for minutes. Keep that live session through long gaps.
            "readTimeout: 1h\n"
            "writeTimeout: 10s\n"
            "authMethod: internal\n"
            "authInternalUsers:\n"
            "  - user: any\n"
            "    pass: ''\n"
            "    ips: ['127.0.0.1']\n"
            "    permissions:\n"
            "      - action: publish\n"
            "        path: rgb\n"
            "      - action: publish\n"
            "        path: thermal\n"
            "  - user: any\n"
            "    pass: ''\n"
            "    ips: []\n"
            "    permissions:\n"
            "      - action: read\n"
            "        path: rgb\n"
            "      - action: read\n"
            "        path: thermal\n"
            "api: false\n"
            "metrics: false\n"
            "pprof: false\n"
            "playback: false\n"
            "rtsp: true\n"
            "rtspTransports: [tcp]\n"
            "rtspEncryption: 'no'\n"
            f"rtspAddress: {json.dumps(f'{self.host}:{self.port}')}\n"
            "rtmp: false\n"
            "hls: false\n"
            "webrtc: false\n"
            "srt: false\n"
            + moq_line
            + "paths:\n"
            "  rgb:\n"
            "    source: publisher\n"
            "  thermal:\n"
            "    source: publisher\n"
        )

    def _spawn(self, command: list[str], log: BinaryIO, *, pipe_input: bool) -> subprocess.Popen[bytes]:
        # Actual files, rather than unread PIPEs, prevent stderr/stdout deadlock.
        log.write(("COMMAND: " + subprocess.list2cmdline(command) + "\n").encode("utf-8"))
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            stdin=subprocess.PIPE if pipe_input else subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            bufsize=0,
            **self._popen_flags(),
        )
        self._children.append(process)
        return process

    def _log_tail(self, name: str) -> str:
        try:
            with self.log_paths[name].open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 8000))
                return stream.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _wait_server(self) -> None:
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            assert self._server is not None
            if self._server.poll() is not None:
                raise RuntimeError(
                    "MediaMTX exited during startup; see "
                    f"{self.log_paths['mediamtx']}\n{self._log_tail('mediamtx')}"
                )
            if self._port_is_busy():
                # A process that lost the bind race must not be mistaken for a
                # healthy server. MediaMTX logs initialization before accepting.
                time.sleep(0.03)
                if self._server.poll() is None:
                    return
            time.sleep(0.05)
        raise TimeoutError(f"MediaMTX did not listen on 127.0.0.1:{self.port} within {self._startup_timeout_s:g}s")

    def _ffmpeg_command(self, path: str) -> list[str]:
        return [
            str(self.ffmpeg_path),
            "-hide_banner", "-loglevel", "warning", "-nostats", "-nostdin",
            "-f", "image2pipe", "-c:v", "mjpeg",
            "-framerate", f"{self.fps:g}",
            "-use_wallclock_as_timestamps", "1",
            "-probesize", "32", "-analyzeduration", "0", "-fpsprobesize", "0",
            "-threads", "1",
            "-i", "pipe:0",
            # image2pipe's 1/FPS input time base quantizes nearby arrivals into
            # the same timestamp. Stamp actual filter delivery on the RTSP
            # 90 kHz clock, while retaining nominal FPS metadata and every
            # input frame. This is a clock expression, not a CFR/fps filter.
            "-an", "-vf", f"scale={self.width}:-2,settb=1/90000,setpts=(RTCTIME-RTCSTART)/(TB*1000000)",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-bf", "0",
            "-g", str(max(1, round(self.fps))),
            "-fps_mode", "passthrough", "-enc_time_base", "1:90000",
            "-flush_packets", "1", "-f", "rtsp", "-rtsp_transport", "tcp",
            self.urls[path],
        ]

    def start(self) -> None:
        """Start owned server/encoders. Refuse an occupied RTSP port."""
        with self._lifecycle_lock:
            if self._started:
                try:
                    self._check_processes()
                except BaseException:
                    self.close()
                    raise
                return
            for name, path in (("FFmpeg", self.ffmpeg_path), ("MediaMTX", self.mediamtx_path)):
                if not path.is_file():
                    raise FileNotFoundError(f"{name} executable not found: {path}")
            if self._port_is_busy():
                raise RuntimeError(f"RTSP port {self.port} is already occupied; choose another port or stop its owner")
            # Give each run its own event so an old writer cannot resume if a
            # caller closes and starts this instance again.
            self._stop = threading.Event()
            self._pairs_sent = 0
            self._last_capture.clear()
            self.results_dir.mkdir(parents=True, exist_ok=True)
            try:
                server_log = self.log_paths["mediamtx"].open("wb")
                self._logs.append(server_log)
                self.config_path.write_text(self._config(include_moq=True), encoding="utf-8")
                command = [str(self.mediamtx_path), str(self.config_path)]
                self._server = self._spawn(command, server_log, pipe_input=False)
                try:
                    self._wait_server()
                except RuntimeError:
                    # Releases predating MoQ reject this field and have no MoQ
                    # listener. Retry only that documented optional feature.
                    tail = self._log_tail("mediamtx")
                    if 'unknown field "moq"' not in tail and "unknown field 'moq'" not in tail:
                        raise
                    self.config_path.write_text(self._config(include_moq=False), encoding="utf-8")
                    self._server = self._spawn(command, server_log, pipe_input=False)
                    self._wait_server()
                for path in ("rgb", "thermal"):
                    log = self.log_paths[path].open("wb")
                    self._logs.append(log)
                    process = self._spawn(self._ffmpeg_command(path), log, pipe_input=True)
                    self._encoders[path] = process
                    jobs: queue.Queue[_WriteJob] = queue.Queue(maxsize=1)
                    self._queues[path] = jobs
                    writer = threading.Thread(
                        target=self._writer,
                        args=(path, process, jobs),
                        name=f"rtsp-{path}-writer",
                        daemon=True,
                    )
                    self._writers.append(writer)
                    writer.start()
                self._started = True
                self._check_processes()
                if self.overlay and Image is None:
                    LOGGER.warning("Pillow is unavailable: RTSP streams work, but PAIR/capture/delay overlays are disabled")
            except BaseException:
                self.close()
                raise

    def _check_processes(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("RTSP output has been stopped")
        for name, process in (("mediamtx", self._server), *self._encoders.items()):
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"{name} process exited ({process.returncode}); see {self.log_paths[name]}\n{self._log_tail(name)}")

    def _writer(self, path: str, process: subprocess.Popen[bytes], jobs: queue.Queue[_WriteJob]) -> None:
        stop = self._stop
        while not stop.is_set():
            try:
                job = jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                while not job.release.wait(0.05):
                    if stop.is_set():
                        raise RuntimeError("RTSP output stopped before dispatch")
                if stop.is_set() or process.poll() is not None or process.stdin is None:
                    raise BrokenPipeError(f"{path} encoder is no longer accepting frames")
                descriptor = process.stdin.fileno()
                remaining = memoryview(job.data)
                while remaining:
                    if stop.is_set():
                        raise RuntimeError("RTSP output stopped during frame write")
                    # os.write handles partial pipe writes. It may block on
                    # Windows; publish's deadline terminates the reader child
                    # to break that wait, without waiting on a buffered lock.
                    amount = os.write(descriptor, remaining)
                    if amount <= 0:
                        raise BrokenPipeError(f"{path} encoder accepted zero bytes")
                    remaining = remaining[amount:]
            except BaseException as error:
                job.error = error
            finally:
                job.done.set()
                jobs.task_done()

    def _font(self, size: int) -> Any:
        if size not in self._fonts:
            font = None
            candidates = ("DejaVuSans.ttf", str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "consola.ttf"))
            for candidate in candidates:
                try:
                    font = ImageFont.truetype(candidate, size=size)
                    break
                except OSError:
                    continue
            if font is None:
                try:
                    font = ImageFont.load_default(size=size)
                except TypeError:
                    font = ImageFont.load_default()
            self._fonts[size] = font
        return self._fonts[size]

    def _annotate(self, jpeg: bytes, pair: Any, stream: str, pair_id: Any) -> bytes:
        if not self.overlay or Image is None:
            return jpeg
        frame = getattr(pair, stream)
        capture_s = frame.capture_us / SECOND
        delay_s = (getattr(frame, "arrival_us", pair.emitted_us) - frame.capture_us) / SECOND
        gap_s = abs(pair.rgb.capture_us - pair.ir.capture_us) / SECOND
        with Image.open(io.BytesIO(jpeg)) as original:
            image = original.convert("RGB")
        size = max(12, round(image.width * 15 / self.width))
        font = self._font(size)
        drawing = ImageDraw.Draw(image)
        padding = max(4, round(image.width * 7 / self.width))
        line_height = size + padding
        drawing.rectangle((0, 0, image.width, line_height * 2 + padding), fill=(10, 15, 22))
        drawing.text((padding, padding), f"PAIR {pair_id} | {stream.upper()} FRAME {frame.frame_id}", font=font, fill=(255, 235, 180))
        drawing.text((padding, padding + line_height), f"capture {capture_s:.3f}s | delay {delay_s:.3f}s | gap {gap_s:.3f}s", font=font, fill=(235, 241, 250))
        result = io.BytesIO()
        image.save(result, format="JPEG", quality=88)
        return result.getvalue()

    def publish(self, pair: Any, rgb_bytes: bytes, ir_bytes: bytes) -> None:
        """Dispatch one pair to both FIFO writers and wait at most 10 seconds.

        Return means both JPEGs entered their encoders, not an acknowledgment
        that every remote player displayed them. Any failure closes all owned
        children, so a surviving encoder cannot continue with unmatched pairs.
        Concurrent callers are serialized to preserve pair order in both paths.
        """
        with self._publish_lock:
            try:
                if not self._started:
                    raise RuntimeError("Call RtspOutput.start() before publish()")
                self._check_processes()
                captures = {"rgb": pair.rgb.capture_us, "ir": pair.ir.capture_us}
                if abs(captures["rgb"] - captures["ir"]) > 2 * SECOND:
                    raise ValueError("Refusing an RGB/IR pair with a capture-time difference greater than 2 seconds")
                for name, capture in captures.items():
                    if name in self._last_capture and capture <= self._last_capture[name]:
                        raise ValueError(f"{name} capture time must advance; refusing a repeated or older frame")
                if not rgb_bytes or not ir_bytes:
                    raise ValueError("Both RGB and IR image bytes are required")
                pair_id = getattr(pair, "id", None)
                if pair_id is None:
                    pair_id = self._pairs_sent + 1
                rgb = self._annotate(rgb_bytes, pair, "rgb", pair_id)
                thermal = self._annotate(ir_bytes, pair, "ir", pair_id)
                release = threading.Event()
                jobs = {"rgb": _WriteJob(rgb, release), "thermal": _WriteJob(thermal, release)}
                deadline = time.monotonic() + self._write_timeout_s
                try:
                    # The shared gate opens after both queues contain their
                    # JPEG, avoiding a sequential large-pipe write skew.
                    for path, job in jobs.items():
                        self._queues[path].put_nowait(job)
                finally:
                    release.set()
                while not all(job.done.is_set() for job in jobs.values()):
                    self._check_processes()
                    for path, job in jobs.items():
                        if job.done.is_set() and job.error is not None:
                            raise RuntimeError(f"{path} frame write failed: {job.error}") from job.error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"RTSP pair write exceeded {self._write_timeout_s:g}s; stopping both streams")
                    pending = next((job for job in jobs.values() if not job.done.is_set()), None)
                    if pending is not None:
                        pending.done.wait(min(0.02, remaining))
                for path, job in jobs.items():
                    if job.error is not None:
                        raise RuntimeError(f"{path} frame write failed: {job.error}") from job.error
                self._check_processes()
                self._last_capture.update(captures)
                self._pairs_sent += 1
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        """Stop only owned children; safe after partial startup or repeated calls."""
        with self._lifecycle_lock:
            self._stop.set()
            self._started = False
            # Terminate all readers first. This unblocks a writer stalled in a
            # pipe; do not close stdin while a writer could hold a Python lock.
            for process in reversed(self._children):
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
            deadline = time.monotonic() + 3.0
            for process in reversed(self._children):
                try:
                    process.wait(timeout=max(0.05, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=1.0)
                    except (OSError, subprocess.TimeoutExpired):
                        LOGGER.warning("Owned RTSP process %s did not exit after termination", process.pid)
                except OSError:
                    LOGGER.warning("Could not wait for owned RTSP process %s", process.pid)
            for writer in self._writers:
                if writer is not threading.current_thread():
                    writer.join(timeout=1.0)
            for jobs in self._queues.values():
                while True:
                    try:
                        job = jobs.get_nowait()
                    except queue.Empty:
                        break
                    job.error = RuntimeError("RTSP output closed before this frame was written")
                    job.done.set()
                    jobs.task_done()
            for process in self._children:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            for log in self._logs:
                try:
                    log.close()
                except OSError:
                    pass
            self._children.clear()
            self._logs.clear()
            self._encoders.clear()
            self._queues.clear()
            self._writers.clear()
            self._server = None
