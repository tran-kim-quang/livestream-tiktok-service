# TikTok Live Comment Service

Service FastAPI thu thập comment realtime từ TikTok Live, chọn comment mới và đáng trả lời nhất cho AI agent, đồng thời cung cấp TTS qua VoiceStudio trên máy GPU.

## Kiến trúc

```text
TikTok Live ──comment──> LiveStreamHub ──> Filter ──> Score ──> CommentBuffer (RAM)
                         │                                      │
                         └──────── WebSocket /ws/live/{id}       └── best/newest ──> AI Agent

FastAPI /api/tts/* ──SSH tunnel/HTTP──> GpuNode ──> VoiceStudio API :3900
```

## Cài đặt

```bash
cp .env.example .env
docker compose up -d
curl http://localhost:8002/health
```

Đặt `TIKTOK_USERNAME` (không có `@`) nếu muốn service tự theo dõi một kênh khi khởi động. Không đặt biến này thì vẫn có thể gọi `POST /api/live/watch`.

## Project structure

```text
app/
  api/                 # Router health, comment, queue, live và TTS
  comments/            # Lọc, chấm điểm và buffer comment trong RAM
  live/hub.py          # Session TikTok và quản lý subscriber
  live/watcher.py      # Background auto-watch
  tts/                 # Interface TTS và VoiceStudio client
  gpu.py               # Cấu hình địa chỉ máy GPU
  main.py              # FastAPI lifespan, app.state, include router
```

## API queue và live

| API | Mô tả |
|---|---|
| `GET /health` | Health, thống kê comment, session, auto-watch và trạng thái TTS |
| `GET /api/queue/pop?timeout=5` | Lấy comment mới, điểm cao nhất và xoá khỏi buffer |
| `GET /api/queue/length` | Số comment mới, chưa phục vụ |
| `GET /api/comments/latest?limit=20` | Comment còn hạn, mới nhất trước; chỉ đọc |
| `GET /api/comments/stats` | Bộ đếm lọc/phục vụ và cấu hình hiện tại |
| `POST /api/live/watch` | Body `{"unique_id":"ten_kenh"}`; bắt đầu theo dõi |
| `DELETE /api/live/watch/{unique_id}` | Dừng theo dõi |
| `GET /api/live/sessions` | Danh sách session |
| `WS /ws/live/{unique_id}` | Nhận comment realtime qua WebSocket |

Response `GET /api/queue/pop` giữ dạng:

```json
{"empty": false, "comment": {"comment": "...", "user": {}, "room_id": 123, "score": 5.2}}
```

Agent hiện có vẫn gọi `/api/queue/pop` như trước. Mỗi comment chỉ được trả một lần; nếu chưa có comment mới thì endpoint chờ tối đa `timeout` giây rồi trả `{"empty":true,"comment":null}`.

## Chọn lọc comment

Comment được xử lý ngay khi nhận từ TikTok, hoàn toàn trong RAM. Service bỏ comment rỗng hoặc chỉ có emoji/dấu câu, quá ngắn/dài, chứa URL, trùng nội dung gần đây, gửi quá nhanh từ cùng người dùng, hoặc chứa từ trong blocklist. So sánh duplicate và blocklist không phân biệt hoa thường/dấu tiếng Việt, rút gọn ký tự lặp và khoảng trắng.

Comment hợp lệ được cộng điểm khi có dấu hiệu câu hỏi tiếng Việt, nội dung có ý nghĩa dài hơn, nhắc streamer hoặc chứa `COMMENT_PRIORITY_KEYWORDS`. `/api/queue/pop` chọn điểm cao nhất trong cửa sổ mới; bằng điểm thì chọn comment đến sau. Comment khác vẫn được giữ và mỗi comment chỉ phục vụ một lần. Tăng/giảm các biến `COMMENT_*` để điều chỉnh theo tốc độ live; đặt `COMMENT_MAX_SERVE_PER_SEC=0` để không giới hạn tốc độ agent lấy comment.

## Tích hợp agent

Agent cũ không cần đổi endpoint hay response handling:

```python
import httpx

response = httpx.get(
    "http://localhost:8002/api/queue/pop",
    params={"timeout": 5},
    timeout=10,
)
data = response.json()
if not data["empty"]:
    comment = data["comment"]
    reply = my_agent.respond(
        query=comment["comment"],
        sender=comment["user"]["nickname"],
    )
```

