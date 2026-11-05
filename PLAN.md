# Voice Study Assistant — Plan

Lớp Voice I/O (STT + TTS) chạy local, bọc quanh Claude. Claude lo chat/research, project này lo nghe và đọc.

> **Bản chi tiết** (gợi ý kỹ thuật, rủi ro, điểm quyết định, checkbox theo dõi tiến độ): mở [docs/plan.html](docs/plan.html) bằng trình duyệt.
> **Việc đang mở:** [docs/tickets.md](docs/tickets.md).

```text
🎤 → Web (push-to-talk) → Python Voice Server → STT → text
                                                   ↓
                                           Claude bridge
                                                   ↓
🔊 ← Web (audio queue) ← TTS ← normalize ← response text
```

**Máy:** RTX 3060 12GB, 64GB RAM, Python 3.11, Node 24.

**Nguyên tắc chung**
- Đo trước, tối ưu sau. Mọi quyết định chọn model đều dựa trên bộ test của Phase 0.
- Claude bridge là lớp mỏng nhất, interface `ask(text) -> text`, thay được bằng LLM khác.
- Các con số trong DoD là mục tiêu ban đầu. Sau khi có baseline ở Phase 1 thì chỉnh lại cho thực tế.

---

## Cấu trúc thư mục dự kiến

```text
AI-assistent/
├── PLAN.md
├── server/                 # Python voice server
│   ├── stt/                # VAD, transcription, glossary, post-correction
│   ├── tts/                # normalize, sentence split, synthesis
│   ├── bridge/             # clipboard, claude_cli
│   ├── agent/              # Phase 9: danh bạ dự án, mở VS Code, theo dõi log phiên Claude Code
│   └── app.py              # FastAPI + WebSocket
├── web/                    # Frontend (React hoặc HTML thuần)
├── bench/                  # Script benchmark STT/TTS
├── data/
│   ├── glossary.txt        # Thuật ngữ kỹ thuật
│   └── testset/            # wav + transcripts.jsonl (không commit)
├── voice/                  # Phase 6: dataset giọng + model clone (không commit audio/weights)
├── models/                 # Model weights tải về / fine-tune (không commit)
└── _local/                 # File nháp, kết quả benchmark (không commit)
```

**Một repo cho toàn bộ dự án.** Commit code, config, glossary, plan và báo cáo. Không commit audio, model weights, log session (dùng `.gitignore` cho `data/testset/`, `models/`, `voice/**/*.wav`, và `.git/info/exclude` cho `_local/`).

---

## Phase 0 — Setup & bộ dữ liệu test

**Mục tiêu:** có môi trường chạy được GPU và bộ test thật để đo mọi thứ về sau.

### TODO
- [ ] Tạo cấu trúc thư mục, `git init`, thêm `_local/` và `data/testset/` vào `.git/info/exclude`
- [ ] Tạo venv Python 3.11, cài PyTorch bản CUDA, kiểm tra `torch.cuda.is_available()`
- [ ] Viết `data/glossary.txt`: 100–200 thuật ngữ hay dùng (AWS, .NET, networking, DB...)
- [ ] Soạn 100 câu hỏi thực tế trong `data/sentences.tsv` (chủ đề: AWS, dev/.NET, TOEIC, IELTS), chia nhóm:
  - `vi` 20 câu tiếng Việt thuần
  - `mix` 40 câu Việt–Anh lẫn (code-switching), gồm cả câu hỏi từ vựng/ngữ pháp tiếng Anh
  - `dense` 20 câu nhiều thuật ngữ dồn dập / từ viết tắt (EC2, S3, DI, EF Core...)
  - `en` 20 câu **tiếng Anh hoàn toàn** (luyện IELTS/TOEIC, hỏi bằng tiếng Anh)
  - Chia `dev` 70 câu / `holdout` 30 câu
- [ ] Viết script ghi âm `bench/record.py`: hiện từng câu, bấm phím để ghi, lưu `wav 16kHz mono`
- [ ] Ghi âm đủ 100 câu bằng **tai nghe** (micro sẽ dùng thật), môi trường thật (phòng học buổi tối)
- [ ] Lưu `transcripts.jsonl`: `{id, file, text, terms: [...], group, split}`

### DoD
- [ ] `torch.cuda.is_available()` trả về `True` trong venv
- [ ] Có 100 file wav + `transcripts.jsonl` khớp 1-1, mỗi câu có danh sách `terms` đã gán
- [ ] Nghe lại ngẫu nhiên 10 file: không bị cắt đầu/cuối, không rè
- [ ] Glossary có ít nhất 100 thuật ngữ

---

## Phase 1 — Benchmark STT

**Mục tiêu:** chọn được cấu hình STT tốt nhất cho tiếng Việt + thuật ngữ tiếng Anh, có số liệu.

