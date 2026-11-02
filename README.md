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

Comment được xử lý ngay khi nhận từ TikTok, hoàn toàn trong RAM. Service bỏ comment rỗng hoặc chỉ có emoji/dấu câu, quá ngắn/dài, chứa URL, trùng nội dung gần đây, hoặc chứa từ trong `COMMENT_BLOCKLIST`. Blocklist mặc định rỗng: comment thô tục không tự động bị bỏ; chỉ các từ/cụm từ được cấu hình làm hard ban mới bị loại. So sánh duplicate và blocklist không phân biệt hoa thường/dấu tiếng Việt, rút gọn ký tự lặp và khoảng trắng.

Các comment chưa được trả lời của cùng user được gộp trong `COMMENT_MERGE_WINDOW_SEC`, tối đa `COMMENT_MERGE_MAX_PARTS` phần và không vượt `COMMENT_MAX_CHARS`. Ví dụ `saberg_6` gửi “sao troll rồi”, rồi 4 giây sau gửi “trả giá vì troll rồi” thì queue giữ một bản ghi `sao troll rồi. trả giá vì troll rồi`, kèm `parts`, `msg_ids` và `merged`.

Comment hợp lệ được so embedding với các dòng ngữ cảnh trong `COMMENT_CONTEXT_FILE` (fallback `AGENT_PROMPT_FILE`) qua Ollama trên máy GPU. Điểm chọn là `similarity_core * COMMENT_CORE_WEIGHT + audience_bonus`; khi Ollama chưa sẵn sàng, lỗi hoặc timeout, service tự dùng heuristic câu hỏi/độ dài cũ đã chuẩn hoá về `[0,1]`. Gift, follow và các cờ subscriber/follower/moderator cộng bonus tại lúc chọn, nên gift đến sau comment vẫn tăng ưu tiên. Gift/follow vừa xảy ra còn cho user bỏ qua cooldown trong `COMMENT_PRIORITY_WINDOW_SEC`; cờ identity lịch sử chỉ cộng điểm, không bỏ qua cooldown.

Ollama dùng model `qwen3-embedding:0.6b`, mặc định chạy trên **CPU** của máy GPU (`COMMENT_EMBED_NUM_GPU=0`, ~30 ms/comment) để nhường VRAM cho VoiceStudio, và đi tới máy GPU giống VoiceStudio: trực tiếp theo `GPU_HOST`, hoặc một SSH tunnel riêng tới port 11434. `COMMENT_EMBED_URL` ghi đè đường đi này. Warmup chạy nền và tự thử lại; `/health` cùng `/api/comments/stats` hiển thị trạng thái model, số passage và bộ đếm fallback. Mẫu ngữ cảnh ngắn nằm ở `context/live_context.example.md`.

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

## Agent trả lời comment

Khi có `LLM_API_KEY`, service bật agent dùng API OpenAI-compatible tại
`{LLM_BASE_URL}/chat/completions` (không dùng OpenAI SDK). Có thể ghi đè
system prompt bằng `AGENT_SYSTEM_PROMPT` hoặc file `AGENT_PROMPT_FILE`.
Prompt mặc định yêu cầu vẫn trả lời comment thô tục, chửi bậy hoặc khiêu khích
một cách lịch sự, bình tĩnh và hài hước nhẹ nhàng, không lặp lại lời tục hay
đáp trả gay gắt; có thể lái câu trả lời về nội dung buổi live.

### Biểu cảm giọng đọc

Mặc định nội dung `context/tts_style_guide.md` được nối vào mọi system prompt,
kể cả prompt tuỳ chỉnh. Đặt `AGENT_TTS_GUIDE_FILE=` để tắt. OmniVoice hiểu các
tag biểu cảm như `[laughter]`, `[sigh]`, `[surprise-ah]` và `[question-ah]`;
service chỉ giữ tối đa `AGENT_TTS_MAX_TAGS` tag hợp lệ trong mỗi reply (mặc định
1). Trước khi đọc và lưu history, service bỏ emoji, markdown, stage direction,
tag lạ và dấu `!`/`?` lặp. `[pause 500ms]` không được hỗ trợ và sẽ bị bỏ.

```bash
curl -X POST http://localhost:8002/api/agent/reply \
  -H 'Content-Type: application/json' \
  -d '{"comment":"Hôm nay có sản phẩm gì?","nickname":"Lan"}'

curl -N -X POST http://localhost:8002/api/agent/speak \
  -H 'Content-Type: application/json' \
  -d '{"comment":"Chào mọi người đi","nickname":"Lan","format":"pcm"}' \
  --output agent_reply.pcm
```

