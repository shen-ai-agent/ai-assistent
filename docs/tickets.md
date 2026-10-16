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
