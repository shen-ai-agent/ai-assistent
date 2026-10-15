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
- [ ] Soạn 30 đoạn văn mẫu kiểu câu trả lời của Claude (có thuật ngữ, viết tắt, số, markdown sót)

**Normalize**
- [ ] Bỏ markdown (`**`, `#`, `-`, bảng, link)
- [ ] Code block: bỏ qua hoặc thay bằng "đoạn code trên màn hình"
- [ ] Từ điển phát âm cho viết tắt/thuật ngữ: `EC2 → "i xi tu"`, `.NET → "dot net"`, `S3 → "ét ba"`... (thử nghiệm cách đọc nào nghe tự nhiên)
- [ ] Đọc số, đơn vị, phiên bản (`v3.11`, `12GB`, `99.9%`)

**Synthesis**
- [ ] Xử lý **câu tiếng Anh hoàn toàn** (ví dụ mẫu IELTS, câu sửa ngữ pháp): model song ngữ, hoặc nhận diện ngôn ngữ từng câu rồi chuyển sang giọng tiếng Anh
- [ ] Benchmark ít nhất 2–3 model TTS tiếng Việt chạy local (ví dụ các bản fine-tune tiếng Việt của XTTS / F5-TTS, model TTS tiếng Việt mới tại thời điểm làm)
- [ ] Chấm điểm thủ công 30 đoạn mẫu (1–5): độ tự nhiên, phát âm thuật ngữ, ngắt nghỉ
- [ ] Sentence splitter: tách câu, gộp câu quá ngắn, cắt câu quá dài
- [ ] Synthesize theo từng câu, stream chunk về web
- [ ] Web: audio queue phát tuần tự, **dừng được ngay** khi bấm ⏹

**Prompt cho Claude**
- [ ] Viết instruction "trả lời văn nói, không markdown, không code block, ~150 từ" cho Claude Project

### DoD
- [ ] Có bảng điểm các model TTS trên 30 đoạn mẫu, chọn được 1 model
- [ ] Điểm trung bình model được chọn ≥ **3.5/5** về phát âm thuật ngữ
- [ ] Thời gian từ lúc có text đến khi nghe câu đầu tiên ≤ **1.5 giây**
- [ ] Không có khoảng lặng > 0.5 giây giữa các câu khi đọc đoạn dài
- [ ] Bấm ⏹ thì dừng trong ≤ **200ms**
- [ ] STT + TTS cùng load trên GPU, tổng VRAM ≤ **11GB**

---

## Phase 4 — Claude bridge (chạy trọn vòng)

**Mục tiêu:** hỏi bằng giọng nói, nghe Claude trả lời, dùng được hằng ngày.

### TODO
**4a — Clipboard bridge (làm trước)**
- [ ] Interface chung `Bridge.ask(text) -> text` (hoặc dạng stream)
- [ ] STT xong → tự copy transcript vào clipboard → bạn paste vào claude.ai
- [ ] Hotkey toàn cục (ví dụ `Ctrl+Alt+R`): đọc text đang có trong clipboard → normalize → TTS
- [ ] State `WAITING_CLAUDE` hiện trên UI trong lúc chờ

**4b — Claude Code headless**
- [ ] `ClaudeCliBridge`: gọi `claude -p "<text>" --output-format json`, lấy `session_id`
- [ ] Các lượt sau dùng `--resume <session_id>` để giữ context trong session
- [ ] System prompt / append prompt cho văn nói (dùng lại từ Phase 3)
- [ ] Bật WebSearch cho câu hỏi cần research
- [ ] Stream response (`--output-format stream-json`) → đẩy từng câu sang TTS ngay khi có
- [ ] Nút "Session mới" trên UI = bỏ `session_id`
- [ ] Đọc lại điều khoản sử dụng subscription cho cách dùng này
- [ ] Lưu log session ra file (`_local/sessions/*.jsonl`): câu hỏi, transcript, response, thời gian từng bước

### DoD
- [ ] **4a:** Một lượt hỏi–đáp hoàn chỉnh chỉ cần: giữ Space nói → paste → bấm hotkey
- [ ] **4b:** Một lượt hỏi–đáp hoàn chỉnh không cần chạm chuột/bàn phím ngoài push-to-talk
- [ ] **4b:** Câu hỏi thứ 3 trong session tham chiếu được câu thứ 1 ("cái đó khác gì cái lúc nãy?")
- [ ] **4b:** Thời gian từ thả phím đến nghe câu đầu tiên ≤ **8 giây** với câu hỏi không cần research
- [ ] Đổi bridge (clipboard ↔ CLI) chỉ cần đổi config, không sửa code STT/TTS
- [ ] **Dùng thật 3 buổi tối học liên tiếp**, ghi lại các điểm khó chịu vào `_local/feedback.md`

---

## Phase 5 — Hoàn thiện trải nghiệm

