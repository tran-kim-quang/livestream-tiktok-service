from __future__ import annotations

import asyncio
import logging
import queue
import shutil
import struct
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from array import array
from contextlib import suppress
from typing import Any

from app.config import AUDIO_OUTPUT_DEVICE, AUDIO_PLAYER

logger = logging.getLogger(__name__)

PCM_RATE = 24000
PCM_BYTES_PER_SEC = PCM_RATE * 2
_STOP = object()


class LocalAudioPlayer(ABC):
    """Hàng đợi phát PCM s16le 24 kHz mono trên máy chạy service."""

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def play(self, pcm: bytes) -> None:
        """Đưa audio vào hàng đợi và trả về ngay, không chặn event loop."""

    @abstractmethod
    async def drain(self) -> None:
        """Chờ đến khi toàn bộ audio đã enqueue thực sự phát xong."""

    @abstractmethod
    async def flush(self) -> None:
        """Dừng audio hiện tại và bỏ toàn bộ hàng đợi."""

    @abstractmethod
    async def aclose(self) -> None:
        pass

    @property
    @abstractmethod
    def active(self) -> bool:
        pass

    @property
    @abstractmethod
    def queued_sec(self) -> float:
        pass

    @abstractmethod
    def info(self) -> dict[str, Any]:
        pass


