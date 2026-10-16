# Tickets

Việc cần làm tiếp, kèm bối cảnh để quay lại sau vẫn hiểu. Đóng ticket thì đổi trạng thái và ghi commit liên quan.

| ID | Tiêu đề | Phase | Trạng thái |
|---|---|---|---|
| T-001 | Chốt DoD Phase 1 | 1 | ✅ 15/10/2026 |
| T-002 | Voice server + web push-to-talk, có chọn chủ đề | 2 | 🟡 code xong, DoD trên trình duyệt chưa thực hiện |
| T-003 | Log lượt nhận dạng + text sửa tay, làm nguồn cho corrections | 2 | ✅ 15/10/2026 |
| T-004 | Ghi thêm câu test mix/dense | 1 (bổ sung) | ⬜ tuỳ chọn |
| T-005 | Nhận xét phát âm tiếng Anh dựa trên chỗ STT nghe sai | 5 | ⬜ |
| T-006 | Quyết định: dự án narration có gộp vào repo này không | — | ✅ 16/10/2026: không |
| T-007 | Vòng tự học có kiểm soát từ bản sửa tay | 2 → 5 | ✅ 16/10/2026 (công cụ); dữ liệu tích luỹ khi dùng |
| T-008 | TTS: chọn model, chuẩn hoá text, stream về web | 3 | ✅ 16/10/2026 |
| T-009 | Claude bridge: clipboard + Claude Code headless, stream câu trả lời sang TTS | 4 | 🟡 code xong, còn dùng thật |
| T-010 | Trợ lý rảnh tay: gọi tên "Mimi", tab Mimi | 5 | 🟡 bước A code xong, chờ ghi bộ test gọi tên |

---

## T-001: Chốt DoD Phase 1

**Bối cảnh.** Benchmark xong, chọn `large-v3` + ép `vi` + prompt theo chủ đề (chi tiết trong `_local/stt_report.md` và PLAN.md). 4/6 tiêu chí DoD đạt. Hai tiêu chí chưa đạt:

- WER nhóm `en` 14.9% (mục tiêu ≤10%). Lỗi do phát âm ("word"→"world", "chart"→"chair"). Ép `en` cũng ra 14.9%, nên đổi cấu hình STT không sửa được.
- Term recall câu mix 88.8%, holdout 89.3% (mục tiêu ≥90%). Holdout chỉ 30 câu, mỗi thuật ngữ bằng khoảng 1.6 điểm, nên chênh lệch này nằm trong sai số.

**Đề xuất.** Hạ mục tiêu WER `en` xuống ≤15%, và coi term recall 88.8% là đạt.

**Xong khi.** Có quyết định; PLAN.md đổi Phase 1 thành ✅ (hoặc ghi rõ việc phải làm thêm).

**Kết quả (15/10/2026).** Đồng ý đề xuất: WER `en` mục tiêu ≤15%, term recall 88.8% coi là đạt. Phase 1 ✅.

## T-002: Voice server + web push-to-talk, có chọn chủ đề

**Bối cảnh.** Xem Phase 2 trong PLAN.md. Kết quả Phase 1 bổ sung yêu cầu:
- Server dùng `server/stt/engine.py` (`WhisperEngine("large-v3")`), load một lần lúc khởi động.
- Prompt theo chủ đề: `build_prompt(glossary, "topic", topic)`. UI có chỗ chọn chủ đề cho phiên (AWS / .NET / Dev / Tiếng Anh).
- Chưa chọn chủ đề thì dùng two_pass: nhận dạng một lần, `detect_topic`, rồi nhận dạng lại.

**Xong khi.** Đạt DoD Phase 2 trong PLAN.md.

**Trạng thái (16/10/2026).** Code và đo qua `bench/ws_client.py` xong. Chưa thực hiện trên trình duyệt: nói 10 câu testset qua mic, tắt/bật server xem web tự kết nối lại, chạy liên tục 1 giờ / 50 lượt.