### TODO
- [x] Viết `bench/stt_bench.py`: chạy 1 cấu hình trên toàn bộ testset, xuất CSV từng câu + tổng
- [x] Metric:
  - WER (sau khi chuẩn hoá: lowercase, bỏ dấu câu)
  - **Term recall** = số thuật ngữ nhận đúng / tổng thuật ngữ trong `terms`
  - RTF (thời gian xử lý / thời lượng audio) và VRAM đỉnh
- [x] Chạy các cấu hình:
  - [x] faster-whisper `large-v3`, int8, không prompt (baseline)
  - [x] faster-whisper `large-v3`, int8, `initial_prompt` = glossary rút gọn
  - [x] faster-whisper `large-v3-turbo` (có/không prompt)
  - [x] PhoWhisper (large)
  - [x] `language="vi"` cố định vs tự nhận diện vs hai lượt (detect rồi mới nhận dạng). **Quan trọng vì có nhóm `en`**: ép `vi` có thể làm hỏng câu tiếng Anh hoàn toàn
  - [x] Thêm: prompt theo chủ đề (glossary chia 4 chủ đề), chọn chủ đề sẵn (oracle) hoặc đoán từ lượt 1 (two_pass)
- [x] Viết post-correction dictionary từ các lỗi lặp lại, chạy lại đo. Kết quả: dev không có lỗi nào lặp ≥2 lần với prompt theo chủ đề, nên chưa thêm rule. Xây lại từ log thật ở Phase 2
- [x] Liệt kê top 20 thuật ngữ hay sai nhất
- [x] Viết báo cáo ngắn `_local/stt_report.md`: bảng so sánh + lựa chọn + lý do

**Kết quả:** chọn `large-v3`, ép `vi`, prompt theo chủ đề. Chi tiết trong `_local/stt_report.md`.

### DoD
- [x] Bảng so sánh ít nhất 4 cấu hình trên cùng testset, cùng metric (15 cấu hình)
- [x] Cấu hình được chọn đạt (mục tiêu ban đầu → đo được với `v3_vi_topic_oracle`; chốt 15/10/2026, T-001):
  - Term recall ≥ **90%** trên nhóm code-switching → 88.8% (holdout 89.3%). Chấp nhận: chênh lệch nằm trong sai số của holdout 30 câu
  - WER ≤ **15%** toàn bộ → 9.2% ✅; WER nhóm `en` ≤ ~~10%~~ **15%** → 14.9% ✅ (đã hạ mục tiêu). Lỗi do phát âm ("word"→"world", "chart"→"chair"), không phải do ép `vi`: ép `en` cũng ra 14.9%
  - RTF ≤ **0.3** trên RTX 3060 → 0.11 ✅
  - VRAM ≤ **5GB** → 2.4GB ✅
- [x] Quyết định rõ ràng: **có cần fine-tune hay không** → **Không** (term recall 87% > ngưỡng 80%)

---

## Phase 2 — Voice server + Web push-to-talk (STT end-to-end)

**Mục tiêu:** nói vào trình duyệt, thấy text hiện ra.

### TODO
**Server**
- [x] FastAPI + WebSocket endpoint `/ws`, thêm `/health` (`server/app.py`, chạy `python -m server`)
- [x] Load model STT 1 lần lúc khởi động, có warm-up, không load lại mỗi request
- [x] Nhận audio PCM 16kHz mono, transcribe, áp post-correction, trả text (`server/stt/service.py`, prompt theo chủ đề, không chọn chủ đề thì two_pass)
- [x] Định nghĩa protocol message (JSON): `hello`, `start`, `stop`, `cancel`, `correction`, `state`, `transcript`, `error` (`server/protocol.py`)
- [x] Log mỗi lượt: thời lượng audio, thời gian STT, chủ đề, text, wav, bản sửa tay (`_local/logs/`, T-003)

**Web**
- [x] Trang tối giản: trạng thái kết nối, trạng thái lượt, mức mic, chọn chủ đề, nút giữ để nói, lịch sử phiên
- [x] `getUserMedia` + AudioWorklet → resample 16kHz → frame 100ms qua WebSocket, pre-roll 300ms
- [x] Push-to-talk: giữ phím Space (hoặc giữ nút) để nói, thả ra để gửi, `Esc` để huỷ
- [x] State machine phía client: `IDLE → LISTENING → TRANSCRIBING → IDLE`, thêm `ERROR`
- [x] Hiện transcript và cho phép sửa tay (bản sửa ghi log làm nguồn cho corrections)
- [x] Trang "Đề xuất học" (`web/learn.html`): lỗi lặp ≥2 lượt dev thành đề xuất thuật ngữ/rule, kiểm tra tự động trên bộ test, người dùng duyệt thì áp dụng ngay (T-007)

