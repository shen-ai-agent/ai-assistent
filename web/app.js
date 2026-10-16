// Voice Study Assistant — web client: push-to-talk (STT), hỏi Claude (bridge), đọc text (TTS).
// Protocol: server/protocol.py

const PREROLL_FRAMES = 3;            // 300ms trước lúc bấm, để không mất đầu câu
const BACKOFF_MS = [1000, 2000, 5000];
const AUDIO_OPTS = ["echoCancellation", "noiseSuppression", "autoGainControl"];
const SENTENCE_GAP_S = 0.15;          // khoảng lặng giữa hai câu, giống tts_bench
const PLAY_LEAD_S = 0.05;             // lùi lịch phát mẩu đầu một chút để mẩu sau kịp tới
const CODE_MARKER = "---CODE---";     // khớp server/tts/normalize.py: phần sau chỉ hiện, không đọc

const $ = (id) => document.getElementById(id);
const ui = {
  conn: $("conn"), state: $("state"), topic: $("topic"), mic: $("mic"), talk: $("talk"),
  level: $("level"), error: $("error"), history: $("history"), tpl: $("turn-tpl"),
  learnCount: $("learn-count"), speak: $("speak"), speakStop: $("speak-stop"), speakText: $("speak-text"),
  speakStatus: $("speak-status"), speakSentences: $("speak-sentences"),
  ask: $("ask"), newSession: $("new-session"), bridgeModel: $("bridge-model"), bridgeStatus: $("bridge-status"),
};

// ---------- State machine: IDLE → LISTENING → TRANSCRIBING → (WAITING_CLAUDE) → IDLE (+ ERROR) ----------

let state = "IDLE";
let releasedAt = 0;

function setState(next) {
  state = next;
  ui.state.textContent = next;
  ui.talk.classList.toggle("listening", next === "LISTENING");
  ui.talk.classList.toggle("transcribing", next === "TRANSCRIBING" || next === "WAITING_CLAUDE");
  ui.talk.textContent = { LISTENING: "Đang nghe…", TRANSCRIBING: "Đang nhận dạng…", WAITING_CLAUDE: "Claude đang trả lời…" }[next]
    ?? "Giữ để nói";
  refreshTalkButton();
}

function refreshTalkButton() {
  const open = ws?.readyState === WebSocket.OPEN;
  ui.talk.disabled = !(open && mic.ready);
  ui.speak.disabled = !(open && player.tts);
  ui.ask.disabled = !(open && bridge.info);
  ui.newSession.disabled = !(open && bridge.info?.kind === "claude_cli");
  ui.bridgeModel.disabled = !open;
  ui.speakStop.disabled = !(player.active || bridge.asking);
}

function showError(text) {
  ui.error.textContent = text;
  ui.error.hidden = !text;
}

// ---------- WebSocket, tự kết nối lại ----------

let ws = null;
let retry = 0;

function connect() {
  ui.conn.className = "dot connecting";
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    retry = 0;
    ui.conn.className = "dot ok";
    showError("");
    refreshTalkButton();
  };
  ws.onmessage = (ev) => (ev.data instanceof ArrayBuffer ? onAudio(ev.data) : onServerMessage(JSON.parse(ev.data)));
  ws.onclose = () => {
    ui.conn.className = "dot";
    stopPlayback();
    bridge.asking = false;
    if (state !== "IDLE") setState("ERROR");
    refreshTalkButton();
    const delay = BACKOFF_MS[Math.min(retry++, BACKOFF_MS.length - 1)];
    ui.state.textContent = `mất kết nối, thử lại sau ${delay / 1000}s`;
    setTimeout(connect, delay);
  };
}

function send(msg) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(typeof msg === "string" || msg instanceof ArrayBuffer ? msg : JSON.stringify(msg));
}

