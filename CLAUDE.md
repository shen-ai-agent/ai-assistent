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
./start.bat                                                   # người dùng: server + tự mở trình duyệt (= python -m server --open)
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
  - `hallucination.py`: bỏ câu Whisper bịa trên tiếng ồn ("Cảm ơn các bạn đã theo dõi…"), áp vào `SttResult.text`, `raw` giữ nguyên.
  - `engine.py`: `WhisperEngine` (faster-whisper). Model trong `models/<tên>` được nhận theo tên thư mục.
  - `service.py`: `SttService`, một lượt nhận dạng theo cấu hình chốt ở Phase 1 (prompt theo chủ đề, không có chủ đề thì two_pass). Tự đọc lại glossary/corrections khi file đổi.
- `server/tts/`: TTS (Phase 3).
  - `normalize.py`: text viết → text đọc. `clean_structure` (cắt sau `---CODE---`, code, markdown, URL) → `split_sentences` → `speakable` từng câu (lexicon `data/lexicon.tsv`, số, đơn vị, viết tắt). `prepare` trả `Segment(text, lang, source)`.
  - `splitter.py`: tách câu, không tách ở số thập phân, viết tắt, tên file.
  - `synth.py`: `ZeroTtsEngine` (ONNX GPU, `synthesize_stream`, 48kHz), `TtsService` (tự đọc lại lexicon).
  - `zerotts_gpu.py`: `ZeroTtsGpu`, bản ZeroTTS ít phụ thuộc CPU (máy bận thì TTS không giật). `local_frame_decode` chạy CUDA Graph bằng bản graph đã sửa trong `models/zerotts-graph/` (tự tạo bằng gói `onnx`, tạo lại khi model đổi), KV cache/state codec giữ trên GPU qua IOBinding. Audio phải trùng bản gốc tới từng mẫu: `GPU_TESTS=1 pytest tests/test_tts_gpu.py` (tắt server trước). Lỗi gì cũng tự quay về chạy thường. Server đặt priority `high` (`[server] priority`).