### DoD
- [ ] Chạy `server` + mở web, nói 10 câu từ testset, transcript khớp chất lượng benchmark Phase 1
  - Đẩy audio testset qua `/ws` (`bench/ws_client.py`, 100 câu): WER 9.2%, term recall 87.8%, khớp benchmark (9.2% / 87.2%). Còn chờ nói thật qua trình duyệt
- [ ] Độ trễ từ lúc thả phím đến lúc thấy text ≤ **2 giây** với câu ~10 giây
  - Qua `ws_client.py`: p50 552ms, p90 653ms, max 839ms. Còn chờ đo trên trình duyệt
- [ ] Reload trang / mất kết nối WebSocket thì tự kết nối lại, không phải restart server
- [ ] Server chạy liên tục 1 giờ, nói 50 lượt, không leak VRAM (VRAM ổn định)
  - 220 lượt liên tiếp qua `ws_client.py`: VRAM 4.30GB → 4.29GB. Còn chờ chạy đủ 1 giờ

---

## Phase 3 — TTS

**Mục tiêu:** đọc câu trả lời tiếng Việt có thuật ngữ tiếng Anh tự nhiên, nghe dễ chịu.

### TODO
**Chuẩn bị**
- [x] Soạn 30 đoạn văn mẫu kiểu câu trả lời của Claude (có thuật ngữ, viết tắt, số, markdown sót) — 35 đoạn, `bench/tts_samples.txt`

**Normalize**
- [x] Bỏ markdown (`**`, `#`, `-`, bảng, link)
- [x] Code block: thay bằng "đoạn code trên màn hình"; phần sau dòng `---CODE---` không đọc
- [x] Từ điển phát âm (`data/lexicon.tsv`) cho viết tắt/thuật ngữ: `EC2 → "i xi tu"`, `.NET → "dot net"`, `S3 → "ét ba"`... (thử nghiệm cách đọc nào nghe tự nhiên)
- [x] Đọc số, đơn vị, phiên bản (`v3.11`, `12GB`, `99.9%`), ngày giờ, giá mỗi đơn vị

**Synthesis**
- [x] Xử lý **câu tiếng Anh hoàn toàn** (ví dụ mẫu IELTS, câu sửa ngữ pháp): model song ngữ, hoặc nhận diện ngôn ngữ từng câu rồi chuyển sang giọng tiếng Anh — nhận diện từng câu (`detect_lang`); ZeroTTS đọc được cả hai, nhóm en 4.6/5
- [x] Benchmark ít nhất 2–3 model TTS tiếng Việt chạy local (ví dụ các bản fine-tune tiếng Việt của XTTS / F5-TTS, model TTS tiếng Việt mới tại thời điểm làm) — VieNeu, OmniVoice (fine-tune vi), ZeroTTS; `bench/tts_bench.py`
- [x] Chấm điểm thủ công 30 đoạn mẫu (1–5): độ tự nhiên, phát âm thuật ngữ, ngắt nghỉ
- [x] Sentence splitter: tách câu, gộp câu quá ngắn, cắt câu quá dài
- [x] Synthesize theo từng câu, stream chunk về web
- [x] Web: audio queue phát tuần tự, **dừng được ngay** khi bấm ⏹ (cả Esc, và khi bắt đầu nói)

**Prompt cho Claude**
- [x] Viết instruction "trả lời văn nói, không markdown, không code block, ~150 từ" cho Claude Project — `prompts/voice_reply.md`

### DoD
- [x] Có bảng điểm các model TTS trên 30 đoạn mẫu, chọn được 1 model — ZeroTTS GPU, 4.37/5 (T-008)
- [x] Điểm trung bình model được chọn ≥ **3.5/5** về phát âm thuật ngữ — 4.2 (nhóm term)
- [x] Thời gian từ lúc có text đến khi nghe câu đầu tiên ≤ **1.5 giây** — p50 81ms, max 342ms (`bench/tts_ws_client.py`)
- [x] Không có khoảng lặng > 0.5 giây giữa các câu khi đọc đoạn dài — hụt audio max 122ms (ngoài 0.15s nghỉ giữa câu)
- [x] Bấm ⏹ thì dừng trong ≤ **200ms** — web tắt tiếng ngay (đã nghe thử 16/10/2026); server nhả GPU sau ~290ms
- [x] STT + TTS cùng load trên GPU, tổng VRAM ≤ **11GB** — đỉnh cả GPU 5.9GB

---

## Phase 4 — Claude bridge (chạy trọn vòng)

**Mục tiêu:** hỏi bằng giọng nói, nghe Claude trả lời, dùng được hằng ngày.

