// Voice Study Assistant — web client: trợ lý rảnh tay (tab Mimi), push-to-talk (STT), hỏi Claude (bridge),
// đọc text (TTS), giao việc cho Claude Code trong VS Code (tab Công việc), giảng tài liệu học (tab Học bài).
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
  orb: $("orb"), orbStatus: $("orb-status"), chat: $("chat"), mimiInput: $("mimi-input"), composer: $("composer"),
  hfToggle: $("hf-toggle"), mimiStop: $("mimi-stop"), echoHint: $("echo-hint"), mimiError: $("mimi-error"),
  heardLog: $("heard-log"), chatTpl: $("msg-tpl"),
  tasksTab: $("tasks-tab"), tasksCount: $("tasks-count"), taskForm: $("task-form"), taskProject: $("task-project"),
  taskProjectInfo: $("task-project-info"), taskRefresh: $("task-refresh"), taskText: $("task-text"),
  taskSend: $("task-send"), taskNote: $("task-note"), taskList: $("task-list"), taskEmpty: $("task-empty"),
  taskTpl: $("task-tpl"),
  workOrb: $("work-orb"), workStatus: $("work-status"), workHf: $("work-hf"), workLog: $("work-log"),
  workYou: $("work-you"), workMimi: $("work-mimi"), workChoices: $("work-choices"),
  studyTab: $("study-tab"), studyLib: $("study-lib"), studyForm: $("study-form"), studyTitle: $("study-title"),
  studyText: $("study-text"), studyAdd: $("study-add"), studyFile: $("study-file"), studyNote: $("study-note"),
  studyEmpty: $("study-empty"), studyList: $("study-list"), docTpl: $("doc-tpl"),
  studyDoc: $("study-doc"), studyBack: $("study-back"), studyOrb: $("study-orb"), studyDocTitle: $("study-doc-title"),
  studyStatus: $("study-status"), studyHf: $("study-hf"), studyBar: $("study-bar"), studyProgress: $("study-progress"),
  studyReset: $("study-reset"), studySource: $("study-source"), studySourceText: $("study-source-text"),
  studyChat: $("study-chat"), studyQuick: $("study-quick"), studyError: $("study-error"),
  studyComposer: $("study-composer"), studyInput: $("study-input"), studyStop: $("study-stop"),
};

// Công việc giao cho VS Code (khai báo sớm vì showTab cần biết tính năng có bật không).
// info: hello.agent (undefined = chưa nhận hello, null = server tắt tính năng). items: task_id → task như protocol.
// cards / chatCards: task_id → thẻ ở tab Công việc / thẻ gọn trong #chat. attention: task cần xem khi đang ở tab khác.
// skew: đồng hồ máy này nhanh hơn server bao nhiêu giây (ước lượng lúc nhận task_start), để tính thời gian đã chạy.
const tasks = { info: undefined, projects: [], items: new Map(), cards: new Map(), chatCards: new Map(),
  attention: new Set(), skew: 0 };

// Học bài (T-012). info: hello.study (undefined = chưa nhận hello, null = server tắt). docs: thư viện (doc.public()).
// docId: tài liệu đang mở (nhớ qua lần tải lại); doc: bản đầy đủ đã tải (thêm text, history); loading: id đang tải.
const study = { info: undefined, docs: [], docId: localStorage.getItem("studyDoc") || null, doc: null, loading: null };

// ---------- Tab: #mimi (mặc định) / #tasks / #study / #detail, một trang để dùng chung WebSocket và mic ----------

function currentTab() {
  const h = location.hash.slice(1);
  if (h === "detail") return "detail";
  if (h === "tasks" && tasks.info !== null) return "tasks"; // chưa có hello thì cứ hiện, hello báo tắt thì về Mimi
  if (h === "study" && study.info !== null) return "study";
  return "mimi";
}

function showTab() {
  const tab = currentTab();
  for (const el of document.querySelectorAll(".tab")) el.hidden = el.id !== `tab-${tab}`;
  for (const a of document.querySelectorAll(".tabs a")) a.classList.toggle("active", a.dataset.tab === tab);
  if (tab === "tasks") clearTaskAttention();
  if (tab === "study" && study.info) renderStudyView(); // trước hello: chưa biết tính năng có bật không
  sendWork();
  sendStudy();
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
  ui.mimiInput.disabled = ui.ask.disabled;
  ui.newSession.disabled = !(open && bridge.info?.kind === "claude_cli");
  ui.bridgeModel.disabled = !open;
  ui.speakStop.disabled = !(player.active || bridge.asking);
  ui.mimiStop.disabled = ui.speakStop.disabled;
  ui.hfToggle.disabled = !(open && mic.ready);
  ui.hfToggle.classList.toggle("on", hf.on);
  ui.hfToggle.querySelector(".label").textContent = hf.on ? "Đang nghe" : "Rảnh tay";
  ui.workHf.disabled = ui.hfToggle.disabled;
  ui.workHf.classList.toggle("on", hf.on);
  ui.workHf.querySelector(".label").textContent = hf.on ? "Đang nghe" : "Rảnh tay";
  ui.echoHint.hidden = !(hf.on && player.tts && !audioOptions().echoCancellation);
  ui.taskSend.disabled = ui.taskRefresh.disabled = !(open && tasks.info);
  ui.studyHf.disabled = ui.hfToggle.disabled;
  ui.studyHf.classList.toggle("on", hf.on);
  ui.studyHf.querySelector(".label").textContent = hf.on ? "Đang nghe" : "Rảnh tay";
  ui.studyInput.disabled = ui.ask.disabled || !study.doc;
  ui.studyStop.disabled = ui.speakStop.disabled;
  for (const b of ui.studyQuick.querySelectorAll("button")) b.disabled = ui.studyInput.disabled;
  ui.studyAdd.disabled = !study.info;
  renderOrb();
  syncWakeLock();
}

// Giữ màn hình sáng khi đang rảnh tay, đang chờ Claude hoặc đang đọc: màn hình tắt thì trình duyệt điện thoại
// treo mic và audio. Trình duyệt tự nhả khoá khi trang bị ẩn, hiện lại thì xin lại.
const wake = { lock: null, pending: false };
async function syncWakeLock() {
  if (!("wakeLock" in navigator) || wake.pending) return;
  const want = (hf.on || player.active || bridge.asking) && !document.hidden;
  if (want && !wake.lock) {
    wake.pending = true;
    try {
      wake.lock = await navigator.wakeLock.request("screen");
      wake.lock.addEventListener("release", () => { wake.lock = null; });
    } catch { /* hết pin, chế độ tiết kiệm…: bỏ qua */ }
    wake.pending = false;
    syncWakeLock(); // trạng thái có thể đã đổi trong lúc chờ
  } else if (!want && wake.lock) {
    const lock = wake.lock;
    wake.lock = null;
    lock.release();
  }
}
document.addEventListener("visibilitychange", syncWakeLock);

