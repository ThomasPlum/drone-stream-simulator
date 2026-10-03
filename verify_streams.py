"""Briefly receive both drone RTSP streams with ffprobe.

This checks real reception and H.264 encoding. It does not compare capture
timestamps or establish synchronization between the two streams.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROBE_SECONDS = 3
PROBE_TIMEOUT_SECONDS = 20
STREAMS = (("rgb", "RGB"), ("thermal", "熱成像"))


def host_argument(value: str) -> str:
    host = value.strip()
    if not host or any(character.isspace() for character in host):
        raise argparse.ArgumentTypeError("--host 必須是 IP 位址或主機名稱")
    if any(character in host for character in "/@?#\\"):
        raise argparse.ArgumentTypeError("--host 請只填 IP 位址或主機名稱，不含 URL")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                "--host 不含連接埠；請使用 --port 指定連接埠"
            ) from error
    elif "[" in host or "]" in host:
        raise argparse.ArgumentTypeError("--host 的方括號格式不正確")
    return host


def port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--port 必須是整數") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--port 必須介於 1 與 65535")
    return port


def resolve_ffprobe(explicit_path: str | None) -> Path:
    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve()
        if not candidate.is_file():
            raise ValueError(f"找不到 ffprobe：{candidate}")
        return candidate
    directory = Path(__file__).resolve().parent
    for candidate in (directory / "tools" / "ffprobe.exe", directory / "tools" / "ffprobe"):
        if candidate.is_file():
            return candidate
    available = shutil.which("ffprobe")
    if available:
        return Path(available).resolve()
    raise ValueError("找不到 ffprobe；請放入 tools/ffprobe.exe 或指定 --ffprobe 路徑")


def rtsp_url(host: str, port: int, stream: str) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"rtsp://{authority}:{port}/{stream}"


def decode_output(value: str | bytes | None) -> str:
    # TimeoutExpired may contain bytes even when subprocess.run uses text=True.
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def positive_integer(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def probe_stream(ffprobe: Path, host: str, port: int, stream: str) -> dict[str, Any]:
    url = rtsp_url(host, port, stream)
    command = [
        str(ffprobe),
        "-rtsp_transport", "tcp",
        "-v", "error",
        "-read_intervals", f"%+{PROBE_SECONDS}",
        "-select_streams", "v:0",
        "-count_frames",
        "-show_entries", "stream=codec_name,width,height,nb_read_frames",
        "-of", "json",
        url,
    ]
    result: dict[str, Any] = {
        "stream": stream,
        "url": url,
        "command": command,
        "probe_seconds": PROBE_SECONDS,
        "timeout_seconds": PROBE_TIMEOUT_SECONDS,
        "returncode": None,
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "ffprobe": None,
        "stdout": "",
        "stderr": "",
        "ok": False,
        "reason": "",
    }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result["returncode"] = completed.returncode
        result["stdout"] = completed.stdout
        result["stderr"] = completed.stderr
    except subprocess.TimeoutExpired as error:
        result["timed_out"] = True
        result["stdout"] = decode_output(error.stdout)
        result["stderr"] = decode_output(error.stderr)
        result["reason"] = f"接收超過 {PROBE_TIMEOUT_SECONDS} 秒，已停止檢查"
    except OSError as error:
        result["reason"] = f"無法執行 ffprobe：{error}"
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)

    if result["stdout"].strip():
        try:
            result["ffprobe"] = json.loads(result["stdout"])
        except json.JSONDecodeError:
            if not result["reason"]:
                result["reason"] = "ffprobe 未回傳有效的 JSON"
    if result["reason"]:
        return result
    if result["returncode"] != 0:
        diagnostic = result["stderr"].strip().splitlines()
        detail = diagnostic[-1][:220] if diagnostic else "未提供錯誤內容"
        result["reason"] = f"ffprobe 失敗（代碼 {result['returncode']}）：{detail}"
        return result
    payload = result["ffprobe"]
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        result["reason"] = "沒有收到可辨識的影像串流"
        return result
    video = streams[0]
    codec = str(video.get("codec_name", "")).lower()
    width = positive_integer(video.get("width"))
    height = positive_integer(video.get("height"))
    frames = positive_integer(video.get("nb_read_frames"))
    result.update(codec=codec, width=width, height=height, received_frames=frames)
    if codec != "h264":
        result["reason"] = f"預期 H.264，實際為 {codec or '未知編碼'}"
    elif frames < 1:
        result["reason"] = "已連線，但沒有收到影格"
    elif width < 1 or height < 1:
        result["reason"] = "影像尺寸無效"
    else:
        result["ok"] = True
        result["reason"] = "成功接收 H.264 影格"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="並行短暫接收 /rgb 與 /thermal，確認兩路皆有 H.264 影格。",
        epilog="另一台電腦可使用：python verify_streams.py --host 192.168.1.10 --port 8554",
    )
    parser.add_argument("--host", type=host_argument, default="127.0.0.1", help="RTSP 伺服器 IP 或主機名稱（預設 127.0.0.1）")
    parser.add_argument("--port", type=port_argument, default=8554, help="RTSP 連接埠（預設 8554）")
    parser.add_argument("--ffprobe", help="ffprobe 執行檔路徑；預設優先使用 tools/ffprobe.exe")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent / "stream_probe_logs", help="各路 JSON 記錄的輸出資料夾")
    return parser


def main(argv: list[str] | None = None) -> int:
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, "reconfigure"):
            output.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        ffprobe = resolve_ffprobe(args.ffprobe)
        log_directory = args.log_dir.expanduser().resolve()
        log_directory.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as error:
        print(f"檢查無法啟動：{error}", file=sys.stderr)
        return 1
    print(f"接收 {args.host}:{args.port} 的兩路串流（每路約 {PROBE_SECONDS} 秒，逾時 {PROBE_TIMEOUT_SECONDS} 秒）…", flush=True)
    results: dict[str, dict[str, Any]] = {}
    log_failures = False
    labels = dict(STREAMS)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {
            executor.submit(probe_stream, ffprobe, args.host, args.port, stream): stream
            for stream, _ in STREAMS
        }
        for future in as_completed(pending):
            stream = pending[future]
            result = future.result()
            results[stream] = result
            log_path = log_directory / f"{stream}_probe.json"
            try:
                log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as error:
                log_failures = True
                print(f"{labels[stream]} 記錄無法寫入：{error}", file=sys.stderr)
            if result["ok"]:
                print(f"{labels[stream]}：OK，H.264 {result['width']}×{result['height']}，收到 {result['received_frames']} 幀")
            else:
                print(f"{labels[stream]}：失敗，{result['reason']}")
    print(f"JSON 記錄：{log_directory}")
    print("此檢查確認接收與編碼；兩路拍攝時間差需另外使用拍攝時間戳驗證。")
    return 0 if not log_failures and all(results[stream]["ok"] for stream, _ in STREAMS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