### TODO
**4a — Clipboard bridge (làm trước)**
- [x] Interface chung `Bridge.ask(text)` dạng stream (`server/bridge/base.py`): yield `TextDelta`, cuối cùng `Done`
- [x] STT xong → tự copy transcript vào clipboard → bạn paste vào claude.ai (`[bridge] kind = "clipboard"`)
- [x] Hotkey toàn cục `Ctrl+Alt+R`: đọc text đang có trong clipboard → normalize → TTS (đã kiểm đăng ký hotkey, đọc/ghi clipboard; chưa bấm thử trọn vòng)
- [x] State `WAITING_CLAUDE` hiện trên UI trong lúc chờ

**4b — Claude Code headless**
- [x] `ClaudeCliBridge`: gọi `claude -p "<text>" --output-format stream-json`, lấy `session_id`. Chạy trong `_local/claude-workdir`, chỉ cho WebSearch/WebFetch, tắt CLAUDE.md/git/memory của repo qua `--settings`
- [x] Các lượt sau dùng `--resume <session_id> --fork-session` để giữ context; lượt bị ngắt hoặc lỗi không thành context của lượt sau
- [x] Append prompt cho văn nói (`prompts/voice_reply.md`, `--append-system-prompt`)
- [x] Bật WebSearch cho câu hỏi cần research
- [x] Stream response → cắt câu dần (`server/bridge/feeder.py`) → đẩy từng câu sang TTS ngay khi có
- [x] Nút "Session mới" trên UI = bỏ `session_id`
- [ ] Đọc lại điều khoản sử dụng subscription cho cách dùng này (người dùng tự đọc)
- [x] Lưu log session ra file (`_local/sessions/*.jsonl`): câu hỏi, response, session_id, thời gian text đầu / trả lời xong, chi phí, lỗi
- [x] Dừng giữa chừng (Esc / ⏹ / bắt đầu nói) thì kill tiến trình `claude`: answer_end sau ~25ms, không sót tiến trình
- [x] Ô gõ tay: "Hỏi Claude" (Ctrl+Enter) hoặc "Đọc"; phần sau `---CODE---` hiện trong khung code, không đọc

### DoD
- [ ] **4a:** Một lượt hỏi–đáp hoàn chỉnh chỉ cần: giữ Space nói → paste → bấm hotkey (chưa thử trên trình duyệt)
- [x] **4b:** Một lượt hỏi–đáp hoàn chỉnh không cần chạm chuột/bàn phím ngoài push-to-talk (`auto_ask = true`; đo qua `bench/claude_ws_client.py`)
- [x] **4b:** Câu hỏi thứ 3 trong session tham chiếu được câu thứ 1 — "dịch vụ đầu tiên mình hỏi" → trả lời về EC2
- [x] **4b:** Thời gian từ thả phím đến nghe câu đầu tiên ≤ **8 giây** — p50 4.4s, max 4.6s (5 câu, 16/10/2026; Claude text đầu 3–4s, STT ~1s)
- [x] Đổi bridge (clipboard ↔ CLI) chỉ cần đổi `[bridge] kind` trong `server/config.toml`, không sửa code STT/TTS
- [ ] **Dùng thật 3 buổi tối học liên tiếp**, ghi lại các điểm khó chịu vào `_local/feedback.md`

---

## Phase 5 — Hoàn thiện trải nghiệm

**Mục tiêu:** sửa những gì Phase 4 "dùng thật" chỉ ra. Ưu tiên theo `feedback.md`, không làm hết.

### TODO (chọn theo feedback)
- [ ] **Ngắt lời (barge-in):** bấm 🎤 khi đang SPEAKING → dừng đọc ngay, bắt đầu nghe
- [x] **Trợ lý rảnh tay (T-010):** mic luôn nghe (VAD Silero streaming), gọi tên "Mimi ơi" / "hey Mimi" thì đáp, ngừng nói thì trả lời; tab Mimi + tab Chi tiết. Bước A (Whisper tìm tên) xong; bước B (openWakeWord) chỉ làm nếu A không đạt DoD
- [x] Ghi bộ test gọi tên (`data/wake_phrases.tsv`, 48 câu)
- [ ] Ghi ≥30 phút âm nền, đo bằng `bench/wake_bench.py`
- [x] **Tab Học bài (T-012, 04/11/2026):** dán text hoặc chọn file .txt/.md/.docx, Mimi đọc hết tài liệu, chia 3–8 phần, giảng từng phần bằng giọng nói, hết phần thì dừng hỏi; "tiếp", "giảng lại", "kiểm tra mình", hỏi chen giữa chừng. Thư viện lưu ở `_local/study/`, mỗi tài liệu một session Claude riêng, mở lại thì học tiếp
- [x] Lệnh giọng nói (`server/assistant/commands.py`): "mở session mới", "dừng lại", "đọc lại" chạy ngay trên server, không hỏi Claude. Chỉ câu ngắn mới là lệnh
- [ ] Bổ sung glossary + post-correction từ các lỗi STT gặp khi dùng thật (công cụ có rồi: trang Đề xuất học, T-007)
- [ ] Ghi thêm 50–100 câu từ log thật vào testset, chạy lại benchmark (công cụ có rồi: `bench/build_personal_set.py` + `stt_bench.py --testset`, T-007)
- [ ] ~~Fine-tune STT~~ → chuyển sang Phase 8 (tự học dài hạn), làm khi đủ audio đã sửa
- [ ] Chỉnh tốc độ đọc, nút "đọc lại", "đọc chậm"
- [x] Script khởi động 1 lệnh (server + mở web): `start.bat` = `python -m server --open`; server đã chạy thì chỉ mở trình duyệt (18/10/2026, chưa bấm đúp thử)