## Cấu hình GPU

`GpuNode` là địa chỉ dùng chung cho các tác vụ GPU. Mặc định `GPU_HOST=local` nghĩa là card ở máy chạy service; ngoài Docker nó trỏ tới `127.0.0.1`. Docker Compose đổi địa chỉ local thành `host.docker.internal` để gọi service trên host.

Muốn dùng máy GPU từ xa, đặt IP LAN/Tailscale hoặc hostname. Ví dụ máy Tailscale đã cấu hình VoiceStudio:

```env
GPU_HOST=100.98.141.59
GPU_NAME=meconlonton-ms-7d48
GPU_SSH_USER=meconlonton
GPU_SSH_KEY=/run/secrets/gpu_ssh_key
VOICESTUDIO_API_PORT=3900
```

VoiceStudio chỉ nghe ở `127.0.0.1:3900` trên máy GPU nên cách khuyến nghị là dùng SSH key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gpu_voicestudio_ed25519
ssh-copy-id -i ~/.ssh/gpu_voicestudio_ed25519.pub meconlonton@100.98.141.59
ssh-keyscan 100.98.141.59 >> ~/.ssh/known_hosts
```

Compose mặc định mount `${GPU_SSH_KEY_HOST_PATH:-~/.ssh/gpu_voicestudio_ed25519}` read-only vào `/run/secrets/gpu_ssh_key`. Có thể đặt `GPU_SSH_KNOWN_HOSTS` tới một file `known_hosts` đã mount. Nếu bỏ trống, tunnel vẫn chạy nhưng không kiểm tra host key và service ghi warning. `GPU_SSH_PASSWORD` chỉ là phương án dự phòng; mật khẩu không xuất hiện trong health/log.

## API TTS

VoiceStudio chạy trên GPU và dùng voice mặc định của nó nếu `TTS_VOICE_ID` để trống.

```bash
curl http://localhost:8002/api/tts/status
curl http://localhost:8002/api/tts/voices
curl -X POST http://localhost:8002/api/tts/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Xin chào từ TikTok Live","voice_id":"599fc048"}' \
  --output reply.wav

curl -N -X POST http://localhost:8002/api/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"Xin chào mọi người. Đây là câu thứ hai!","format":"pcm"}' \
  --output reply.pcm
