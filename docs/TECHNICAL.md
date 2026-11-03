# Tài liệu kỹ thuật — livestream-tiktok-service

Service đọc comment TikTok Live, lọc và chọn comment đáng trả lời, gọi LLM (chuẩn OpenAI) sinh câu trả lời, rồi đọc câu trả lời bằng VoiceStudio TTS chạy trên máy GPU từ xa. Âm thanh phát ra loa của máy Windows local.

README hướng dẫn cài đặt và chạy. Tài liệu này mô tả kiến trúc, luồng dữ liệu, thuật toán và các tham số cấu hình.

---

## 1. Tổng quan kiến trúc

```
TikTok Live ──WebSocket──▶ LiveHub (app/live)
                              │  comment dict (msg_id, create_time, user, …)
                              ▼
                        CommentFilter ──drop──▶ stats
                              │ pass
                              ▼
                        CommentBuffer (lọc → merge → chấm điểm → cooldown)
                              │ next()
                              ▼
                     auto_reply_loop (app/agent/loop.py)
                              │
             ┌────────────────┴─────────────────┐
             ▼                                  ▼
     LiveAgent.stream_reply             make_greeting ("Lan ơi,")
     (OpenAI-compatible SSE)                    │
             │ delta token                      │
             ▼                                  │
     chunker (_take_ready_chunk)                │
             │ (text, num_step)                 │
             └──────────────┬───────────────────┘
                            ▼
              VoiceStudio TTS (GPU, qua SSH tunnel) ── PCM 24 kHz s16le mono
                            │
          ┌─────────────────┴──────────────────┐
          ▼                                    ▼
  LocalAudioPlayer (chạy native)      WS /api/agent/ws ──▶ audio bridge (Windows)
  sounddevice / ffplay                 reply_start / PCM / reply_end   ack "played"
```

| Thành phần | File | Vai trò |
|---|---|---|
| App, lifespan | `app/main.py` | Khởi tạo hub, buffer, TTS, LLM, agent, player; chạy watcher và auto loop |
| Cấu hình | `app/config.py` | Đọc toàn bộ biến môi trường (mục 9) |
| Live | `app/live/hub.py`, `app/live/watcher.py` | Kết nối TikTokLive, chuẩn hoá comment, tự kết nối lại |
| Comment | `app/comments/filters.py`, `buffer.py`, `scoring.py` | Lọc, gộp, chấm điểm, chọn comment |
| Agent | `app/agent/llm.py`, `responder.py`, `names.py` | Client LLM, prompt và history, tên đọc được |
| Speech | `app/agent/speech.py`, `loop.py` | Pipeline LLM → chunk → TTS → audio, vòng lặp tự động |
| TTS | `app/tts/voicestudio.py`, `app/ssh_tunnel.py`, `app/gpu.py` | Gọi VoiceStudio trên GPU qua SSH tunnel |
| Audio | `app/audio/player.py`, `app/audio/bridge.py` | Phát local, bridge cho Docker |
| API | `app/api/*.py` | REST và WebSocket |

---

## 2. Nhận comment TikTok Live

- Thư viện: TikTokLive 7.0.1. Sign key Euler Stream đọc từ `EULER` (fallback `SIGN_API_KEY`) và gán vào `WebDefaults.tiktok_sign_api_key`.
- `_comment_to_dict` (`app/live/hub.py`) chuẩn hoá mỗi comment, gồm:
  - `msg_id` lấy từ `event.common.msg_id`, dùng để dedup.
  - `create_time` (epoch ms) lấy từ `event.common.create_time`, dùng để bỏ backlog.
  - `user.nickname` là tên hiển thị; `user.unique_id` là handle `@…`.
  - `session_started_at`: thời điểm (ms) phiên kết nối bắt đầu, gán trước `client.start()`.
- Hub cũng nghe `GiftEvent` và `FollowEvent`, chuẩn hoá cùng dạng user, loại backlog bằng
  cùng mốc `session_started_at`, rồi vừa cập nhật `AudienceTracker` vừa broadcast qua
  WebSocket. Gift đang streak bị bỏ qua cho tới event `repeat_end`; lúc đó diamonds bằng
  `diamond_count * repeat_count`.
- Watcher (`auto_watch`) chờ trên `session.task` thay vì `client.start()`, vì `start()` trả về trước khi WebSocket kết nối xong.
  - Nếu nhận được event live end → log "live kết thúc", chờ `TIKTOK_LIVE_RETRY_SEC` rồi thử lại.
  - Nếu mất kết nối mà live chưa kết thúc → kết nối lại sau 3 giây.