API gồm `GET /api/agent/status`, `POST /api/agent/reply`,
`GET /api/agent/replies`, `WS /api/agent/ws`, `POST /api/agent/speak` và
`POST /api/agent/audio/stop`.
Endpoint `speak` nhận token LLM song song với việc tổng hợp các chunk TTS đã
hoàn chỉnh, nhằm giảm thời gian tới audio đầu tiên.

Mặc định `AGENT_GREETING=1`: service lấy display name TikTok (`nickname`, không
dùng `@handle`/ID), chuẩn hoá NFKC rồi bỏ emoji, ký hiệu trang trí và ký tự
zero-width trước khi tạo lời chào ngắn như `Saber ơi,`. Tên được giới hạn 3
từ/24 ký tự; nickname rỗng sau khi làm sạch sẽ fallback sang `unique_id`, rồi
`bạn`. Các mẫu trong `AGENT_GREETING_TEMPLATES` phân cách bằng `|` và không lặp
lại ngay hai lần liên tiếp.

Greeting được đưa vào TTS ngay khi bắt đầu request, song song lúc chờ LLM; chunk
LLM đầu tiên cũng dùng cấu hình TTS nhanh. PCM greeting chỉ được gửi/phát sau
delta LLM đầu tiên, nên reply LLM rỗng vẫn bị bỏ hoàn toàn. Vì vậy thời gian tới
audio đầu tiên xấp xỉ giá trị lớn hơn giữa TTS greeting và token LLM đầu, thay vì
tổng của hai khoảng này. Reply trả về/lưu/broadcast gồm greeting + nội dung LLM,
nhưng history gửi lại LLM chỉ lưu nội dung LLM để model không học cách chào lại.

Agent có hai auto mode khi `AGENT_AUTO=1`:

- `AGENT_AUTO_SPEAK=1` (mặc định): comment → LLM streaming → TTS theo chunk →
  raw PCM qua `WS /api/agent/ws`. Chế độ này chỉ hoạt động khi TTS bật; nếu TTS
  tắt, service ghi warning và tự lùi về text-only.
- `AGENT_AUTO_SPEAK=0`: lưu reply và gửi JSON `{"type":"reply", ...}` như
  trước, không gọi TTS.

Khi `AGENT_AUTO=1`, service là **consumer duy nhất** của comment buffer và
`GET /api/queue/pop` trả HTTP 409. Không gọi endpoint pop từ agent ngoài ở chế
độ này. Mặc định `AGENT_SKIP_WHEN_NO_LISTENER=1`: khi chưa có WebSocket listener
**và** local player không hoạt động, service không lấy comment và không gọi
LLM/TTS; comment còn nguyên trong buffer rồi hết hạn tự nhiên. Đặt bằng `0` nếu
muốn agent vẫn sinh và lưu reply phục vụ stats dù không có listener.

Mỗi spoken reply trên WebSocket có thứ tự frame cố định:

1. JSON text `{"type":"reply_start","id":"...","comment":{...},"format":"pcm","sample_rate":24000,"channels":1,"sample_format":"s16le"}`.
2. Một hoặc nhiều binary frame raw PCM s16le 24 kHz mono, gửi ngay khi từng chunk TTS xong.
3. JSON text `{"type":"reply_end","id":"...","reply":"...","latency_ms":{"llm_first_token":120,"greeting_ready":760,"first_audio":780,"first_audio_played":781,"total":1450},"audio_sec":2.1}`.

Nếu lỗi giữa reply, service gửi `{"type":"reply_error","id":"...","error":"..."}`
rồi tiếp tục comment kế. Reply LLM rỗng bị bỏ qua, không có `reply_start` hay
audio. Client quá chậm làm đầy queue sẽ bị đóng kết nối để không mất âm thanh
giữa reply.

Client tối thiểu (cài package `websockets` riêng nếu môi trường chưa có):

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8002/api/agent/ws") as ws:
        with open("agent.pcm", "wb") as output:
            async for frame in ws:
                if isinstance(frame, bytes):
                    output.write(frame)
                else:
                    print(json.loads(frame))

asyncio.run(main())
```

## Docker + phát âm thanh trên Windows (audio bridge)

Container Docker không có sound device của Windows. Service vì vậy chỉ sinh và
gửi PCM; một process Python nhỏ chạy native trên host nhận PCM qua WebSocket và
phát bằng player `sounddevice`/`ffplay` có sẵn trong project:

```text
TikTok → service Docker :8002 → WS /api/agent/ws → audio bridge Windows → loa/headset
             LLM + TTS            PCM + reply_end       played ACK