function showError(text) {
  ui.error.textContent = ui.mimiError.textContent = ui.studyError.textContent = text;
  ui.error.hidden = ui.mimiError.hidden = ui.studyError.hidden = !text;
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
      onAgentHello(msg.agent);
      onStudyHello(msg.study);
      setState("IDLE");
      break;
    case "projects":
      fillProjects(msg.items);
      break;
    case "tasks":
      onTasks(msg.items);
      break;
    case "task_start":
      onTaskStart(msg);
      break;
    case "task_status":
      onTaskStatus(msg);
      break;
    case "task_step":
      onTaskStep(msg);
      break;
    case "task_step_end":
      onTaskStepEnd(msg);
      break;
    case "task_end":
      onTaskEnd(msg);
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
      chatUser(msg.text, true);
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
    case "study_doc":
      onStudyDoc(msg.doc);
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
        agent_disabled: "Server chưa bật giao việc cho VS Code ([agent] enabled trong server/config.toml).",
        study_disabled: "Tab Học bài cần [bridge] kind = \"claude_cli\" trong server/config.toml.",
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
  chatUser(msg.text, false);
  if (msg.ack) {
    hf.ackFrom = hf.endAt;
    hf.ackLi = li;
    chatReply(msg.ack, "", true);
  }
  refreshTalkButton();
}

// ---------- Hội thoại ở tab Mimi và tab Học bài: bong bóng của bạn (phải) và của Mimi (trái) ----------

// Mỗi khung chat (#chat, #study-chat) một trạng thái; chat = khung đang nhận lượt hiện tại (useChat).
// box: thẻ <ol>. pendingUser: bong bóng câu vừa nói, chưa có câu trả lời. Đoạn "heard" (chưa chốt, chữ mờ) được
// transcript thay bằng bản chốt; câu mới sau đó thành bong bóng mới. reply: bong bóng Mimi đang viết.
// follow: còn cuộn theo Mimi; người dùng tự cuộn (lăn chuột, vuốt) thì thôi, cuộn về cuối thì theo lại.
// notesMd: phần sau ---CODE--- là ghi chú markdown (tab Học bài), không phải code.
const CHAT_MAX = 60;

function makeChat(box, notesMd = false, max = CHAT_MAX) {
  const c = { box, notesMd, max, pendingUser: null, reply: null, follow: true };
  const stopFollow = () => (c.follow = false);
  box.addEventListener("wheel", stopFollow, { passive: true });
  box.addEventListener("touchmove", stopFollow, { passive: true });
  box.addEventListener("scroll", () => {
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 40) c.follow = true;
  });
  box.addEventListener("click", copyCode);
  return c;
}

// Tab Học bài giữ cả buổi học (lịch sử tải từ server), nên không cắt bớt như tab Mimi
const chats = { mimi: makeChat(ui.chat), study: makeChat(ui.studyChat, true, Infinity) };
let chat = chats.mimi;
const useChat = (inStudy) => (chat = inStudy ? chats.study : chats.mimi);
// Câu hỏi lúc này đi tới gia sư của tài liệu đang mở (server nhận message study khi doc đã tải xong)
const studyActive = () => currentTab() === "study" && !!study.doc;

function chatAppend(li, c = chat) {
  const box = c.box;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.append(li);
  while (box.children.length > c.max) box.firstElementChild.remove();
  if (atBottom || li.classList.contains("me")) box.scrollTop = box.scrollHeight;
}

// c: khung chat cho câu này; mặc định theo tab đang mở (đang học bài thì vào khung tab Học bài)
function chatUser(text, final, c = null) {
  if (!text) return;
  chat = c ?? useChat(studyActive());
  let li = chat.pendingUser;
  if (!li || li.dataset.final) {
    li = document.createElement("li");
    li.className = "msg me";
    li.append(document.createElement("div"));
    li.firstChild.className = "bubble";
    chatAppend(li);
    chat.pendingUser = li;
  }
  li.firstChild.textContent = text;
  li.classList.toggle("dim", !final);
  if (final) li.dataset.final = "1";
  workSay(text, null, !final);
}

// fresh: mở bong bóng mới (lượt trả lời mới); không thì cập nhật bong bóng đang viết.
// speakId: lượt đọc của câu trả lời này, để tô câu đang đọc (onSpeakMore / onSpeakSentence).
function chatReply(text, code = "", fresh = false, speakId = null) {
  if (fresh || !chat.reply) {
    const li = ui.chatTpl.content.firstElementChild.cloneNode(true);
    Object.assign(li, { speakId, sents: [], now: -1 });
    chat.reply = li;
    chat.pendingUser = null;
    chat.follow = true;
    chatAppend(li);
  }
  const li = chat.reply;
  // Claude tách đoạn bằng dòng trống; trong bong bóng chỉ cần xuống dòng
  li.raw = text.replace(/\n\s*\n/g, "\n");
  renderReplyText(li);
  li.querySelector(".code-wrap").hidden = !code || chat.notesMd;
  li.querySelector("pre").textContent = chat.notesMd ? "" : code;
  const notes = li.querySelector(".notes");
  notes.hidden = !code || !chat.notesMd;
  if (chat.notesMd && li.notesSrc !== code) {
    li.notesSrc = code;
    notes.innerHTML = renderMarkdown(code); // renderMarkdown escape HTML trước
  }
  revealReply(li);
  workSay(null, li.raw);
}