### DoD
- [ ] Mỗi mục được làm có số liệu trước/sau (term recall, độ trễ, hoặc điểm cảm nhận)
- [ ] Term recall trên testset mở rộng ≥ **95%**
- [ ] Ngắt lời: từ lúc bấm đến khi dừng đọc ≤ **200ms**
- [ ] Rảnh tay (`wake_bench.py`): gọi tên nhận đúng ≥ **95%**; nhận nhầm 0 trên câu gần giống + testset, ≤ **1 lần/giờ** âm nền; câu hỏi bị cắt giữa chừng ≤ **1/20**; ngừng gọi tên → nghe câu đáp ≤ **1.5s**
- [ ] ~~Ngắt lời bằng giọng (gọi tên lúc đang đọc)~~ bỏ 17/10/2026: lúc đang đọc không nhận dạng để TTS hết giật; ngắt bằng nút/phím (mục 200ms ở trên)
- [ ] Khởi động toàn bộ hệ thống bằng 1 lệnh

---

## Phase 6 — Giọng của chính mình (tuỳ chọn)

**Mục tiêu:** trợ lý đọc bằng giọng của bạn.

### TODO
- [ ] Ghi âm dataset giọng: 30–60 phút, câu đa dạng, có thuật ngữ tiếng Anh, phòng yên tĩnh
- [ ] Làm sạch: cắt câu, chuẩn hoá âm lượng, transcript chính xác
- [ ] Thử zero-shot clone trước (vài giây audio tham chiếu) với model đã chọn ở Phase 3
- [ ] Nếu zero-shot chưa đủ → fine-tune model TTS trên dataset giọng mình
- [ ] So sánh mù: giọng clone vs giọng mặc định trên 30 đoạn mẫu Phase 3

### DoD
- [ ] Điểm phát âm thuật ngữ của giọng clone không thấp hơn giọng mặc định
- [ ] Độ trễ câu đầu tiên vẫn ≤ **1.5 giây**
- [ ] Nghe 10 đoạn liên tục không thấy méo tiếng, lạc giọng, hoặc đọc sai ngôn ngữ
- [ ] Dataset giọng và model nằm trong `voice/` (thư mục riêng trong cùng repo)

---

## Phase 7 — Dùng từ mọi thiết bị

**Mục tiêu:** điện thoại, laptop, tablet đều dùng được trợ lý; model vẫn chạy trên máy RTX 3060 ở nhà.

> Ảnh hưởng ngay từ bây giờ: web phải **responsive** và có **nút giữ để nói** (điện thoại không có phím Space). Clipboard bridge không dùng được trên điện thoại, nên Phase 4b là bắt buộc nếu muốn dùng từ xa.

### TODO
- [x] Bước 0 — **Cùng Wi-Fi**: cổng HTTPS 8766 (chứng chỉ tự ký), điện thoại mở `https://192.168.1.199:8766`; không đăng nhập vì chỉ trong LAN (17/10/2026)
- [ ] Bước 1 — **Mạng riêng (Tailscale)**: chỉ thiết bị của mình truy cập được, có HTTPS (`tailscale serve`)
- [ ] Bước 2 (nếu thật sự cần public) — Cloudflare Tunnel + Cloudflare Access (đăng nhập bằng email), không mở port router
- [ ] Bắt buộc **xác thực** trước WebSocket: server gọi được Claude và chạy GPU, để lộ là bị dùng chùa
- [ ] Rate limit + giới hạn độ dài audio mỗi lượt
- [ ] Web trên điện thoại: nút giữ để nói, phát audio sau lần chạm đầu tiên (chính sách autoplay của iOS/Android)
- [x] Màn hình không tắt khi đang rảnh tay / chờ Claude / đang đọc (Wake Lock, 18/10/2026)
- [x] PWA: `web/manifest.json`, icon, `web/sw.js` (chỉ hiện `offline.html` khi mất kết nối, không cache CSS/JS); qua cổng 8766 cần cài `/lan.crt` làm CA trên điện thoại (18/10/2026)
- [ ] Audio qua mạng: nén (Opus) thay vì PCM thô nếu mạng 4G chậm
- [ ] Server tự chạy khi bật máy (Windows service / Task Scheduler)

