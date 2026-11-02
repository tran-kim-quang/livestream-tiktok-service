import os


def _truthy(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── TikTok ────────────────────────────────────────────────────────────────────
# Username TikTok được cấu hình sẵn; service tự kết nối khi startup
TIKTOK_USERNAME: str = os.environ.get("TIKTOK_USERNAME", "")
# Giây chờ trước khi thử kết nối lại (khi chưa live hoặc live kết thúc)
TIKTOK_LIVE_RETRY_SEC: float = float(os.environ.get("TIKTOK_LIVE_RETRY_SEC", "30"))
# Euler Stream sign key: mặc định đọc EULER, fallback SIGN_API_KEY (tên TikTokLive dùng).
SIGN_API_KEY: str = (os.environ.get("EULER") or os.environ.get("SIGN_API_KEY") or "").strip()

# ── Chọn lọc comment ──────────────────────────────────────────────────────────
COMMENT_MIN_CHARS: int = max(0, int(os.environ.get("COMMENT_MIN_CHARS", "2")))
COMMENT_MAX_CHARS: int = max(1, int(os.environ.get("COMMENT_MAX_CHARS", "300")))
COMMENT_DEDUP_WINDOW_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_DEDUP_WINDOW_SEC", "60"))
)
# Bỏ comment TikTok gửi lại từ trước lúc kết nối (backlog), cho phép lệch đồng hồ N giây
COMMENT_SKIP_BACKLOG = _truthy("COMMENT_SKIP_BACKLOG", True)
COMMENT_BACKLOG_SLACK_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_BACKLOG_SLACK_SEC", "2"))
)
COMMENT_MSG_ID_TTL_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_MSG_ID_TTL_SEC", "600"))
)
COMMENT_USER_COOLDOWN_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_USER_COOLDOWN_SEC", "8"))
)
COMMENT_MERGE_WINDOW_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_MERGE_WINDOW_SEC", "10"))
)
COMMENT_MERGE_MAX_PARTS: int = max(
    1, int(os.environ.get("COMMENT_MERGE_MAX_PARTS", "3"))
)
COMMENT_BLOCKLIST: tuple[str, ...] = tuple(
    value.strip() for value in os.environ.get("COMMENT_BLOCKLIST", "").split(",") if value.strip()
)
COMMENT_PRIORITY_KEYWORDS: tuple[str, ...] = tuple(
    value.strip()
    for value in os.environ.get("COMMENT_PRIORITY_KEYWORDS", "").split(",")
    if value.strip()
)
COMMENT_BUFFER_SIZE: int = max(1, int(os.environ.get("COMMENT_BUFFER_SIZE", "50")))
COMMENT_MAX_AGE_SEC: float = max(0.0, float(os.environ.get("COMMENT_MAX_AGE_SEC", "30")))
COMMENT_FRESH_WINDOW_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_FRESH_WINDOW_SEC", "15"))
)
COMMENT_MAX_SERVE_PER_SEC: float = max(
    0.0, float(os.environ.get("COMMENT_MAX_SERVE_PER_SEC", "0"))
)

# ── GPU ───────────────────────────────────────────────────────────────────────
# Địa chỉ máy GPU (IP LAN/Tailscale hoặc hostname). "local" = card trên máy này.
GPU_HOST: str = os.environ.get("GPU_HOST", "local")
# Tên hiển thị cho máy GPU (tuỳ chọn, chỉ để log / health)
GPU_NAME: str = os.environ.get("GPU_NAME", "")
# "local" trỏ tới đâu — trong Docker cần host.docker.internal thay vì 127.0.0.1
GPU_LOCAL_HOST: str = os.environ.get("GPU_LOCAL_HOST", "127.0.0.1")
GPU_SSH_USER: str = os.environ.get("GPU_SSH_USER", "")
GPU_SSH_PORT: int = int(os.environ.get("GPU_SSH_PORT", "22"))
GPU_SSH_KEY: str = os.environ.get("GPU_SSH_KEY", "")
GPU_SSH_PASSWORD: str = os.environ.get("GPU_SSH_PASSWORD", "")
GPU_SSH_KNOWN_HOSTS: str = os.environ.get("GPU_SSH_KNOWN_HOSTS", "")