// Tab Công việc: hiện lượt nói gần nhất (câu bạn vừa nói và câu Mimi đáp) ngay dưới orb nhỏ
function workSay(you, mimi, dim = false) {
  ui.workLog.hidden = false;
  if (you !== null) {
    ui.workYou.textContent = you;
    ui.workYou.classList.toggle("dim", dim);
    ui.workMimi.textContent = "…";
  }
  if (mimi !== null) ui.workMimi.textContent = clip(mimi, 400);
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Câu TTS đã nhận (li.sents, theo thứ tự) bọc trong <span class="sent">, tìm lần lượt trong text,
// bỏ qua khác biệt khoảng trắng. Câu không tìm thấy (normalize đã đổi chữ) thì không tô.
function renderReplyText(li) {
  const text = li.raw;
  const nodes = [];
  let pos = 0;
  li.sents.forEach((src, i) => {
    const words = src.trim().split(/\s+/).filter(Boolean).map(escapeRe);
    if (!words.length) return;
    const re = new RegExp(words.join("\\s+"), "g");
    re.lastIndex = pos;
    const m = re.exec(text);
    if (!m) return;
    if (m.index > pos) nodes.push(text.slice(pos, m.index));
    const span = document.createElement("span");
    span.className = "sent";
    span.dataset.i = i;
    span.classList.toggle("now", i === li.now);
    span.textContent = m[0];
    nodes.push(span);
    pos = m.index + m[0].length;
  });
  if (pos < text.length) nodes.push(text.slice(pos)); // không để text node rỗng, giữ :empty cho dấu "…"
  li.querySelector(".text").replaceChildren(...nodes);
}

// Chữ đang stream: cuộn theo cho tới khi đầu bong bóng chạm mép trên, sau đó đứng yên để đọc
function revealReply(li) {
  if (!chat.follow) return;
  const box = chat.box;
  const top = box.scrollTop + li.getBoundingClientRect().top - box.getBoundingClientRect().top - 8;
  const target = Math.min(box.scrollHeight - box.clientHeight, top);
  if (target > box.scrollTop) box.scrollTop = target;
}

// Tô câu thứ i của bong bóng đang đọc (-1: bỏ tô), cuộn tới câu đó nếu nó ra ngoài khung nhìn
function highlightReply(speakId, i) {
  const li = chat.reply;
  if (!li || li.speakId !== speakId) return;
  li.now = i;
  let cur = null;
  for (const s of li.querySelectorAll(".sent")) {
    const on = Number(s.dataset.i) === i;
    s.classList.toggle("now", on);
    if (on) cur = s;
  }
  if (!cur || !chat.follow) return;
  const el = chat.box;
  const box = el.getBoundingClientRect();
  const r = cur.getBoundingClientRect();
  if (r.top < box.top + 8 || r.bottom > box.bottom - 8) {
    el.scrollTo({ top: el.scrollTop + r.top - box.top - box.height / 3, behavior: "smooth" });
  }
}

async function copyCode(e) {
  const btn = e.target.closest(".copy");
  if (!btn) return;
  try {
    await navigator.clipboard.writeText(btn.parentElement.querySelector("pre").textContent);
    btn.textContent = "Đã chép";
  } catch {
    btn.textContent = "Không chép được"; // clipboard cần HTTPS hoặc localhost
  }
  setTimeout(() => (btn.textContent = "Chép"), 1500);
}

ui.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  if (askNow(ui.mimiInput.value.trim())) ui.mimiInput.value = "";
});

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
  if (document.body.dataset.orb !== s) document.body.dataset.orb = s; // màu nền ở đáy đổi theo orb
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
  if (ui.workOrb.offsetParent) { // orb nhỏ ở tab Công việc, chỉ cập nhật khi đang hiện
    ui.workOrb.className = `orb mini ${s}`;
    ui.workOrb.style.setProperty("--level", level.toFixed(3));
    ui.workStatus.textContent = s === "sleep" ? `Nói "${hf.info?.name ?? "Mimi"} ơi" rồi nói việc cần làm` : orbStatus(s);
  }
  if (ui.studyOrb.offsetParent) { // orb nhỏ ở tab Học bài
    ui.studyOrb.className = `orb mini ${s}`;
    ui.studyOrb.style.setProperty("--level", level.toFixed(3));
    ui.studyStatus.textContent = s === "sleep" ? `Nói "${hf.info?.name ?? "Mimi"} ơi, tiếp" hoặc hỏi về bài` : orbStatus(s);
  }
}

function orbLoop() {
  if (!document.hidden) renderOrb();
  requestAnimationFrame(orbLoop);
}

ui.hfToggle.onclick = toggleHandsFree;
ui.workHf.onclick = toggleHandsFree;
ui.studyHf.onclick = toggleHandsFree;
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
  highlightReply(player.id, -1);
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
  const reply = chat.reply;
  if (reply?.speakId === msg.speak_id) {
    reply.sents.push(...msg.sentences.map((s) => s.source));
    renderReplyText(reply);
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
    highlightReply(msg.speak_id, msg.index);
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
  highlightReply(player.id, -1);
  refreshTalkButton();
}

ui.speak.onclick = speakNow;
ui.speakStop.onclick = stopSpeaking;
ui.ask.onclick = () => askNow();
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

// Trả về true nếu đã gửi (để ô nhập ở tab Mimi xoá chữ)
function askNow(text = ui.speakText.value.trim()) {
  if (!text || ui.ask.disabled) return false;
  stopPlayback();
  ensurePlayer();
  bridge.startedAt = performance.now();
  bridge.pendingTurn = null;
  send({ type: "ask", text });
  return true;
}

function onAnswerStart(msg) {
  useChat(msg.doc_id != null); // câu trả lời của gia sư (tab Học bài) vào khung chat của tab đó
  let el = bridge.pendingTurn;
  if (!el) {
    // Lượt gõ tay hoặc hotkey clipboard: lượt mới trong lịch sử, câu hỏi chỉ đọc
    el = ui.tpl.content.firstElementChild.cloneNode(true);
    el.querySelector(".meta").textContent = `${new Date().toLocaleTimeString()} · gõ tay${msg.doc_id ? " · học bài" : ""}`;
    const box = el.querySelector("textarea");
    box.value = msg.question;
    box.readOnly = true;
    ui.history.prepend(el);
    chatUser(msg.question, true, chat);
  } else {
    bridge.startedAt = turnEndAt; // lượt nói: đo từ lúc thả phím / ngừng nói
  }
  bridge.pendingTurn = null;
  bridge.asking = true;
  bridge.current = { askId: msg.ask_id, speakId: msg.speak_id, el, raw: "", startedAt: bridge.startedAt, firstAudioMs: null,
    end: null, docId: msg.doc_id ?? null };
  if (msg.speak_id !== null) player.clickedAt = bridge.startedAt;
  el.querySelector(".answer").hidden = false;
  el.querySelector(".answer-meta").textContent = "Claude đang trả lời…";
  chatReply("", "", true, msg.speak_id); // bong bóng trống hiện dấu "…" đang gõ
  refreshTalkButton();
}

function onAnswerDelta(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  cur.raw += msg.text;
  const [text, codeText] = splitCode(cur.raw);
  cur.el.querySelector(".answer-text").textContent = text;
  const code = cur.el.querySelector(".answer-code");
  code.hidden = !cur.raw.includes(CODE_MARKER);
  code.textContent = codeText;
  chatReply(text, codeText);
}

// Câu trả lời → [phần đọc, phần chỉ hiện sau ---CODE---]
function splitCode(raw) {
  const i = raw.indexOf(CODE_MARKER);
  return i < 0 ? [raw, ""] : [raw.slice(0, i), raw.slice(i + CODE_MARKER.length).trim()];
}

// Lệnh giọng nói (mở session mới / dừng / đọc lại): server tự chạy, không hỏi Claude
function onCommand(msg) {
  const label = { new_session: "mở session mới", stop: "dừng", repeat: "đọc lại", dev_task: "giao việc VS Code",
    work: "công việc" }[msg.name]
    ?? msg.name;
  const el = bridge.pendingTurn;
  bridge.pendingTurn = null;
  bridge.asking = false;
  if (el) {
    el.querySelector(".answer").hidden = false;
    el.querySelector(".answer-text").textContent = msg.reply ?? "";
    el.querySelector(".answer-meta").textContent = `lệnh: ${label}`;
  }
  if (msg.name === "stop") stopPlayback();
  if (msg.reply) chatReply(msg.reply, "", true);
  showChoices(msg.choices ?? []);
  refreshTalkButton();
}

// Mimi hỏi lại "dự án nào?": mỗi ứng viên một nút (bấm = trả lời bằng tên), thêm nút "Không phải".
// Nút ở lần hỏi trước bị vô hiệu khi có câu đáp mới.
function showChoices(names) {
  for (const old of document.querySelectorAll(".msg .choices button")) old.disabled = true;
  ui.workChoices.hidden = !names.length;
  ui.workChoices.replaceChildren();
  if (!names.length) return;
  const make = () => [...names, "Không phải"].map((name, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = i < names.length ? "" : "secondary";
    b.textContent = name;
    b.onclick = () => pickChoice(i < names.length ? name : "không phải");
    return b;
  });
  ui.workChoices.append(...make());
  const bubble = chat.reply?.querySelector(".bubble");
  if (bubble) {
    const box = document.createElement("div");
    box.className = "choices";
    box.append(...make());
    bubble.append(box);
  }
}