### DoD
- [ ] Từ điện thoại dùng 4G (không chung Wi-Fi): hỏi–đáp trọn vòng được
- [ ] Truy cập không đăng nhập → bị từ chối ở cả HTTP lẫn WebSocket
- [ ] Độ trễ thả nút → nghe câu đầu không tệ hơn dùng tại máy quá **1.5 giây**
- [ ] Khởi động lại máy → server tự lên, không cần thao tác

---

## Phase 8 — Tự học dài hạn

**Mục tiêu:** trợ lý tốt lên theo thời gian dùng. Hệ thống tự gom tín hiệu lỗi khi dùng thật, tự đề xuất và tự kiểm tra; người dùng chỉ duyệt. Làm song song với Phase 5–7.

> Mọi vòng học đi theo cùng một đường: gom tín hiệu → đề xuất → kiểm tra bằng số đo (nếu đo được) → duyệt → áp dụng → đo lại định kỳ. Giữ chia dev/holdout như Phase 1: chỉ lượt dev sinh đề xuất.

### TODO
**A. Cách đọc của TTS** (làm trước: lỗi nghe thấy rõ nhất, vd "TODO" bị đánh vần thành "ti ô đi ô")
- [x] Thêm vào `data/lexicon.tsv` các từ viết hoa đọc như từ thường: TODO, FIXME, README, PLAN.md, CLAUDE.md (17/10/2026)
- [ ] Ghi log các từ TTS phải đoán cách đọc: chữ in hoa không có trong lexicon (đang bị đánh vần), từ tiếng Anh không có trong lexicon
- [ ] Tab "Cách đọc" trên trang Đề xuất học: từ gặp nhiều lần → nghe thử, sửa cách đọc, chọn "đọc như từ" / "đánh vần"; duyệt thì ghi vào lexicon (TTS tự nạp lại)
- [ ] Bấm vào một từ trong câu trả lời để sửa cách đọc, nghe lại ngay
- [ ] Gợi ý phiên âm cho cả đợt từ mới bằng Claude, người dùng duyệt
- [ ] Mỗi tháng: thêm câu trả lời thật vào bộ mẫu TTS, chấm mù lại bằng `bench/tts_bench.py`

**B. STT** (mở rộng vòng tự học T-007)
- [ ] Đánh dấu lượt nghi sai: Claude hỏi lại kiểu "mình chưa nghe rõ…", hoặc người dùng nói lại câu gần giống trong vài giây; đưa vào hàng chờ sửa một chạm
- [ ] Lượt đã sửa kèm audio tự vào bộ test cá nhân (`bench/build_personal_set.py`); chạy `stt_bench.py` định kỳ (mỗi tuần hoặc mỗi 50 bản sửa) lúc server rảnh, không chạy hai job GPU cùng lúc
- [ ] Biểu đồ tiến bộ trên trang Đề xuất học: WER cá nhân theo tuần, số lỗi đã học
- [ ] Fine-tune LoRA large-v3 khi có ≥ 2–3 giờ audio đã sửa ở dev (chuyển từ Phase 5); thử trước xem có vừa 12GB VRAM không
- [ ] Kịch bản ghi âm cho LoRA lấy từ prompt người dùng đã gõ trong Claude Code (`~/.claude/projects/*/*.jsonl`, ~500 câu trộn Anh–Việt đúng văn phong), 40–50% câu trộn; giữ nhóm `en` để kiểm tra không tụt tiếng Anh

**C. Gọi tên Mimi**
- [ ] Gom audio gọi tên thật (`_local/logs/wake-*.jsonl`) thành bộ gọi tên lớn dần
- [ ] Đủ vài trăm mẫu + âm nền: train openWakeWord riêng cho "Mimi" (bước B của T-010), so với bước A bằng `bench/wake_bench.py`

**D. Hiểu ý giao việc** (Phase 9; dự án mới lúc đầu phải dạy, sau đó tự nhận)
- [x] Nút "Sai rồi" trên tab Công việc: chọn lại dự án đúng / sửa câu yêu cầu → ghi nhãn (audio, câu nghe được, dự án đúng, yêu cầu đúng) vào `_local/logs/intents-*.jsonl`
  - 01/11/2026: nút "Sai dự án" trên thẻ việc (message `task_fix`) giao lại ở dự án đúng. Nhãn có `turn_id` trỏ về `turns-*.jsonl` (audio, câu nghe được), `heard`, `query`, `project`, `wrong`, `request`, `via`
  - Trả lời khi Mimi hỏi lại: "ừ / đúng rồi" (một ứng viên), "cái thứ hai", "không phải" rồi nói tên, hoặc bấm nút ứng viên (`command.choices`). Nói "sai rồi" / "không phải, X cơ" trong 2 phút sau khi giao việc: thôi theo dõi việc đó, hỏi lại hoặc giao sang X