**Backlog khi kết nối.** Mỗi lần kết nối, TikTok gửi lại khoảng 12 comment cũ (4–12 phút trước), có khi gửi 2 lần. Comment có `create_time < session_started_at − COMMENT_BACKLOG_SLACK_SEC` bị bỏ với lý do `backlog`. Nhờ đó, khi khởi động, service chỉ trả lời comment mới.

---

## 3. Lọc comment (`CommentFilter`)

Thứ tự kiểm tra. Lý do bị bỏ được đếm trong `/api/comments/stats`.

| # | Lý do | Điều kiện |
|---|---|---|
| 1 | `empty` | Nội dung rỗng |
| 2 | `backlog` | Comment cũ từ trước lúc kết nối (mục 2) |
| 3 | `duplicate_id` | `msg_id` đã gặp trong `COMMENT_MSG_ID_TTL_SEC` (600 s) |
| 4 | `emoji_or_punctuation_only` | Chỉ có emoji hoặc dấu câu |
| 5 | `link_spam` | Chứa link |
| 6 | `too_short` / `too_long` | Ngoài khoảng `COMMENT_MIN_CHARS`..`COMMENT_MAX_CHARS` |
| 7 | `blocklist` | Chứa từ trong `COMMENT_BLOCKLIST` |
| 8 | `duplicate` | Cùng nội dung (đã chuẩn hoá) trong `COMMENT_DEDUP_WINDOW_SEC` |

Các quyết định thiết kế:
- **Không lọc mention "@"**. Người xem thường thêm @ để TikTok không ẩn comment.
- **Không lọc từ thô tục.** Comment thô tục vẫn được giữ; system prompt yêu cầu LLM trả lời lịch sự, bình tĩnh, không lặp lại từ thô tục.
- Dedup `msg_id` dùng dict theo thứ tự chèn. Phần tử hết hạn được pop từ đầu dict, nên chi phí dọn là O(số phần tử hết hạn) chứ không phải O(n).

---

## 4. Chọn comment (`CommentBuffer`)

Buffer chạy trong bộ nhớ (không dùng Redis), dung lượng `COMMENT_BUFFER_SIZE`.

### 4.1 Gộp comment liên tiếp (merge)
Nếu một người gửi thêm comment trong khi comment trước của họ **chưa được phục vụ** và còn trong `COMMENT_MERGE_WINDOW_SEC` (10 s), comment mới được nối vào record cũ. Mỗi record gộp tối đa `COMMENT_MERGE_MAX_PARTS` (3) phần. Record gộp giữ `parts` và `msg_ids`, và được chấm điểm lại trên nội dung đã gộp.

### 4.2 Chấm điểm relevance và audience

Ollama trên máy GPU (`qwen3-embedding:0.6b`, gọi API gốc `/api/embed` với
`options.num_gpu=0` nên chạy trên **CPU** của máy đó: ~31 ms/comment, 0 VRAM) embed từng dòng
trong `COMMENT_CONTEXT_FILE` (fallback `AGENT_PROMPT_FILE`) bằng một request batch khi warmup.
Không đặt lên GPU vì card 6 GB không chứa nổi cả OmniVoice và model embedding (+1,4 GB): đã đo
thấy TTS bị CUDA OOM khi hai model cùng nằm trong VRAM. Dòng tiêu đề `#` và dòng
dưới 4 từ bị bỏ: passage quá ngắn/chung chung ("Elen live") làm comment nào cũng có similarity
cao. Mỗi comment dùng một request (~55 ms qua SSH tunnel); cosine lớn nhất với passage được
đưa về core `[0,1]`:

```text
core = clamp((similarity - COMMENT_SIM_FLOOR)
             / (COMMENT_SIM_CEIL - COMMENT_SIM_FLOOR), 0, 1)
total = core * COMMENT_CORE_WEIGHT + audience_bonus
```

Mặc định floor/ceil là 0,30/0,70 và core weight là 10. Nếu embedding tắt, chưa sẵn
sàng, lỗi, timeout hoặc circuit breaker đang mở, fallback là
`min(scoring.score(comment) / 11.5, 1)`. Circuit breaker ngừng gọi Ollama 30 giây sau
ba lỗi liên tiếp; warmup chạy nền và tự thử lại khi tunnel chưa sẵn sàng. Ollama nhả model
sau `COMMENT_EMBED_KEEP_ALIVE` (30 phút) không dùng; lần nạp lại mất vài giây, vượt timeout
0,5 s của comment, nên khi gặp timeout service gửi nền một request nạp lại (timeout 30 s).

