# Topic Radar v2 – Hướng dẫn cài đặt

Bot chạy **Thứ 2 và Thứ 5 lúc khoảng 7:17 sáng** (giờ VN) trên GitHub Actions, miễn phí, và gửi top 5 chủ đề vào chat riêng với bot Telegram.

## Bot làm gì mỗi lần chạy
1. **Đọc tin:** Google Trends, Google News, tin khoa học, Hacker News.
2. **Tạo từ khóa:** 10 từ khóa gốc (trong cấu hình) cộng khoảng 10 từ khóa Gemini rút ra từ tin tuần này.
3. **Nhánh cơ bản:**
   - **YouTube:** tìm video 14 ngày gần nhất theo từ khóa, lọc video nào có lượt xem ≥ số subscriber của kênh (outlier).
   - **Tự học:** kênh nào lọt outlier ≥ 2 lần thì bot tự theo dõi (tối đa 40 kênh).
   - **Gợi ý tìm kiếm:** những gì người ta gõ trên YouTube/Google.
4. **Nhánh vidIQ, lần gọi 1:** hỏi vidIQ chủ đề và xu hướng đang lên.
5. **Gemini:** gộp hai nhánh và chọn top 5.
6. **vidIQ kiểm tra lần cuối, lần gọi 2:** chấm lượt tìm kiếm và độ cạnh tranh, có thể đổi thứ tự top 5.
7. **Gửi Telegram.** Cuối tin có dòng 📊 tình trạng từng nguồn và số credit vidIQ còn lại.

## Các file

| File | Lên GitHub? | Công dụng |
|---|---|---|
| `radar.py` | Có | Code chính |
| `vidiq.py` | Có | Kết nối vidIQ |
| `vidiq_login.py` | Có | Đăng nhập vidIQ (chạy 1 lần trên laptop) |
| `requirements.txt` | Có | Danh sách thư viện |
| `.gitignore` | Có | Chặn các file riêng tư bị đẩy lên |
| `.github/workflows/radar.yml` | Có | Lịch chạy |
| `RADAR_CONFIG.yaml` | **KHÔNG** | Cấu hình riêng. Nội dung dán vào Secret |
| `VIDIQ_AUTH.txt`, `vidiq_tools.json` | **KHÔNG** | Do `vidiq_login.py` tạo ra |
| `state.enc` | Bot tự tạo | Lịch sử chủ đề, các kênh tự học, token vidIQ (đã mã hóa) |

## Bước 1. Repo
Đã xong: Claude đã đẩy code lên repo `CyrusHayden/radar`. Không upload `RADAR_CONFIG.yaml` lên repo.

## Bước 2. Lấy YouTube API key (miễn phí)
1. Vào https://console.cloud.google.com, chọn project đang chứa Gemini key.
2. Vào **APIs & Services → Library**, tìm **YouTube Data API v3** và bấm **Enable**.
3. Vào **APIs & Services → Credentials → Create credentials → API key**, rồi copy key.

## Bước 3. Đăng nhập vidIQ (1 lần, trên laptop)
1. Tạo tài khoản vidIQ Free nếu chưa có.
2. Mở cửa sổ lệnh (cmd) **trong thư mục `topic-radar`** và chạy:
   ```
   pip install requests
   python vidiq_login.py
   ```
3. Trình duyệt mở ra. Đăng nhập vidIQ và bấm cho phép.
4. Script tạo file `VIDIQ_AUTH.txt` để dán vào Secret ở Bước 5.

Lưu ý: việc bạn đã kết nối vidIQ vào Claude **không** dùng chung được cho bot. Bot chạy trên GitHub nên cần một lượt đăng nhập riêng.

Nếu script báo lỗi, chụp màn hình gửi Claude. Bot vẫn chạy được không cần vidIQ, chỉ thiếu nhánh vidIQ.

## Bước 4. Lấy TELEGRAM_CHAT_ID (chat riêng với bot cũ)
1. Nhắn cho bot cũ một tin bất kỳ.
2. Mở trình duyệt, vào `https://api.telegram.org/bot<TOKEN>/getUpdates` (thay `<TOKEN>` bằng token bot).
3. Tìm `"chat":{"id":123456789` và copy con số đó.

## Bước 5. Thêm Secrets
Trong repo, vào **Settings → Secrets and variables → Actions → New repository secret**:

| Tên | Giá trị |
|---|---|
| `GEMINI_API_KEY` | Gemini key bạn đang dùng |
| `YOUTUBE_API_KEY` | Key ở Bước 2 |
| `TELEGRAM_BOT_TOKEN` | Token bot cũ |
| `TELEGRAM_CHAT_ID` | Số ở Bước 4 |
| `STATE_KEY` | Tự đặt một mật khẩu dài, **không đổi về sau** (đổi thì bot mất lịch sử và các kênh tự học) |
| `RADAR_CONFIG` | Toàn bộ nội dung `RADAR_CONFIG.yaml` |
| `VIDIQ_AUTH` | Toàn bộ nội dung `VIDIQ_AUTH.txt` |

## Bước 6. Chạy thử
Vào **Actions → topic-radar → Run workflow**. Khoảng 2–3 phút sau, tin sẽ tới Telegram.

## Khi nào cần làm lại
- **Cuối tin ghi "vidIQ: đăng nhập hết hạn":** chạy lại `python vidiq_login.py` rồi dán `VIDIQ_AUTH.txt` mới vào Secret.
- **Muốn đổi từ khóa gốc hay mức lọc:** sửa `RADAR_CONFIG.yaml`, rồi vào Secret `RADAR_CONFIG` và bấm **Update** để dán lại.
- **Credit vidIQ:** gói Free khoảng 150 credit/tháng, bot dùng khoảng 26 lần gọi/tháng. Hết credit thì bot tự bỏ qua vidIQ, **không bao giờ bị trừ tiền** (vidIQ không bán thêm credit).

## Bảo mật (repo public)
- Từ khóa, cấu hình, kênh tự học và token vidIQ không có trong code. Chúng nằm trong Secret và trong `state.enc` đã mã hóa.
- Log GitHub Actions ai cũng xem được, nên bot chỉ in **số lượng**.