## T-003: Log lượt nhận dạng + text sửa tay

**Bối cảnh.** Phase 1 không thêm được rule corrections: ở dev không có lỗi nào lặp ≥2 lần, và viết rule từ lỗi gặp một lần là học thuộc bộ test. Dữ liệu dùng thật sẽ cho biết lỗi nào thật sự lặp lại.

**Việc.**
- Mỗi lượt ghi lại: thời điểm, chủ đề, thời lượng audio, text STT, text sau khi người dùng sửa (nếu có).
- Lưu ở `_local/`, không commit.
- Thêm script tổng hợp các cặp (STT sai → sửa đúng) lặp lại ≥2 lần để đề xuất rule cho `data/corrections.tsv`.

**Kết quả (15/10/2026).**
- `server/turnlog.py` ghi `_local/logs/turns-YYYY-MM-DD.jsonl`: event `turn` (chủ đề, có tự đoán không, audio_ms, stt_ms, `raw`, `text` sau corrections, file wav) và event `correction` (text người dùng sửa trên web).
- `bench/suggest_corrections.py` ghép lượt với bản sửa cuối, diff theo từ, in các cặp lặp ≥2 lần dưới dạng dòng TSV.
- Việc còn lại thuộc về dùng thật: sau vài tuần chạy script, chép rule phù hợp vào `data/corrections.tsv`.

## T-004: Ghi thêm câu test mix/dense (tuỳ chọn)

**Bối cảnh.** Holdout 30 câu thì số liệu dao động mạnh. Nên làm trước khi so sánh model mới hoặc cân nhắc fine-tune.

**Việc.**
- Thêm 30–50 câu vào `data/sentences.tsv` (id mới, không sửa câu cũ), giữ tỉ lệ dev/holdout 70/30.
- Ghi bằng `bench/record.py`, rồi chạy lại `stt_bench.py`.

## T-005: Nhận xét phát âm tiếng Anh

**Bối cảnh.** Ở Phase 1, lỗi nhóm `en` phần lớn do phát âm: "word"→"world", "get"→"yet", "obtain"→"often", "nevertheless"→"never thee less". Khi luyện IELTS/TOEIC, những chỗ STT nghe sai chính là tín hiệu phát âm chưa rõ.

**Ý tưởng.** Ở chế độ luyện nói, so câu mẫu với kết quả STT và chỉ ra từ bị nghe nhầm. Cân nhắc dùng thêm xác suất từng từ (word-level probability) của Whisper.

## T-006: Dự án narration có gộp vào repo này không

**Bối cảnh.** Kế hoạch ban đầu giả định có một dự án narration tiếng Việt bằng giọng người dùng, dùng chung dataset giọng của Phase 6.

**Kết quả (16/10/2026).** Không có dự án đó, không gộp. Phase 6 chỉ phục vụ trợ lý này; đã bỏ các chỗ nhắc narration trong PLAN.md và plan.html.

## T-007: Vòng tự học có kiểm soát từ bản sửa tay

**Bối cảnh.** Người dùng sửa transcript trên web, nhưng T-003 chỉ có script in gợi ý; không có gì được học lại. Mục tiêu: bản sửa tay thành thuật ngữ glossary / rule corrections, nhưng không học bừa. Ở Phase 1, thuật ngữ có trong prompt đúng 92%, không có thì ~59%, nên thêm thuật ngữ là đòn bẩy lớn nhất.

**Nguyên tắc.**
- Không học từ một lần sửa: lỗi phải lặp ở ≥2 lượt **dev**, và người dùng bấm duyệt.
- Lượt nói chia dev/holdout cố định theo `sha1(turn_id) % 100 < 30` → holdout. Holdout chỉ dùng để kiểm chứng.
- Chỉ học cụm từ Latin (thuật ngữ, từ tiếng Anh). Lỗi giữa các từ tiếng Việt không thành rule, vì rule kiểu đó phá câu khác.
- Không đề xuất rule có vế sai chứa thuật ngữ đúng của glossary. Rule có vế sai là chữ Latin thì hiện cảnh báo.
- Trước khi duyệt phải kiểm tra tự động; làm điểm giảm thì không duyệt được.

