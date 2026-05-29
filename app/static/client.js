"use strict";

const SESSION_ID = crypto.randomUUID();
const WS_PROTOCOL = location.protocol === "https:" ? "wss" : "ws";
const WS_URL = `${WS_PROTOCOL}://${location.host}/ws/${SESSION_ID}`;
const SAMPLE_RATE = 16000;
const CHUNK_SIZE = 2048;

const btnTalk = document.getElementById("btn-talk");
const btnStop = document.getElementById("btn-stop");
const elStation = document.getElementById("station-name");
const elDocks = document.getElementById("dock-count");
const elStatus = document.getElementById("monitor-status");
const elLog = document.getElementById("log");
const elMessage = document.getElementById("message");
const elCandidatesPanel = document.getElementById("candidates-panel");
const elCandidatesList = document.getElementById("candidates-list");
const elOptionsPanel = document.getElementById("options-panel");
const elOptionsList = document.getElementById("options-list");

let ws = null;
let audioCtx = null;
let micStream = null;
let processorNode = null;
let isTalking = false;
let playbackCtx = null;
let playbackQueue = [];
let isPlaying = false;
let currentPlaybackSource = null;
let shouldReconnect = true;
let isConnected = false;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    isConnected = true;
    btnTalk.disabled = false;
    btnStop.style.display = "block";
    btnStop.disabled = true;  // enabled only once monitoring starts
    log("Connected. Hold to speak your destination.");
  };

  ws.onclose = () => {
    isConnected = false;
    btnTalk.disabled = true;
    if (!shouldReconnect) {
      log("Session ended. Reload to start again.");
      return;
    }
    log("Disconnected. Reconnecting...");
    setTimeout(connect, 3000);
  };

  ws.onerror = (event) => {
    console.error("WebSocket error", event);
    log("Connection error. Check the server terminal.");
  };

  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      handleServerEvent(JSON.parse(event.data));
      return;
    }
    handleAudioFrame(event.data);
  };
}

function handleServerEvent(data) {
  if (data.type === "session_ended") {
    shouldReconnect = false;
    btnTalk.disabled = true;
    btnStop.style.display = "none";
    elStatus.textContent = "Session ended";
    log("Session ended. Reload to start again.");
    return;
  }
  if (data.type === "error") {
    log(data.message || "Server error.");
    return;
  }
  if (data.type !== "status") {
    return;
  }

  const stopped = data.monitor_status === "STOPPED";
  elStation.textContent = stopped ? "No station selected" : (data.target_station_name || "No station selected");
  elDocks.textContent = stopped ? "—" : (data.docks == null ? "No live dock count yet" : `${data.docks} open docks`);
  elStatus.textContent = (data.monitor_status || "Not started").replaceAll("_", " ");
  renderMessage(data.message || "");
  renderStationRows(elCandidatesPanel, elCandidatesList, data.candidates || []);
  renderStationRows(elOptionsPanel, elOptionsList, data.options || []);
  const monitoring = ["MONITORING_SAFE", "MONITORING_WATCH", "MONITORING_WARNING", "ALERTED"]
    .includes(data.monitor_status);
  btnStop.disabled = !monitoring;
}

function renderMessage(message) {
  if (!message) {
    elMessage.style.display = "none";
    elMessage.textContent = "";
    return;
  }
  elMessage.style.display = "block";
  elMessage.textContent = message;
}

function renderStationRows(panel, list, rows) {
  list.innerHTML = "";
  if (!rows.length) {
    panel.style.display = "none";
    return;
  }

  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    const stationName = row.station_name || row.name || "Nearby station";
    const docks = row.docks_available ?? row.available_docks ?? row.num_docks_available ?? 0;
    const distance = row.distance_m ?? row.distance_meters;
    const role = row.candidate_role;
    const roleLabel = role === "recommended" ? "Recommended" : role === "closest" ? "Closest" : "";

    const item = document.createElement("div");
    item.className = "option-row";

    const index = document.createElement("div");
    index.className = "option-index";
    index.textContent = `${i + 1}`;

    const name = document.createElement("div");
    name.className = "option-name";
    name.textContent = stationName;

    const meta = document.createElement("div");
    meta.className = "option-meta";
    const stationMeta = distance == null ? `${docks} docks` : `${docks} docks, ${distance} m`;
    meta.textContent = roleLabel ? `${roleLabel} - ${stationMeta}` : stationMeta;

    item.append(index, name, meta);
    list.append(item);
  }

  panel.style.display = "grid";
}