function onServerMessage(msg) {
  switch (msg.type) {
    case "hello":
      fillTopics(msg.topics);
      document.title = `Voice Study Assistant — ${msg.model}`;
      player.tts = msg.tts;
      ui.speakStatus.textContent = msg.tts ? `giọng ${msg.tts.voice}` : "Server đang tắt TTS ([tts] enabled trong server/config.toml).";
      bridge.info = msg.bridge;
      showBridge(msg.bridge?.session_id);
      fillModels(msg.bridge);
      setState("IDLE");
      break;
    case "state":
      // LISTENING/TRANSCRIBING đã được client đặt trước cho nhanh; nhận IDLE và WAITING_CLAUDE từ server
      if ((msg.value === "IDLE" || msg.value === "WAITING_CLAUDE") && state !== "LISTENING") setState(msg.value);
      break;
    case "transcript":
      addTurn(msg, performance.now() - releasedAt);
      break;
    case "answer_start":
      onAnswerStart(msg);
      break;
    case "answer_delta":
      onAnswerDelta(msg);
      break;
    case "answer_end":
      onAnswerEnd(msg);
      break;
    case "bridge_session":
      showBridge(msg.session_id);
      break;
    case "bridge_model":
      if (bridge.info) bridge.info.model = msg.model;
      ui.bridgeModel.value = msg.model;
      break;
    case "speak_start":
      onSpeakStart(msg);
      break;
    case "speak_more":
      onSpeakMore(msg);
      break;
    case "speak_sentence":
      onSpeakSentence(msg);
      break;
    case "speak_end":
      onSpeakEnd(msg);
      break;
    case "error":
      showError({
        too_short: "Nói ngắn quá, giữ phím lâu hơn chút.",
        no_speech: "Không nghe thấy tiếng nói. Kiểm tra micro đang chọn và vạch âm lượng.",
        tts_disabled: "Server đang tắt TTS.",
        bridge_disabled: "Server chưa bật bridge tới Claude ([bridge] kind trong server/config.toml).",
        bridge_failed: `Không hỏi được Claude: ${msg.message}`,
      }[msg.code] ?? `${msg.code}: ${msg.message}`);
      break;
  }
}

function fillTopics(topics) {
  const saved = localStorage.getItem("topic") ?? "";
  ui.topic.length = 1; // giữ "Tự đoán"
  for (const t of topics) ui.topic.add(new Option(t, t));
  ui.topic.value = topics.includes(saved) ? saved : "";
}
ui.topic.onchange = () => localStorage.setItem("topic", ui.topic.value);

// ---------- Mic: mở liên tục, giữ buffer vòng cho pre-roll ----------

const mic = { ready: false, ctx: null, stream: null, node: null, preroll: [] };

function audioOptions() {
  const opts = {};
  for (const k of AUDIO_OPTS) opts[k] = localStorage.getItem(`opt-${k}`) === "1";
  return opts;
}

async function startMic() {
  stopMic();
  try {
    const deviceId = localStorage.getItem("micId");
    const audio = { channelCount: 1, ...audioOptions(), ...(deviceId ? { deviceId: { exact: deviceId } } : {}) };
    try {
      mic.stream = await navigator.mediaDevices.getUserMedia({ audio });
    } catch (e) {
      if (!deviceId) throw e;
      localStorage.removeItem("micId"); // mic đã lưu không còn (rút tai nghe): quay về mặc định
      delete audio.deviceId;
      mic.stream = await navigator.mediaDevices.getUserMedia({ audio });
    }
    await fillMics(mic.stream.getAudioTracks()[0]);
    try {
      mic.ctx = new AudioContext({ sampleRate: 16000 });
    } catch {
      mic.ctx = new AudioContext(); // trình duyệt không cho chọn sample rate: worklet tự resample
    }
    await mic.ctx.audioWorklet.addModule("capture-worklet.js");
    const src = mic.ctx.createMediaStreamSource(mic.stream);
    mic.node = new AudioWorkletNode(mic.ctx, "capture");
    mic.node.port.onmessage = (ev) => onFrame(ev.data.pcm, ev.data.level);
    src.connect(mic.node);
    mic.ready = true;
    showError("");
  } catch (e) {
    showError(`Không mở được mic: ${e.message}`);
  }
  refreshTalkButton();
}