Audience bonus được tính lại ngay lúc `next()`/`latest()`: gift theo thang log và
decay half-life, follow gần đây +2, subscriber +1,5, gift-giver lịch sử +1,
follower +0,5 và moderator +0,5. Vì vậy gift đến sau comment vẫn thay đổi thứ tự.
Record merge giữ core của từng phần và dùng core cao nhất.

### 4.3 `next()` — lấy comment để trả lời
1. Bỏ các record quá `COMMENT_MAX_AGE_SEC` (30 s).
2. Chỉ xét record nhận trong `COMMENT_FRESH_WINDOW_SEC` (15 s) gần nhất. Record cũ hơn không được chọn và sẽ hết hạn ở bước 1.
3. **Cooldown theo người dùng**: người vừa được trả lời trong `COMMENT_USER_COOLDOWN_SEC` (8 s) bị **tạm bỏ qua chứ không bị xoá**. User có gift/follow session trong `COMMENT_PRIORITY_WINDOW_SEC` được bỏ qua cooldown; `0` nghĩa là suốt vòng đời process. Các cờ identity lịch sử chỉ cộng bonus, không bypass. Thời điểm served vẫn được ghi lại cho user bypass.
4. Chọn record điểm cao nhất; nếu bằng điểm thì chọn record mới hơn.

Kết hợp merge (4.1) và cooldown tính từ lúc phục vụ (4.3), comment thứ hai của cùng một người không bị bỏ oan:
- Comment gửi khi comment đầu còn chờ → được gộp vào comment đầu.
- Comment gửi sau khi comment đầu đã được trả lời → nằm chờ hết cooldown, rồi được phục vụ.

---

## 5. Agent LLM

### 5.1 Client (`OpenAICompatLLM`)
- `POST {LLM_BASE_URL}/chat/completions`, `Authorization: Bearer`, stream SSE (`data: …`, `[DONE]`). Chỉ hỗ trợ chuẩn OpenAI.
- `LLM_BASE_URL` được chuẩn hoá:
  - Bỏ đuôi `/chat/completions`.
  - Đổi `/api/chat` hoặc `/api` của Ollama thành `/v1`. Ví dụ `https://ollama.com/api/chat` → `https://ollama.com/v1`.
- Tên model đọc từ `LLM_MODEL_NAME`, fallback `LLM_MODEL`.
- Giới hạn tải:
  - `LLM_MAX_RPS` (rate guard).
  - `LLM_MAX_CONCURRENCY` (semaphore).
  - Retry 1 lần khi nhận 429, tôn trọng `Retry-After`.
- Lỗi trả về dạng `LLMError(status, body)`; API key không bao giờ xuất hiện trong thông điệp lỗi.

### 5.2 Prompt và history (`LiveAgent`)
- System prompt lấy theo thứ tự ưu tiên: `AGENT_SYSTEM_PROMPT`, rồi `AGENT_PROMPT_FILE`, rồi prompt mặc định. Prompt mặc định gồm:
  - Trả lời ngắn, tự nhiên, phù hợp để đọc thành tiếng.
  - Comment thô tục vẫn phải trả lời một cách lịch sự, hài hước nhẹ nhàng; không bao giờ trả về rỗng.
  - Khi bật greeting: thêm lệnh "KHÔNG chào", vì lời chào do service tự sinh.
- Mỗi lượt user có dạng `Người xem: <tên>\nComment: <nội dung>`. History giữ `AGENT_HISTORY_TURNS` lượt gần nhất (mặc định 0 = mỗi comment độc lập). Lời chào không được lưu vào history.
- Live context theo từng comment: agent lấy `similarity` của comment (từ buffer; comment gọi qua API thì embed ngay).
  - `similarity >= AGENT_CONTEXT_SIM_THRESHOLD` (0.5): nội dung `COMMENT_CONTEXT_FILE` được nối vào lượt user, kèm chỉ dẫn chỉ trả lời theo context, không có thì nói không biết (`context_mode=context`).
  - Dưới ngưỡng: không gửi context, chỉ dẫn trò chuyện bình thường (`context_mode=chat`).
  - Không có similarity (embedding lỗi/chưa sẵn sàng): gửi context kèm chỉ dẫn kết hợp (`context_mode=unknown`).
  - Record reply có thêm `similarity` và `context_mode`.