function ensurePlaybackCtx() {
  if (!playbackCtx) {
    playbackCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 24000,
    });
  }
  if (playbackCtx.state === "suspended") {
    playbackCtx.resume().catch(() => {});
  }
}

function handleAudioFrame(arrayBuffer) {
  ensurePlaybackCtx();
  const pcm16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(pcm16.length);
  for (let i = 0; i < pcm16.length; i += 1) {
    float32[i] = pcm16[i] / 32768;
  }
  const buffer = playbackCtx.createBuffer(1, float32.length, 24000);
  buffer.copyToChannel(float32, 0);
  playbackQueue.push(buffer);
  if (!isPlaying) {
    playNext();
  }
}

function playNext() {
  if (playbackQueue.length === 0) {
    isPlaying = false;
    currentPlaybackSource = null;
    if (isConnected && !isTalking) {
      btnTalk.disabled = false;
      btnTalk.textContent = "Hold to talk";
    }
    return;
  }

  isPlaying = true;
  if (!isTalking) {
    btnTalk.disabled = false;
    btnTalk.textContent = "Hold to interrupt";
  }

  const source = playbackCtx.createBufferSource();
  source.buffer = playbackQueue.shift();
  source.connect(playbackCtx.destination);
  source.onended = playNext;
  source.start();
  currentPlaybackSource = source;
}

async function startMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    log("Microphone unavailable. Use HTTPS or localhost.");
    btnTalk.disabled = true;
    return false;
  }

  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
  }
  if (audioCtx.state === "suspended") {
    await audioCtx.resume();
  }

  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch (error) {
    log(`Microphone access denied: ${error.message}`);
    btnTalk.disabled = true;
    return false;
  }

  const source = audioCtx.createMediaStreamSource(micStream);
  processorNode = audioCtx.createScriptProcessor(CHUNK_SIZE, 1, 1);

  processorNode.onaudioprocess = (event) => {
    if (!isTalking || !ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }
    const float32 = event.inputBuffer.getChannelData(0);
    const pcm16 = floatToPCM16(float32);
    ws.send(pcm16.buffer);
  };

  source.connect(processorNode);
  processorNode.connect(audioCtx.destination);
  log("Mic ready. Hold to talk.");
  return true;
}

function sendCurrentLocation() {
  if (!navigator.geolocation || !ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (position) => {
      ws.send(JSON.stringify({
        type: "current_location",
        lat: position.coords.latitude,
        lon: position.coords.longitude,
        accuracy_m: position.coords.accuracy,
        observed_at: new Date().toISOString(),
      }));
    },
    () => {},
    {
      enableHighAccuracy: true,
      maximumAge: 15000,
      timeout: 5000,
    },
  );
}

function floatToPCM16(float32) {
  const pcm = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i += 1) {
    const value = Math.max(-1, Math.min(1, float32[i]));
    pcm[i] = value < 0 ? value * 32768 : value * 32767;
  }
  return pcm;
}

function stopTalking() {
  if (!isTalking) {
    return;
  }
  isTalking = false;
  btnTalk.classList.remove("listening");
  btnTalk.textContent = "Hold to talk";
  log("Processing...");
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "end_of_speech" }));
  }
}

btnTalk.addEventListener("pointerdown", async (event) => {
  event.preventDefault();
  btnTalk.setPointerCapture(event.pointerId);

  if (!micStream) {
    const ok = await startMic();
    if (!ok) {
      return;
    }
  }

  ensurePlaybackCtx();
  sendCurrentLocation();
  if (isPlaying) {
    if (currentPlaybackSource) {
      try {
        currentPlaybackSource.stop();
      } catch (_) {}
      currentPlaybackSource = null;
    }
    playbackQueue = [];
    isPlaying = false;
    log("Listening...");
  }

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "start_of_speech" }));
  }
  isTalking = true;
  btnTalk.classList.add("listening");
  btnTalk.textContent = "Listening...";
  log("Listening...");
});

btnTalk.addEventListener("pointerup", stopTalking);
btnTalk.addEventListener("pointercancel", stopTalking);
document.addEventListener("pointerup", stopTalking);
document.addEventListener("pointercancel", stopTalking);

btnStop.addEventListener("click", async () => {
  log("Stopping...");
  await fetch(`/session/${SESSION_ID}/stop`, { method: "POST" });
  btnStop.disabled = true;
  elStatus.textContent = "Stopped";
});

function log(message) {
  elLog.textContent = message;
}

btnTalk.disabled = true;
connect();
