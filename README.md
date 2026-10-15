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
```

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

### Đề xuất học (T-007)

Link "Đề xuất học" ở góc trên trang Nói (số đỏ là số đề xuất đang chờ), hoặc http://127.0.0.1:8765/learn.html.

- Lỗi bạn sửa ở **≥2 lượt** thành đề xuất: *thuật ngữ* (thêm vào glossary của một chủ đề) hoặc *rule* (thêm vào `data/corrections.tsv`). Sửa một lần thì chưa học.
- Khoảng 30% lượt nói là holdout, chỉ dùng để kiểm chứng, không sinh đề xuất. Chỉ học thuật ngữ / từ tiếng Anh, không học lỗi giữa các từ tiếng Việt.
- **Kiểm tra**: chấm lại bộ test 100 câu và các lượt nói của bạn. Thuật ngữ cần GPU, mất vài chục giây (xếp hàng với lượt nói).
- **Duyệt**: chỉ bấm được khi kiểm tra đạt; áp dụng ngay từ lượt nói sau, không cần restart. **Bỏ qua**: không hiện lại. Quyết định ghi ở `_local/learning/decisions.jsonl`.
- Xem đề xuất ở dòng lệnh: `.venv\Scripts\python bench\suggest_corrections.py [--min 1]`.

Bộ test cá nhân từ các lượt đã sửa (lớn dần theo thời gian dùng; tắt server trước khi chạy benchmark):

```powershell
.venv\Scripts\python benchuild_personal_set.py                                  # → _local\personal	ranscripts.jsonl
.venv\Scripts\python bench\stt_bench.py --testset _local\personal --config v3_vi_topic_oracle
```

Kiểm tra server bằng audio của bộ test, không cần nói (server phải đang chạy):

```powershell
.venv\Scripts\python bench\ws_client.py --limit 20   # chấm như stt_bench, đo thời gian thả phím → text
.venv\Scripts\python bench\ws_client.py --turns 50   # chạy bền, xem VRAM có tăng dần không
```