function pickChoice(text) {
  if (ws?.readyState !== WebSocket.OPEN) return;
  stopPlayback();
  ensurePlayer();
  bridge.pendingTurn = null;
  chatUser(text, true);
  showChoices([]);
  send({ type: "ask", text });
}

function onAnswerEnd(msg) {
  const cur = bridge.current;
  if (cur?.askId !== msg.ask_id) return;
  bridge.asking = false;
  cur.end = msg;
  if (msg.error) {
    cur.el.querySelector(".answer-text").textContent += `\n[lỗi] ${msg.error}`;
    if (chat.reply) {
      chat.reply.raw += `\n[lỗi] ${msg.error}`;
      renderReplyText(chat.reply);
    }
  }
  // Lượt của gia sư (tab Học bài) dùng session riêng của tài liệu, không phải session của tab Mimi
  if (msg.session_id !== undefined && bridge.info && !cur.docId) showBridge(msg.session_id);
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

// ---------- Công việc: giao việc cho Claude Code trong VS Code (tab #tasks) ----------

// Server tìm dự án, mở VS Code, đưa yêu cầu vào ô chat Claude Code rồi đọc log phiên để báo tiến độ.
// Gửi: projects, task {text, project}, task_cancel {task_id}, task_open {task_id},
// task_fix {task_id, project, request} (nút "Sai dự án": giao lại ở dự án đúng, server ghi nhãn).
// Nhận (mọi tab đều nhận): projects; tasks = ảnh chụp toàn bộ ngay sau hello;
// task_start → task_status (opening/prefilled/running/waiting) + task_step / task_step_end → task_end.
// Giao việc từ tab Mimi: server gửi command dev_task (câu đáp ngắn) rồi mới tới task_start.
// Mimi hỏi lại tên dự án: command work có choices = ứng viên, hiện thành nút (showChoices).
// Báo cáo cuối do server tự đọc bằng giọng; ở đây chỉ hiển thị.

const TASK_ACTIVE = new Set(["opening", "prefilled", "running", "waiting"]);
const TASK_LABEL = { opening: "Đang mở VS Code", prefilled: "Chờ bạn bấm Enter", running: "Claude đang làm",
  waiting: "Chờ duyệt hoặc chạy lâu", done: "Xong", failed: "Lỗi", cancelled: "Đã ngừng theo dõi", interrupted: "Bị ngắt" };
const TASK_STEPS_SHOWN = 8;   // thẻ gọn chỉ hiện chừng này bước cuối
const TASK_FILES_SHOWN = 30;

const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const serverNow = () => Date.now() / 1000 - tasks.skew;

function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

function fmtWords(ms) {
  const s = Math.round(ms / 1000);
  if (s < 1) return "dưới 1 giây";
  if (s < 60) return `${s} giây`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} phút` : `${Math.floor(m / 60)} giờ ${m % 60} phút`;
}

function timeAgo(epochS) {
  const s = serverNow() - epochS;
  if (s < 60) return "vừa xong";
  if (s < 3600) return `${Math.floor(s / 60)} phút trước`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ trước`;
  return `${Math.floor(s / 86400)} ngày trước`;
}

function onAgentHello(agent) {
  tasks.info = agent ?? null;
  ui.tasksTab.hidden = !tasks.info;
  ui.taskNote.textContent = !tasks.info ? ""
    : tasks.info.auto_submit ? "Mimi sẽ tự gửi yêu cầu vào ô chat Claude." : "Mimi điền sẵn, bạn bấm Enter trong VS Code.";
  showTab(); // đang ở #tasks mà server tắt tính năng thì về Mimi
  if (tasks.info) send({ type: "projects" });
}

// Báo server đang ở tab Công việc hay không: ở tab này mọi câu nói là giao việc (không hỏi Claude của Mimi),
// câu không nêu tên dự án thì làm ở dự án đang chọn, không chọn thì dự án của việc gần nhất.
function sendWork() {
  if (tasks.info) send({ type: "work", on: currentTab() === "tasks", project: ui.taskProject.value || null });
}

// Ô chọn dự án: "Tự nhận từ câu" rồi danh sách server gửi (đã sắp theo dùng gần nhất). Giữ lựa chọn nếu còn.
function fillProjects(items) {
  tasks.projects = items ?? [];
  for (const li of tasks.cards.values()) {
    const t = tasks.items.get(li.taskId);
    if (t && !li.querySelector(".task-fix-form").hidden) fillFixForm(li, t);
  }
  const keep = ui.taskProject.value || localStorage.getItem("workProject") || "";
  ui.taskProject.replaceChildren(new Option("Dự án vừa làm (hoặc nêu tên trong câu)", ""), ...tasks.projects.map((p) => {
    const desc = p.description || (p.stack ?? []).join(", ");
    const opt = new Option(desc ? `${p.name} · ${clip(desc, 48)}` : p.name, p.name);
    opt.title = p.path;
    return opt;
  }));
  ui.taskProject.value = tasks.projects.some((p) => p.name === keep) ? keep : "";
  showProjectInfo();
  sendWork();
}

function showProjectInfo() {
  const p = tasks.projects.find((x) => x.name === ui.taskProject.value);
  ui.taskProjectInfo.textContent = p
    ? [p.path, p.stack?.length && p.stack.join(", "), p.last_used && `dùng ${timeAgo(p.last_used)}`].filter(Boolean).join(" · ")
    : tasks.projects.length ? `${tasks.projects.length} dự án. Nêu tên dự án trong câu thì Mimi làm ở dự án đó.`
    : "Chưa có danh sách dự án.";
  ui.taskText.placeholder = p ? `Yêu cầu cho ${p.name} (để trống = chỉ mở dự án)`
    : "Ví dụ: mở dự án job hay rồi thêm phân trang cho trang danh sách việc";
}

function sendTask() {
  const text = ui.taskText.value.trim();
  const project = ui.taskProject.value || null;
  if (ui.taskSend.disabled || (!text && !project)) return;
  send({ type: "task", text, project });
  ui.taskText.value = "";
}

ui.taskForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendTask();
});
ui.taskText.addEventListener("keydown", (e) => {
  if (e.code === "Enter" && e.ctrlKey) {
    e.preventDefault();
    sendTask();
  }
});
ui.taskRefresh.onclick = () => send({ type: "projects" });
ui.taskProject.onchange = () => {
  localStorage.setItem("workProject", ui.taskProject.value);
  showProjectInfo();
  sendWork();
};

// Task chưa biết (message tới trước task_start, hoặc trước ảnh chụp): tạo khung rỗng rồi điền dần
function taskOf(id) {
  let t = tasks.items.get(id);
  if (!t) {
    t = { task_id: id, project: null, request: "", source: null, status: "opening", detail: null, steps: [],
      report: null, files: [], tools: 0, duration_ms: null, session_id: null, started_at: serverNow() };
    tasks.items.set(id, t);
  }
  return t;
}

