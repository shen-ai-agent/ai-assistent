# Tickets

Việc cần làm tiếp, kèm bối cảnh để quay lại sau vẫn hiểu. Đóng ticket thì đổi trạng thái và ghi commit liên quan.

| ID | Tiêu đề | Phase | Trạng thái |
|---|---|---|---|
| T-001 | Chốt DoD Phase 1 | 1 | 🔴 chờ quyết định |
| T-002 | Voice server + web push-to-talk, có chọn chủ đề | 2 | ⬜ |
| T-003 | Log lượt nhận dạng + text sửa tay, làm nguồn cho corrections | 2 | ⬜ |
| T-004 | Ghi thêm câu test mix/dense | 1 (bổ sung) | ⬜ tuỳ chọn |
| T-005 | Nhận xét phát âm tiếng Anh dựa trên chỗ STT nghe sai | 5 | ⬜ |
| T-006 | Quyết định: dự án narration có gộp vào repo này không | — | 🔴 chờ quyết định |

---

## T-001: Chốt DoD Phase 1

**Bối cảnh.** Benchmark xong, chọn `large-v3` + ép `vi` + prompt theo chủ đề (chi tiết trong `_local/stt_report.md` và PLAN.md). 4/6 tiêu chí DoD đạt. Hai tiêu chí chưa đạt:

- WER nhóm `en` 14.9% (mục tiêu ≤10%). Lỗi do phát âm ("word"→"world", "chart"→"chair"). Ép `en` cũng ra 14.9%, nên đổi cấu hình STT không sửa được.
- Term recall câu mix 88.8%, holdout 89.3% (mục tiêu ≥90%). Holdout chỉ 30 câu, mỗi thuật ngữ bằng khoảng 1.6 điểm, nên chênh lệch này nằm trong sai số.

**Đề xuất.** Hạ mục tiêu WER `en` xuống ≤15%, và coi term recall 88.8% là đạt.

**Xong khi.** Có quyết định; PLAN.md đổi Phase 1 thành ✅ (hoặc ghi rõ việc phải làm thêm).

## T-002: Voice server + web push-to-talk, có chọn chủ đề

**Bối cảnh.** Xem Phase 2 trong PLAN.md. Kết quả Phase 1 bổ sung yêu cầu:
- Server dùng `server/stt/engine.py` (`WhisperEngine("large-v3")`), load một lần lúc khởi động.
- Prompt theo chủ đề: `build_prompt(glossary, "topic", topic)`. UI có chỗ chọn chủ đề cho phiên (AWS / .NET / Dev / Tiếng Anh).
- Chưa chọn chủ đề thì dùng two_pass: nhận dạng một lần, `detect_topic`, rồi nhận dạng lại.

**Xong khi.** Đạt DoD Phase 2 trong PLAN.md.

## T-003: Log lượt nhận dạng + text sửa tay

**Bối cảnh.** Phase 1 không thêm được rule corrections: ở dev không có lỗi nào lặp ≥2 lần, và viết rule từ lỗi gặp một lần là học thuộc bộ test. Dữ liệu dùng thật sẽ cho biết lỗi nào thật sự lặp lại.

**Việc.**
- Mỗi lượt ghi lại: thời điểm, chủ đề, thời lượng audio, text STT, text sau khi người dùng sửa (nếu có).
- Lưu ở `_local/`, không commit.
- Thêm script tổng hợp các cặp (STT sai → sửa đúng) lặp lại ≥2 lần để đề xuất rule cho `data/corrections.tsv`.

## T-004: Ghi thêm câu test mix/dense (tuỳ chọn)

**Bối cảnh.** Holdout 30 câu thì số liệu dao động mạnh. Nên làm trước khi so sánh model mới hoặc cân nhắc fine-tune.

**Việc.**
- Thêm 30–50 câu vào `data/sentences.tsv` (id mới, không sửa câu cũ), giữ tỉ lệ dev/holdout 70/30.
- Ghi bằng `bench/record.py`, rồi chạy lại `stt_bench.py`.

## T-005: Nhận xét phát âm tiếng Anh

**Bối cảnh.** Ở Phase 1, lỗi nhóm `en` phần lớn do phát âm: "word"→"world", "get"→"yet", "obtain"→"often", "nevertheless"→"never thee less". Khi luyện IELTS/TOEIC, những chỗ STT nghe sai chính là tín hiệu phát âm chưa rõ.

**Ý tưởng.** Ở chế độ luyện nói, so câu mẫu với kết quả STT và chỉ ra từ bị nghe nhầm. Cân nhắc dùng thêm xác suất từng từ (word-level probability) của Whisper.

## T-006: Dự án narration có gộp vào repo này không

**Bối cảnh.** Câu hỏi mở từ lúc lập kế hoạch, chưa có quyết định.
