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