// Mới nhất ở trên: theo started_at rồi task_id, giảm dần
const cmpTask = (a, b) => (b.started_at - a.started_at) || (b.task_id - a.task_id);

function onTasks(items) {
  const old = tasks.items;
  tasks.items = new Map();
  for (const t of items ?? []) {
    t.steps ??= [];
    t.files ??= [];
    t.showAll = old.get(t.task_id)?.showAll ?? false;
    tasks.items.set(t.task_id, t);
  }
  ui.taskList.replaceChildren(); // vẽ lại toàn bộ
  tasks.cards.clear();
  for (const id of tasks.attention) if (!tasks.items.has(id)) tasks.attention.delete(id);
  renderTaskBadge();
  for (const t of [...tasks.items.values()].sort(cmpTask)) {
    renderTask(t);
    renderChatTask(t); // chỉ cập nhật thẻ chat đã có (kết nối lại giữa chừng), không thêm thẻ cho task cũ
  }
  ui.taskEmpty.hidden = tasks.items.size > 0;
}

function onTaskStart(msg) {
  tasks.skew = Date.now() / 1000 - msg.started_at;
  const t = taskOf(msg.task_id);
  Object.assign(t, { project: msg.project, request: msg.request ?? "", source: msg.source, started_at: msg.started_at });
  renderTask(t);
  chatTask(t);
}

function onTaskStatus(msg) {
  const t = taskOf(msg.task_id);
  t.status = msg.status;
  t.detail = msg.detail ?? null;
  renderTask(t);
  renderChatTask(t);
  if (msg.status === "waiting" || msg.status === "prefilled") flagTask(t);
}

function onTaskStep(msg) {
  const t = taskOf(msg.task_id);
  t.steps.push({ step_id: msg.step_id, tool: msg.tool, summary: msg.summary, ok: null });
  renderTask(t);
}

function onTaskStepEnd(msg) {
  const t = taskOf(msg.task_id);
  const step = t.steps.findLast((s) => s.step_id === msg.step_id);
  if (step) step.ok = msg.ok;
  renderTask(t);
}

function onTaskEnd(msg) {
  const t = taskOf(msg.task_id);
  Object.assign(t, { status: msg.status, detail: null, report: msg.report ?? null, files: msg.files ?? [],
    tools: msg.tools ?? t.steps.length, duration_ms: msg.duration_ms ?? null, session_id: msg.session_id ?? null });
  renderTask(t);
  renderChatTask(t);
  flagTask(t);
}

// Badge trên tab "Công việc": task kết thúc hoặc cần người dùng (waiting/prefilled) lúc đang ở tab khác
function flagTask(t) {
  if (currentTab() === "tasks") return;
  tasks.attention.add(t.task_id);
  renderTaskBadge();
}

function clearTaskAttention() {
  tasks.attention.clear();
  renderTaskBadge();
}

function renderTaskBadge() {
  const n = tasks.attention.size;
  ui.tasksCount.textContent = n;
  ui.tasksCount.hidden = n === 0;
}

function setStatusChip(el, status) {
  el.className = `tstat st-${status}`;
  el.textContent = TASK_LABEL[status] ?? status;
}

function taskElapsedMs(t) {
  return TASK_ACTIVE.has(t.status) ? (serverNow() - t.started_at) * 1000 : t.duration_ms;
}

function bannerText(t) {
  if (t.status === "waiting") return `Claude đang chờ: ${t.detail || "một bước chưa xong"}. Nếu VS Code hỏi quyền thì duyệt ở đó.`;
  if (t.status === "prefilled") return t.detail || "Yêu cầu đã nằm sẵn trong ô chat Claude ở VS Code, bấm Enter để gửi.";
  return t.detail ?? "";
}

// File trong dự án thì bỏ phần đường dẫn dự án cho gọn (so không phân biệt / và \, hoa thường)
function shortPath(file, root) {
  if (!root) return file;
  const norm = (s) => s.replace(/\\/g, "/").toLowerCase();
  const r = norm(root).replace(/\/$/, "") + "/";
  return norm(file).startsWith(r) ? file.slice(r.length) : file;
}

// Vẽ (hoặc cập nhật) thẻ của một task ở tab Công việc
function renderTask(t) {
  let li = tasks.cards.get(t.task_id);
  if (!li) {
    li = ui.taskTpl.content.firstElementChild.cloneNode(true);
    li.taskId = t.task_id;
    tasks.cards.set(t.task_id, li);
    const after = [...ui.taskList.children].find((c) => cmpTask(t, tasks.items.get(c.taskId)) < 0);
    ui.taskList.insertBefore(li, after ?? null);
    ui.taskEmpty.hidden = true;
  }
  const q = (sel) => li.querySelector(sel);
  const active = TASK_ACTIVE.has(t.status);
  li.className = `task st-${t.status}`;
  setStatusChip(q(".tstat"), t.status);
  q(".task-project").textContent = t.project?.name ?? "Chưa rõ dự án";
  q(".task-path").textContent = t.project?.path ?? "";
  q(".task-path").hidden = !t.project?.path;
  q(".task-request").textContent = t.request || "(chỉ mở dự án)";

  const banner = q(".task-banner");
  const text = bannerText(t);
  banner.hidden = !text;
  banner.classList.toggle("attn", t.status === "waiting" || t.status === "prefilled");
  banner.textContent = text;

  renderSteps(li, t);

  const rep = q(".task-report");
  rep.hidden = !t.report;
  if (t.report && rep.mdSource !== t.report) {
    rep.innerHTML = renderMarkdown(t.report); // renderMarkdown đã escape HTML
    rep.mdSource = t.report;
  }
  rep.classList.toggle("bad", t.status === "failed" || t.status === "interrupted");

  const files = q(".task-files");
  const list = t.files ?? [];
  files.hidden = !list.length;
  files.replaceChildren(`File đã sửa (${list.length}):`, ...list.slice(0, TASK_FILES_SHOWN).map((f) => {
    const code = document.createElement("code");
    code.textContent = shortPath(f, t.project?.path);
    code.title = f;
    return code;
  }), ...(list.length > TASK_FILES_SHOWN ? [`+${list.length - TASK_FILES_SHOWN} file`] : []));

  const start = new Date((t.started_at + tasks.skew) * 1000);
  const tools = t.tools || t.steps.length;
  q(".task-meta").textContent = [
    `${{ voice: "nói ", text: "gõ " }[t.source] ?? ""}lúc ${start.toLocaleTimeString()}`,
    tools && `${tools} tool`,
    t.duration_ms != null && `chạy ${fmtWords(t.duration_ms)}`,
  ].filter(Boolean).join(" · ");

  q(".task-cancel").hidden = !active;
  q(".task-open").hidden = !t.session_id;
  q(".task-fix").hidden = t.status === "cancelled";
  if (t.status === "cancelled") q(".task-fix-form").hidden = true;
  updateElapsed(t, li);
}

function updateElapsed(t, li = tasks.cards.get(t.task_id)) {
  if (!li) return;
  const ms = taskElapsedMs(t);
  li.querySelector(".task-elapsed").textContent = ms == null ? "" : fmtClock(ms);
}