# ── TTS (VoiceStudio chạy trên máy GPU) ───────────────────────────────────────
TTS_ENABLED = _truthy("TTS_ENABLED", True)
# Port web API có VoiceStudio (Pyramid Video Web) trên máy GPU
VOICESTUDIO_PORT: int = int(os.environ.get("VOICESTUDIO_PORT", "8080"))
VOICESTUDIO_API_PORT: int = int(os.environ.get("VOICESTUDIO_API_PORT", "3900"))
VOICESTUDIO_API_KEY: str = os.environ.get("VOICESTUDIO_API_KEY", "")
TTS_ENGINE: str = os.environ.get("TTS_ENGINE", "voicestudio").strip().lower()
_tts_num_step = os.environ.get("TTS_NUM_STEP", "").strip()
TTS_NUM_STEP: int | None = int(_tts_num_step) if _tts_num_step else None
TTS_FIRST_CHUNK_MAX_WORDS: int = int(os.environ.get("TTS_FIRST_CHUNK_MAX_WORDS", "6"))
_tts_first_chunk_num_step = os.environ.get("TTS_FIRST_CHUNK_NUM_STEP", "8").strip()
TTS_FIRST_CHUNK_NUM_STEP: int | None = (
    int(_tts_first_chunk_num_step)
    if _tts_first_chunk_num_step and int(_tts_first_chunk_num_step) > 0
    else None
)
# ID giọng mặc định; để trống = dùng giọng đang đặt mặc định trên VoiceStudio
TTS_VOICE_ID: str = os.environ.get("TTS_VOICE_ID", "")
TTS_TIMEOUT_SEC: float = float(os.environ.get("TTS_TIMEOUT_SEC", "120"))
# Số request TTS chạy song song tới GPU (GPU dùng chung với job render video)
TTS_MAX_CONCURRENCY: int = max(1, int(os.environ.get("TTS_MAX_CONCURRENCY", "1")))

# ── Phát audio trên máy local ─────────────────────────────────────────────────
AUDIO_PLAYER_EXPLICIT: bool = "AUDIO_PLAYER" in os.environ
AUDIO_PLAYER: str = os.environ.get("AUDIO_PLAYER", "auto").strip().lower()
AUDIO_OUTPUT_DEVICE: str = os.environ.get("AUDIO_OUTPUT_DEVICE", "").strip()
AGENT_LOCAL_PLAYBACK: bool = _truthy("AGENT_LOCAL_PLAYBACK", True)

# ── Agent / LLM ──────────────────────────────────────────────────────────
def _openai_base_url(url: str) -> str:
    """Chuẩn hoá về base URL kiểu OpenAI (…/v1); service tự nối /chat/completions."""
    url = url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    # API gốc của Ollama (/api/chat) không theo chuẩn OpenAI → dùng /v1 của cùng host
    for native in ("/api/chat", "/api"):
        if url.endswith(native):
            url = url[: -len(native)] + "/v1"
            break
    return url


LLM_BASE_URL: str = _openai_base_url(os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL: str = (
    os.environ.get("LLM_MODEL_NAME") or os.environ.get("LLM_MODEL") or "gpt-4o-mini"
).strip()
LLM_TIMEOUT_SEC: float = max(0.1, float(os.environ.get("LLM_TIMEOUT_SEC", "30")))
LLM_MAX_TOKENS: int = max(1, int(os.environ.get("LLM_MAX_TOKENS", "150")))
LLM_TEMPERATURE: float = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
LLM_MAX_RPS: float = max(0.0, float(os.environ.get("LLM_MAX_RPS", "60")))
LLM_MAX_CONCURRENCY: int = max(1, int(os.environ.get("LLM_MAX_CONCURRENCY", "2")))
AGENT_ENABLED: bool = _truthy("AGENT_ENABLED", True)
AGENT_AUTO: bool = _truthy("AGENT_AUTO", False)
AGENT_AUTO_SPEAK: bool = _truthy("AGENT_AUTO_SPEAK", True)
AGENT_GREETING: bool = _truthy("AGENT_GREETING", True)
AGENT_GREETING_TEMPLATES: tuple[str, ...] = tuple(
    value.strip()
    for value in os.environ.get(
        "AGENT_GREETING_TEMPLATES", "{name} ơi,|Chào {name},|À {name},"
    ).split("|")
    if value.strip()
) or ("{name} ơi,",)
AGENT_SKIP_WHEN_NO_LISTENER: bool = _truthy("AGENT_SKIP_WHEN_NO_LISTENER", True)
AGENT_REPLY_GAP_SEC: float = max(
    0.0, float(os.environ.get("AGENT_REPLY_GAP_SEC", "0.6"))
)
AGENT_PLAYED_ACK_GRACE_SEC: float = max(
    0.0, float(os.environ.get("AGENT_PLAYED_ACK_GRACE_SEC", "3"))
)
AGENT_SYSTEM_PROMPT: str = os.environ.get("AGENT_SYSTEM_PROMPT", "").strip()
AGENT_PROMPT_FILE: str = os.environ.get("AGENT_PROMPT_FILE", "").strip()
AGENT_HISTORY_TURNS: int = max(0, int(os.environ.get("AGENT_HISTORY_TURNS", "6")))
AGENT_REPLY_BUFFER: int = max(1, int(os.environ.get("AGENT_REPLY_BUFFER", "50")))