**Kết quả (16/10/2026).**
- `server/learning.py`: đọc log, sinh đề xuất (`term` / `rule`), kiểm tra, ghi quyết định vào `_local/learning/decisions.jsonl`, áp dụng vào `data/`.
  - Kiểm tra rule (không cần GPU): áp vào transcript thô của mọi lượt (đếm tốt hơn / xấu hơn theo dev, holdout) và vào `per_utt.csv` của baseline `v3_vi_topic_oracle`.
  - Kiểm tra term (GPU, xếp hàng chung khoá với lượt nói): chạy lại STT với glossary đã thêm thuật ngữ trên các lượt có thuật ngữ đó và các câu testset cùng chủ đề; cho WER lệch 0.5 điểm. Báo số token prompt, cảnh báo nếu vượt 223.
- `server/learn_api.py`: `GET /api/learn`, `POST /api/learn/{id}/check|approve|reject`. Duyệt khi chưa kiểm tra hoặc kiểm tra không đạt → 409.
- `web/learn.html`: trang "Đề xuất học", trang Nói có link kèm số đề xuất đang chờ. Duyệt xong áp dụng ngay từ lượt sau (server đọc lại file khi đổi).
- `bench/build_personal_set.py`: bộ test cá nhân `_local/personal/` từ các lượt đã sửa; `stt_bench.py --testset _local/personal` lưu kết quả ở `_local/bench/stt-personal/`.
- `bench/suggest_corrections.py`: in đề xuất ở dòng lệnh, gọi `server/learning.py`.
- Trên log hiện có (25 lượt, 13 lượt sửa: 8 dev, 5 holdout): chưa có đề xuất, mỗi lỗi mới gặp 1 lần. Ứng viên: `Roboto → Proposal`, `GFDT → JWT`, `interface → integrate`, thuật ngữ `Proposal`, `integrate`.

**Còn lại.** Tích luỹ khi dùng thật. Fine-tune LoRA vẫn ở Phase 5, chỉ làm khi bộ test cá nhân đủ lớn và glossary/rule không còn cải thiện được.

## T-008: TTS: chọn model, chuẩn hoá text, stream về web

**Bối cảnh.** Xem Phase 3 trong PLAN.md. Câu trả lời của Claude có thuật ngữ, viết tắt, số, markdown sót và cả câu tiếng Anh, nên phải chuẩn hoá trước khi đọc. Model phải stream được để nghe câu đầu sớm.

**Kết quả (16/10/2026).**
- `server/tts/normalize.py` + `splitter.py`: bỏ code/markdown/URL, cắt phần sau `---CODE---`, đọc số/đơn vị/phiên bản/ngày giờ, từ điển phát âm `data/lexicon.tsv`, tách câu và nhận diện câu tiếng Anh. 76 test trong `tests/test_tts_normalize.py`.
- `bench/tts_bench.py` + `bench/tts_samples.txt` (35 đoạn: clean/term/dirty/en): so 3 model, chấm mù 1–5 ở `_local/tts/rate.html`. Mỗi ứng viên một venv riêng ở `_local/venvs/`.
- Chọn **ZeroTTS** (MIT, ONNX trên GPU, giọng `maichi`): điểm nghe 4.37 (OmniVoice 4.43 nhưng không stream được, VieNeu 4.11); phát âm thuật ngữ 4.2. Chi tiết trong `_local/tts_report.md`.
- `server/tts/synth.py`, message `speak` / `speak_stop` trong `server/protocol.py`; audio gửi dạng binary (4 byte speak_id + PCM int16 48kHz). Bắt đầu nói (start) thì server tự dừng đọc.
- Web: khối "Đọc" (dán text, Ctrl+Enter), phát nối tiếp theo lịch, ⏹ / Esc tắt tiếng ngay, tô câu đang đọc, hiện số đo.
- `prompts/voice_reply.md`: instruction văn nói cho Claude Project.
- Đo qua WebSocket (`bench/tts_ws_client.py`, 35 đoạn, STT và TTS cùng nằm trong server): tiếng đầu p50 81ms / max 342ms, hụt audio giữa các mẩu max 122ms, RTF 0.53, VRAM đỉnh cả GPU 5.9GB.