- Các reply gần nhất nằm trong ring buffer `AGENT_REPLY_BUFFER`, xem qua `GET /api/agent/replies`.

### 5.3 Tên đọc được (`speakable_name`)
1. Chuẩn hoá NFKC, rồi map chữ small-caps (`ᴄᴏɴᴀɴ` → `conan`), vì NFKC không đổi được loại chữ này.
2. Đổi `_` và `.` thành khoảng trắng. Giữ lại chữ, số, dấu tiếng Việt, `'` và `-`; bỏ emoji và ký hiệu.
3. Cắt còn tối đa 3 từ và 24 ký tự.
4. Ưu tiên `nickname` (tên hiển thị, ví dụ "Conan"). Nếu nickname toàn emoji thì dùng `unique_id` (`saberg_6` → "saberg 6"). Nếu cả hai rỗng thì dùng "bạn".

---

## 6. Pipeline giọng nói (`app/agent/speech.py`)

```
t=0  comment ──┬─▶ TTS(greeting, num_step=8)  ───────────────┐ (tổng hợp song song)
               └─▶ LLM stream ─▶ delta đầu tiên ─▶ release greeting PCM
                         └─▶ chunker ─▶ TTS(chunk1, 8) ─▶ TTS(chunk2.., TTS_NUM_STEP)
```

- **Greeting**: chọn ngẫu nhiên một mẫu trong `AGENT_GREETING_TEMPLATES` (mặc định `{name} ơi,|Chào {name},|À {name},`). Greeting được tổng hợp TTS song song với lúc chờ LLM, nhưng **chỉ phát sau khi có delta LLM đầu tiên**. Nếu LLM trả rỗng, PCM của greeting bị bỏ, không phát lời chào trơ trọi.
- **Chunker (`_take_ready_chunk`)**:
  - Ưu tiên cắt ở dấu câu `, ; . ! ? … \n`. Dấu `.` và `,` chỉ được tính khi theo sau là khoảng trắng, để không cắt `2.5` hay `1,5`.
  - Nếu không có dấu câu, cắt theo số từ: chunk đầu tối đa `TTS_FIRST_CHUNK_MAX_WORDS` (6) từ, các chunk sau tối đa 12 từ.
  - **Chỉ cắt ở từ đã hoàn chỉnh**, tức là từ đã có khoảng trắng phía sau. Token LLM có thể dừng giữa từ (`"s"` + `"úng"`); quy tắc này tránh việc "súng" bị đọc thành "s… úng".
  - Từ chunk thứ hai trở đi, chunk phải có ít nhất 3 từ; chunk ngắn hơn được gộp sang dấu câu kế tiếp.
  - Khi stream kết thúc, phần còn lại được chia bằng `plan_tts_chunks`.
- **num_step**: chunk đầu (gồm greeting) dùng `TTS_FIRST_CHUNK_NUM_STEP` (8) để có tiếng nhanh hơn; các chunk sau dùng `TTS_NUM_STEP`.
- **Prefetch**: queue audio có `maxsize=2`, nên GPU tổng hợp chunk kế tiếp trong lúc chunk trước đang phát. `TTS_MAX_CONCURRENCY` giới hạn số request tới GPU, vì GPU dùng chung với job render.
- **Định dạng audio**: PCM 24 kHz, s16le, mono.

### 6.1 Guide và sanitizer TTS

`AGENT_TTS_GUIDE_FILE` (mặc định `context/tts_style_guide.md`) được đọc sau khi
system prompt đã được chọn và nối vào cả prompt mặc định lẫn prompt tuỳ chỉnh.
Giá trị rỗng tắt guide; file đã cấu hình nhưng không tồn tại chỉ tạo một warning.

Mỗi reply có một sanitizer dùng chung ngân sách `AGENT_TTS_MAX_TAGS`. Sanitizer
chạy trên từng chunk ngay trước queue TTS và trên toàn reply trước khi lưu
history/API: giữ tag hợp lệ trong ngân sách, bỏ ngoặc vuông khác, stage direction
ngắn, emoji/pictograph và marker markdown, rồi chuẩn hoá dấu câu và khoảng trắng.
Chunk không còn nội dung đọc được sẽ không được gửi sang TTS.