// Danh sách bước: icon theo ok (null = đang chạy), mặc định chỉ TASK_STEPS_SHOWN bước cuối.
// Đang ở đáy danh sách thì giữ ở đáy khi có bước mới.
function renderSteps(li, t, toBottom = false) {
  const wrap = li.querySelector(".task-steps-wrap");
  const list = li.querySelector(".task-steps");
  wrap.hidden = !t.steps.length;
  const stick = toBottom || list.scrollHeight - list.scrollTop - list.clientHeight < 24;
  const active = TASK_ACTIVE.has(t.status);
  const shown = t.showAll ? t.steps : t.steps.slice(-TASK_STEPS_SHOWN);
  list.replaceChildren(...shown.map((s) => {
    const item = document.createElement("li");
    item.className = s.ok === true ? "ok" : s.ok === false ? "fail" : active ? "run" : "unknown";
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = { ok: "✓", fail: "✗", unknown: "·" }[item.className] ?? "";
    const tool = document.createElement("b");
    tool.className = "tool";
    tool.textContent = s.tool;
    const sum = document.createElement("span");
    sum.className = "sum";
    sum.textContent = s.summary ?? "";
    sum.title = s.summary ?? "";
    item.append(icon, tool, sum);
    return item;
  }));
  li.querySelector(".task-steps-title").textContent = `Các bước (${t.steps.length})`;
  const all = li.querySelector(".task-all");
  all.hidden = t.steps.length <= TASK_STEPS_SHOWN;
  all.textContent = t.showAll ? "Thu gọn" : `Xem tất cả ${t.steps.length} bước`;
  if (stick) list.scrollTop = list.scrollHeight;
}

ui.taskList.addEventListener("click", (e) => {
  const li = e.target.closest(".task");
  const t = li && tasks.items.get(li.taskId);
  if (!t) return;
  const form = li.querySelector(".task-fix-form");
  if (e.target.closest(".task-cancel")) send({ type: "task_cancel", task_id: t.task_id });
  else if (e.target.closest(".task-open")) send({ type: "task_open", task_id: t.task_id });
  else if (e.target.closest(".task-fix")) {
    form.hidden = !form.hidden;
    if (form.hidden) return;
    if (!tasks.projects.length) send({ type: "projects" });
    fillFixForm(li, t);
    form.querySelector(".task-fix-request").value = t.request ?? "";
    form.querySelector(".task-fix-project").focus();
  } else if (e.target.closest(".task-fix-close")) form.hidden = true;
  else if (e.target.closest(".task-fix-send")) {
    const project = form.querySelector(".task-fix-project").value;
    if (!project) return;
    send({ type: "task_fix", task_id: t.task_id, project, request: form.querySelector(".task-fix-request").value.trim() });
    form.hidden = true;
  }
  else if (e.target.closest(".task-all")) {
    t.showAll = !t.showAll;
    renderSteps(li, t, true);
  }
});

// Ô chọn dự án đúng của form "Sai dự án": mọi dự án trừ dự án đã giao nhầm
function fillFixForm(li, t) {
  const sel = li.querySelector(".task-fix-project");
  const keep = sel.value;
  const names = tasks.projects.map((p) => p.name).filter((n) => n !== t.project?.name);
  sel.replaceChildren(new Option(names.length ? "Chọn dự án đúng…" : "Đang tải danh sách dự án…", ""),
    ...names.map((n) => new Option(n, n)));
  if (names.includes(keep)) sel.value = keep;
}

// Thời gian đã chạy tự tăng cho các task chưa xong
setInterval(() => {
  for (const t of tasks.items.values()) if (TASK_ACTIVE.has(t.status)) updateElapsed(t);
}, 1000);

// Thẻ gọn trong hội thoại tab Mimi, chỉ tạo khi có task_start (không tạo cho task cũ trong ảnh chụp)
function chatTask(t) {
  if (tasks.chatCards.get(t.task_id)?.isConnected) return renderChatTask(t);
  const li = document.createElement("li");
  li.className = "msg task-msg";
  const a = document.createElement("a");
  a.href = "#tasks";
  a.className = "task-pill";
  a.title = "Xem tiến độ ở tab Công việc";
  const head = document.createElement("span");
  head.className = "head";
  const name = document.createElement("b");
  name.className = "name";
  const req = document.createElement("span");
  req.className = "req";
  head.append("🛠 Giao việc cho VS Code · ", name, req);
  const chip = document.createElement("span");
  chip.className = "tstat";
  const sum = document.createElement("span");
  sum.className = "task-sum";
  a.append(head, chip, sum);
  li.append(a);
  tasks.chatCards.set(t.task_id, li);
  chatAppend(li, chats.mimi); // gắn vào DOM trước: renderChatTask bỏ thẻ không còn trong khung chat
  renderChatTask(t);
}

function renderChatTask(t) {
  const li = tasks.chatCards.get(t.task_id);
  if (!li) return;
  if (!li.isConnected) return void tasks.chatCards.delete(t.task_id); // đã bị đẩy khỏi khung chat (CHAT_MAX)
  li.querySelector(".name").textContent = t.project?.name ?? "chưa rõ dự án";
  li.querySelector(".req").textContent = t.request ? `: ${clip(t.request, 140)}` : "";
  setStatusChip(li.querySelector(".tstat"), t.status);
  const files = t.files?.length ?? 0;
  li.querySelector(".task-sum").textContent = TASK_ACTIVE.has(t.status) ? "" : [
    files && `${files} file`,
    t.duration_ms != null && fmtWords(t.duration_ms),
  ].filter(Boolean).join(" · ");
}

// Markdown cho báo cáo task và ghi chú học bài: escape HTML TRƯỚC rồi mới thêm thẻ, nên text của Claude không
// chèn được HTML. Hỗ trợ khối ```, đoạn văn, tiêu đề #, danh sách lồng nhau (- * + / 1.), bảng |a|b|,
// trích dẫn >, đường kẻ ---; inline: `code`, **đậm**, *nghiêng* / _nghiêng_, ~~gạch~~, [link](https://…).
const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function renderMarkdown(src) {
  return renderMdLines(escapeHtml(src.replace(/\r\n?/g, "\n")).split("\n"));
}

const MD_TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