**Lưu ý cài đặt.** faster-whisper kéo `onnxruntime` bản CPU, ghi đè `onnxruntime-gpu`; onnxruntime-gpu 1.31 cần CUDA 13 và lặng lẽ chạy CPU. Dùng 1.23.2 (README). Server ghi cảnh báo nếu ZeroTTS không chạy CUDA.

**Nghe thử (16/10/2026).** Người dùng thấy đọc ổn; bấm ⏹ tắt tiếng ngay. Phase 3 ✅.

**Còn lại.** Chưa kiểm trên trình duyệt: giữ Space khi đang đọc thì dừng đọc và nhận dạng bình thường (có test `test_start_interrupts_speaking` phía server).

## T-009: Claude bridge: clipboard + Claude Code headless, stream câu trả lời sang TTS

**Bối cảnh.** Xem Phase 4 trong PLAN.md. Hỏi bằng giọng nói, nghe Claude trả lời; câu đầu phải đến sớm nên không chờ Claude viết xong mới đọc.

**Kết quả (16/10/2026).**
- `server/bridge/`: interface `Bridge` (`ask` là async generator: `TextDelta` … `Done`, lỗi `BridgeError`), chọn bằng `[bridge] kind` (`claude_cli` / `clipboard` / `none`). Mặc định của `Config` là `none` để test không gọi Claude.
- `claude_cli.py`: `claude -p --output-format stream-json --include-partial-messages`, `--resume --fork-session` theo session của server, `--append-system-prompt` từ `prompts/voice_reply.md`, chỉ WebSearch/WebFetch. Chạy trong `_local/claude-workdir`; vì thư mục này nằm trong repo nên tắt CLAUDE.md, git status, memory qua `--settings` (đã kiểm: không tắt thì Claude đọc CLAUDE.md của repo). Không dùng `--bare` vì chế độ đó chỉ nhận API key. Huỷ thì kill tiến trình; có timeout.
- `clipboard.py`: đọc/ghi clipboard Win32 và hotkey toàn cục (`RegisterHotKey`) qua ctypes, không thêm package. Hotkey khi không có lượt nào chờ thì đọc clipboard ở tab mới nhất.
- `feeder.py`: cắt text stream ở dấu kết câu + khoảng trắng hoặc xuống dòng, không cắt trong code block, dừng ở `---CODE---`.
- Protocol: `ask`, `ask_stop`, `new_session`; server gửi `answer_start/delta/end`, `bridge_session`, state `WAITING_CLAUDE`. `speak_start` không còn danh sách câu: câu đến dần qua `speak_more`. Bắt đầu nói thì dừng cả hỏi lẫn đọc.
- Web: câu trả lời hiện dưới lượt nói, khung code riêng, số đo (text đầu, trả lời xong, chi phí, thả phím → nghe câu đầu); ô gõ tay có "Hỏi Claude", nút "Session mới".
- Log `_local/sessions/YYYY-MM-DD.jsonl`.
- Test: `tests/test_bridge.py` (feeder, parser stream-json, `ClaudeCliBridge` với tiến trình claude giả: resume, fork, lỗi, huỷ, timeout), bridge giả trong `tests/test_server.py`. 101 test.
- Đo (`bench/claude_ws_client.py`: server tự đọc câu hỏi thành audio rồi gửi như lượt nói, 5 câu): thả phím → nghe câu đầu p50 4.4s / max 4.6s; STT ~1s, Claude text đầu 3–4s. Câu 3 "dịch vụ đầu tiên mình hỏi" trả lời đúng EC2. Chi phí CLI báo $0.025 → $0.088 mỗi lượt, tăng dần theo độ dài session.
- Sửa sau khi người dùng thử trên web: lượt bị ngắt vẫn nằm trong session (`--resume` ghi tiếp vào cùng session id), nên lượt sau ("hello ?") Claude trả lời bù câu bị ngắt, đọc hơn 60 giây. Giờ mỗi lượt resume dùng `--fork-session`, và chỉ lượt trả lời xong mới thành session của lượt sau; lượt bị ngắt hoặc lỗi bị bỏ. Đã kiểm với Claude thật: ngắt câu lịch sử AWS rồi hỏi "hello ?" thì chỉ được một câu chào. Prompt thêm quy tắc: câu hỏi đơn giản trả lời một hai câu, chỉ trả lời câu mới nhất.
- Chọn model trên web: `set_model` / `bridge_model`, danh sách từ `[bridge] models`, tên model thật lấy từ event `init` và hiện trong số đo, ghi vào log. Thử một câu "EC2 là gì?" mỗi model (một mẫu, chưa phải benchmark): mặc định = claude-opus-5-5 text đầu 3.2s; sonnet 2.0s; haiku 4.1s. Chi phí CLI báo dao động theo cache ($0.009–0.031) nên một lượt không so được model nào rẻ hơn.

