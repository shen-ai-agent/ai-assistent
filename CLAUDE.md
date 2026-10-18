# CLAUDE.md

Voice Study Assistant: lớp Voice I/O (STT + TTS) chạy local, bọc quanh Claude. Người dùng là dev, học AWS, .NET, IELTS, TOEIC; nói tiếng Việt lẫn thuật ngữ tiếng Anh, có cả câu tiếng Anh hoàn toàn.

- Kế hoạch, TODO, DoD từng phase: [PLAN.md](PLAN.md). Bản chi tiết: [docs/plan.html](docs/plan.html).
- Khi xong một phase hoặc một mục TODO: cập nhật bảng tiến độ trong PLAN.md và nội dung tương ứng trong docs/plan.html. Hai file phải khớp nhau.

## Giao tiếp

- Trả lời bằng tiếng Việt có dấu đầy đủ. Comment và docstring trong code cũng viết tiếng Việt; identifier tiếng Anh.
- Chỉ commit/push khi người dùng yêu cầu.

## Môi trường

Windows 11, RTX 3060 12GB, Python 3.11 trong `.venv`. Shell của Claude Code là Git Bash.

```bash
.venv/Scripts/python -m pytest -q tests                      # unit test
.venv/Scripts/python bench/stt_bench.py --config v3_vi_prompt # benchmark một cấu hình
.venv/Scripts/python bench/stt_bench.py --compare             # bảng so sánh kết quả đã lưu
.venv/Scripts/python bench/stt_bench.py --rescore [--with-corrections]  # chấm lại, không cần GPU
.venv/Scripts/python -m server > _local/server.log 2>&1      # voice server (chạy nền), http://127.0.0.1:8765
.venv/Scripts/python bench/ws_client.py --limit 20            # đẩy audio testset qua /ws, server phải đang chạy
.venv/Scripts/python bench/suggest_corrections.py             # đề xuất học từ log (giống trang /learn.html)
.venv/Scripts/python bench/build_personal_set.py              # bộ test cá nhân _local/personal, chạy với stt_bench.py --testset
.venv/Scripts/python bench/tts_bench.py --model zerotts --gpu --voice maichi  # benchmark TTS (35 đoạn mẫu)
.venv/Scripts/python bench/tts_bench.py --compare             # bảng so sánh các run TTS
.venv/Scripts/python bench/tts_ws_client.py                   # đo DoD TTS qua /ws, server phải đang chạy
.venv/Scripts/python bench/record.py --sentences data/wake_phrases.tsv --out data/wake_testset  # bộ gọi tên (người dùng ghi)
.venv/Scripts/python bench/wake_bench.py [--name X --min-score 0.75] / --compare  # đo nhận tên, tắt server trước
.venv/Scripts/python bench/claude_ws_client.py                # đo DoD Phase 4 (hỏi Claude thật, tốn hạn mức), server phải đang chạy
```

- Tắt server: tìm PID bằng `netstat -ano | grep ":8765 .*LISTENING"` rồi `taskkill //F //PID <pid>`. Đừng kill mọi `python.exe`.
- Có `_local/certs/lan.crt|key` thì server mở thêm `https://0.0.0.0:8766` (`lan_port`) cho điện thoại cùng Wi-Fi, cùng tiến trình với cổng 8765, không có đăng nhập. Tạo chứng chỉ theo README.
- Server giữ model trên GPU (~2.5GB). Tắt server trước khi chạy `stt_bench.py`, nếu không sẽ thành hai job GPU cùng lúc.