// Tách một dòng bảng thành ô, bỏ | ở hai đầu
function mdCells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// Các dòng đã escape HTML → HTML (gọi lại cho nội dung trong trích dẫn)
function renderMdLines(lines) {
  const out = [];
  let para = [];
  const lists = []; // danh sách đang mở, lồng nhau theo độ thụt: { tag: "ul" | "ol", indent, items: [html] }
  const top = () => lists[lists.length - 1];
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map(inlineMd).join("<br>")}</p>`);
    para = [];
  };
  const closeList = () => { // danh sách con gắn vào cuối mục đang mở của danh sách cha
    const l = lists.pop();
    const html = `<${l.tag}>${l.items.map((x) => `<li>${x}</li>`).join("")}</${l.tag}>`;
    if (lists.length) top().items[top().items.length - 1] += html;
    else out.push(html);
  };
  const flushBlock = () => {
    flushPara();
    while (lists.length) closeList();
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flushBlock();
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      out.push(`<pre><code>${code.join("\n")}</code></pre>`); // khối chưa đóng: lấy tới hết text
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && MD_TABLE_SEP.test(lines[i + 1])) {
      flushBlock();
      const head = mdCells(line);
      const align = mdCells(lines[i + 1]).map((c) =>
        c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : "");
      const cell = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${inlineMd(c ?? "")}</${tag}>`;
      const rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(mdCells(lines[i]));
      i--;
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c, k) => cell("th", c, k)).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${head.map((_, k) => cell("td", r[k], k)).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const quote = line.match(/^\s{0,3}&gt;\s?(.*)$/);
    if (quote) {
      flushBlock();
      const inner = [quote[1]];
      while (i + 1 < lines.length && (lines[i + 1].match(/^\s{0,3}&gt;\s?(.*)$/))) inner.push(lines[++i].replace(/^\s{0,3}&gt;\s?/, ""));
      out.push(`<blockquote>${renderMdLines(inner)}</blockquote>`);
      continue;
    }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
    const item = line.match(/^(\s*)(?:([-*+])|\d+[.)])\s+(.*)$/);
    if (!line.trim()) {
      flushBlock();
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushBlock();
      out.push("<hr>");
    } else if (heading) {
      flushBlock();
      const tag = heading[1].length <= 2 ? "h4" : "h5";
      out.push(`<${tag}>${inlineMd(heading[2])}</${tag}>`);
    } else if (item) {
      flushPara();
      const indent = item[1].replace(/\t/g, "    ").length;
      const tag = item[2] ? "ul" : "ol";
      while (lists.length && indent < top().indent) closeList();
      if (lists.length && indent > top().indent) lists.push({ tag, indent, items: [] }); // thụt sâu hơn: danh sách con
      else if (lists.length && top().tag !== tag) {
        closeList();
        lists.push({ tag, indent, items: [] });
      } else if (!lists.length) lists.push({ tag, indent, items: [] });
      top().items.push(inlineMd(item[3]));
    } else if (lists.length && /^\s+\S/.test(line)) {
      top().items[top().items.length - 1] += ` ${inlineMd(line.trim())}`; // dòng thụt vào: nối tiếp mục danh sách
    } else {
      while (lists.length) closeList();
      para.push(line);
    }
  }
  flushBlock();
  return out.join("");
}