**Còn lại.**
- Thử bridge clipboard trọn vòng trên trình duyệt (nói → paste vào Claude → copy câu trả lời → Ctrl+Alt+R).
- Dùng thật 3 buổi tối, ghi `_local/feedback.md` ≥5 mục. Để ý: chi phí tăng theo session dài (bấm "Session mới" khi đổi chủ đề), câu cần WebSearch chậm hơn.
- Người dùng tự đọc điều khoản subscription cho cách dùng `claude -p` này.

## T-010: Trợ lý rảnh tay: gọi tên "Mimi", tab Mimi

**Bối cảnh.** Người dùng muốn giao diện như một trợ lý thật: mic luôn nghe, gọi "Mimi ơi" / "hey Mimi" / "ê Mimi" thì trợ lý đáp một câu ngẫu nhiên ("Có mình đây."), nói câu hỏi, ngừng nói thì trợ lý nghĩ rồi trả lời. Phần số đo, log lượt nói chuyển sang một tab riêng. Thay mục "VAD (Silero) cho chế độ rảnh tay" của Phase 5.

**Hướng làm (đã chốt).** Làm hai bước, có số đo mới đi tiếp.
- Bước A (đã làm): VAD streaming + Whisper không prompt tìm tên ở đầu câu. Không cần train gì, đổi tên chỉ cần sửa config. Mỗi đoạn nói lúc chờ tên tốn một lượt Whisper (~0.4s GPU).
- Bước B (chỉ làm nếu bước A không đạt DoD): model wake word riêng (openWakeWord, train từ giọng TTS tổng hợp) chạy trước Whisper. Rẻ hơn, nghe được cả lúc loa đang đọc, nhưng phải train lại khi đổi tên.