def _resample_pcm(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Đổi sample rate bằng nội suy tuyến tính, giữ nguyên PCM int16 mono."""
    if not pcm or source_rate == target_rate:
        return pcm
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Sample rate phải lớn hơn 0.")
    # Bỏ byte lẻ thay vì đọc quá cuối buffer PCM.
    pcm = pcm[: len(pcm) // 2 * 2]
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return b""
    output_count = max(1, round(sample_count * target_rate / source_rate))
    if sample_count == 1:
        return pcm * output_count
    try:
        import numpy as np

        source = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        positions = np.linspace(0, sample_count - 1, output_count)
        result = np.interp(positions, np.arange(sample_count), source)
        return np.clip(np.rint(result), -32768, 32767).astype("<i2").tobytes()
    except ImportError:
        samples = struct.unpack(f"<{sample_count}h", pcm)
        if output_count == 1:
            return struct.pack("<h", samples[0])
        result = array("h")
        scale = (sample_count - 1) / (output_count - 1)
        for index in range(output_count):
            position = index * scale
            left = min(int(position), sample_count - 1)
            right = min(left + 1, sample_count - 1)
            fraction = position - left
            value = round(samples[left] + (samples[right] - samples[left]) * fraction)
            result.append(max(-32768, min(32767, value)))
        if result.itemsize == 2 and struct.pack("=H", 1) != struct.pack("<H", 1):
            result.byteswap()
        return result.tobytes()


def _import_sounddevice():
    try:
        import sounddevice

        return sounddevice
    except (ImportError, OSError):
        # Linux/Docker có thể cài package nhưng thiếu thư viện PortAudio hệ thống.
        return None


def _output_devices(sd: Any) -> list[tuple[int, Any]]:
    try:
        return [
            (index, device)
            for index, device in enumerate(sd.query_devices())
            if int(device.get("max_output_channels", 0)) > 0
        ]
    except Exception:
        return []


def _resolve_output_device(sd: Any, requested: str) -> tuple[int | None, dict[str, Any]]:
    devices = _output_devices(sd)
    if not devices:
        raise RuntimeError("Không tìm thấy thiết bị output audio.")
    value = requested.strip()
    if value:
        if value.lstrip("+-").isdigit():
            index = int(value)
            matches = [device for device_index, device in devices if device_index == index]
            if not matches:
                raise RuntimeError(f"Thiết bị audio output #{index} không tồn tại.")
            return index, dict(matches[0])
        lowered = value.casefold()
        matches = [
            (index, device)
            for index, device in devices
            if lowered in str(device.get("name", "")).casefold()
        ]
        if not matches:
            raise RuntimeError(f"Không tìm thấy thiết bị audio chứa tên '{value}'.")
        return matches[0][0], dict(matches[0][1])

    try:
        default = sd.default.device
        default_index = default[1] if isinstance(default, (tuple, list)) else default
        for index, device in devices:
            if index == default_index:
                return None, dict(device)
    except Exception:
        pass
    # device=None vẫn để PortAudio chọn default; metadata chỉ dùng để log.
    return None, dict(devices[0][1])


class SoundDevicePlayer(LocalAudioPlayer):
    def __init__(self, output_device: str = "", sounddevice_module: Any = None) -> None:
        self._requested_device = output_device
        self._sd = sounddevice_module
        self._device: int | None = None
        self._device_name = "system default"
        self._rate = PCM_RATE
        self._stream: Any = None
        self._queue: queue.Queue[object] = queue.Queue()
        self._condition = threading.Condition()
        self._stream_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._busy = False
        self._pending_bytes = 0
        self._generation = 0
        self._error: Exception | None = None
        self._last_write_at: float | None = None

    async def start(self) -> None:
        if self._running:
            return
        await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> None:
        self._sd = self._sd or _import_sounddevice()
        if self._sd is None:
            raise RuntimeError("sounddevice/PortAudio không khả dụng.")
        self._device, metadata = _resolve_output_device(self._sd, self._requested_device)
        self._device_name = str(metadata.get("name") or "system default")
        default_rate = round(float(metadata.get("default_samplerate") or PCM_RATE))
        try:
            self._stream = self._open_stream(PCM_RATE)
            self._stream.start()
            self._rate = PCM_RATE
        except Exception:
            if self._stream is not None:
                with suppress(Exception):
                    self._stream.close()
                self._stream = None
            if default_rate == PCM_RATE:
                raise
            logger.info(
                "Thiết bị audio không nhận 24000 Hz; resample sang %s Hz.", default_rate
            )
            self._stream = self._open_stream(default_rate)
            self._stream.start()
            self._rate = default_rate
        self._running = True
        self._thread = threading.Thread(
            target=self._writer, name="local-audio-sounddevice", daemon=True
        )
        self._thread.start()

    def _open_stream(self, rate: int):
        return self._sd.RawOutputStream(
            samplerate=rate, channels=1, dtype="int16", device=self._device
        )

    async def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        if not self._running:
            raise RuntimeError("Local audio player chưa được start.")
        chunk = bytes(pcm)
        with self._condition:
            self._pending_bytes += len(chunk)
            generation = self._generation
        self._queue.put_nowait((generation, chunk))

    def _writer(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            generation, pcm = item  # type: ignore[misc]
            with self._condition:
                if generation != self._generation:
                    continue
                self._busy = True
            try:
                output = _resample_pcm(pcm, PCM_RATE, self._rate)
                self._stream.write(output)
                with self._condition:
                    if generation == self._generation:
                        self._last_write_at = time.monotonic()
            except Exception as exc:
                with self._condition:
                    if generation == self._generation:
                        self._error = exc
            finally:
                with self._condition:
                    if generation == self._generation:
                        self._pending_bytes = max(0, self._pending_bytes - len(pcm))
                        self._busy = False
                    self._condition.notify_all()

    def _wait_empty(self) -> bool:
        with self._condition:
            while self._running and (self._pending_bytes or self._busy):
                self._condition.wait(timeout=0.1)
            error = self._error
            self._error = None
            had_audio = self._last_write_at is not None
        if error is not None:
            raise RuntimeError(f"Không phát được local audio: {error}") from error
        return had_audio

    async def drain(self) -> None:
        had_audio = await asyncio.to_thread(self._wait_empty)
        if had_audio and self._stream is not None:
            latency = self._stream.latency
            if isinstance(latency, (tuple, list)):
                latency = latency[-1]
            await asyncio.sleep(max(0.0, float(latency or 0.0)))
        with self._condition:
            if self._pending_bytes == 0 and not self._busy:
                self._last_write_at = None

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _flush_sync(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending_bytes = 0
            self._busy = False
            self._error = None
            self._last_write_at = None
            self._clear_queue()
            self._condition.notify_all()
        if self._stream is not None:
            with self._stream_lock:
                self._stream.abort()
                if self._running:
                    self._stream.start()

    async def flush(self) -> None:
        if self._running:
            await asyncio.to_thread(self._flush_sync)

    def _close_sync(self) -> None:
        if not self._running and self._stream is None:
            return
        self._running = False
        with self._condition:
            self._generation += 1
            self._pending_bytes = 0
            self._last_write_at = None
            self._condition.notify_all()
        self._clear_queue()
        self._queue.put(_STOP)
        if self._stream is not None:
            with suppress(Exception):
                self._stream.abort()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._stream is not None:
            with suppress(Exception):
                self._stream.close()
        self._stream = None

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    @property
    def active(self) -> bool:
        return self._running and self._stream is not None

    @property
    def queued_sec(self) -> float:
        with self._condition:
            remaining = self._pending_bytes / PCM_BYTES_PER_SEC
            if self._last_write_at is not None and self._stream is not None:
                latency = self._stream.latency
                if isinstance(latency, (tuple, list)):
                    latency = latency[-1]
                remaining += max(0.0, float(latency or 0) - (time.monotonic() - self._last_write_at))
            return round(remaining, 3)

    def info(self) -> dict[str, Any]:
        return {
            "backend": "sounddevice",
            "device": self._device_name,
            "active": self.active,
            "queued_sec": self.queued_sec,
            "sample_rate": self._rate,
        }


class FfplayPlayer(LocalAudioPlayer):
    def __init__(self, executable: str = "ffplay") -> None:
        self._executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[object] = queue.Queue()
        self._condition = threading.Condition()
        self._process_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._busy = False
        self._pending_bytes = 0
        self._generation = 0
        self._play_started_at: float | None = None
        self._written_bytes = 0
        self._channel_args = ["-ch_layout", "mono"]
        self._error: Exception | None = None

    async def start(self) -> None:
        if self._running:
            return
        await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> None:
        self._channel_args = self._detect_channel_args()
        self._running = True
        try:
            self._restart_process()
        except Exception:
            self._running = False
            raise
        self._thread = threading.Thread(
            target=self._writer, name="local-audio-ffplay", daemon=True
        )
        self._thread.start()

    def _detect_channel_args(self) -> list[str]:
        try:
            result = subprocess.run(
                [self._executable, "-h", "full"], capture_output=True, timeout=5,
                text=True, encoding="utf-8", errors="ignore",
            )
            if "-ch_layout" in (result.stdout + result.stderr):
                return ["-ch_layout", "mono"]
        except Exception:
            pass
        return ["-ac", "1"]

    def _command(self) -> list[str]:
        return [
            self._executable, "-nodisp", "-loglevel", "quiet", "-fflags", "nobuffer",
            "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
            "-f", "s16le", "-ar", "24000", *self._channel_args, "-i", "-",
        ]

    def _restart_process(self) -> None:
        self._kill_process()
        self._process = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _kill_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()
        if process.poll() is None:
            with suppress(Exception):
                process.kill()
        with suppress(Exception):
            process.wait(timeout=2)

    async def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        if not self._running:
            raise RuntimeError("Local audio player chưa được start.")
        chunk = bytes(pcm)
        with self._condition:
            self._pending_bytes += len(chunk)
            generation = self._generation
        self._queue.put_nowait((generation, chunk))

    def _writer(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            generation, pcm = item  # type: ignore[misc]
            with self._condition:
                if generation != self._generation:
                    continue
                self._busy = True
            try:
                with self._process_lock:
                    if self._process is None or self._process.poll() is not None:
                        self._restart_process()
                    assert self._process is not None and self._process.stdin is not None
                    with self._condition:
                        if generation == self._generation and self._play_started_at is None:
                            self._play_started_at = time.monotonic()
                    self._process.stdin.write(pcm)
                    self._process.stdin.flush()
                with self._condition:
                    if generation == self._generation:
                        self._written_bytes += len(pcm)
            except Exception as exc:
                with self._condition:
                    if generation == self._generation:
                        self._error = exc
            finally:
                with self._condition:
                    if generation == self._generation:
                        self._pending_bytes = max(0, self._pending_bytes - len(pcm))
                        self._busy = False
                    self._condition.notify_all()

    def _wait_written(self) -> tuple[float | None, int, Exception | None]:
        with self._condition:
            while self._running and (self._pending_bytes or self._busy):
                self._condition.wait(timeout=0.1)
            return self._play_started_at, self._written_bytes, self._error

    async def drain(self) -> None:
        started_at, written_bytes, error = await asyncio.to_thread(self._wait_written)
        if error is not None:
            with self._condition:
                self._error = None
            raise RuntimeError(f"Không phát được local audio: {error}") from error
        if started_at is not None:
            remaining = written_bytes / PCM_BYTES_PER_SEC - (time.monotonic() - started_at)
            await asyncio.sleep(max(0.0, remaining))
        with self._condition:
            # Chỉ reset nếu chưa có chunk mới tới trong lúc sleep.
            if self._pending_bytes == 0 and self._written_bytes == written_bytes:
                self._play_started_at = None
                self._written_bytes = 0

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _flush_sync(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending_bytes = 0
            self._busy = False
            self._play_started_at = None
            self._written_bytes = 0
            self._error = None
            self._clear_queue()
            self._condition.notify_all()
        # Kill ngoài lock để ngắt ngay một write đang block trong writer thread.
        self._kill_process()
        with self._process_lock:
            if self._running:
                self._restart_process()

    async def flush(self) -> None:
        if self._running:
            await asyncio.to_thread(self._flush_sync)

    def _close_sync(self) -> None:
        self._running = False
        with self._condition:
            self._generation += 1
            self._pending_bytes = 0
            self._condition.notify_all()
        self._clear_queue()
        self._queue.put(_STOP)
        self._kill_process()
        if self._thread is not None:
            self._thread.join(timeout=2)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    @property
    def active(self) -> bool:
        # Process chết sẽ được writer tự mở lại ở chunk kế tiếp.
        return self._running

    @property
    def queued_sec(self) -> float:
        with self._condition:
            queued = self._pending_bytes / PCM_BYTES_PER_SEC
            if self._play_started_at is not None:
                queued += max(
                    0.0,
                    self._written_bytes / PCM_BYTES_PER_SEC
                    - (time.monotonic() - self._play_started_at),
                )
            return round(queued, 3)

    def info(self) -> dict[str, Any]:
        return {
            "backend": "ffplay",
            "device": "system default",
            "active": self.active,
            "queued_sec": self.queued_sec,
            "sample_rate": PCM_RATE,
        }


def create_player(
    backend: str | None = None, output_device: str | None = None
) -> LocalAudioPlayer | None:
    """Chọn backend; việc mở stream/process được thực hiện bởi ``start``."""
    selected = (backend if backend is not None else AUDIO_PLAYER).strip().lower()
    device = AUDIO_OUTPUT_DEVICE if output_device is None else output_device
    if selected not in {"auto", "sounddevice", "ffplay", "none"}:
        raise ValueError("AUDIO_PLAYER phải là auto, sounddevice, ffplay hoặc none.")
    if selected == "none":
        return None

    sd = _import_sounddevice()
    if selected in {"auto", "sounddevice"}:
        if sd is not None and _output_devices(sd):
            return SoundDevicePlayer(device, sd)
        if selected == "sounddevice":
            logger.warning("AUDIO_PLAYER=sounddevice nhưng không có output device/PortAudio.")
            return None

    ffplay = shutil.which("ffplay")
    if ffplay:
        return FfplayPlayer(ffplay)
    if selected == "ffplay":
        logger.warning("AUDIO_PLAYER=ffplay nhưng không tìm thấy ffplay trong PATH.")
    elif selected == "auto":
        logger.warning("Không có sounddevice output hoặc ffplay; local audio bị tắt.")
    return None
