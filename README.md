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