**Kết quả bước A (16/10/2026).**
- `server/assistant/vad.py`: `StreamingVad` chạy Silero v6 có sẵn trong faster-whisper (ONNX, cửa sổ 512 mẫu + 64 mẫu context, giữ trạng thái h/c giữa các frame). Đã kiểm trên 4 câu testset: xác suất khớp tuyệt đối với bản chạy cả file. `Segmenter` cắt đoạn theo ngưỡng trễ (bắt đầu p≥0.5, kết thúc khi p<0.35 liên tục `silence_ms`), có preroll 300ms, bỏ đoạn ngắn hơn 250ms, cắt ép ở 30s.
- `server/assistant/wake.py`: `WakeMatcher` bỏ dấu, gộp chữ lặp, y = i, rồi so gần đúng (difflib ≥0.8) với tên và alias. Tên chỉ tính khi đứng đầu câu, trước nó tối đa 2 từ gọi (hey, ê, này, alo...). Câu "hôm qua Mimi nói" không đánh thức. Tên không đưa vào `initial_prompt` vì prompt làm Whisper nghe ra tên khi không có. `decide()` chọn ask / ack / ignore, server và benchmark dùng chung.
- `server/handsfree.py`: máy trạng thái SLEEP → (gọi tên) → BUSY → ACTIVE → SLEEP.
  - Gọi tên kèm câu hỏi một hơi ("Mimi ơi, EC2 là gì") thì hỏi luôn, không đáp.
  - Chỉ gọi tên thì chờ thêm `ack_grace_ms` (300ms). Nếu nói tiếp trong lúc đó ("Mimi ơi … EC2 là gì", ngập ngừng làm VAD cắt hai đoạn) thì đoạn sau là câu hỏi; không thì đáp.
  - Đọc xong câu trả lời thì nghe thêm `follow_up_s` (6s) để hỏi tiếp không cần gọi tên. Im lặng `active_timeout_s` thì ngủ lại.
  - Chống loa dội vào mic: câu đáp không chứa tên. Mốc "loa phát xong" ước lượng từ lúc bắt đầu đọc + độ dài audio + 400ms. Đoạn nói bắt đầu trước mốc đó chỉ dùng để tìm tên (ngắt lời), không coi là câu hỏi.
  - Lúc đang đọc, rảnh tay không nhận dạng đoạn nói (VAD vẫn chạy) để GPU dành cho TTS; ngắt lời bằng Space / 🎤. Gọi tên lúc đang nghĩ (chưa đọc) vẫn đáp như bình thường. Giữ Space vẫn chạy như cũ, frame lúc giữ Space không vào bộ tìm tên.
- Protocol: client `listen {on, topic}`; server `listen(mode)`, `vad(speaking)`, `heard(text, wake, action, ack, stt_ms)`, `hello.assistant`. Log `_local/logs/wake-YYYY-MM-DD.jsonl`, audio của các đoạn có tên lưu ở `wake-audio/`.
- Web: hai tab. **Mimi** có quả cầu theo trạng thái (ngủ / đang nghe / đang nghĩ / đang nói, sáng theo âm lượng mic hoặc loa), câu vừa nghe, câu trả lời, nút bật rảnh tay (nhớ qua localStorage). **Chi tiết** là giao diện cũ, có thêm danh sách đoạn rảnh tay đã nghe.
- Bộ test: `data/wake_phrases.tsv` gồm 48 câu: `name` (chỉ gọi tên), `ask` (tên + câu hỏi), `near` (gần giống tên: "mì này", "Minh ơi", "Mỹ Mỹ ơi", nhắc tên giữa câu). Chia dev/holdout. Ghi bằng `bench/record.py --sentences data/wake_phrases.tsv --out data/wake_testset`. Ghi âm nền bằng `record.py --ambient <phút>`.
- `bench/wake_bench.py`: chạy đúng đường của server (VAD streaming → cắt đoạn → Whisper → match → decide) trên bộ gọi tên, 100 câu testset (âm tính) và âm nền. Báo theo nhóm/split: đúng, có tên, bị cắt; recall, nhận nhầm, nhận nhầm/giờ, STT p50/p95, VRAM.
- Đo trên 100 câu testset (chưa có bộ gọi tên): 0 lần nhận nhầm. STT tìm tên p50 425ms, p95 544ms. VRAM +3.3GB (cả quá trình). Bị cắt thành nhiều đoạn: 14% ở silence 500ms (lúc chờ tên), 1% ở 900ms (lúc nghe câu hỏi).
- Thử với server thật (TTS của server đọc câu, đẩy vào /ws): "Mimi ơi" → ack sau 300ms STT; câu không tên → ignore. "Hey Mimi" giọng TTS bị nghe thành "Hãy nhìn đi". Đây là audio tổng hợp nên chưa kết luận được, chờ số đo trên giọng thật.
- Test: `tests/test_assistant.py` (wake, VAD streaming, segmenter, luồng rảnh tay qua WebSocket với VAD/Whisper giả). 128 test.