- [ ] Nhãn ngầm, không cần bấm: chọn ứng viên khi Mimi hỏi lại, ngừng theo dõi trong 30 giây đầu, nói lại câu gần giống ngay sau đó (01/11/2026: đã có nhãn khi chọn ứng viên / nói tên lúc Mimi hỏi lại; còn ngừng theo dõi sớm, nói lại câu gần giống)
- [ ] Tự sinh biệt danh dự án (`_local/projects.toml`) khi cùng một cách nghe sai trỏ về cùng một dự án ≥ 2 lần ở dev, như rule corrections
- [ ] `bench/intent_bench.py`: chấm kho nhãn theo dev/holdout (đúng dự án, đúng loại câu: việc / hỏi / tiến độ); mọi thay đổi alias, rule, prompt chỉ áp dụng khi không tụt
- [ ] Bộ luật không chắc thì hỏi LLM nhỏ (Claude Haiku): danh sách dự án + vài ví dụ gần nhất lấy từ kho nhãn; LLM cũng không chắc thì Mimi hỏi lại
- [ ] Job học hằng đêm lúc server rảnh: gom nhãn mới → đề xuất → chạy bench → tự áp dụng thứ qua kiểm tra, thứ chưa chắc đưa lên trang Đề xuất học
- [ ] Số theo ngày trên trang Đề xuất học: tỉ lệ đúng dự án, số lần phải sửa trên 10 việc

### DoD
- [ ] Từ đã duyệt cách đọc thì không bị đọc sai lại ở lần chấm sau
- [ ] Từ TTS phải đoán xuất hiện ≥ 3 lần đều hiện trên tab Cách đọc
- [ ] WER cá nhân trên holdout sau 1 tháng dùng thấp hơn lúc bắt đầu, có số theo từng tuần
- [ ] Mọi thay đổi được áp dụng (lexicon, glossary, rule, model) có số trước/sau và gỡ lại được
- [ ] Duyệt một đề xuất ≤ **2 chạm**
- [ ] Giao việc: số lần phải sửa trên 10 việc giảm theo tuần; dự án mới nhận đúng sau ≤ **2** lần sửa

---

## Phase 9 — Giao việc cho VS Code

**Mục tiêu:** nói "Mimi ơi, mở dự án job hay rồi thêm phân trang cho trang danh sách" → Mimi tìm đúng thư mục dự án, mở VS Code ở đó, đưa yêu cầu vào ô chat Claude Code, theo dõi tiến độ, xong thì đọc báo cáo ngắn. Chi tiết: [docs/tickets.md](docs/tickets.md) T-011.

> Mimi không tự sửa code. Claude Code trong VS Code làm việc, người dùng vẫn duyệt quyền như bình thường. Mimi chỉ mở đúng chỗ, giao việc, đọc log phiên (`~/.claude/projects/<slug>/<session>.jsonl`) để báo tiến độ.

### TODO
**A1. Danh bạ dự án + lệnh "mở dự án X"**
- [x] `server/agent/projects.py`: quét `[agent] roots` (mỗi thư mục con là một dự án), thêm dự án từng mở bằng Claude Code, biệt danh/loại trừ trong `_local/projects.toml`; tìm gần đúng theo tên đọc lên ("job hay" → `job-hay`)
- [x] `server/agent/intent.py`: tách câu nói thành dự án + yêu cầu; câu hỏi kiến thức ("dự án là gì") vẫn đi tới Claude
- [x] Hai dự án giống nhau hoặc không tìm thấy: Mimi hỏi lại / báo không tìm thấy, không mở bừa

**A2. Giao việc + theo dõi + báo cáo**
- [x] `server/agent/vscode.py`: mở VS Code ở thư mục dự án, mở tab Claude bằng URI `vscode://anthropic.claude-code/open?prompt=...` (chỉ điền sẵn), tự nhấn Enter khi cửa sổ đúng dự án ở trên cùng (`auto_submit`, tắt được)
- [x] `server/agent/watcher.py`: đọc tăng dần log phiên, nhận ra lượt vừa gửi, báo từng bước tool, "chờ duyệt hoặc chạy lâu" khi tool chưa xong quá `stall_s`, kết thúc khi `end_turn`
- [x] `server/agent/tasks.py`: `TaskManager` điều phối, broadcast cho mọi tab, đọc báo cáo bằng giọng khi tab rảnh, log `_local/tasks`
- [x] Web: tab **Công việc** (chọn dự án + gõ yêu cầu, thẻ task có timeline bước, báo cáo, ngừng theo dõi, mở lại phiên trong VS Code); thẻ task trong chat Mimi