| Tag hỗ trợ | Ý nghĩa |
|---|---|
| `[laughter]`, `[sigh]` | Cười, thở dài |
| `[confirmation-en]` | Xác nhận |
| `[question-en]`, `[question-ah]`, `[question-oh]`, `[question-ei]`, `[question-yi]` | Ngữ điệu hỏi |
| `[surprise-ah]`, `[surprise-oh]`, `[surprise-wa]`, `[surprise-yo]` | Ngạc nhiên |
| `[dissatisfaction-hnn]` | Chưa hài lòng |

Tag chỉ hợp lệ khi viết thường đúng như bảng. `[pause]` và `[pause 500ms]`
không được route VoiceStudio này hỗ trợ, nên sanitizer luôn bỏ chúng.

Độ trễ đo trên live thật (@elenanh235, LLM `gemma4:31b-cloud` qua Ollama Cloud):

| Mốc | Thời gian |
|---|---|
| LLM first token | ~0.6 s |
| Greeting sẵn sàng / tiếng đầu tiên | ~0.96 s |
| Toàn bộ reply phát xong | ~3.6 s |

---

## 7. Chế độ tự động (`AGENT_AUTO=true`)

`auto_reply_loop` (`app/agent/loop.py`) là consumer duy nhất của buffer. Khi chế độ này bật, `GET /api/queue/pop` trả **409**.

Mỗi vòng lặp:
1. `next()` lấy comment. Nếu `AGENT_SKIP_WHEN_NO_LISTENER=true` và không có player local hay WS client nào nghe, vòng lặp không lấy comment. Comment đã lấy ra khỏi buffer thì luôn được xử lý, kể cả khi listener vừa rời.
2. Chạy pipeline giọng nói (mục 6) và phát:
   - Trực tiếp ra player local, nếu `AGENT_LOCAL_PLAYBACK=true` và có player.
   - Broadcast tới mọi WS client.
3. **Pacing** — chờ reply phát xong rồi mới sang comment tiếp theo:
   - Có player local: chờ `drain()`.
   - Có bridge: chờ ack `played`, tối đa thời lượng audio + `AGENT_PLAYED_ACK_GRACE_SEC`.
   - Không có cả hai: ước lượng theo thời lượng audio.
   - Sau đó nghỉ thêm `AGENT_REPLY_GAP_SEC` (0.6 s).

### Giao thức WebSocket `/api/agent/ws`

Server gửi xuống client:
```json
{"type": "reply_start", "id": "<uuid>", "comment": {...}, "format": "pcm", "sample_rate": 24000, "channels": 1, "sample_format": "s16le"}
<binary PCM frame> × N
{"type": "reply_end", "id": "<uuid>", "reply": "Lan ơi, ...", "audio_sec": 2.6, "latency_ms": {"llm_first_token": 593, "greeting_ready": 964, "first_audio": 964, "first_audio_played": 970, "total": 3582}}
{"type": "reply_error", "id": "<uuid>", "error": "..."}
```

Client gửi lên server:
```json
{"type": "hello", "role": "player", "name": "PC-01", "backend": "sounddevice", "device": "default"}
{"type": "played", "id": "<uuid>", "played_at": 1730000000.0}
```

Nếu queue gửi của một client bị đầy (256 frame), server đóng client đó với mã 1013.

---

## 8. Phát âm thanh

### 8.1 Chạy native trên Windows
`create_player` (`app/audio/player.py`) chọn backend theo `AUDIO_PLAYER`:
- `sounddevice`: dùng `RawOutputStream` với một writer thread.
- `ffplay`: chạy một tiến trình ffplay thường trực, nhận PCM qua stdin.
- `auto`: thử lần lượt sounddevice rồi ffplay.
- `none`: tắt phát local.

`AUDIO_OUTPUT_DEVICE` chọn thiết bị ra. Để trống thì dùng thiết bị mặc định của Windows ("Microsoft Sound Mapper").

### 8.2 Chạy trong Docker: audio bridge
Container không truy cập được loa Windows. Vì vậy container đặt `AUDIO_PLAYER=none` và `AGENT_LOCAL_PLAYBACK=0`; âm thanh do bridge chạy native trên Windows phát:

