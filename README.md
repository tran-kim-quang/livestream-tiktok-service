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

### Khởi động nhanh bằng make

Trên Windows 11, cài GNU Make bằng `winget install ezwinports.make` hoặc
`choco install make`, sau đó chạy một lệnh:

```powershell
make start TIKTOK=ten_kenh
```

Lệnh sẽ chuẩn bị `.env`, chạy service, chờ health và mở audio bridge.
Image chỉ được build tự động ở lần đầu (khi chưa có); sau khi sửa code, chạy
`make build` hoặc `make start BUILD=1` để build lại.
Nhấn Ctrl+C chỉ dừng bridge; service vẫn chạy nền. Các target thường dùng:
`make status`, `make logs`, `make watch TIKTOK=kenh_khac`, `make say`,
`make stop-audio`, `make restart` và `make stop`. Chạy `make help` để xem đầy đủ.

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

## Chat-chit, rate limit và cache

Comment hợp lệ được chia thành ba lane: `question` cho câu liên quan ngữ cảnh live,
`greeting` cho lời chào thuần và `chat` cho phần còn lại. Scheduler ưu tiên lần lượt:
cảm ơn gift/follow, câu hỏi, lời chào đã gom batch, rồi chat. `COMMENT_QUESTIONS_ONLY=0`
là mặc định; đặt `1` để giữ hành vi cũ, chỉ nhận câu hỏi. Đặt `AGENT_CHAT=0` để scheduler
chỉ nói câu hỏi và lời cảm ơn; chat/greeting trong buffer sẽ tự hết hạn.

Khi câu hỏi đang chờ, tổng airtime của chat + greeting không vượt
`AGENT_CHAT_AIRTIME_RATIO=0.2` trong cửa sổ 60 giây. Ví dụ đã nói 16 giây câu hỏi thì
một chat ước lượng 4 giây được phép vì `4 / (16 + 4) = 20%`; nếu chưa có airtime thì
chat phải nhường câu hỏi. Khi không có câu hỏi, chat được phục vụ không cần kiểm tra tỷ lệ.
Chat gọi LLM còn chịu token bucket 6 lượt/phút, burst 2. Mỗi user có cooldown 60 giây và
tối đa 3 chat/greeting trong 600 giây; gift/follow mới được phép bỏ qua các giới hạn này.

Lời chào thuần và cảm ơn gift/follow dùng template cố định, gom tối đa 4 tên hoặc chờ 6 giây,
rồi tổng hợp cả câu bằng đúng một request TTS để giọng liền mạch. Gift được cảm ơn trước follow;
hai loại không trộn chung. Cache TTS chỉ nằm trong RAM, dùng LRU theo tổng byte và single-flight
cho các request giống nhau; restart service sẽ xoá cache.

Các biến mới:

- Lane: `COMMENT_CHAT_MAX_AGE_SEC=12`, `COMMENT_CHAT_USER_COOLDOWN_SEC=60`,
  `COMMENT_CHAT_MAX_PER_USER=3`, `COMMENT_CHAT_USER_WINDOW_SEC=600`.
- Airtime/chat: `AGENT_CHAT=1`, `AGENT_CHAT_AIRTIME_WINDOW_SEC=60`,
  `AGENT_CHAT_AIRTIME_RATIO=0.2`, `AGENT_CHAT_RATE_PER_MIN=6`, `AGENT_CHAT_BURST=2`,
  `AGENT_CHAT_STEER_RATIO=0.33`.
- Greeting: `AGENT_GREETING_BATCH_SEC=6`, `AGENT_GREETING_BATCH_MAX=4`,
  `AGENT_GREETING_MAX_WORDS=6`, `AGENT_GREETING_SIM_THRESHOLD=0.75`.
- Cảm ơn: `AGENT_THANKS=1`, `AGENT_THANKS_BATCH_SEC=6`, `AGENT_THANKS_BATCH_MAX=4`,
  `AGENT_THANKS_MIN_GAP_SEC=20`, `AGENT_THANKS_MIN_DIAMONDS=1`.
- Cache: `TTS_CACHE=1`, `TTS_CACHE_MAX_MB=200`.

## Cấu hình GPU

`GpuNode` là địa chỉ dùng chung cho các tác vụ GPU. Mặc định `GPU_HOST=local` nghĩa là card ở máy chạy service; ngoài Docker nó trỏ tới `127.0.0.1`. Docker Compose đổi địa chỉ local thành `host.docker.internal` để gọi service trên host.

Muốn dùng máy GPU từ xa, đặt IP LAN/Tailscale hoặc hostname. Ví dụ máy Tailscale đã cấu hình VoiceStudio:

```env
GPU_HOST=
GPU_NAME=
GPU_SSH_USER=
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