**Đo trên giọng người dùng (16/10/2026, 48 câu + 100 câu testset, chưa có âm nền).** Chỉ đọc lỗi ở dev.

| Run | Recall (name+ask) | Nhận nhầm (near+stt) | name/dev | name/holdout | ask/dev | ask/holdout |
|---|---|---|---|---|---|---|
| baseline (difflib ratio) | 91% | 3% | 91% | 80% | 100% | 80% |
| maxlen (điểm = khớp / độ dài chuỗi dài hơn) | 88% | 2% | 82% | 80% | 100% | 80% |
| p_mimi (prompt "Mimi ơi.") | 84% | 2% | 82% | 60% | 100% | 80% |
| **p_hey (prompt "Hey Mimi. Mimi ơi.")** | **94%** | **2%** | 100% | 100% | 91% | 80% |

- Bug được sửa (maxlen): "Ê, mai mấy giờ họp" ghép "mai"+"mấy" thành "maimai", nhưng `ratio()` cho 0.8 nên bị nhận là tên. "Ok Mimi" (Whisper nghe "Ok mấy mày") trước đó trúng chỉ vì cùng lỗi này.
- Không prompt thì Whisper hay nghe sai tên ngắn ("mê mê", "mấy mày"). Prompt sửa được việc này, nhưng có một lỗi mới ở dev: "Mi ơi, đưa mình cái sạc" bị nghe thành "Mimi ơi" (prompt làm Whisper nghe ra tên).
- Chọn `wake_prompt = "Hey Mimi. Mimi ơi."`. Cần đo âm nền để kiểm nhận nhầm/giờ với prompt.
- Lỗi còn lại ở dev, không sửa:
  - "Mỹ Mỹ ơi" trùng tên sau khi bỏ dấu.
  - "Mi mi mi mi" đúng là âm của tên.
  - "Mimi ơi, nghe không?" ra `ask` vì sau tên có 2 từ (Claude sẽ đáp, chấp nhận được).

**DoD.** Đo bằng `wake_bench.py`. Riêng mục cuối đo trên trình duyệt.
- Gọi tên trên bộ gọi tên (dev + holdout): nhận đúng ≥ **95%**.
- Nhận nhầm: 0 trên `near` + 100 câu testset, ≤ **1 lần/giờ** âm nền.
- Ngừng gọi tên → nghe câu đáp ≤ **1.5s** (500ms im lặng + ~0.4s STT + 300ms chờ + tiếng đầu TTS).
- Câu hỏi bị cắt giữa chừng ≤ **1/20** lượt.
- Ngắt lời: DoD "≤200ms" của Phase 5 tính cho nút bấm/phím Esc. Ngắt bằng giọng phải chờ hết câu gọi tên + STT, mục tiêu ≤ **1.5s**.

**Lệnh giọng nói (16/10/2026).** Khi dùng thật, người dùng nói "mở session mới đi" và Claude chỉ bảo gõ `/clear`, vì Claude không điều khiển được app. Giờ `server/assistant/commands.py` bắt các câu ngắn ("mở session mới", "dừng lại", "đọc lại") trong `start_ask` và chạy ngay trên server, không gửi cho Claude. Web nhận message `command`. Câu dài vẫn hỏi Claude: "đọc lại câu vừa rồi chậm hơn được không" không bị coi là lệnh.

**Còn lại.**
- Người dùng ghi bộ gọi tên (48 câu, khoảng 5 phút) và ít nhất 30 phút âm nền, rồi chạy `wake_bench.py`. Chỉ sửa alias/ngưỡng theo lỗi ở dev.
- Thử trên trình duyệt với loa ngoài, không tai nghe. Nếu tiếng loa vẫn đánh thức nhầm hoặc bị nhận thành câu hỏi thì cân nhắc bước B.
- Chưa có: đổi tên trợ lý trên web, nhận diện giọng người nói.