// Tên thiết bị chỉ có sau khi đã cấp quyền mic, nên điền danh sách sau getUserMedia
async function fillMics(track) {
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  ui.mic.length = 1;
  for (const d of devices) {
    if (d.deviceId !== "default" && d.deviceId !== "communications") ui.mic.add(new Option(d.label || d.deviceId, d.deviceId));
  }
  ui.mic.value = localStorage.getItem("micId") ?? "";
  ui.mic.title = `Đang thu: ${track?.label ?? "?"}`;
  ui.mic.options[0].text = `Mặc định (${track?.label ?? "?"})`.slice(0, 60);
}
ui.mic.onchange = () => {
  if (ui.mic.value) localStorage.setItem("micId", ui.mic.value);
  else localStorage.removeItem("micId");
  startMic();
};

function stopMic() {
  mic.ready = false;
  mic.stream?.getTracks().forEach((t) => t.stop());
  mic.ctx?.close();
  mic.ctx = mic.stream = mic.node = null;
  mic.preroll = [];
}

function onFrame(pcm, level) {
  ui.level.style.width = `${Math.min(100, Math.sqrt(level) * 180)}%`;
  if (state === "LISTENING") {
    send(pcm);
  } else {
    mic.preroll.push(pcm);
    if (mic.preroll.length > PREROLL_FRAMES) mic.preroll.shift();
  }
}

for (const k of AUDIO_OPTS) {
  const box = $(`opt-${k}`);
  box.checked = localStorage.getItem(`opt-${k}`) === "1";
  box.onchange = () => {
    localStorage.setItem(`opt-${k}`, box.checked ? "1" : "0");
    startMic();
  };
}

// AudioContext tạo trước thao tác người dùng sẽ bị treo (autoplay policy): resume ở lần chạm đầu
const resumeAudio = () => mic.ctx?.state === "suspended" && mic.ctx.resume();
addEventListener("pointerdown", resumeAudio, true);
addEventListener("keydown", resumeAudio, true);

// ---------- Push-to-talk ----------

function beginTalk() {
  if ((state !== "IDLE" && state !== "WAITING_CLAUDE") || ui.talk.disabled) return;
  resumeAudio();
  ensurePlayer(); // tạo AudioContext ngay trong thao tác người dùng, để câu trả lời phát được
  stopPlayback(); // nói chen ngang: server tự dừng hỏi + dừng đọc khi nhận start, client tắt tiếng ngay
  bridge.asking = false;
  showError("");
  send({ type: "start", topic: ui.topic.value || null });
  for (const f of mic.preroll) send(f);
  mic.preroll = [];
  setState("LISTENING");
}

function endTalk() {
  if (state !== "LISTENING") return;
  send({ type: "stop" });
  releasedAt = performance.now();
  setState("TRANSCRIBING");
}

function cancelTalk() {
  if (state === "LISTENING" || state === "TRANSCRIBING") {
    send({ type: "cancel" });
    setState("IDLE");
  }
}

const isTyping = (el) => el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || el.tagName === "SELECT" || el.isContentEditable);

addEventListener("keydown", (e) => {
  if (e.code === "Escape") return player.active || bridge.asking ? stopSpeaking() : cancelTalk();
  if (e.code !== "Space" || isTyping(document.activeElement)) return;
  e.preventDefault();
  if (!e.repeat) beginTalk();
});
addEventListener("keyup", (e) => {
  if (e.code !== "Space" || isTyping(document.activeElement)) return;
  e.preventDefault();
  endTalk();
});
addEventListener("blur", endTalk); // chuyển cửa sổ khi đang giữ Space: keyup không bao giờ tới

ui.talk.addEventListener("pointerdown", (e) => {
  ui.talk.setPointerCapture(e.pointerId);
  beginTalk();
});
ui.talk.addEventListener("pointerup", endTalk);
ui.talk.addEventListener("pointercancel", endTalk);
ui.talk.addEventListener("keydown", (e) => e.code === "Space" && e.preventDefault()); // không "click" nút bằng Space

// ---------- Đọc: nhận PCM int16 từng mẩu, xếp lịch phát nối tiếp ----------