**Mục tiêu:** sửa những gì Phase 4 "dùng thật" chỉ ra. Ưu tiên theo `feedback.md`, không làm hết.

### TODO (chọn theo feedback)
- [ ] **Ngắt lời (barge-in):** bấm 🎤 khi đang SPEAKING → dừng đọc ngay, bắt đầu nghe
- [ ] **VAD (Silero)** cho chế độ rảnh tay: tự phát hiện bắt đầu/kết thúc câu
- [ ] Bổ sung glossary + post-correction từ các lỗi STT gặp khi dùng thật (công cụ có rồi: trang Đề xuất học, T-007)
- [ ] Ghi thêm 50–100 câu từ log thật vào testset, chạy lại benchmark (công cụ có rồi: `bench/build_personal_set.py` + `stt_bench.py --testset`, T-007)
- [ ] **Fine-tune STT** (chỉ khi Phase 1 kết luận cần): LoRA trên whisper với dữ liệu giọng mình + thuật ngữ
- [ ] Chỉnh tốc độ đọc, nút "đọc lại", "đọc chậm"
- [ ] Script khởi động 1 lệnh (server + mở web)

### DoD
- [ ] Mỗi mục được làm có số liệu trước/sau (term recall, độ trễ, hoặc điểm cảm nhận)
- [ ] Term recall trên testset mở rộng ≥ **95%**
- [ ] Ngắt lời: từ lúc bấm đến khi dừng đọc ≤ **200ms**
- [ ] VAD (nếu làm): trong 20 lượt nói tự nhiên, ≤ 1 lần cắt giữa câu, ≤ 1 lần kích hoạt nhầm
- [ ] Khởi động toàn bộ hệ thống bằng 1 lệnh

---

## Phase 6 — Giọng của chính mình (tuỳ chọn)

**Mục tiêu:** trợ lý đọc bằng giọng của bạn. Dùng chung với dự án narration tiếng Việt.

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
- [ ] Dataset giọng và model nằm trong `voice/` (thư mục riêng trong cùng repo), tái sử dụng được cho dự án narration

---

## Phase 7 — Dùng từ mọi thiết bị

**Mục tiêu:** điện thoại, laptop, tablet đều dùng được trợ lý; model vẫn chạy trên máy RTX 3060 ở nhà.

> Ảnh hưởng ngay từ bây giờ: web phải **responsive** và có **nút giữ để nói** (điện thoại không có phím Space). Clipboard bridge không dùng được trên điện thoại, nên Phase 4b là bắt buộc nếu muốn dùng từ xa.

### TODO
- [ ] Bước 1 — **Mạng riêng (Tailscale)**: chỉ thiết bị của mình truy cập được, có HTTPS (`tailscale serve`)
- [ ] Bước 2 (nếu thật sự cần public) — Cloudflare Tunnel + Cloudflare Access (đăng nhập bằng email), không mở port router
- [ ] Bắt buộc **xác thực** trước WebSocket: server gọi được Claude và chạy GPU, để lộ là bị dùng chùa
- [ ] Rate limit + giới hạn độ dài audio mỗi lượt
- [ ] Web trên điện thoại: nút giữ để nói, phát audio sau lần chạm đầu tiên (chính sách autoplay của iOS/Android), màn hình không tắt khi đang nói
- [ ] PWA: cài lên màn hình chính như một app
- [ ] Audio qua mạng: nén (Opus) thay vì PCM thô nếu mạng 4G chậm
- [ ] Server tự chạy khi bật máy (Windows service / Task Scheduler)

### DoD
- [ ] Từ điện thoại dùng 4G (không chung Wi-Fi): hỏi–đáp trọn vòng được
- [ ] Truy cập không đăng nhập → bị từ chối ở cả HTTP lẫn WebSocket
- [ ] Độ trễ thả nút → nghe câu đầu không tệ hơn dùng tại máy quá **1.5 giây**
- [ ] Khởi động lại máy → server tự lên, không cần thao tác

---

## Theo dõi tiến độ

| Phase | Nội dung | Trạng thái |
|---|---|---|
| 0 | Setup & dataset | ✅ 15/10/2026 — 100 câu, 8.3 phút, micro Realtek 3.5mm |
| 1 | Benchmark STT | ✅ 15/10/2026 — large-v3 + prompt theo chủ đề, WER 9.2%, term recall 87%, không fine-tune |
| 2 | Voice server + Web (STT) | 🟡 TODO xong, chờ thử trên trình duyệt (T-002) |
| 3 | TTS | ⬜ |
| 4 | Claude bridge, chạy trọn vòng | ⬜ |
| 5 | Hoàn thiện trải nghiệm | ⬜ |
| 6 | Giọng của chính mình | ⬜ |
| 7 | Dùng từ mọi thiết bị | ⬜ |