```

Compose đã đặt `AUDIO_PLAYER=none` và `AGENT_LOCAL_PLAYBACK=0`, nên image không
mở audio và không cần cài `sounddevice`. Khởi động service, sau đó mở terminal
PowerShell thứ hai trên Windows:

```powershell
docker compose up --build
scripts\audio-bridge.ps1
```

Script tạo `.venv-bridge`, cài `requirements-bridge.txt` một lần rồi chạy bridge.
Có thể dùng wrapper `scripts\audio-bridge.bat`, hoặc cài dependencies và chạy tay:

```powershell
python -m pip install -r requirements-bridge.txt
python -m app.audio.bridge --url ws://127.0.0.1:8002/api/agent/ws
```

Chọn thiết bị bằng index hoặc một phần tên, và có thể buộc backend:

```powershell
python -m app.audio.bridge --device "Speakers" --backend sounddevice --name may-live
python -c "import sounddevice as sd; print(sd.query_devices())"
```

Bridge gửi `hello` với role `player`. Sau mỗi `reply_end`, bridge chờ player phát
thật sự xong (`drain`) rồi gửi ACK `played`; auto loop chỉ lấy comment kế sau ACK
và `AGENT_REPLY_GAP_SEC`. Timeout là độ dài PCM cộng
`AGENT_PLAYED_ACK_GRACE_SEC` (mặc định 3 giây), nên bridge lỗi không làm loop kẹt.
`GET /api/agent/status` hiển thị riêng `listeners`, `players` và latency audio đầu
của bridge.

Nếu không có tiếng, kiểm tra bridge còn báo “Đã kết nối”, volume của Windows và
output device; thử `--device` với index chính xác. Nếu `sounddevice`/PortAudio
không mở được thiết bị, cài `ffplay` vào `PATH` rồi dùng `--backend ffplay`.
Kết nối dùng `127.0.0.1`, nên thông thường không cần rule firewall; nếu đổi URL
sang IP LAN thì mới cần kiểm tra firewall/port 8002.

Khi chạy service native (không Docker), `AGENT_LOCAL_PLAYBACK=1` và
`AUDIO_PLAYER=auto` vẫn phát trực tiếp như trước, không cần audio bridge.

Sau `reply_end`, loop gọi `drain()` khi phát local, chờ ACK khi có bridge (hoặc
ước lượng phần audio client chưa phát xong nếu chỉ có WebSocket), rồi chờ thêm `AGENT_REPLY_GAP_SEC`
(mặc định 0,6 giây), rồi mới chọn comment mới/điểm cao nhất lúc đó. Budget quan
sát gồm `llm_first_token` (provider bắt đầu trả token), `first_audio` (binary đầu
sẵn sàng), `greeting_ready` (PCM greeting tổng hợp xong), `first_audio_played`
(chunk đầu được giao cho local player) và `total`
(sinh toàn bộ reply); thời gian phát audio không nằm trong `total` mà thể hiện
bằng `audio_sec`. Dùng
`python test/test_agent_ws.py --play` để vừa xem event/timing, lưu `OUT` PCM và
pipe binary frame sang ffplay.

### Phát âm thanh trên máy local

Khi `AGENT_LOCAL_PLAYBACK=1` (mặc định), auto-speak phát từng chunk ngay trên
loa/headset của **máy đang chạy service**, đồng thời vẫn broadcast PCM qua
WebSocket. Nên chạy service native trên Windows để dùng sound device; container
Compose đặt `AUDIO_PLAYER=none` vì không có thiết bị audio.

`AUDIO_PLAYER=auto` ưu tiên `sounddevice`, sau đó fallback sang `ffplay` trên
`PATH`; cũng có thể chọn thẳng `sounddevice`, `ffplay` hoặc `none`.
`AUDIO_OUTPUT_DEVICE` nhận index hoặc một phần tên thiết bị không phân biệt hoa
thường; để trống sẽ dùng output mặc định. Liệt kê thiết bị bằng:

```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

Nếu thiết bị không nhận PCM 24 kHz, backend `sounddevice` tự mở ở sample rate
mặc định của thiết bị và resample. Kiểm tra nhanh bằng
`python test/test_local_play.py --tone`, hoặc gọi service:

```bash
curl -X POST http://localhost:8002/api/agent/speak \
  -H 'Content-Type: application/json' \
  -d '{"comment":"Nói một câu để kiểm tra loa","play_local":true}'

curl -X POST http://localhost:8002/api/agent/audio/stop
```