// id: speak_id đang phát; frame của lượt cũ (đến muộn sau khi dừng) bị bỏ.
// nextTime: thời điểm (theo ctx.currentTime) mẩu kế tiếp bắt đầu. Đo: tiếng đầu tính từ lúc bấm Đọc;
// maxGapS = lần hết audio lâu nhất trong lúc chờ mẩu sau (DoD: khoảng lặng giữa câu ≤ 0.5s).
const player = { tts: null, ctx: null, id: -1, active: false, ended: false, sources: new Set(), nextTime: 0,
  pendingGap: 0, clickedAt: 0, firstAudioMs: null, maxGapS: 0, items: [] };

function ensurePlayer() {
  if (!player.tts) return;
  if (!player.ctx) player.ctx = new AudioContext({ sampleRate: player.tts.sample_rate });
  player.ctx.resume();
}

function speakNow() {
  const text = ui.speakText.value.trim();
  if (!text || ui.speak.disabled) return;
  stopPlayback();
  ensurePlayer();
  player.clickedAt = performance.now();
  player.active = true;
  ui.speakStatus.textContent = "đang tổng hợp…";
  send({ type: "speak", text });
  refreshTalkButton();
}

// Dừng đọc; đang chờ Claude thì dừng luôn lượt hỏi (server kill tiến trình claude)
function stopSpeaking() {
  send({ type: bridge.asking ? "ask_stop" : "speak_stop" });
  bridge.asking = false;
  stopPlayback();
}

function stopPlayback() {
  for (const src of player.sources) src.stop();
  player.sources.clear();
  player.id = -1; // bỏ mọi frame còn trên đường tới
  player.active = false;
  for (const x of player.items) x.classList.remove("now");
  refreshTalkButton();
}

function onSpeakStart(msg) {
  player.id = msg.speak_id;
  player.active = true;
  player.ended = false;
  player.nextTime = 0;
  player.pendingGap = 0;
  player.firstAudioMs = null;
  player.maxGapS = 0;
  player.items = [];
  ui.speakSentences.replaceChildren();
  refreshTalkButton();
}

// Câu đến dần (câu trả lời của Claude còn đang viết): nối vào cuối danh sách
function onSpeakMore(msg) {
  if (msg.speak_id !== player.id) return;
  for (const s of msg.sentences) {
    const li = document.createElement("li");
    li.textContent = s.source;
    if (s.text !== s.source) {
      const spoken = document.createElement("span");
      spoken.className = "spoken";
      spoken.textContent = `đọc: ${s.text}`;
      li.append(spoken);
    }
    ui.speakSentences.append(li);
    player.items.push(li);
  }
}

function onSpeakSentence(msg) {
  if (msg.speak_id !== player.id) return;
  if (msg.index > 0) player.pendingGap = SENTENCE_GAP_S;
  // Server báo lúc bắt đầu tổng hợp câu; tô câu đó khi tiếng của nó thật sự phát
  const li = player.items[msg.index];
  const delay = Math.max(0, player.nextTime + player.pendingGap - player.ctx.currentTime);
  setTimeout(() => {
    if (msg.speak_id !== player.id) return;
    for (const x of player.items) x.classList.toggle("now", x === li);
  }, delay * 1000);
}

function onAudio(buf) {
  const id = new DataView(buf).getUint32(0, true);
  if (id !== player.id || !player.ctx) return;
  const pcm = new Int16Array(buf, 4);
  const audio = player.ctx.createBuffer(1, pcm.length, player.tts.sample_rate);
  const ch = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;

  const now = player.ctx.currentTime;
  let start;
  if (player.firstAudioMs === null) {
    start = now + PLAY_LEAD_S;
    player.firstAudioMs = performance.now() - player.clickedAt + PLAY_LEAD_S * 1000;
    if (bridge.current?.speakId === id) {
      bridge.current.firstAudioMs = player.firstAudioMs; // DoD Phase 4: thả phím → nghe câu đầu
      renderAnswerMeta(bridge.current);
    }
  } else {
    const due = player.nextTime + player.pendingGap;
    if (due < now) player.maxGapS = Math.max(player.maxGapS, now - player.nextTime); // hụt: im lâu hơn dự kiến
    start = Math.max(due, now);
  }
  player.pendingGap = 0;

  const src = player.ctx.createBufferSource();
  src.buffer = audio;
  src.connect(player.ctx.destination);
  src.onended = () => {
    player.sources.delete(src);
    if (!player.sources.size && id === player.id && player.ended) finishPlayback();
  };
  src.start(start);
  player.sources.add(src);
  player.nextTime = start + audio.duration;
}