**A2b. Giao việc bằng giọng nói ở tab Công việc**
- [x] Đang ở tab Công việc (message `work`): mọi câu nói là giao việc, không hỏi Claude của Mimi; câu không nêu tên dự án thì làm ở dự án đang chọn, không chọn thì dự án của việc gần nhất, chưa có thì Mimi hỏi "làm ở dự án nào?" rồi nhận câu trả lời tên dự án
- [x] Tên dự án mơ hồ: Mimi hỏi lại, câu trả lời kế tiếp (trong 90 giây) chọn dự án và giữ yêu cầu cũ
- [x] Hỏi tiến độ ("tới đâu rồi", "xong chưa") và "ngừng theo dõi" bằng giọng nói, ở mọi tab khi đang có việc chạy
- [x] Web: thanh giọng nói đầu tab (orb nhỏ, nút Rảnh tay, chọn dự án mặc định, câu bạn nói và câu Mimi đáp); form gõ tay thu vào "Gõ tay"
- [x] STT nghe đúng tên dự án: ở tab Công việc, prompt Whisper thêm "Dự án: job hay, fruit rush, …" (30 dự án dùng gần nhất + biệt danh); câu thiếu từ "dự án" vẫn nhận tên ở bất kỳ đâu trong câu khi khớp chắc (≥ 0.85)
- [x] Sửa theo lần thử thật 01/11/2026: (1) lượt dò tên đã nghe "Mimi" thì luôn bỏ tên khỏi câu, kể cả khi lượt nhận dạng chính viết tên khác ("Nhiên mê ơi, mở diễn."); (2) bỏ câu Whisper bịa khi audio là tiếng ồn ("Cảm ơn các bạn đã theo dõi…", "Hãy subscribe cho kênh…"); (3) câu ngắn "mở X" mà X không khớp chắc dự án nào thì hỏi lại kèm ứng viên ("guitar karaoke?"), không giao cho dự án đang chọn

**Để sau**
- [ ] A3: Mimi tự làm (Claude headless trong thư mục dự án, duyệt quyền qua web/giọng nói)
- [ ] A4: điều khiển máy ngoài VS Code (mở app, trình duyệt...)

### DoD
- [ ] Thử thật: nói "mở dự án X rồi <yêu cầu chỉ đọc>" → VS Code mở đúng dự án, yêu cầu được gửi, tab Công việc hiện các bước, nghe báo cáo
- [ ] Tìm đúng dự án ≥ **9/10** câu nói thật có tên dự án
- [ ] Từ lúc nói xong đến lúc yêu cầu vào ô chat ≤ **10s** khi VS Code chưa mở
- [ ] Không gửi Enter nhầm cửa sổ: cửa sổ trên cùng không phải VS Code đúng dự án thì chỉ điền sẵn và nhắc người dùng

---

## Theo dõi tiến độ

| Phase | Nội dung | Trạng thái |
|---|---|---|
| 0 | Setup & dataset | ✅ 15/10/2026 — 100 câu, 8.3 phút, micro Realtek 3.5mm |
| 1 | Benchmark STT | ✅ 15/10/2026 — large-v3 + prompt theo chủ đề, WER 9.2%, term recall 87%, không fine-tune |
| 2 | Voice server + Web (STT) | 🟡 TODO xong; DoD trên trình duyệt chưa thực hiện: nói 10 câu, tự kết nối lại, chạy 1 giờ (T-002) |
| 3 | TTS | ✅ 16/10/2026 — ZeroTTS GPU, giọng maichi, tiếng đầu ~80ms, VRAM 5.9GB (T-008) |
| 4 | Claude bridge, chạy trọn vòng | 🟡 code xong (T-009), thả phím → nghe câu đầu 4.4s; còn thử 4a trên trình duyệt và dùng thật 3 buổi tối |
| 5 | Hoàn thiện trải nghiệm | 🟡 rảnh tay bước A code xong (T-010); bộ gọi tên: recall 94%, nhận nhầm 2% (chưa đạt 95% / 0); chờ đo âm nền. 04/11/2026: tab Học bài (T-012) |
| 6 | Giọng của chính mình | ⬜ |
| 7 | Dùng từ mọi thiết bị | 🟡 18/10/2026 — dùng được trong LAN qua HTTPS 8766, có PWA + giữ màn hình sáng; từ xa (4G) chưa làm |
| 8 | Tự học dài hạn | 🟡 01/11/2026 — có kế hoạch (thêm D: hiểu ý giao việc); D: nút Sai dự án, trả lời "ừ / cái thứ hai / sai rồi", ghi nhãn intents; lexicon thêm TODO, README, PLAN.md… |
| 9 | Giao việc cho VS Code | 🟡 01/11/2026 — A1 + A2 + giao việc bằng giọng nói code xong (T-011); thử thật lần 1: sửa cắt tên, lọc câu ảo giác, hỏi lại khi "mở X" không rõ |