// Inline trên text đã escape. `code` và link được giữ chỗ trước, để * và _ bên trong (tên biến, URL)
// không thành chữ nghiêng.
function inlineMd(s) {
  const keep = [];
  const hold = (html) => `\u0000${keep.push(html) - 1}\u0000`;
  const emphasis = (t) => t
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/~~(.+?)~~/g, "<s>$1</s>")
    .replace(/(^|[^*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])/g, "$1<i>$2</i>")
    .replace(/(^|[^\w])_(?![\s_])(.+?)(?<![\s_])_(?!\w)/g, "$1<i>$2</i>");
  const link = (text, url) => hold(`<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`);
  s = s.replace(/`([^`]+)`/g, (_, c) => hold(`<code>${c}</code>`));
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => link(emphasis(t), u));
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<]*[^\s<.,;:!?)])/g, (_, pre, u) => pre + link(u, u));
  s = emphasis(s);
  for (let n = 0; n < 3 && s.includes("\u0000"); n++) s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[i]); // link chứa code
  return s;
}

// ---------- Học bài (tab #study, T-012): thư viện tài liệu, Mimi giảng từng phần ----------

// REST /api/study (server/study/api.py): danh sách, thêm (dán text hoặc file base64), xem, đổi tên, học lại, xoá.
// WebSocket: gửi {type: "study", doc_id} khi mở/đóng tài liệu (đóng = null, hoặc khi rời tab), câu hỏi gửi bằng
// ask như thường; server trả answer_start có doc_id và study_doc mỗi khi tiến độ (phần k/N) đổi.

const STUDY_START = "Bắt đầu nhé";
const STUDY_QUICK = [
  ["Tiếp phần sau", "tiếp", true],
  ["Giảng lại dễ hơn", "chưa hiểu lắm, giảng lại phần này dễ hơn"],
  ["Cho ví dụ", "cho mình thêm ví dụ"],
  ["Kiểm tra mình", "kiểm tra mình"],
  ["Tóm tắt", "tóm tắt những gì đã học"],
];
const MAX_UPLOAD = 10 * 2 ** 20; // khớp server/study/api.py

async function studyApi(path = "", method = "GET", body = undefined) {
  const res = await fetch(`/api/study${path}`, body === undefined ? { method }
    : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw Object.assign(new Error(data?.detail ?? `HTTP ${res.status}`), { status: res.status });
  return data;
}

function onStudyHello(info) {
  study.info = info ?? null;
  ui.studyTab.hidden = !study.info;
  showTab(); // đang ở #study mà server tắt tính năng thì về Mimi; kết nối lại thì gửi lại tài liệu đang mở
  if (study.info) loadDocs();
}

// Báo server tài liệu đang học. Chỉ gửi khi đã tải được tài liệu (id cũ trong localStorage có thể đã bị xoá).
function sendStudy() {
  if (study.info) send({ type: "study", doc_id: studyActive() ? study.doc.id : null });
}

async function loadDocs() {
  try {
    study.docs = (await studyApi()).docs;
  } catch (e) {
    ui.studyNote.textContent = `Không tải được thư viện: ${e.message}`;
  }
  renderDocList();
}

function partLabel(d) {
  if (!d.total) return d.started ? "Đang học" : "Chưa học";
  const name = d.parts?.[String(d.part)];
  return `Phần ${d.part}/${d.total}${name ? `: ${name}` : ""}`;
}

function renderDocList() {
  ui.studyEmpty.hidden = study.docs.length > 0;
  ui.studyList.replaceChildren(...study.docs.map((d) => {
    const li = ui.docTpl.content.firstElementChild.cloneNode(true);
    li.querySelector(".doc-title").textContent = d.title;
    li.querySelector(".doc-meta").textContent = [d.source === "paste" ? "dán text" : d.source,
      `${d.chars.toLocaleString("vi-VN")} ký tự`, timeAgo(d.updated)].join(" · ");
    li.querySelector(".fill").style.width = `${d.total ? (100 * d.part) / d.total : 0}%`;
    li.querySelector(".doc-part").textContent = partLabel(d);
    li.querySelector(".doc-open").onclick = () => openDoc(d.id);
    li.querySelector(".doc-rename").onclick = () => renameDoc(d);
    li.querySelector(".doc-delete").onclick = () => deleteDoc(d);
    return li;
  }));
}

// Thư viện hay một tài liệu; tài liệu nhớ từ lần trước thì tải khi mở tab
function renderStudyView() {
  ui.studyLib.hidden = !!study.docId;
  ui.studyDoc.hidden = !study.docId;
  if (study.docId && !study.doc && study.loading !== study.docId) openDoc(study.docId);
}

// Câu trả lời của gia sư đang chạy thì dừng trước khi đổi tài liệu, để không viết vào khung chat đã xoá
function stopStudyAnswer() {
  if (bridge.asking && bridge.current?.docId) stopSpeaking();
}

async function openDoc(id, start = false) {
  if (study.doc?.id !== id) stopStudyAnswer();
  study.docId = id;
  study.doc = null;
  study.loading = id;
  localStorage.setItem("studyDoc", id);
  Object.assign(chats.study, { pendingUser: null, reply: null, follow: true });
  ui.studyChat.replaceChildren();
  ui.studySource.open = false;
  ui.studyDocTitle.textContent = study.docs.find((d) => d.id === id)?.title ?? "…";
  ui.studyQuick.replaceChildren();
  renderStudyView();
  refreshTalkButton();
  let full;
  try {
    full = await studyApi(`/${encodeURIComponent(id)}`);
  } catch (e) {
    if (study.loading === id) study.loading = null;
    if (study.docId !== id) return;
    if (e.status === 404) return closeDoc();
    showError(`Không mở được tài liệu: ${e.message}`);
    return;
  }
  if (study.docId !== id) return; // trong lúc tải đã mở tài liệu khác
  study.loading = null;
  study.doc = full;
  ui.studySourceText.innerHTML = renderMarkdown(full.text);
  const prev = chat;
  chat = chats.study;
  for (const h of full.history) {
    chatUser(h.question, true, chats.study);
    chatReply(...splitCode(h.answer), true);
  }
  chat = prev;
  ui.studyChat.scrollTop = ui.studyChat.scrollHeight;
  renderStudyDoc();
  sendStudy();
  refreshTalkButton();
  if (start && !full.history.length) askNow(STUDY_START);
}

function closeDoc() {
  stopStudyAnswer();
  study.docId = study.doc = study.loading = null;
  localStorage.removeItem("studyDoc");
  ui.studyChat.replaceChildren();
  renderStudyView();
  sendStudy();
  refreshTalkButton();
  renderDocList();
}

function renderStudyDoc() {
  const d = study.doc;
  if (!d) return;
  ui.studyDocTitle.textContent = d.title;
  ui.studyBar.style.width = `${d.total ? (100 * d.part) / d.total : 0}%`;
  ui.studyProgress.textContent = partLabel(d);
  ui.studyReset.hidden = !d.started;
  const started = d.started || d.history?.length;
  const quick = started ? STUDY_QUICK : [["Bắt đầu học", STUDY_START, true]];
  ui.studyQuick.replaceChildren(...quick.map(([label, text, main]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.className = main ? "main" : "";
    b.onclick = () => askNow(text);
    return b;
  }));
  refreshTalkButton();
}

// Server báo tiến độ / session mới của một tài liệu (sau dòng [Phần k/N] và khi lượt trả lời xong)
function onStudyDoc(doc) {
  const i = study.docs.findIndex((d) => d.id === doc.id);
  if (i >= 0) study.docs.splice(i, 1);
  study.docs.unshift(doc); // vừa học: lên đầu như thứ tự server (updated mới nhất)
  if (study.doc?.id === doc.id) {
    Object.assign(study.doc, doc);
    renderStudyDoc();
  }
  renderDocList();
}

async function createDoc(body) {
  ui.studyNote.textContent = "Đang lưu…";
  ui.studyAdd.disabled = true;
  ui.studyFile.parentElement.classList.add("busy");
  try {
    const doc = await studyApi("", "POST", body);
    ui.studyTitle.value = ui.studyText.value = "";
    ui.studyNote.textContent = "";
    study.docs.unshift(doc);
    renderDocList();
    openDoc(doc.id, true); // tài liệu mới: Mimi đọc và giảng phần 1 luôn
  } catch (e) {
    ui.studyNote.textContent = e.message;
  } finally {
    ui.studyFile.parentElement.classList.remove("busy");
    refreshTalkButton();
  }
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] ?? "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

async function renameDoc(d) {
  const title = prompt("Tên mới cho tài liệu", d.title)?.trim();
  if (!title || title === d.title) return;
  try {
    onStudyDoc(await studyApi(`/${encodeURIComponent(d.id)}`, "PATCH", { title }));
  } catch (e) {
    ui.studyNote.textContent = e.message;
  }
}

async function deleteDoc(d) {
  if (!confirm(`Xoá "${d.title}"? Mất cả tài liệu, tiến độ và lịch sử hỏi đáp.`)) return;
  try {
    await studyApi(`/${encodeURIComponent(d.id)}`, "DELETE");
  } catch (e) {
    if (e.status !== 404) return void (ui.studyNote.textContent = e.message);
  }
  study.docs = study.docs.filter((x) => x.id !== d.id);
  if (study.docId === d.id) closeDoc();
  renderDocList();
}

ui.studyForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = ui.studyText.value;
  if (!text.trim()) return void (ui.studyNote.textContent = "Dán nội dung tài liệu vào ô trên trước đã.");
  createDoc({ text, title: ui.studyTitle.value.trim() });
});
ui.studyText.addEventListener("keydown", (e) => {
  if (e.code === "Enter" && e.ctrlKey) {
    e.preventDefault();
    ui.studyForm.requestSubmit();
  }
});
ui.studyFile.addEventListener("change", async () => {
  const file = ui.studyFile.files[0];
  ui.studyFile.value = ""; // chọn lại cùng file vẫn có sự kiện change
  if (!file) return;
  if (file.size > MAX_UPLOAD) return void (ui.studyNote.textContent = `File lớn quá ${MAX_UPLOAD / 2 ** 20}MB.`);
  try {
    createDoc({ filename: file.name, data_b64: await fileBase64(file), title: ui.studyTitle.value.trim() });
  } catch (e) {
    ui.studyNote.textContent = `Không đọc được file: ${e.message}`;
  }
});
ui.studyBack.onclick = closeDoc;
ui.studyStop.onclick = () => stopSpeaking();
ui.studyComposer.addEventListener("submit", (e) => {
  e.preventDefault();
  if (askNow(ui.studyInput.value.trim())) ui.studyInput.value = "";
});
ui.studyReset.onclick = async () => {
  const d = study.doc;
  if (!d || !confirm("Học lại từ đầu? Mimi quên bài đã giảng, lịch sử hỏi đáp của tài liệu này bị xoá.")) return;
  stopStudyAnswer();
  try {
    const doc = await studyApi(`/${encodeURIComponent(d.id)}/reset`, "POST");
    if (study.doc?.id !== d.id) return;
    d.history = [];
    Object.assign(chats.study, { pendingUser: null, reply: null, follow: true });
    ui.studyChat.replaceChildren();
    onStudyDoc(doc);
  } catch (e) {
    showError(`Không học lại được: ${e.message}`);
  }
};

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
  // Server tự hỏi sau lượt nói: auto_ask, hoặc đang học bài (câu nói ở tab Học bài luôn hỏi gia sư)
  if ((bridge.info?.auto_ask || studyActive()) && msg.text.trim()) bridge.pendingTurn = li;
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
// PWA: cần HTTPS có chứng chỉ được tin (hoặc localhost); chứng chỉ tự ký chưa cài lên máy thì đăng ký thất bại
navigator.serviceWorker?.register("sw.js").catch((e) => console.warn("service worker:", e.message));