function onSpeakEnd(msg) {
  if (msg.speak_id !== player.id) return;
  player.ended = true;
  const parts = [
    msg.cancelled ? "đã dừng" : "xong",
    player.firstAudioMs !== null && `bấm → tiếng đầu ${Math.round(player.firstAudioMs)}ms`,
    `server TTFA ${msg.ttfa_ms ?? "–"}ms`,
    `audio ${(msg.audio_ms / 1000).toFixed(1)}s / tổng hợp ${(msg.synth_ms / 1000).toFixed(1)}s`,
    `hụt dài nhất ${Math.round(player.maxGapS * 1000)}ms`,
  ];
  ui.speakStatus.textContent = parts.filter(Boolean).join(" · ");
  if (!player.sources.size) finishPlayback();
}

function finishPlayback() {
  player.active = false;
  for (const x of player.items) x.classList.remove("now");
  refreshTalkButton();
}

ui.speak.onclick = speakNow;
ui.speakStop.onclick = stopSpeaking;
ui.ask.onclick = askNow;
ui.newSession.onclick = () => send({ type: "new_session" });
ui.bridgeModel.onchange = () => send({ type: "set_model", model: ui.bridgeModel.value });
ui.speakText.addEventListener("keydown", (e) => {
  if (e.code === "Enter" && e.ctrlKey) {
    e.preventDefault();
    bridge.info ? askNow() : speakNow();
  }
});

// ---------- Claude: câu trả lời stream về, gắn vào lượt nói vừa xong hoặc thành lượt gõ tay ----------

// current: lượt hỏi đang hiện { askId, speakId, el, raw, startedAt, firstAudioMs, end }.
// pendingTurn: <li> của transcript vừa nhận, chờ server tự hỏi (auto_ask).
const bridge = { info: null, asking: false, current: null, pendingTurn: null, startedAt: 0 };

function showBridge(sessionId) {
  const b = bridge.info;
  ui.bridgeStatus.textContent = !b ? "Chưa bật bridge tới Claude: chỉ nhận dạng và đọc."
    : b.kind === "clipboard" ? "Bridge clipboard: transcript đã nằm trong clipboard, dán vào Claude. Copy câu trả lời rồi bấm Ctrl+Alt+R để nghe."
    : `Claude Code${b.auto_ask ? ", tự hỏi sau mỗi lượt nói" : ""} · ${sessionId ? `session ${sessionId.slice(0, 8)}` : "session mới"}`;
  refreshTalkButton();
}

// Ô chọn model: chỉ hiện khi bridge chọn được model (claude_cli). "" = mặc định của Claude Code.
function fillModels(b) {
  const models = b?.models ?? [];
  ui.bridgeModel.hidden = !models.length;
  ui.bridgeModel.replaceChildren(...["", ...models].map((m) => new Option(m || "model mặc định", m)));
  if (models.length) ui.bridgeModel.value = b.model ?? "";
}

function askNow() {
  const text = ui.speakText.value.trim();
  if (!text || ui.ask.disabled) return;
  stopPlayback();
  ensurePlayer();
  bridge.startedAt = performance.now();
  bridge.pendingTurn = null;
  send({ type: "ask", text });
}