```
scripts\audio-bridge.bat     # tự tạo .venv-bridge, cài requirements-bridge.txt
python -m app.audio.bridge --url ws://127.0.0.1:8002/api/agent/ws [--device ...] [--backend auto|sounddevice|ffplay] [--name PC-01]
```

Bridge làm các việc sau:
- Gửi `hello` (role `player`).
- Phát PCM của từng reply.
- Gửi ack `played` sau khi `drain()` xong.
- Xử lý lệnh `flush` (`POST /api/agent/audio/stop`).
- Khi mất kết nối, tự kết nối lại với backoff từ 1 s đến 10 s.

---

## 9. Cấu hình (biến môi trường)

Giá trị mặc định lấy từ `app/config.py`. Mẫu đầy đủ ở `.env.example`.

### TikTok
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TIKTOK_USERNAME` | — | Username tự kết nối khi khởi động |
| `TIKTOK_LIVE_RETRY_SEC` | 30 | Thời gian chờ trước khi thử lại khi chưa live hoặc live đã kết thúc |
| `EULER` / `SIGN_API_KEY` | — | Sign key Euler Stream |

### Comment
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `COMMENT_SKIP_BACKLOG` | true | Bỏ comment cũ gửi lại lúc kết nối |
| `COMMENT_BACKLOG_SLACK_SEC` | 2 | Độ lệch đồng hồ cho phép khi xét backlog |
| `COMMENT_MSG_ID_TTL_SEC` | 600 | Thời gian nhớ `msg_id` để dedup |
| `COMMENT_DEDUP_WINDOW_SEC` | 60 | Cửa sổ dedup theo nội dung |
| `COMMENT_MIN_CHARS` / `COMMENT_MAX_CHARS` | 2 / 300 | Giới hạn độ dài comment |
| `COMMENT_BLOCKLIST` | — | Danh sách từ chặn, phân cách bằng dấu phẩy |
| `COMMENT_PRIORITY_KEYWORDS` | — | Từ khoá được cộng điểm |
| `COMMENT_BUFFER_SIZE` | 50 | Số record tối đa trong buffer |
| `COMMENT_MAX_AGE_SEC` | 30 | Comment cũ hơn mốc này bị bỏ |
| `COMMENT_FRESH_WINDOW_SEC` | 15 | Cửa sổ ưu tiên comment mới |
| `COMMENT_USER_COOLDOWN_SEC` | 8 | Cooldown mỗi người, tính từ lúc được trả lời |
| `COMMENT_MERGE_WINDOW_SEC` / `COMMENT_MERGE_MAX_PARTS` | 10 / 3 | Cửa sổ gộp và số phần gộp tối đa |
| `COMMENT_MAX_SERVE_PER_SEC` | 0 | Giới hạn tốc độ phục vụ (0 = không giới hạn) |
| `COMMENT_RELEVANCE` | true | Bật similarity core qua Ollama; tắt thì luôn dùng heuristic |
| `COMMENT_EMBED_MODEL` / `COMMENT_EMBED_PORT` | `qwen3-embedding:0.6b` / 11434 | Model và port Ollama trên GPU |
| `COMMENT_EMBED_URL` | — | URL trực tiếp ghi đè GPU/SSH tunnel |
| `COMMENT_EMBED_KEEP_ALIVE` / `COMMENT_EMBED_TIMEOUT_SEC` | `30m` / 0,5 | Thời gian giữ model và timeout mỗi comment |
| `COMMENT_EMBED_NUM_GPU` | 0 | Số layer Ollama đưa lên GPU; 0 = CPU, trống = Ollama tự chọn |
| `COMMENT_CONTEXT_FILE` | fallback `AGENT_PROMPT_FILE` | File passage ngữ cảnh UTF-8 |
| `COMMENT_SIM_FLOOR` / `COMMENT_SIM_CEIL` | 0,30 / 0,70 | Khoảng cosine ánh xạ sang core `[0,1]` |
| `COMMENT_CORE_WEIGHT` | 10 | Trọng số relevance core trong total |
| `COMMENT_QUESTIONS_ONLY` | 1 | Chỉ nhận comment có similarity >= `AGENT_CONTEXT_SIM_THRESHOLD` (bỏ với lý do `not_question`); không có similarity thì chỉ nhận comment dạng câu hỏi |
| `COMMENT_BONUS_GIFT` / `COMMENT_BONUS_FOLLOW` | 4 / 2 | Bonus gift tối đa và follow gần đây |
| `COMMENT_BONUS_SUBSCRIBER` / `COMMENT_BONUS_GIFT_GIVER` | 1,5 / 1 | Bonus identity subscriber/gifter lịch sử |
| `COMMENT_BONUS_FOLLOWER` / `COMMENT_BONUS_MODERATOR` | 0,5 / 0,5 | Bonus identity follower/moderator |
| `COMMENT_GIFT_HALF_LIFE_SEC` / `COMMENT_GIFT_DIAMOND_CAP` | 300 / 500 | Decay và mức cap log của diamonds |
| `COMMENT_PRIORITY_WINDOW_SEC` | 600 | Cửa sổ follow/gift bonus và bypass cooldown; 0 = suốt process |

### GPU và TTS
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `GPU_HOST` | local | IP hoặc host của máy GPU; nếu khác `local` thì mở SSH tunnel |
| `GPU_SSH_USER` / `GPU_SSH_PORT` / `GPU_SSH_KEY` / `GPU_SSH_PASSWORD` | — / 22 | Thông tin SSH tới máy GPU |
| `VOICESTUDIO_PORT` / `VOICESTUDIO_API_PORT` | 8080 / 3900 | Port của VoiceStudio trên máy GPU |
| `TTS_ENABLED` / `TTS_ENGINE` | true / voicestudio | Bật TTS và chọn engine |
| `TTS_VOICE_ID` | — | Giọng đọc; để trống thì dùng giọng mặc định của VoiceStudio |
| `TTS_SPEED` | 1.2 | Tốc độ đọc (1.0 = bình thường) |
| `TTS_NUM_STEP` | — | num_step cho các chunk sau chunk đầu |
| `TTS_FIRST_CHUNK_NUM_STEP` | 8 | num_step cho chunk đầu |
| `TTS_FIRST_CHUNK_MAX_WORDS` | 6 | Số từ tối đa của chunk đầu |
| `TTS_MAX_CONCURRENCY` | 1 | Số request TTS song song tới GPU |
| `TTS_TIMEOUT_SEC` | 120 | Timeout mỗi request TTS |

### LLM và Agent
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `LLM_BASE_URL` | https://api.openai.com/v1 | Base URL chuẩn OpenAI (tự chuẩn hoá) |
| `LLM_API_KEY` | — | API key |
| `LLM_MODEL_NAME` (`LLM_MODEL`) | gpt-4o-mini | Tên model |
| `LLM_TIMEOUT_SEC` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` | 30 / 150 / 0.7 | Tham số gọi LLM |
| `DNS_CACHE_TTL_SEC` | 300 | Cache DNS trong process: trả kết quả cũ ngay và làm mới ở nền; `0` = tắt |
| `LLM_MAX_RPS` / `LLM_MAX_CONCURRENCY` | 60 / 2 | Giới hạn tốc độ và số request song song |
| `AGENT_ENABLED` | true | Bật lớp agent |
| `AGENT_AUTO` | false | Bật vòng lặp tự động |
| `AGENT_AUTO_SPEAK` | true | Trong chế độ tự động, đọc reply thành tiếng |
| `AGENT_GREETING` / `AGENT_GREETING_TEMPLATES` | true / `{name} ơi,\|Chào {name},\|À {name},` | Lời chào đầu reply |
| `AGENT_SKIP_WHEN_NO_LISTENER` | true | Không lấy comment khi không ai nghe |
| `AGENT_REPLY_GAP_SEC` | 0.6 | Khoảng nghỉ giữa hai reply |
| `AGENT_PLAYED_ACK_GRACE_SEC` | 3 | Thời gian chờ thêm cho ack `played` |
| `AGENT_SYSTEM_PROMPT` / `AGENT_PROMPT_FILE` | — | System prompt tuỳ chỉnh |
| `AGENT_TTS_GUIDE_FILE` | `context/tts_style_guide.md` | Guide nối vào mọi system prompt; rỗng để tắt |
| `AGENT_TTS_SINGLE_SHOT` | 1 | 1 = một lần TTS cho cả greeting + reply (num_step = `TTS_NUM_STEP`); 0 = stream theo chunk như mục trên |
| `AGENT_TTS_MAX_TAGS` | 1 | Số tag biểu cảm tối đa mỗi reply; 0 = bỏ toàn bộ |
| `AGENT_HISTORY_TURNS` / `AGENT_REPLY_BUFFER` | 6 / 50 | Số lượt history và số reply lưu lại |