```

`POST /api/tts/stream` nhận text tối đa 2000 ký tự. Service tạo chunk đầu ngắn (mặc định 6 từ), tăng dần kích thước chunk sau và tổng hợp trước trong producer riêng để client phát liên tục. Format mặc định là raw PCM 24 kHz, mono, s16le; chọn `"format":"wav"` để nhận WAV streaming. VoiceStudio/OmniVoice không stream thật, service stream theo chunk. Có thể ghi đè từng request bằng `first_chunk_num_step` (1–64) và `first_chunk_max_words` (2–30).

Độ trễ tới audio đầu tiên đo trực tiếp với VoiceStudio `/v1/audio/speech`, OmniVoice, RTX 3050 (chưa tính khoảng 200 ms overhead cố định qua SSH tunnel):

| `num_step` | 4 từ | 8 từ | 16 từ |
|---|---:|---:|---:|
| mặc định (16) | 1,65 s | 1,87 s | 2,26 s |
| 8 | 0,85 s | 0,98 s | 1,15 s |
| 4 | 0,47 s | 0,54 s | 0,67 s |

Audio tiếng Việt dài khoảng 0,25–0,28 giây mỗi từ. Vì vậy mặc định dùng `num_step=8` riêng cho chunk đầu; các chunk sau dùng `TTS_NUM_STEP` và được prefetch ngay khi chunk trước tổng hợp xong.

Khi `TTS_ENABLED=0`, `GET /api/tts/status` trả `{"ok": false, "enabled": false}`; các endpoint TTS khác trả HTTP 503. Lỗi gọi VoiceStudio từ API voices/synthesize trả HTTP 502; API status trả `ok: false` cùng mô tả lỗi.

Script thử nhanh từ service đang chạy:

```bash
python test/test_tts.py
# BASE_URL=http://127.0.0.1:8002 OUT=reply.wav python test/test_tts.py
python test/test_tts_stream.py
# FORMAT=wav OUT=tts_stream.wav python test/test_tts_stream.py
# Phát ngay bằng ffplay và báo thời gian tới audio đầu tiên/underrun
python test/test_tts_stream.py --play
```

## Biến môi trường

| Biến | Mặc định | Mô tả |
|---|---|---|
| `TIKTOK_USERNAME` | rỗng | Username TikTok tự theo dõi |
| `TIKTOK_LIVE_RETRY_SEC` | `30` | Giây chờ trước lần reconnect live |
| `EULER` | rỗng | Euler Stream sign key cho TikTokLive (fallback `SIGN_API_KEY`) |
| `COMMENT_MIN_CHARS` | `2` | Số chữ/số tối thiểu sau khi bỏ emoji/dấu câu |
| `COMMENT_MAX_CHARS` | `300` | Độ dài comment tối đa |
| `COMMENT_DEDUP_WINDOW_SEC` | `60` | Cửa sổ loại nội dung trùng từ mọi user |
| `COMMENT_USER_COOLDOWN_SEC` | `10` | Cooldown giữa comment hợp lệ của cùng user |
| `COMMENT_BLOCKLIST` | rỗng | Từ/cụm từ chặn, phân cách bằng dấu phẩy |
| `COMMENT_PRIORITY_KEYWORDS` | rỗng | Từ/cụm từ được cộng điểm, phân cách bằng dấu phẩy |
| `COMMENT_BUFFER_SIZE` | `50` | Số comment hợp lệ tối đa giữ trong RAM |
| `COMMENT_MAX_AGE_SEC` | `30` | Tuổi tối đa trước khi loại khỏi buffer |
| `COMMENT_FRESH_WINDOW_SEC` | `10` | Cửa sổ comment đủ mới để `/queue/pop` chọn |
| `COMMENT_MAX_SERVE_PER_SEC` | `0` | Giới hạn lượt pop mỗi giây; `0` là không giới hạn |
| `GPU_HOST` | `local` | `local`, IP LAN/Tailscale hoặc hostname GPU |
| `GPU_NAME` | rỗng | Tên hiển thị máy GPU trong log/health |
| `GPU_LOCAL_HOST` | `127.0.0.1` | Địa chỉ thật khi `GPU_HOST=local` |
| `GPU_SSH_USER` | rỗng | User SSH; có giá trị sẽ bật tunnel cho GPU remote |
| `GPU_SSH_PORT` | `22` | Port SSH của máy GPU |
| `GPU_SSH_KEY` | rỗng | Private key SSH (trong Compose: `/run/secrets/gpu_ssh_key`) |
| `GPU_SSH_PASSWORD` | rỗng | Mật khẩu SSH dự phòng, không được log |
| `GPU_SSH_KNOWN_HOSTS` | rỗng | File known_hosts; rỗng là không kiểm tra host key |
| `TTS_ENGINE` | `voicestudio` | `voicestudio` hoặc `pyramid` |
| `VOICESTUDIO_API_PORT` | `3900` | Port VoiceStudio API v0.5.x |
| `VOICESTUDIO_API_KEY` | rỗng | Bearer token khi gọi remote trực tiếp |
| `VOICESTUDIO_PORT` | `8080` | Port Pyramid Video Web (engine legacy) |
| `TTS_ENABLED` | `true` | Tắt TTS bằng `0` |
| `TTS_VOICE_ID` | rỗng | Voice ID mặc định; rỗng dùng default của VoiceStudio |
| `TTS_FIRST_CHUNK_MAX_WORDS` | `6` | Số từ tối đa ưu tiên cho chunk đầu |
| `TTS_FIRST_CHUNK_NUM_STEP` | `8` | `num_step` của chunk đầu; rỗng/`0` dùng mặc định engine |
| `TTS_NUM_STEP` | rỗng | `num_step` gửi tới OmniVoice; rỗng dùng mặc định engine |
| `TTS_MAX_CONCURRENCY` | `1` | Số request TTS song song, bảo vệ GPU dùng chung |
| `TTS_TIMEOUT_SEC` | `120` | Timeout gọi VoiceStudio (giây) |
| `LIVESTREAM_SERVICE_PORT` | `8002` | Port expose ra ngoài |
