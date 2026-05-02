# TikTok DM AI Bot

Bot TikTok Messages tự động — Python + Playwright + FastAPI + OpenAI.

## Cài đặt nhanh

```bash
cd /opt/tiktok-bot

# 1. Copy .env
cp .env.example .env
nano .env   # điền OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, ...

# 2. Copy storage_state (từ máy đã login TikTok)
cp /path/to/tiktok_storage_state.json /opt/tiktok-bot/

# 3. Install
chmod +x manage.sh
./manage.sh install

# 4. Start
./manage.sh start
```

## Lệnh quản lý

```bash
./manage.sh start          # Khởi động
./manage.sh stop           # Dừng
./manage.sh restart        # Khởi động lại
./manage.sh status         # Trạng thái
./manage.sh logs           # Xem log gần đây
./manage.sh logs-bot       # Theo dõi log bot realtime
./manage.sh logs-backend   # Theo dõi log backend realtime
./manage.sh test-backend   # Test backend (BTC price)
./manage.sh test-storage   # Kiểm tra storage_state
./manage.sh compile-check  # Kiểm tra syntax Python
```

## Cấu trúc thư mục

```
/opt/tiktok-bot/
├── .env                          # Biến môi trường (không commit)
├── tiktok_storage_state.json     # TikTok session (không commit)
├── requirements.txt
├── manage.sh
├── backend/
│   └── server.py                 # FastAPI backend + OpenAI
├── bot/
│   ├── tiktok_bot.py             # Playwright bot main
│   ├── tools.py                  # BTC price, screenshot
│   ├── memory.py                 # Per-user memory
│   └── reminders.py              # Reminder system
├── data/
│   ├── memory.json               # User memories
│   └── reminders.json            # Reminders
├── screenshots/                  # /shot output
├── logs/
└── systemd/
    ├── tiktok-backend.service
    └── tiktok-bot.service
```

## Commands trong chat TikTok

| Command | Mô tả |
|---|---|
| `/memory` | Xem memory của bạn |
| `/forget` | Xoá memory |
| `/reminders` | Xem danh sách reminder |
| `/shot <url>` | Chụp screenshot URL |
| `nhắc tao 7h tối...` | Đặt reminder |
| `giá bitcoin hôm nay` | Giá BTC realtime |

## Lấy storage_state từ máy đã login

```python
# Chạy script này trên máy có Chrome đã login TikTok
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://www.tiktok.com/messages")
    input("Login xong nhấn Enter...")
    ctx.storage_state(path="tiktok_storage_state.json")
    browser.close()
```

## Nếu bot không login được

1. `./manage.sh test-storage` — kiểm tra file
2. Xem screenshot debug tại `/opt/tiktok-bot/screenshots/debug_login_fail_*.png`
3. Cần tạo lại storage_state mới và copy vào `/opt/tiktok-bot/`
