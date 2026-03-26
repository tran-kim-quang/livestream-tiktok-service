# TikTok Live Comment Service

Service thu thập comment realtime từ TikTok Live, đẩy vào Redis queue để hệ thống AI agent xử lý.

## Kiến trúc

```
TikTok Live ──comment──▶ Service ──LPUSH──▶ Redis Queue
                                                  │
                                           BRPOP (FIFO)
                                                  │
                                           AI Agent
                                    GET /api/queue/pop
```

## Yêu cầu

- Docker & Docker Compose
- Port **8002** và **6380** chưa bị chiếm

---

## Cài đặt

### 1. Clone repo

```bash
git clone https://github.com/tran-kim-quang/livestream-tiktok-service.git
cd livestream-tiktok-service
```

### 2. Tạo file `.env`

```bash
cp .env.example .env
```

Mở `.env` và điền username TikTok cần theo dõi:

```env
TIKTOK_USERNAME=ten_kenh_tiktok   # ← đổi thành username thật, không có @
TIKTOK_LIVE_RETRY_SEC=30

REDIS_URL=redis://redis:6379/0
REDIS_QUEUE_KEY=livestream:comments
REDIS_ROOM_MAX_COMMENTS=2000

LIVESTREAM_SERVICE_PORT=8002
```

### 3. Khởi động

```bash
docker compose up -d
```

Kiểm tra service đã sẵn sàng:

```bash
curl http://localhost:8002/health
```

Kết quả mong đợi:

```json
{
  "status": "ok",
  "redis": "ok",
  "queue_length": 0,
  "sessions": [...],
  "auto_watch": "ten_kenh_tiktok"
}
```

---

## Đổi username không cần rebuild

```bash
# Sửa .env
nano .env   # đổi TIKTOK_USERNAME

# Restart service
docker compose restart livestream-service
```

---

## API cho AI Agent

### Lấy comment tiếp theo (FIFO, xoá khỏi queue)

```
GET /api/queue/pop?timeout=5
```

| Param | Mô tả |
|---|---|
| `timeout` | Giây chờ nếu queue rỗng (0 = non-blocking, khuyên dùng 5) |

**Response có comment:**

```json
{
  "empty": false,
  "comment": {
    "comment": "nội dung comment",
    "user": {
      "nickname": "Tên hiển thị",
      "unique_id": "username_tiktok",
      "id": 123456789
    },
    "streamer_unique_id": "ten_kenh_tiktok",
    "room_id": 7621306973392210706,
    "enqueued_at": "2026-03-26T01:00:00+00:00"
  }
}
```

**Response queue rỗng:**

```json
{ "empty": true, "comment": null }
```

### Xem số comment đang chờ

```
GET /api/queue/length
```

### Xem lịch sử comment theo phiên live (không xoá)

```
GET /api/live/{room_id}/comments?limit=50
```

`room_id` lấy từ `GET /api/live/sessions` hoặc field `room_id` trong response của `/api/queue/pop`.

### Quản lý session

```
GET    /api/live/sessions              # Liệt kê session đang chạy
POST   /api/live/watch                 # Theo dõi thêm username khác
DELETE /api/live/watch/{unique_id}     # Dừng theo dõi
```

**POST /api/live/watch body:**

```json
{ "unique_id": "ten_kenh_khac" }
```

---

## Tích hợp vào Agent (Python)

```python
import httpx

BASE = "http://localhost:8002"
POP_TIMEOUT = 5

_timeout = httpx.Timeout(connect=5.0, read=POP_TIMEOUT + 8, write=5.0, pool=5.0)
_client  = httpx.Client(timeout=_timeout)

def get_next_comment() -> dict | None:
    """Lấy comment tiếp theo từ queue (blocking). Trả None nếu rỗng."""
    try:
        r = _client.get(f"{BASE}/api/queue/pop", params={"timeout": POP_TIMEOUT})
        r.raise_for_status()
        data = r.json()
        return None if data["empty"] else data["comment"]
    except httpx.ReadTimeout:
        return None

# Vòng lặp agent
while True:
    item = get_next_comment()
    if item is None:
        continue  # queue rỗng, chờ comment mới (không tốn CPU)

    username = item["user"]["nickname"]   # hoặc ["unique_id"]
    query    = item["comment"]

    # → Gửi vào LLM/agent của bạn
    reply = my_agent.respond(query=query, sender=username)
    print(f"[{username}]: {query}")
    print(f"[agent]: {reply}")
```

---

## Tích hợp vào docker-compose của project agent

Nếu livestream service cần kết nối với Redis của project agent (đang map port 6380):

```yaml
services:
  livestream-service:
    image: livestream-service:latest   # build trước: docker build -t livestream-service .
    ports:
      - "8002:8002"
    environment:
      TIKTOK_USERNAME: ten_kenh_tiktok
      REDIS_URL: redis://redis:6379/0   # tên service Redis trong cùng compose network
    networks:
      - your-agent-network             # cùng network với Redis của agent
```

Hoặc nếu chạy độc lập và trỏ tới Redis host:

```env
REDIS_URL=redis://host.docker.internal:6380/0
```

---

## Script test nhanh

Chạy consumer mô phỏng agent (hiển thị comment + username từ queue):

```bash
cd livestream-tiktok-service
source .venv/bin/activate   # hoặc dùng .venv/bin/python trực tiếp
python test/read_comments_terminal.py
```

---

## Tắt service

```bash
docker compose down        # dừng container, giữ data Redis
docker compose down -v     # dừng + xoá data Redis
```

---

## Biến môi trường

| Biến | Mặc định | Mô tả |
|---|---|---|
| `TIKTOK_USERNAME` | *(bắt buộc)* | Username TikTok cần theo dõi (không có `@`) |
| `TIKTOK_LIVE_RETRY_SEC` | `30` | Giây chờ giữa mỗi lần thử kết nối lại |
| `REDIS_URL` | `redis://redis:6379/0` | URL kết nối Redis |
| `REDIS_QUEUE_KEY` | `livestream:comments` | Tên key hàng đợi trong Redis |
| `REDIS_ROOM_MAX_COMMENTS` | `2000` | Số comment tối đa lưu theo room (lịch sử) |
| `LIVESTREAM_SERVICE_PORT` | `8002` | Port expose ra ngoài |
| `REDIS_CONNECT_RETRIES` | `8` | Số lần thử kết nối Redis lúc startup |
