# ai-assistent

Trợ lý học bằng giọng nói: lớp Voice I/O (STT + TTS) chạy local, bọc quanh Claude.
Hỗ trợ tiếng Việt lẫn thuật ngữ tiếng Anh (AWS, dev) và luyện IELTS/TOEIC.

- Kế hoạch tóm tắt: [PLAN.md](PLAN.md)
- Kế hoạch chi tiết: mở [docs/plan.html](docs/plan.html) bằng trình duyệt

## Cài đặt

Yêu cầu: Windows, Python 3.11, GPU NVIDIA, ffmpeg.

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip uninstall -y onnxruntime
.venv\Scripts\python -m pip install --force-reinstall --no-deps onnxruntime-gpu==1.23.2
```

faster-whisper phụ thuộc `onnxruntime` (bản CPU), gói này ghi đè `onnxruntime-gpu` mà TTS cần. Hai lệnh cuối
gỡ bản CPU và cài lại bản GPU (VAD của faster-whisper vẫn chạy được trên bản GPU). Chạy lại hai lệnh này sau
mỗi lần `pip install -r requirements.txt`.

## Ghi âm bộ test STT

```powershell
.venv\Scripts\python bench\record.py --list-devices      # tìm số thứ tự micro
.venv\Scripts\python bench\record.py --device 1 --test-mic   # chỉnh mức thu
.venv\Scripts\python bench\record.py --device 1          # ghi các câu trong data\sentences.tsv
```

Audio lưu ở `data/testset/` và không được commit.

## Benchmark STT

Các cấu hình nằm trong `bench/stt_configs.toml`. Kết quả lưu ở `_local/bench/stt/`.

```powershell
.venv\Scripts\python bench\stt_bench.py --config v3_vi v3_vi_prompt   # chạy vài cấu hình
.venv\Scripts\python bench\stt_bench.py --all                          # chạy tất cả
.venv\Scripts\python bench\stt_bench.py --compare                      # bảng so sánh
.venv\Scripts\python bench\stt_bench.py --rescore --with-corrections   # chấm lại khi áp data\corrections.tsv
.venv\Scripts\python -m pytest -q tests
```

## Voice server (Phase 2)

```powershell
.venv\Scripts\python -m server
```

Mở http://127.0.0.1:8765, cho phép dùng micro, rồi **giữ Space** (hoặc giữ nút tròn) để nói và thả ra để nhận transcript. Bấm `Esc` để huỷ.

- Chọn chủ đề (AWS, .NET / C#, Dev chung, Tiếng Anh) để nhận đúng thuật ngữ hơn. Để "Tự đoán" thì server nhận dạng hai lượt nên chậm hơn.
- Transcript sai thì sửa trực tiếp trong ô (gõ đủ dấu cách). Bản sửa được ghi vào log, là nguồn cho trang **Đề xuất học**.
- Cấu hình (model, port, lưu audio) nằm trong `server/config.toml`.
- Log từng lượt: `_local/logs/turns-YYYY-MM-DD.jsonl`, kèm wav trong `_local/logs/audio/`.

### Dùng từ điện thoại cùng Wi-Fi

Server mở thêm cổng HTTPS `lan_port` (mặc định 8766) cho máy khác trong mạng LAN; trình duyệt chỉ cho dùng micro qua HTTPS. Tạo chứng chỉ tự ký một lần (Git Bash, thay IP bằng IP cố định của máy):

```bash
mkdir -p _local/certs
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout _local/certs/lan.key -out _local/certs/lan.crt -subj "/CN=Voice Study Assistant" \
  -addext "subjectAltName=IP:192.168.1.199,DNS:localhost,IP:127.0.0.1" -addext "extendedKeyUsage=serverAuth"
```

Trên điện thoại mở `https://192.168.1.199:8766`. Lần đầu trình duyệt cảnh báo chứng chỉ: chọn "Nâng cao" → "Tiếp tục truy cập". Đổi IP máy thì tạo lại chứng chỉ. Cổng này **không có đăng nhập**: máy nào trong mạng LAN cũng dùng được, đừng port-forward ra Internet. Không có chứng chỉ thì server chỉ chạy cổng 8765 như cũ.

### Đề xuất học (T-007)

Link "Đề xuất học" ở góc trên trang Nói (số đỏ là số đề xuất đang chờ), hoặc http://127.0.0.1:8765/learn.html.