- `server/`: voice server FastAPI (Phase 2). `protocol.py` là nguồn duy nhất định nghĩa message WebSocket; sửa protocol thì sửa cả `web/app.js`. `app.py` có `create_app(cfg, engine_factory, tts_factory, bridge_factory, vad_factory)` để test thay engine giả (`tests/test_server.py`); `Config.tts_enabled` mặc định tắt, `Config.bridge_kind` mặc định `"none"`; `server/config.toml` bật cả hai. STT và TTS mỗi bên một khoá GPU riêng. `turnlog.py` ghi log lượt nói và bản sửa tay.
  - `learning.py`: vòng tự học (T-007): đề xuất thuật ngữ/rule từ bản sửa tay, kiểm tra, áp dụng vào `data/`. `learn_api.py` là REST `/api/learn` cho `web/learn.html`.
  - Lượt nói chia dev/holdout cố định bằng `split_of(turn_id)`; chỉ lượt dev sinh đề xuất, giống quy tắc đo đạc bên dưới.
  - `bridge/`: hỏi Claude (Phase 4, T-009). `base.py` là interface `Bridge.ask() -> TextDelta | Done`, huỷ bằng cách huỷ task / đóng generator. `claude_cli.py` gọi `claude -p --output-format stream-json`, `clipboard.py` (Win32 clipboard + hotkey qua ctypes), `feeder.py` cắt câu từ stream cho TTS, `asklog.py` ghi `_local/sessions`. Test dùng `FakeBridge` qua `bridge_factory` hoặc script `claude` giả (`tests/test_bridge.py`), không gọi Claude thật.
  - `claude_cli` chạy trong `_local/claude-workdir` (nằm trong repo) nên phải tắt CLAUDE.md/git/memory qua `--settings`. Không dùng `--bare`: chế độ đó chỉ nhận API key, không dùng được subscription.
  - `assistant/`: rảnh tay (T-010). `vad.py`: `StreamingVad` (Silero v6 của faster-whisper, giữ trạng thái giữa frame), `Segmenter` (cắt đoạn, mốc thời gian tính bằng số mẫu mic). `wake.py`: `WakeMatcher` (tên ở đầu câu, so gần đúng sau `fold`), `decide()` dùng chung cho server và `bench/wake_bench.py`. `commands.py`: lệnh giọng nói (`match_command`), `App.start_ask` chạy lệnh thay vì hỏi Claude, báo web bằng message `command`.
  - `handsfree.py`: `HandsFree` một phiên, SLEEP/ACTIVE/BUSY. Mọi mốc thời gian theo `seg.pos` (mẫu mic), không theo đồng hồ, nên test chỉ cần đẩy PCM (`tests/test_assistant.py`, VAD giả qua `vad_factory`). Test phải đẩy đủ im lặng sau `speak_end` thì cửa sổ ACTIVE mới mở. Tên trợ lý không đưa vào `initial_prompt`; câu đáp không chứa tên.
  - Test phải trỏ `Config(glossary=..., corrections=...)` và `L.TESTSET`/`L.BASELINE_RUN` vào `tmp_path` để không ghi vào `data/` thật (`tests/test_learning.py`).
  - `agent/`: giao việc cho VS Code (Phase 9, T-011). `projects.py`: `ProjectRegistry` (quét `[agent] roots` hai tầng, overrides `_local/projects.toml`, so tên nói ra), `claude_project_dirs` (thư mục log phiên, slug không phân biệt hoa thường). `intent.py`: `parse_dev_task` tách câu thành dự án + yêu cầu, câu hỏi trả None để hỏi Claude. `vscode.py`: `VsCodeLauncher` mở thư mục, URI `vscode://anthropic.claude-code/open?prompt=` (chỉ điền sẵn, không tự gửi), Enter qua Win32 khi cửa sổ trên cùng đúng dự án. `watcher.py`: `SessionWatcher` đọc tăng dần `~/.claude/projects/<slug>/*.jsonl`, phát `Submitted/ToolStep/ToolDone/Waiting/Finished/NotSubmitted`. `tasks.py`: `TaskManager` điều phối, broadcast cho mọi session; `route` quyết định câu nói là giao việc, hỏi tiến độ, hỏi lại tên dự án hay để hỏi Claude (tab Công việc gửi message `work`: mọi câu là giao việc; câu ngắn "mở X" không khớp chắc thì `guess_open` hỏi lại kèm ứng viên, không rơi về dự án đang chọn). Khi hỏi lại, `Session.pending` giữ `Pending`; `reply_kind` nhận "ừ / cái thứ hai / không phải", "sai rồi" ngay sau khi giao thì hỏi lại. Mỗi lần người dùng chỉ ra dự án đúng (kể cả nút `task_fix`) ghi nhãn qua `on_label` vào `_local/logs/intents-*.jsonl` (Phase 8 D).
  - Test agent dùng launcher/watcher giả qua `agent_factory` và `app.state.agent.watcher_factory` (`tests/test_agent_tasks.py`); không mở VS Code, không gửi phím thật. Nhãn hiểu ý ghi theo `Config(log_dir=tmp_path/...)`.
  - `study/`: tab Học bài (T-012). `library.py`: `Library`, mỗi tài liệu một thư mục `_local/study/<id>/` (`doc.md`, `meta.json`, `history.jsonl`), `extract_text` đọc .txt/.md/.docx (docx bằng zipfile, không thêm package). `service.py`: `StudyService`, mỗi tài liệu một `ClaudeCliBridge` riêng chạy với cwd là thư mục tài liệu; Claude đọc `doc.md` bằng tool Read (không nhét tài liệu vào dòng lệnh vì Windows giới hạn ~32K ký tự). `MarkerFilter` cắt dòng `[Phần k/N: Tên]` khỏi stream trước TTS và lưu thành tiến độ. `api.py`: REST `/api/study` (upload JSON base64, không multipart). Client gửi message `study` khi mở/đóng tài liệu; đang mở thì câu hỏi đi tới gia sư, `answer_start.doc_id` báo bubble thuộc hội thoại nào.
  - Test học bài dùng gia sư giả qua `create_app(..., tutor_factory=...)` và `Config(study_dir=tmp_path/"study")` (`tests/test_study.py`), không gọi Claude thật.
- `web/`: HTML/JS thuần, không có bước build. `capture-worklet.js` resample mic về PCM 16kHz, frame 100ms.
  - PWA: `manifest.json`, `icons/` (PNG xuất từ `icon.svg`), `sw.js`. Service worker chỉ trả `offline.html` khi mở trang lỗi mạng; đừng cache CSS/JS (server gửi `no-cache` để sửa là thấy ngay).
- `bench/`: `record.py` (ghi bộ test), `stt_bench.py`, `stt_configs.toml` (mỗi bảng TOML là một cấu hình), `ws_client.py` (chấm server qua WebSocket), `build_personal_set.py`, `suggest_corrections.py`, `tts_bench.py` + `tts_samples.txt` (35 đoạn clean/term/dirty/en), `tts_ws_client.py`, `claude_ws_client.py`, `wake_bench.py` (+ `data/wake_phrases.tsv`: nhóm `name`/`ask`/`near`; `record.py --ambient` ghi âm nền vào `_local/wake_ambient/`).
- `prompts/voice_reply.md`: instruction văn nói cho Claude, khớp với marker `---CODE---` của normalize. `prompts/study_reply.md`: instruction gia sư cho tab Học bài (dòng tiến độ `[Phần k/N: Tên]`, ghi chú sau `---CODE---`).
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