### Audio
| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AUDIO_PLAYER` | auto | `auto`, `sounddevice`, `ffplay` hoặc `none` |
| `AUDIO_OUTPUT_DEVICE` | — | Thiết bị ra; để trống thì dùng mặc định |
| `AGENT_LOCAL_PLAYBACK` | true | Phát trực tiếp trong process service |

---

## 10. API

| Method | Path | Mô tả |
|---|---|---|
| GET | `/health` | Trạng thái service, GPU, TTS, agent |
| POST / DELETE | `/api/live/watch`, `/api/live/watch/{unique_id}` | Bắt đầu / dừng theo dõi một live |
| GET | `/api/live/sessions` | Các phiên đang theo dõi |
| WS | `/ws/live/{unique_id}` | Stream comment thô |
| GET | `/api/comments/latest`, `/api/comments/stats` | Comment gần nhất; thống kê lọc, gộp, cooldown |
| GET | `/api/queue/pop`, `/api/queue/length` | Lấy comment thủ công (409 khi `AGENT_AUTO`) |
| GET | `/api/tts/status`, `/api/tts/voices` | Trạng thái TTS, danh sách giọng |
| POST | `/api/tts/synthesize`, `/api/tts/stream` | Tổng hợp WAV, hoặc stream PCM theo chunk |
| GET | `/api/agent/status` | Cấu hình agent, số listener, thống kê |
| POST | `/api/agent/reply` | Sinh reply dạng text |
| GET | `/api/agent/replies` | Các reply gần nhất |
| POST | `/api/agent/speak` | Sinh reply và stream PCM; có tuỳ chọn `play_local` |
| POST | `/api/agent/audio/stop` | Dừng phát và gửi `flush` tới bridge |
| WS | `/api/agent/ws` | Giao thức ở mục 7 |

---

## 11. Kiểm thử

```
python -m unittest discover -s test -p "test_*unit*.py"
python -m unittest test.test_comments
```

| File | Phạm vi |
|---|---|
| `test_comments.py` | Filter (backlog, msg_id, giữ mention), buffer (merge, cooldown) |
| `test_agent_unit.py` | Client LLM (payload, SSE, 429), LiveAgent, API, auto loop |
| `test_agent_chunking_unit.py` | Chunker: không cắt giữa từ, số thập phân, chunk tối thiểu 3 từ |
| `test_agent_greeting_unit.py` | `speakable_name`, greeting gated, LLM rỗng |
| `test_agent_auto_unit.py`, `test_agent_local_unit.py`, `test_agent_bridge_unit.py` | Pacing, phát local, ack bridge |
| `test_audio_player_unit.py`, `test_audio_bridge_unit.py` | Player backend, bridge |

Script thủ công:
- `test/test_agent.py`, `test/test_agent_ws.py --url … [--play] [--out file.pcm]`: kiểm tra agent trên service đang chạy.
- `test/test_local_play.py [--tone]`: kiểm tra loa local.

---

## 12. Hạn chế đã biết

- **LLM tự bịa thông tin** về streamer (game đang chơi, bạn duo…) và xưng hô chưa nhất quán. Cách khắc phục: viết file prompt mô tả streamer, rồi trỏ `AGENT_PROMPT_FILE` tới file đó.
- **Greeting được phát sau delta LLM đầu tiên.** Đánh đổi này tránh phát lời chào khi LLM lỗi, nhưng làm tiếng đầu tiên trễ thêm bằng thời gian LLM ra first token, nếu thời gian đó lớn hơn thời gian TTS greeting.
- **Chữ hoa sau dấu phẩy của greeting** ("Lan ơi, Anh…"): chỉ khác ở text, không ảnh hưởng giọng đọc.
- **Chưa kiểm thử với live đông người** (nhiều comment mỗi giây) để đánh giá scoring, merge và cooldown dưới tải.

> **DNS chậm trên Windows + Tailscale:** adapter Tailscale khai báo DNS IPv6 `fec0:0:0:ffff::1..3` không phản hồi, nên mỗi lần cache DNS của OS hết hạn `getaddrinfo("ollama.com")` mất ~15 s (truy vấn AAAA chờ timeout) và LLM first token bị cộng thêm 15 s. `app/dns_cache.py` thay `socket.getaddrinfo` bằng bản có cache (stale-while-revalidate, `DNS_CACHE_TTL_SEC`) và tra trước host LLM lúc khởi động.
