// Trang duyệt đề xuất học. API: server/learn_api.py

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function showError(text) {
  $("error").textContent = text;
  $("error").hidden = !text;
}

async function api(path, body) {
  const res = await fetch(`/api/learn${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function load() {
  showError("");
  let data;
  try {
    data = await api("");
  } catch (e) {
    $("stats").textContent = "";
    return showError(`Không tải được đề xuất: ${e.message}`);
  }
  const s = data.stats;
  const by = s.fixed_by_split;
  $("stats").textContent = `${s.turns} lượt nói, ${s.fixed} lượt đã sửa (dev ${by.dev ?? 0}, holdout ${by.holdout ?? 0}).`
    + ` Ngưỡng đề xuất: lỗi lặp ở ${s.min_turns} lượt dev.`;
  const list = $("list");
  list.replaceChildren();
  if (!data.suggestions.length) {
    list.innerHTML = `<p class="empty">Chưa có đề xuất nào. Cứ nói và sửa transcript khi sai; lỗi nào lặp lại sẽ hiện ở đây.</p>`;
  }
  for (const sg of data.suggestions) list.append(renderCard(sg, data.topics));
}

// ---------- Một đề xuất ----------

function renderCard(sg, topics) {
  const card = $("card-tpl").content.firstElementChild.cloneNode(true);
  const q = (sel) => card.querySelector(sel);
  const isTerm = sg.kind === "term";

  q(".kind").textContent = isTerm ? "Thuật ngữ" : "Rule sửa lỗi";
  q(".kind").classList.add(sg.kind);
  q(".what").innerHTML = isTerm
    ? `+ <b>${esc(sg.right)}</b> vào glossary`
    : `<s>${esc(sg.wrong)}</s> → <b>${esc(sg.right)}</b>`;
  q(".count").textContent = `${sg.turn_ids.length} lượt`;

  if (sg.warning) {
    q(".warning").textContent = sg.warning;
    q(".warning").hidden = false;
  }

  const select = q(".topic");
  if (isTerm) {
    q(".topic-row").hidden = false;
    select.innerHTML = `<option value="">— chọn —</option>` + topics.map((t) => `<option>${esc(t)}</option>`).join("");
    select.value = sg.topic ?? "";
  }

  for (const ex of sg.examples) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="heard">${esc(ex.heard)}</span><br><span class="fixed">${esc(ex.fixed)}</span>`;
    q(".examples").append(li);
  }

  const topicKey = () => (isTerm ? select.value : "");
  const showResult = (r) => {
    q(".result").hidden = !r;
    q(".result").innerHTML = r ? renderResult(r, isTerm) : "";
    q(".approve").disabled = !r?.passed;
  };
  showResult(sg.checks[topicKey()]);
  select.addEventListener("change", () => showResult(sg.checks[topicKey()])); // kết quả kiểm tra gắn với chủ đề

  const busy = (on, label) => {
    for (const b of card.querySelectorAll("button")) b.disabled = on;
    q(".check").textContent = label ?? "Kiểm tra";
    if (!on) q(".approve").disabled = !sg.checks[topicKey()]?.passed;
  };

  q(".check").addEventListener("click", async () => {
    if (isTerm && !select.value) return showError("Chọn chủ đề cho thuật ngữ trước khi kiểm tra.");
    showError("");
    busy(true, isTerm ? "Đang chạy lại STT…" : "Đang chấm…");
    try {
      sg.checks[topicKey()] = await api(`/${sg.id}/check`, { topic: select.value || null });
      showResult(sg.checks[topicKey()]);
    } catch (e) {
      showError(e.message);
    }
    busy(false);
  });

  q(".approve").addEventListener("click", async () => {
    busy(true);
    try {
      await api(`/${sg.id}/approve`, { topic: select.value || null });
      return load();
    } catch (e) {
      showError(e.message);
    }
    busy(false);
  });

  q(".reject").addEventListener("click", async () => {
    if (!confirm("Bỏ qua đề xuất này? Nó sẽ không hiện lại.")) return;
    busy(true);
    try {
      await api(`/${sg.id}/reject`, {});
      return load();
    } catch (e) {
      showError(e.message);
    }
    busy(false);
  });
  return card;
}

// ---------- Kết quả kiểm tra ----------

const pct = (x) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);

function scoreRow(label, ts) {
  if (!ts.available) return `<li>${label}: không có dữ liệu (chưa chạy benchmark / chưa có câu cùng chủ đề)</li>`;
  const { before: b, after: a } = ts;
  const wer = a.wer < b.wer ? "better" : a.wer > b.wer ? "worse" : "";
  const term = a.terms_hit > b.terms_hit ? "better" : a.terms_hit < b.terms_hit ? "worse" : "";
  return `<li>${label} (${b.n} câu): WER ${pct(b.wer)} → <span class="${wer}">${pct(a.wer)}</span>,`
    + ` thuật ngữ đúng ${b.terms_hit}/${b.terms} → <span class="${term}">${a.terms_hit}/${a.terms}</span></li>`;
}

function renderResult(r, isTerm) {
  const head = r.passed
    ? `<p class="pass">✓ Đạt, có thể duyệt</p>`
    : `<p class="fail">✗ Không đạt: ${esc(r.reason)}</p>`;
  const rows = [scoreRow("Bộ test", r.testset)];
  if (isTerm) {
    const own = (sp) => (r.own[sp][1] ? `${r.own[sp][0]}/${r.own[sp][1]} đúng` : "không có");
    rows.push(`<li>Lượt nói của bạn có thuật ngữ này, chạy lại với prompt mới: dev ${own("dev")}, holdout ${own("holdout")}</li>`);
    if (r.prompt_tokens != null) {
      rows.push(`<li>Prompt chủ đề "${esc(r.topic)}": ${r.prompt_tokens} token`
        + (r.prompt_cut ? ` <span class="worse">— vượt 223, Whisper sẽ cắt các thuật ngữ đầu prompt</span>` : "") + `</li>`);
    }
  } else {
    const t = r.turns;
    rows.push(`<li>Lượt nói của bạn bị rule đổi: dev tốt hơn ${t.dev.better} / xấu hơn ${t.dev.worse},`
      + ` holdout tốt hơn ${t.holdout.better} / xấu hơn ${t.holdout.worse}</li>`);
    for (const ex of r.worse_examples) {
      rows.push(`<li class="worse">${esc(ex.before)} → ${esc(ex.after)}</li>`);
    }
  }
  return head + `<ul>${rows.join("")}</ul>`;
}

load();