function onAnswerStart(msg) {
  let el = bridge.pendingTurn;
  if (!el) {
    // Lượt gõ tay hoặc hotkey clipboard: lượt mới trong lịch sử, câu hỏi chỉ đọc
    el = ui.tpl.content.firstElementChild.cloneNode(true);
    el.querySelector(".meta").textContent = `${new Date().toLocaleTimeString()} · gõ tay`;
    const box = el.querySelector("textarea");
    box.value = msg.question;
    box.readOnly = true;
    ui.history.prepend(el);
  } else {
    bridge.startedAt = releasedAt; // lượt nói: đo từ lúc thả phím
  }
  bridge.pendingTurn = null;
  bridge.asking = true;
  bridge.current = { askId: msg.ask_id, speakId: msg.speak_id, el, raw: "", startedAt: bridge.startedAt, firstAudioMs: null, end: null };
  if (msg.speak_id !== null) player.clickedAt = bridge.startedAt;
  el.querySelector(".answer").hidden = false;
  el.querySelector(".answer-meta").textContent = "Claude đang trả lời…";
  refreshTalkButton();
}

function onAnswerDelta(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  cur.raw += msg.text;
  const i = cur.raw.indexOf(CODE_MARKER);
  cur.el.querySelector(".answer-text").textContent = i < 0 ? cur.raw : cur.raw.slice(0, i);
  const code = cur.el.querySelector(".answer-code");
  code.hidden = i < 0;
  if (i >= 0) code.textContent = cur.raw.slice(i + CODE_MARKER.length).trim();
}

function onAnswerEnd(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  bridge.asking = false;
  cur.end = msg;
  if (msg.error) cur.el.querySelector(".answer-text").textContent += `\n[lỗi] ${msg.error}`;
  if (msg.session_id !== undefined && bridge.info) showBridge(msg.session_id);
  renderAnswerMeta(cur);
  refreshTalkButton();
}

function renderAnswerMeta(cur) {
  const m = cur.end;
  const parts = !m ? ["Claude đang trả lời…"] : [
    m.cancelled ? "đã dừng" : m.error ? "lỗi" : "xong",
    m.first_text_ms !== null && `text đầu ${(m.first_text_ms / 1000).toFixed(1)}s`,
    `trả lời xong ${(m.total_ms / 1000).toFixed(1)}s`,
    m.cost_usd != null && `$${m.cost_usd.toFixed(3)}`,
    m.model && m.model.replace(/^claude-/, ""),
  ];
  if (cur.firstAudioMs !== null) parts.push(`thả phím/bấm → nghe câu đầu ${(cur.firstAudioMs / 1000).toFixed(1)}s`);
  cur.el.querySelector(".answer-meta").textContent = parts.filter(Boolean).join(" · ");
}

// ---------- Lịch sử phiên, transcript sửa được ----------

function addTurn(msg, roundTripMs) {
  const li = ui.tpl.content.firstElementChild.cloneNode(true);
  const meta = li.querySelector(".meta");
  const box = li.querySelector("textarea");
  const topic = msg.topic ? `${msg.topic}${msg.topic_auto ? " (tự đoán)" : ""}` : "không rõ chủ đề";
  meta.textContent = `${new Date().toLocaleTimeString()} · ${topic} · audio ${(msg.audio_ms / 1000).toFixed(1)}s`
    + ` · STT ${msg.stt_ms}ms · thả phím → text ${Math.round(roundTripMs)}ms`;
  box.value = msg.text;
  let sent = msg.text;
  box.addEventListener("change", () => {
    const text = box.value.trim();
    if (text === sent) return;
    sent = text;
    send({ type: "correction", turn_id: msg.turn_id, text });
    box.classList.toggle("edited", text !== msg.text);
    setTimeout(refreshLearnCount, 500); // server ghi bản sửa xong mới tính lại đề xuất
  });
  ui.history.prepend(li);
  if (bridge.info?.auto_ask && msg.text.trim()) bridge.pendingTurn = li;
}

// Số đề xuất đang chờ duyệt (web/learn.html)
async function refreshLearnCount() {
  try {
    const res = await fetch("/api/learn");
    if (!res.ok) return;
    const n = (await res.json()).suggestions.length;
    ui.learnCount.textContent = n;
    ui.learnCount.hidden = n === 0;
  } catch { /* server chưa chạy: bỏ qua */ }
}

// ---------- Khởi động ----------

setState("IDLE");
connect();
startMic();
refreshLearnCount();