- Script in tiếng Việt phải gọi `sys.stdout.reconfigure(encoding="utf-8")` đầu `main()`. Console Windows mặc định cp1252, và `python -I` bỏ qua `PYTHONIOENCODING`.
- CTranslate2 cần `cublas64_12.dll`; `server/stt/engine.py` lấy từ `torch/lib` (torch cu128). Đừng cài CUDA toolkit riêng.
- TTS cần `onnxruntime-gpu==1.23.2`. faster-whisper kéo `onnxruntime` bản CPU ghi đè nó; bản 1.31 cần CUDA 13 và lặng lẽ chạy CPU. Cài lại theo README nếu log server báo ZeroTTS chạy CPU. Phải `import torch` trước khi tạo session để nạp cuDNN.
- Ứng viên TTS khác (VieNeu, OmniVoice) cài trong venv riêng `_local/venvs/<model>`, dùng chung torch của `.venv` qua file `.pth`; đừng cài vào `.venv`.
- Đo VRAM bằng NVML (gói `nvidia-ml-py`, import tên `pynvml`), không dùng `torch.cuda`, vì CTranslate2 không cấp phát qua torch.
- Thêm package thì ghi vào `requirements.txt` kèm version cố định. torch cài riêng theo hướng dẫn trong README.
- Benchmark mất vài phút: chạy nền, không chạy hai job GPU cùng lúc.
- Viết file có `\` (regex) thì dùng Write/Edit, không dùng heredoc trong bash vì dễ hỏng escape.

## Cấu trúc

- `server/stt/`: code STT dùng chung cho benchmark và server (Phase 2). `bench/` chỉ import và gọi, không chép logic.
  - `textnorm.py`: chuẩn hoá text **chỉ để chấm điểm**. `contains_term` bỏ qua khác biệt khoảng trắng ("CICD" khớp "CI/CD").
  - `glossary.py`: đọc glossary, dựng prompt (`list` / `sentence` / `topic`), `detect_topic`.
  - `corrections.py`: sửa lỗi STT theo `data/corrections.tsv`.
  - `engine.py`: `WhisperEngine` (faster-whisper). Model trong `models/<tên>` được nhận theo tên thư mục.
  - `service.py`: `SttService`, một lượt nhận dạng theo cấu hình chốt ở Phase 1 (prompt theo chủ đề, không có chủ đề thì two_pass). Tự đọc lại glossary/corrections khi file đổi.
- `server/tts/`: TTS (Phase 3).
  - `normalize.py`: text viết → text đọc. `clean_structure` (cắt sau `---CODE---`, code, markdown, URL) → `split_sentences` → `speakable` từng câu (lexicon `data/lexicon.tsv`, số, đơn vị, viết tắt). `prepare` trả `Segment(text, lang, source)`.
  - `splitter.py`: tách câu, không tách ở số thập phân, viết tắt, tên file.
  - `synth.py`: `ZeroTtsEngine` (ONNX GPU, `synthesize_stream`, 48kHz), `TtsService` (tự đọc lại lexicon).
- `server/`: voice server FastAPI (Phase 2). `protocol.py` là nguồn duy nhất định nghĩa message WebSocket; sửa protocol thì sửa cả `web/app.js`. `app.py` có `create_app(cfg, engine_factory, tts_factory, bridge_factory, vad_factory)` để test thay engine giả (`tests/test_server.py`); `Config.tts_enabled` mặc định tắt, `Config.bridge_kind` mặc định `"none"`; `server/config.toml` bật cả hai. STT và TTS mỗi bên một khoá GPU riêng. `turnlog.py` ghi log lượt nói và bản sửa tay.
  - `learning.py`: vòng tự học (T-007): đề xuất thuật ngữ/rule từ bản sửa tay, kiểm tra, áp dụng vào `data/`. `learn_api.py` là REST `/api/learn` cho `web/learn.html`.
  - Lượt nói chia dev/holdout cố định bằng `split_of(turn_id)`; chỉ lượt dev sinh đề xuất, giống quy tắc đo đạc bên dưới.
  - `bridge/`: hỏi Claude (Phase 4, T-009). `base.py` là interface `Bridge.ask() -> TextDelta | Done`, huỷ bằng cách huỷ task / đóng generator. `claude_cli.py` gọi `claude -p --output-format stream-json`, `clipboard.py` (Win32 clipboard + hotkey qua ctypes), `feeder.py` cắt câu từ stream cho TTS, `asklog.py` ghi `_local/sessions`. Test dùng `FakeBridge` qua `bridge_factory` hoặc script `claude` giả (`tests/test_bridge.py`), không gọi Claude thật.
  - `claude_cli` chạy trong `_local/claude-workdir` (nằm trong repo) nên phải tắt CLAUDE.md/git/memory qua `--settings`. Không dùng `--bare`: chế độ đó chỉ nhận API key, không dùng được subscription.
  - `assistant/`: rảnh tay (T-010). `vad.py`: `StreamingVad` (Silero v6 của faster-whisper, giữ trạng thái giữa frame), `Segmenter` (cắt đoạn, mốc thời gian tính bằng số mẫu mic). `wake.py`: `WakeMatcher` (tên ở đầu câu, so gần đúng sau `fold`), `decide()` dùng chung cho server và `bench/wake_bench.py`. `commands.py`: lệnh giọng nói (`match_command`), `App.start_ask` chạy lệnh thay vì hỏi Claude, báo web bằng message `command`.
  - `handsfree.py`: `HandsFree` một phiên, SLEEP/ACTIVE/BUSY. Mọi mốc thời gian theo `seg.pos` (mẫu mic), không theo đồng hồ, nên test chỉ cần đẩy PCM (`tests/test_assistant.py`, VAD giả qua `vad_factory`). Test phải đẩy đủ im lặng sau `speak_end` thì cửa sổ ACTIVE mới mở. Tên trợ lý không đưa vào `initial_prompt`; câu đáp không chứa tên.
  - Test phải trỏ `Config(glossary=..., corrections=...)` và `L.TESTSET`/`L.BASELINE_RUN` vào `tmp_path` để không ghi vào `data/` thật (`tests/test_learning.py`).
- `web/`: HTML/JS thuần, không có bước build. `capture-worklet.js` resample mic về PCM 16kHz, frame 100ms.
  - PWA: `manifest.json`, `icons/` (PNG xuất từ `icon.svg`), `sw.js`. Service worker chỉ trả `offline.html` khi mở trang lỗi mạng; đừng cache CSS/JS (server gửi `no-cache` để sửa là thấy ngay).
- `bench/`: `record.py` (ghi bộ test), `stt_bench.py`, `stt_configs.toml` (mỗi bảng TOML là một cấu hình), `ws_client.py` (chấm server qua WebSocket), `build_personal_set.py`, `suggest_corrections.py`, `tts_bench.py` + `tts_samples.txt` (35 đoạn clean/term/dirty/en), `tts_ws_client.py`, `claude_ws_client.py`, `wake_bench.py` (+ `data/wake_phrases.tsv`: nhóm `name`/`ask`/`near`; `record.py --ambient` ghi âm nền vào `_local/wake_ambient/`).
- `prompts/voice_reply.md`: instruction văn nói cho Claude, khớp với marker `---CODE---` của normalize.
- `data/glossary.txt`: dòng `[*]Thuật ngữ | alias...`, chia chủ đề bằng `# ---------- Tên ----------`. `*` = ưu tiên, đưa vào prompt mặc định.
- `data/sentences.tsv`: 100 câu test (`id, group, split, text, terms`). Nhóm `vi`, `mix`, `dense`, `en`; split `dev` / `holdout`.
- Không commit: `data/testset/`, `data/wake_testset/` (audio ghi âm), `models/`, `_local/` (kết quả benchmark, báo cáo, file nháp).

## Quy tắc đo đạc

- Mọi quyết định chọn model hoặc cấu hình phải dựa trên số đo từ `stt_bench.py`, không dựa vào cảm giác hay vài câu thử.
- Chỉ tinh chỉnh (corrections, alias, prompt) dựa trên lỗi ở **dev**. **holdout** chỉ dùng để kiểm chứng; không đọc lỗi holdout để sửa.
- Rule trong `corrections.tsv` chỉ thêm khi lỗi lặp lại ít nhất 2 lần ở dev.
- Không sửa `data/sentences.tsv` sau khi đã ghi âm: text trong file phải khớp audio đã ghi.
- Đổi cách chấm (textnorm) thì chạy `--rescore` cho mọi kết quả cũ để còn so sánh được với nhau.
- Báo kết quả theo nhóm (vi/mix/dense/en) và split (dev/holdout), gồm WER, term recall, RTF, VRAM.
