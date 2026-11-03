from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env"
DEFAULT_EXAMPLE = ROOT / ".env.example"

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


class DevError(Exception):
    pass


def log(message: str) -> None:
    print(f"[dev] {message}", flush=True)


def clean_tiktok(value: str | None) -> str:
    return (value or "").strip().lstrip("@").strip()


def env_value(path: Path, key: str, default: str = "") -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$")
    value = default
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line in handle:
            match = pattern.match(line.rstrip("\r\n"))
            if not match:
                continue
            raw = match.group(1).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            else:
                raw = re.sub(r"\s+#.*$", "", raw).strip()
            value = raw
    return value


def set_env_value(path: Path, key: str, value: str) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^(\s*(?:export\s+)?{re.escape(key)}\s*=\s*)")
    found = False
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = pattern.match(body)
        if match:
            old_value = body[match.end() :]
            inline_comment = re.search(r"(\s+#.*)$", old_value)
            suffix = inline_comment.group(1) if inline_comment else ""
            updated.append(f"{match.group(1)}{value}{suffix}{ending}")
            found = True
        else:
            updated.append(line)
    if not found:
        if text and not text.endswith(("\n", "\r")):
            updated.append(os.linesep)
        ending = "\r\n" if "\r\n" in text else "\n"
        updated.append(f"{key}={value}{ending}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(updated))


def command_env(args: argparse.Namespace) -> None:
    env_file = Path(args.file)
    example_file = Path(args.example)
    if not env_file.exists():
        if not example_file.exists():
            raise DevError(f"Không tìm thấy file mẫu: {example_file}")
        shutil.copyfile(example_file, env_file)
        log(f"Đã tạo {env_file} từ {example_file}. Hãy kiểm tra LLM_API_KEY và GPU_*.")

    username = clean_tiktok(args.tiktok)
    if args.tiktok is not None:
        set_env_value(env_file, "TIKTOK_USERNAME", username)
    set_env_value(env_file, "AGENT_AUTO", "1")

    if not clean_tiktok(env_value(env_file, "TIKTOK_USERNAME")):
        raise DevError("Thiếu TikTok username. Chạy: make start TIKTOK=ten_kenh")
    if not env_value(env_file, "LLM_API_KEY"):
        log("Cảnh báo: LLM_API_KEY đang trống; agent có thể không hoạt động.")
    log("Cấu hình .env đã sẵn sàng.")


def service_url(path: str, env_file: Path = DEFAULT_ENV) -> str:
    port = env_value(env_file, "LIVESTREAM_SERVICE_PORT", "8002") or "8002"
    if not port.isdigit():
        raise DevError("LIVESTREAM_SERVICE_PORT trong .env không hợp lệ.")
    return f"http://127.0.0.1:{port}{path}"


def request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> tuple[int, str, bytes]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(service_url(path), data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as exc:
        body = exc.read()
        detail = body.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
        except json.JSONDecodeError:
            pass
        raise DevError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, OSError, HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise DevError(f"Không kết nối được service: {reason}") from exc


def decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DevError("Service trả về dữ liệu JSON không hợp lệ.") from exc


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def watched_names(health: dict[str, Any]) -> list[str]:
    names: list[str] = []
    auto_watch = health.get("auto_watch")
    if isinstance(auto_watch, str) and auto_watch:
        names.append(auto_watch)
    sessions = health.get("sessions", [])
    items = list(sessions.values()) if isinstance(sessions, dict) else sessions
    if isinstance(items, list):
        for item in items:
            name = item.get("unique_id") if isinstance(item, dict) else item
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def tts_summary(tts: Any) -> str | None:
    if tts is None:
        return None
    if not isinstance(tts, dict):
        return str(tts)
    for key in ("ok", "available", "enabled", "status"):
        if key in tts:
            if isinstance(tts[key], bool):
                return "ok" if tts[key] else "lỗi"
            return str(tts[key])
    return "đã cấu hình"


def command_wait(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    url = service_url("/health")
    log(f"Đang chờ service tại {url}...")
    while time.monotonic() < deadline:
        try:
            status, _, body = request("/health", timeout=2)
            health = decode_json(body)
            if status == 200 and isinstance(health, dict):
                parts = ["service đã sẵn sàng: status ok"]
                names = watched_names(health)
                if names:
                    parts.append("kênh " + ", ".join(f"@{name}" for name in names))
                tts = tts_summary(health.get("tts"))
                if tts is not None:
                    parts.append(f"TTS {tts}")
                log("; ".join(parts) + ".")
                return
        except DevError:
            pass
        time.sleep(2)
    raise DevError(f"Service chưa sẵn sàng sau {args.timeout}s. Xem log bằng: make logs")


def command_watch(args: argparse.Namespace) -> None:
    username = clean_tiktok(args.tiktok)
    if not username:
        raise DevError("Thiếu TikTok username. Chạy: make watch TIKTOK=ten_kenh")
    _, _, body = request("/api/live/watch", method="POST", payload={"unique_id": username})
    log(f"Đã gửi yêu cầu theo dõi @{username}.")
    print_json(decode_json(body))


def command_status(_: argparse.Namespace) -> None:
    for label, path in (("Health", "/health"), ("Agent", "/api/agent/status")):
        _, _, body = request(path)
        log(f"{label}:")
        print_json(decode_json(body))


def command_say(args: argparse.Namespace) -> None:
    text = args.text or "Nói một câu để kiểm tra loa"
    status, content_type, body = request(
        "/api/agent/speak", method="POST", payload={"comment": text}
    )
    if "json" in content_type.lower():
        log(f"Yêu cầu phát thử hoàn tất (HTTP {status}).")
        print_json(decode_json(body))
    else:
        log(f"Yêu cầu phát thử hoàn tất (HTTP {status}, nhận {len(body)} byte audio).")


def command_stop_audio(_: argparse.Namespace) -> None:
    status, _, body = request("/api/agent/audio/stop", method="POST", payload={})
    log(f"Đã gửi lệnh dừng audio (HTTP {status}).")
    print_json(decode_json(body))


def command_bridge(args: argparse.Namespace) -> None:
    venv = ROOT / ".venv-bridge"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".bridge-requirements-installed"
    if not python.exists():
        log("Tạo môi trường Python .venv-bridge...")
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    if not marker.exists():
        log("Cài dependencies audio bridge lần đầu...")
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-bridge.txt")]
        )
        marker.touch()
    command = [str(python), "-m", "app.audio.bridge", *args.bridge_args]
    os.chdir(ROOT)
    if os.name == "nt":
        raise SystemExit(subprocess.call(command))
    os.execv(str(python), command)


def command_make_help(_: argparse.Namespace) -> None:
    print(
        """Livestream TikTok Service

Targets:
  start       Chạy service, chờ sẵn sàng rồi chạy audio bridge
  env         Tạo/cập nhật .env
  build       Build image Docker (khi sửa code)
  up          Chạy service, chỉ build nếu chưa có image
  wait        Chờ service sẵn sàng
  bridge      Chạy audio bridge ở foreground
  watch       Chuyển/thêm kênh TikTok đang theo dõi
  status      Xem health và trạng thái agent
  say         Kiểm tra agent/TTS (TEXT=\"...\")
  stop-audio  Dừng audio đang phát
  logs        Theo dõi log service
  restart     Khởi động lại container
  stop        Dừng và gỡ container

Ví dụ: make start TIKTOK=ten_kenh
       make start BUILD=1   (build lại image rồi chạy)"""
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Công cụ phát triển livestream-tiktok-service")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("make-help", help=argparse.SUPPRESS).set_defaults(func=command_make_help)

    env_parser = commands.add_parser("env", help="Tạo/cập nhật file .env")
    env_parser.add_argument("--tiktok", metavar="NAME")
    env_parser.add_argument("--file", default=str(DEFAULT_ENV), help="file đích (mặc định: .env)")
    env_parser.add_argument(
        "--example", default=str(DEFAULT_EXAMPLE), help="file mẫu (mặc định: .env.example)"
    )
    env_parser.set_defaults(func=command_env)

    wait_parser = commands.add_parser("wait", help="Chờ service sẵn sàng")
    wait_parser.add_argument("--timeout", type=float, default=120)
    wait_parser.set_defaults(func=command_wait)

    watch_parser = commands.add_parser("watch", help="Theo dõi kênh TikTok")
    watch_parser.add_argument("--tiktok", required=True, metavar="NAME")
    watch_parser.set_defaults(func=command_watch)

    commands.add_parser("status", help="Xem trạng thái service").set_defaults(func=command_status)
    say_parser = commands.add_parser("say", help="Kiểm tra agent và loa")
    say_parser.add_argument("--text")
    say_parser.set_defaults(func=command_say)
    commands.add_parser("stop-audio", help="Dừng audio").set_defaults(func=command_stop_audio)

    bridge_parser = commands.add_parser("bridge", help="Chạy audio bridge")
    bridge_parser.add_argument("bridge_args", nargs=argparse.REMAINDER)
    bridge_parser.set_defaults(func=command_bridge)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["bridge"] and values[1:] != ["--help"]:
        args = build_parser().parse_args(["bridge"])
        args.bridge_args = values[1:]
        if args.bridge_args[:1] == ["--"]:
            args.bridge_args.pop(0)
        return args
    return build_parser().parse_args(values)


def main() -> int:
    args = parse_args()
    try:
        args.func(args)
        return 0
    except DevError as exc:
        log(str(exc))
        return 1
    except KeyboardInterrupt:
        log("Đã dừng.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
