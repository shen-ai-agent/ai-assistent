// Voice Study Assistant — web client (push-to-talk). Protocol: server/protocol.py

const PREROLL_FRAMES = 3;            // 300ms trước lúc bấm, để không mất đầu câu
const BACKOFF_MS = [1000, 2000, 5000];
const AUDIO_OPTS = ["echoCancellation", "noiseSuppression", "autoGainControl"];

const $ = (id) => document.getElementById(id);
const ui = {
  conn: $("conn"), state: $("state"), topic: $("topic"), mic: $("mic"), talk: $("talk"),
  level: $("level"), error: $("error"), history: $("history"), tpl: $("turn-tpl"),
  learnCount: $("learn-count"),
};

// ---------- State machine: IDLE → LISTENING → TRANSCRIBING → IDLE (+ ERROR) ----------

let state = "IDLE";
let releasedAt = 0;

function setState(next) {
  state = next;
  ui.state.textContent = next;
  ui.talk.classList.toggle("listening", next === "LISTENING");
  ui.talk.classList.toggle("transcribing", next === "TRANSCRIBING");
  ui.talk.textContent = { LISTENING: "Đang nghe…", TRANSCRIBING: "Đang nhận dạng…" }[next] ?? "Giữ để nói";
  refreshTalkButton();
}

function refreshTalkButton() {
  ui.talk.disabled = !(ws?.readyState === WebSocket.OPEN && mic.ready);
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
  ws.onmessage = (ev) => onServerMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    ui.conn.className = "dot";
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
      setState("IDLE");
      break;
    case "state":
      // LISTENING/TRANSCRIBING đã được client đặt trước cho nhanh; chỉ nhận IDLE từ server
      if (msg.value === "IDLE" && state !== "LISTENING") setState("IDLE");
      break;
    case "transcript":
      addTurn(msg, performance.now() - releasedAt);
      break;
    case "error":
      showError({
        too_short: "Nói ngắn quá, giữ phím lâu hơn chút.",
        no_speech: "Không nghe thấy tiếng nói. Kiểm tra micro đang chọn và vạch âm lượng.",
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
  if (state !== "IDLE" || ui.talk.disabled) return;
  resumeAudio();
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
  if (e.code === "Escape") return cancelTalk();
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
