// Voice Study Assistant — web client: trợ lý rảnh tay (tab Mimi), push-to-talk (STT), hỏi Claude (bridge),
// đọc text (TTS). Protocol: server/protocol.py

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
  orb: $("orb"), orbStatus: $("orb-status"), caption: $("caption"), reply: $("reply"),
  replyText: document.querySelector("#reply .reply-text"), replyCode: document.querySelector("#reply .reply-code"),
  hfToggle: $("hf-toggle"), mimiStop: $("mimi-stop"), echoHint: $("echo-hint"), mimiError: $("mimi-error"),
  heardLog: $("heard-log"),
};

// ---------- Tab: #mimi (mặc định) / #detail, một trang để dùng chung WebSocket và mic ----------

function showTab() {
  const tab = location.hash === "#detail" ? "detail" : "mimi";
  for (const el of document.querySelectorAll(".tab")) el.hidden = el.id !== `tab-${tab}`;
  for (const a of document.querySelectorAll(".tabs a")) a.classList.toggle("active", a.dataset.tab === tab);
}
addEventListener("hashchange", showTab);
showTab();

// ---------- State machine: IDLE → LISTENING → TRANSCRIBING → (WAITING_CLAUDE) → IDLE (+ ERROR) ----------

let state = "IDLE";
let releasedAt = 0;
let turnEndAt = 0;     // lúc lượt nói kết thúc: thả phím (push-to-talk) hoặc ngừng nói (rảnh tay)
let turnSource = "ptt";

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
  ui.mimiStop.disabled = ui.speakStop.disabled;
  ui.hfToggle.disabled = !(open && mic.ready);
  ui.hfToggle.classList.toggle("on", hf.on);
  ui.hfToggle.textContent = hf.on ? "🎙 Rảnh tay: bật" : "🎙 Rảnh tay: tắt";
  ui.echoHint.hidden = !(hf.on && player.tts && !audioOptions().echoCancellation);
  renderOrb();
}