- Lỗi bạn sửa ở **≥2 lượt** thành đề xuất: *thuật ngữ* (thêm vào glossary của một chủ đề) hoặc *rule* (thêm vào `data/corrections.tsv`). Sửa một lần thì chưa học.
- Khoảng 30% lượt nói là holdout, chỉ dùng để kiểm chứng, không sinh đề xuất. Chỉ học thuật ngữ / từ tiếng Anh, không học lỗi giữa các từ tiếng Việt.
- **Kiểm tra**: chấm lại bộ test 100 câu và các lượt nói của bạn. Thuật ngữ cần GPU, mất vài chục giây (xếp hàng với lượt nói).
- **Duyệt**: chỉ bấm được khi kiểm tra đạt; áp dụng ngay từ lượt nói sau, không cần restart. **Bỏ qua**: không hiện lại. Quyết định ghi ở `_local/learning/decisions.jsonl`.
- Xem đề xuất ở dòng lệnh: `.venv\Scripts\python bench\suggest_corrections.py [--min 1]`.

Bộ test cá nhân từ các lượt đã sửa (lớn dần theo thời gian dùng; tắt server trước khi chạy benchmark):

```powershell
.venv\Scripts\python bench\build_personal_set.py   # → _local\personal\transcripts.jsonl
.venv\Scripts\python bench\stt_bench.py --testset _local\personal --config v3_vi_topic_oracle
```

Kiểm tra server bằng audio của bộ test, không cần nói (server phải đang chạy):

```powershell
.venv\Scripts\python bench\ws_client.py --limit 20   # chấm như stt_bench, đo thời gian thả phím → text
.venv\Scripts\python bench\ws_client.py --turns 50   # chạy bền, xem VRAM có tăng dần không
```

## Đọc thành tiếng (Phase 3)

Khối **Đọc** trên trang chính: dán text rồi bấm **Đọc** (hoặc `Ctrl+Enter`). Bấm ⏹ hoặc `Esc` để dừng; bắt đầu nói (giữ Space) cũng dừng đọc.
Danh sách bên dưới hiện từng câu và cách server sẽ đọc nó, câu đang đọc sáng lên.

- Model: ZeroTTS trên GPU, giọng `maichi`. Đổi giọng hoặc tắt TTS trong bảng `[tts]` của `server/config.toml`.
- Cách đọc thuật ngữ / viết tắt: `data/lexicon.tsv`, server tự đọc lại khi file đổi.
- Muốn Claude trả lời theo văn nói thì dán `prompts/voice_reply.md` vào Custom instructions của Claude Project.
- Log server có cảnh báo "ZeroTTS đang chạy CPU…" nghĩa là `onnxruntime-gpu` sai bản: chạy lại hai lệnh cuối ở phần Cài đặt.

Benchmark và đo (tắt server trước khi chạy `tts_bench.py`):

```powershell
.venv\Scripts\python bench\tts_bench.py --model zerotts --gpu --voice maichi # sinh audio 35 đoạn mẫu, đo TTFA/RTF/VRAM
.venv\Scripts\python bench\tts_bench.py --compare                 # bảng so sánh các run
.venv\Scripts\python bench\tts_bench.py --rate-page               # trang chấm mù _local\tts\rate.html
.venv\Scripts\python bench\tts_ws_client.py                       # server đang chạy: đo tiếng đầu, hụt, dừng, VRAM
```

Ứng viên khác (VieNeu, OmniVoice) cần venv riêng vì xung đột thư viện; xem đầu `bench/tts_bench.py`.

## Hỏi Claude (Phase 4)

Giữ Space nói câu hỏi, thả ra: server nhận dạng, hỏi Claude, rồi đọc câu trả lời ngay khi câu đầu tiên về (không chờ trả lời xong).
Câu trả lời hiện dưới lượt nói; phần code (sau `---CODE---`) chỉ hiện, không đọc. Gõ câu hỏi trong khối **Gõ** rồi bấm **Hỏi Claude** (`Ctrl+Enter`) cũng được.