Request `play_local=true` trả JSON ngay sau khi tổng hợp/enqueue xong, không chờ
loa phát hết. Endpoint `audio/stop` dừng phần đang phát và xóa hàng đợi (barge-in).
`GET /api/agent/status` hiển thị backend, device, trạng thái và số giây đang đợi.
Khi không chạy auto-speak, đặt rõ `AUDIO_PLAYER=auto`, `sounddevice` hoặc
`ffplay` để player được mở sẵn cho request `play_local`.

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
| `COMMENT_SKIP_BACKLOG` | `1` | Bỏ comment cũ TikTok gửi lại lúc kết nối (tạo trước lúc service kết nối) |
| `COMMENT_BACKLOG_SLACK_SEC` | `2` | Độ lệch đồng hồ cho phép khi so `create_time` với lúc kết nối |
| `COMMENT_MSG_ID_TTL_SEC` | `600` | Thời gian ghi nhớ TikTok message ID để loại replay |
| `COMMENT_USER_COOLDOWN_SEC` | `8` | Cooldown của user sau khi comment được lấy để trả lời |
| `COMMENT_MERGE_WINDOW_SEC` | `10` | Cửa sổ gộp comment chưa được trả lời của cùng user |
| `COMMENT_MERGE_MAX_PARTS` | `3` | Số phần tối đa trong một comment đã gộp |
| `COMMENT_BLOCKLIST` | rỗng | Hard ban: từ/cụm từ làm comment bị bỏ, phân cách bằng dấu phẩy |
| `COMMENT_PRIORITY_KEYWORDS` | rỗng | Từ/cụm từ được cộng điểm, phân cách bằng dấu phẩy |
| `COMMENT_BUFFER_SIZE` | `50` | Số comment hợp lệ tối đa giữ trong RAM |
| `COMMENT_MAX_AGE_SEC` | `30` | Tuổi tối đa trước khi loại khỏi buffer |
| `COMMENT_FRESH_WINDOW_SEC` | `15` | Cửa sổ comment đủ mới để `/queue/pop` chọn |
| `COMMENT_MAX_SERVE_PER_SEC` | `0` | Giới hạn lượt pop mỗi giây; `0` là không giới hạn |
| `COMMENT_RELEVANCE` | `true` | Bật similarity core qua Ollama |
| `COMMENT_EMBED_MODEL` / `COMMENT_EMBED_PORT` | `qwen3-embedding:0.6b` / `11434` | Model và port embedding trên GPU |
| `COMMENT_EMBED_URL` | rỗng | URL Ollama trực tiếp ghi đè GPU/SSH |
| `COMMENT_EMBED_KEEP_ALIVE` / `COMMENT_EMBED_TIMEOUT_SEC` | `30m` / `0.5` | Giữ model và timeout comment |
| `COMMENT_EMBED_NUM_GPU` | `0` | Layer Ollama đưa lên GPU; `0` = chạy CPU máy GPU, trống = Ollama tự chọn |
| `COMMENT_CONTEXT_FILE` | fallback `AGENT_PROMPT_FILE` | File passage ngữ cảnh UTF-8 |
| `COMMENT_SIM_FLOOR` / `COMMENT_SIM_CEIL` | `0.30` / `0.70` | Khoảng cosine ánh xạ sang core |
| `COMMENT_CORE_WEIGHT` | `10.0` | Trọng số core trong tổng điểm |
| `COMMENT_QUESTIONS_ONLY` | `1` | Chỉ nhận comment có similarity >= `AGENT_CONTEXT_SIM_THRESHOLD` (bỏ với lý do `not_question`); không có similarity thì chỉ nhận comment dạng câu hỏi |
| `COMMENT_BONUS_GIFT` / `COMMENT_BONUS_FOLLOW` | `4.0` / `2.0` | Bonus gift tối đa và follow gần đây |
| `COMMENT_BONUS_SUBSCRIBER` / `COMMENT_BONUS_GIFT_GIVER` | `1.5` / `1.0` | Bonus identity subscriber/gifter |
| `COMMENT_BONUS_FOLLOWER` / `COMMENT_BONUS_MODERATOR` | `0.5` / `0.5` | Bonus identity follower/moderator |
| `COMMENT_GIFT_HALF_LIFE_SEC` / `COMMENT_GIFT_DIAMOND_CAP` | `300` / `500` | Decay và cap diamonds |
| `COMMENT_PRIORITY_WINDOW_SEC` | `600` | Cửa sổ gift/follow và bypass cooldown; `0` = suốt process |
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
| `TTS_SPEED` | `1.2` | Tốc độ đọc gửi tới VoiceStudio (1.0 = bình thường) |
| `TTS_NUM_STEP` | rỗng | `num_step` gửi tới OmniVoice; rỗng dùng mặc định engine |
| `TTS_MAX_CONCURRENCY` | `1` | Số request TTS song song, bảo vệ GPU dùng chung |
| `TTS_TIMEOUT_SEC` | `120` | Timeout gọi VoiceStudio (giây) |
| `AUDIO_PLAYER` | `auto` | Local backend: `auto`, `sounddevice`, `ffplay` hoặc `none` |
| `AUDIO_OUTPUT_DEVICE` | rỗng | Index/phần tên output device; rỗng dùng system default |
| `BRIDGE_WS_URL` | `ws://127.0.0.1:8002/api/agent/ws` | URL service cho audio bridge native |
| `AGENT_LOCAL_PLAYBACK` | `true` | Phát auto-speak trên máy chạy service, song song WebSocket |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL API OpenAI-compatible |
| `LLM_API_KEY` | rỗng | Bearer key; rỗng thì agent tắt |
| `LLM_MODEL_NAME` | `gpt-4o-mini` | Model chat completions (fallback `LLM_MODEL`) |
| `LLM_TIMEOUT_SEC` | `30` | Timeout mỗi request LLM |
| `LLM_MAX_TOKENS` | `150` | Giới hạn token câu trả lời |
| `LLM_TEMPERATURE` | `0.7` | Temperature gửi provider |
| `LLM_MAX_RPS` | `60` | Giới hạn request LLM mỗi giây |
| `LLM_MAX_CONCURRENCY` | `2` | Số request LLM song song |
| `DNS_CACHE_TTL_SEC` | `300` | Cache DNS trong process: trả kết quả cũ ngay và làm mới ở nền; `0` = tắt |
| `AGENT_ENABLED` | `true` | Đặt `false` để buộc tắt agent |
| `AGENT_AUTO` | `false` | Tự consume comment queue và broadcast reply |
| `AGENT_AUTO_SPEAK` | `true` | Auto mode stream LLM → TTS → PCM qua WebSocket; cần TTS bật |
| `AGENT_GREETING` | `true` | Service tạo greeting ngắn và tổng hợp song song khi chờ LLM |
| `AGENT_GREETING_TEMPLATES` | `{name} ơi,\|Chào {name},\|À {name},` | Các mẫu greeting phân cách bằng `|`, tránh lặp mẫu liền nhau |
| `AGENT_SKIP_WHEN_NO_LISTENER` | `true` | Không consume comment/call LLM khi chưa có WS listener |
| `AGENT_REPLY_GAP_SEC` | `0.6` | Khoảng nghỉ sau khi client phát xong mỗi reply |
| `AGENT_PLAYED_ACK_GRACE_SEC` | `3` | Thời gian grace ngoài `audio_sec` để chờ bridge ACK `played` |
| `AGENT_SYSTEM_PROMPT` | rỗng | System prompt ghi đè trực tiếp |
| `AGENT_PROMPT_FILE` | rỗng | File system prompt, dùng khi prompt trực tiếp rỗng |
| `AGENT_TTS_GUIDE_FILE` | `context/tts_style_guide.md` | File hướng dẫn nối vào mọi system prompt; rỗng để tắt |
| `AGENT_TTS_SINGLE_SHOT` | `1` | Chờ LLM trả xong rồi đọc greeting + reply bằng một lần TTS; `0` để stream theo chunk |
| `AGENT_TTS_MAX_TAGS` | `1` | Số tag biểu cảm tối đa mỗi reply; `0` để bỏ toàn bộ |
| `AGENT_HISTORY_TURNS` | `0` | Số cặp comment/reply giữ làm ngữ cảnh; `0` = mỗi comment độc lập |
| `AGENT_NAME` | `Bomne` | Tên trợ lý trong system prompt mặc định |
| `AGENT_CONTEXT_SIM_THRESHOLD` | `0.4` | Similarity với live context từ ngưỡng này: gửi kèm `COMMENT_CONTEXT_FILE` và chỉ trả lời theo đó; dưới ngưỡng: chat bình thường |
| `AGENT_REPLY_BUFFER` | `50` | Số reply gần đây giữ trong RAM |
| `LIVESTREAM_SERVICE_PORT` | `8002` | Port expose ra ngoài |