function showError(text) {
  ui.error.textContent = ui.mimiError.textContent = text;
  ui.error.hidden = ui.mimiError.hidden = !text;
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
      hf.info = msg.assistant;
      for (const el of document.querySelectorAll(".wake-name")) el.textContent = msg.assistant?.name ?? "Mimi";
      hf.mode = "OFF";
      if (hf.on) sendListen(); // kết nối lại: server đã quên chế độ rảnh tay
      setState("IDLE");
      break;
    case "listen":
      hf.mode = msg.mode;
      renderOrb();
      break;
    case "vad":
      onVad(msg.speaking);
      break;
    case "heard":
      onHeard(msg);
      break;
    case "state":
      // LISTENING/TRANSCRIBING đã được client đặt trước cho nhanh; nhận IDLE và WAITING_CLAUDE từ server
      if ((msg.value === "IDLE" || msg.value === "WAITING_CLAUDE") && state !== "LISTENING") setState(msg.value);
      break;
    case "transcript":
      addTurn(msg, performance.now() - turnEndAt);
      ui.caption.textContent = msg.text;
      ui.caption.className = "caption";
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
    case "command":
      onCommand(msg);
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
ui.topic.onchange = () => {
  localStorage.setItem("topic", ui.topic.value);
  if (hf.on) sendListen(); // server dùng chủ đề này cho câu hỏi rảnh tay
};

// ---------- Mic: mở liên tục, giữ buffer vòng cho pre-roll ----------

const mic = { ready: false, ctx: null, stream: null, node: null, preroll: [], level: 0 };

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
  mic.level = Math.min(1, Math.sqrt(level) * 1.8);
  ui.level.style.width = `${mic.level * 100}%`;
  if (state === "LISTENING" || hf.mode !== "OFF") {
    send(pcm); // rảnh tay: gửi liên tục, server tự cắt đoạn nói
  }
  if (state !== "LISTENING") {
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
const resumeAudio = () => {
  if (mic.ctx?.state === "suspended") mic.ctx.resume();
  if (hf.on) ensurePlayer(); // tải lại trang lúc đang rảnh tay: chạm lần đầu thì tạo luôn bộ phát
};
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
  releasedAt = turnEndAt = performance.now();
  turnSource = "ptt";
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
  if (e.code === "Escape") return player.active || bridge.asking || hf.mode === "ACTIVE" ? stopSpeaking() : cancelTalk();
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

// ---------- Rảnh tay (T-010): mic gửi liên tục, server cắt đoạn nói và chờ gọi tên ----------

// on: người dùng muốn bật (nhớ qua lần tải lại). mode: trạng thái server báo (OFF/SLEEP/ACTIVE/BUSY).
// endAt: lúc (ước lượng) người dùng ngừng nói = lúc VAD báo hết đoạn trừ khoảng im lặng server chờ.
const hf = { on: localStorage.getItem("handsfree") === "1", mode: "OFF", info: null, speaking: false,
  endAt: 0, ackFrom: null, ackSpeakId: null, ackLi: null };

function sendListen() {
  send({ type: "listen", on: hf.on, topic: ui.topic.value || null });
}

function toggleHandsFree() {
  hf.on = !hf.on;
  localStorage.setItem("handsfree", hf.on ? "1" : "0");
  resumeAudio();
  ensurePlayer(); // AudioContext phải tạo trong thao tác người dùng, để câu đáp phát được
  sendListen();
  refreshTalkButton();
}

function onVad(speaking) {
  hf.speaking = speaking;
  if (!speaking && state !== "LISTENING") {
    const silence = hf.mode === "ACTIVE" ? hf.info?.active_silence_ms : hf.info?.sleep_silence_ms;
    hf.endAt = performance.now() - (silence ?? 0);
    turnEndAt = hf.endAt;
    turnSource = "hf";
  }
}

function onHeard(msg) {
  const li = document.createElement("li");
  li.className = msg.wake ? "wake" : "";
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = { ack: "gọi", ask: "gọi + hỏi", ignore: "bỏ qua" }[msg.action] ?? msg.action;
  li.append(tag, `${new Date().toLocaleTimeString()} · "${msg.text}" · STT ${msg.stt_ms}ms`);
  ui.heardLog.prepend(li);
  while (ui.heardLog.children.length > 100) ui.heardLog.lastChild.remove();
  if (!msg.wake) return;
  stopPlayback(); // gọi tên lúc đang đọc: server đã ngắt, client tắt tiếng ngay
  bridge.asking = false;
  ui.caption.textContent = msg.text;
  ui.caption.className = "caption dim";
  if (msg.ack) {
    hf.ackFrom = hf.endAt;
    hf.ackLi = li;
    showReply(msg.ack);
  }
  refreshTalkButton();
}

function showReply(text, code = "") {
  ui.reply.hidden = false;
  ui.replyText.textContent = text;
  ui.replyCode.hidden = !code;
  ui.replyCode.textContent = code;
}

// Orb: off → idle (tắt rảnh tay) / sleep (chờ tên) → listening → thinking → speaking
function orbState() {
  if (ws?.readyState !== WebSocket.OPEN || !mic.ready) return "off";
  if (state === "ERROR") return "error";
  if (state === "LISTENING") return "listening";
  if (player.active && player.sources.size) return "speaking";
  if (state === "TRANSCRIBING" || state === "WAITING_CLAUDE" || bridge.asking || player.active) return "thinking";
  if (hf.mode === "ACTIVE") return "listening";
  if (hf.mode !== "OFF") return "sleep";
  return "idle";
}

function orbStatus(s) {
  const name = hf.info?.name ?? "Mimi";
  if (s === "off") return ws?.readyState !== WebSocket.OPEN ? "Mất kết nối server…" : "Chưa mở được micro";
  if (mic.ctx?.state === "suspended" && hf.on) return "Bấm vào trang một lần để bật micro";
  return {
    error: "Có lỗi, xem bên dưới",
    listening: state === "LISTENING" ? "Đang nghe (thả Space khi nói xong)…" : "Mình đang nghe…",
    thinking: "Đang nghĩ…",
    speaking: "",
    sleep: `Nói "${name} ơi" để gọi`,
    idle: "Bật Rảnh tay, hoặc giữ Space để nói",
  }[s];
}

const outLevel = new Float32Array(512);
function renderOrb() {
  const s = orbState();
  ui.orb.className = `orb ${s}`;
  ui.orbStatus.textContent = orbStatus(s);
  let level = 0;
  if (s === "listening") level = mic.level;
  else if (s === "speaking" && player.analyser) {
    player.analyser.getFloatTimeDomainData(outLevel);
    let sum = 0;
    for (const v of outLevel) sum += v * v;
    level = Math.min(1, Math.sqrt(sum / outLevel.length) * 4);
  } else if (s === "sleep" && hf.speaking) level = mic.level * 0.4; // có tiếng nói nhưng chưa gọi tên
  ui.orb.style.setProperty("--level", level.toFixed(3));
}

function orbLoop() {
  if (!document.hidden) renderOrb();
  requestAnimationFrame(orbLoop);
}

ui.hfToggle.onclick = toggleHandsFree;
ui.mimiStop.onclick = () => stopSpeaking();

// ---------- Đọc: nhận PCM int16 từng mẩu, xếp lịch phát nối tiếp ----------

// id: speak_id đang phát; frame của lượt cũ (đến muộn sau khi dừng) bị bỏ.
// nextTime: thời điểm (theo ctx.currentTime) mẩu kế tiếp bắt đầu. Đo: tiếng đầu tính từ lúc bấm Đọc;
// maxGapS = lần hết audio lâu nhất trong lúc chờ mẩu sau (DoD: khoảng lặng giữa câu ≤ 0.5s).
const player = { tts: null, ctx: null, analyser: null, id: -1, active: false, ended: false, sources: new Set(), nextTime: 0,
  pendingGap: 0, clickedAt: 0, firstAudioMs: null, maxGapS: 0, items: [] };

function ensurePlayer() {
  if (!player.tts) return;
  if (!player.ctx) {
    player.ctx = new AudioContext({ sampleRate: player.tts.sample_rate });
    player.analyser = player.ctx.createAnalyser(); // âm lượng đang phát, cho orb
    player.analyser.fftSize = 512;
    player.analyser.connect(player.ctx.destination);
  }
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
  if (hf.ackFrom !== null) {
    player.clickedAt = hf.ackFrom;
    hf.ackSpeakId = msg.speak_id;
    hf.ackFrom = null;
  }
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
    if (hf.ackSpeakId === id && hf.ackLi) {
      hf.ackLi.append(` · ngừng nói → nghe câu đáp ${Math.round(player.firstAudioMs)}ms`);
      hf.ackLi = null;
    }
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
  src.connect(player.analyser);
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
    bridge.startedAt = turnEndAt; // lượt nói: đo từ lúc thả phím / ngừng nói
  }
  bridge.pendingTurn = null;
  bridge.asking = true;
  bridge.current = { askId: msg.ask_id, speakId: msg.speak_id, el, raw: "", startedAt: bridge.startedAt, firstAudioMs: null, end: null };
  if (msg.speak_id !== null) player.clickedAt = bridge.startedAt;
  el.querySelector(".answer").hidden = false;
  el.querySelector(".answer-meta").textContent = "Claude đang trả lời…";
  showReply("");
  refreshTalkButton();
}

function onAnswerDelta(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  cur.raw += msg.text;
  const i = cur.raw.indexOf(CODE_MARKER);
  const text = i < 0 ? cur.raw : cur.raw.slice(0, i);
  const codeText = i < 0 ? "" : cur.raw.slice(i + CODE_MARKER.length).trim();
  cur.el.querySelector(".answer-text").textContent = text;
  const code = cur.el.querySelector(".answer-code");
  code.hidden = i < 0;
  code.textContent = codeText;
  showReply(text, codeText);
}

// Lệnh giọng nói (mở session mới / dừng / đọc lại): server tự chạy, không hỏi Claude
function onCommand(msg) {
  const label = { new_session: "mở session mới", stop: "dừng", repeat: "đọc lại" }[msg.name] ?? msg.name;
  const el = bridge.pendingTurn;
  bridge.pendingTurn = null;
  bridge.asking = false;
  if (el) {
    el.querySelector(".answer").hidden = false;
    el.querySelector(".answer-text").textContent = msg.reply ?? "";
    el.querySelector(".answer-meta").textContent = `lệnh: ${label}`;
  }
  if (msg.name === "stop") stopPlayback();
  if (msg.reply) showReply(msg.reply);
  refreshTalkButton();
}

function onAnswerEnd(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  bridge.asking = false;
  cur.end = msg;
  if (msg.error) {
    cur.el.querySelector(".answer-text").textContent += `\n[lỗi] ${msg.error}`;
    ui.replyText.textContent += `\n[lỗi] ${msg.error}`;
  }
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
  if (cur.firstAudioMs !== null) parts.push(`thả phím/ngừng nói/bấm → nghe câu đầu ${(cur.firstAudioMs / 1000).toFixed(1)}s`);
  cur.el.querySelector(".answer-meta").textContent = parts.filter(Boolean).join(" · ");
}

// ---------- Lịch sử phiên, transcript sửa được ----------

function addTurn(msg, roundTripMs) {
  const li = ui.tpl.content.firstElementChild.cloneNode(true);
  const meta = li.querySelector(".meta");
  const box = li.querySelector("textarea");
  const topic = msg.topic ? `${msg.topic}${msg.topic_auto ? " (tự đoán)" : ""}` : "không rõ chủ đề";
  meta.textContent = `${new Date().toLocaleTimeString()} · ${topic} · audio ${(msg.audio_ms / 1000).toFixed(1)}s`
    + ` · STT ${msg.stt_ms}ms · ${turnSource === "hf" ? "rảnh tay · ngừng nói" : "thả phím"} → text ${Math.round(roundTripMs)}ms`;
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
orbLoop();
connect();
startMic();
refreshLearnCount();