- `Esc` / ⏹ / bắt đầu nói: dừng cả hỏi lẫn đọc.
- Ô chọn model cạnh **Session mới** (`claude_cli`): đổi cho các lượt sau, áp cho mọi tab, về lại `model` trong config khi restart server. "model mặc định" là model mặc định của Claude Code theo gói đăng nhập. Danh sách lấy từ `models` trong `[bridge]`; tên model thật hiện ở dòng số đo dưới câu trả lời.
- **Session mới**: Claude quên ngữ cảnh cũ. Session càng dài thì mỗi lượt càng tốn hạn mức, đổi chủ đề thì nên bấm.
- Cấu hình ở bảng `[bridge]` của `server/config.toml`:
  - `kind = "claude_cli"`: gọi `claude -p` của Claude Code (cần cài và đăng nhập `claude` trước), chạy trong `_local/claude-workdir`, chỉ có WebSearch/WebFetch.
  - `kind = "clipboard"`: transcript được chép vào clipboard, dán vào Claude Desktop/web; copy câu trả lời rồi bấm `Ctrl+Alt+R` (đổi bằng `hotkey`) để nghe.
  - `kind = "none"`: chỉ STT + TTS như Phase 3.
  - `auto_ask = false`: nói xong không tự hỏi, sửa transcript rồi mới bấm **Hỏi Claude**.
- Mỗi lượt hỏi được ghi vào `_local/sessions/YYYY-MM-DD.jsonl` (câu hỏi, trả lời, độ trễ, chi phí CLI báo).

Đo độ trễ (server đang chạy, `kind = "claude_cli"`, mỗi lần chạy tốn hạn mức như hỏi thật 5 câu):

```powershell
.venv\Scripts\python bench\claude_ws_client.py          # thả phím → nghe câu đầu, kiểm tra nhớ ngữ cảnh
.venv\Scripts\python bench\claude_ws_client.py --text   # hỏi bằng text, bỏ qua STT
```

## Trợ lý rảnh tay "Mimi" (T-010)

Tab **Mimi** (mặc định) là giao diện trợ lý; tab **Chi tiết** là giao diện cũ (lượt nói, transcript, số đo, đoạn rảnh tay đã nghe).

1. Bấm nút 🎙 **Rảnh tay** (trình duyệt cần được bấm/chạm một lần sau khi tải trang mới phát được âm thanh). Trạng thái bật được nhớ cho lần sau.
2. Gọi tên ở **đầu câu**: "Mimi ơi", "hey Mimi", "ê Mimi", "này Mimi"... Trợ lý đáp một câu ngẫu nhiên ("Có mình đây.", "Dạ?"...).
3. Nói câu hỏi, ngừng nói ~1 giây thì trợ lý nghĩ và trả lời. Đọc xong còn nghe thêm 6 giây để hỏi tiếp không cần gọi tên.
4. Nói liền "Mimi ơi, EC2 là gì?" thì hỏi luôn, không đáp.
5. Đang đọc mà gọi tên thì trợ lý dừng và nghe câu mới. `Esc` / ⏹ cũng dừng. Giữ Space vẫn dùng được như cũ.

- Nhắc tên giữa câu ("hôm qua Mimi nói...") không đánh thức trợ lý.
- Lệnh chạy ngay, không hỏi Claude (cả khi gõ tay): "mở session mới" / "phiên mới" / "quên hết", "dừng lại" / "thôi", "đọc lại" / "nhắc lại". Chỉ câu ngắn mới là lệnh; câu dài vẫn gửi cho Claude.
- Dùng **tai nghe** là chắc nhất. Với loa ngoài, trợ lý không coi tiếng nói lúc loa còn đang đọc là câu hỏi, nhưng tiếng loa vẫn có thể lọt vào câu hỏi nếu bạn nói chồng lên.
- Cấu hình ở bảng `[assistant]` của `server/config.toml`: tên, `aliases` (cách Whisper hay viết sai tên), các câu đáp (đừng chứa tên), ngưỡng, thời gian im lặng.
- Log: `_local/logs/wake-YYYY-MM-DD.jsonl` (mọi đoạn nghe được lúc chờ tên), audio các đoạn có tên ở `_local/logs/wake-audio/`.

Đo nhận tên (tắt server trước, cần GPU):

```powershell
.venv\Scripts\python bench\record.py --sentences data\wake_phrases.tsv --out data\wake_testset   # 48 câu, ~5 phút
.venv\Scripts\python bench\record.py --ambient 30      # 30 phút âm nền, sinh hoạt bình thường, không gọi tên
.venv\Scripts\python bench\wake_bench.py               # tham số trong config.toml
.venv\Scripts\python bench\wake_bench.py --name s075 --min-score 0.75
.venv\Scripts\python bench\wake_bench.py --compare
```

Benchmark dùng cả 100 câu `data/testset` làm câu âm tính (không có tên). Chỉ sửa `aliases`/ngưỡng theo lỗi ở **dev**.
