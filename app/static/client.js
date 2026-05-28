/**
 * DockTalk browser client
 *
 * Audio pipeline:
 *   getUserMedia → AudioContext → ScriptProcessorNode → PCM16 → WebSocket (binary)
 *
 * Playback pipeline:
 *   WebSocket binary → PCM16 bytes → AudioContext buffer → gapless queue
 *
 * Status events (JSON text frames):
 *   { type: "status", monitor_status, target_station_id,
 *     target_station_name, docks }
 */

"use strict";

// ── Config ──────────────────────────────────────────────────────────────────
const SESSION_ID    = crypto.randomUUID();
// Use wss:// when the page itself is served over HTTPS (required for mobile
// browsers that block plain ws:// on non-localhost origins).
const WS_PROTOCOL   = location.protocol === "https:" ? "wss" : "ws";
const WS_URL        = `${WS_PROTOCOL}://${location.host}/ws/${SESSION_ID}`;
const SAMPLE_RATE   = 16000;   // PCM16, mono, 16 kHz — what Gemini Live expects
const CHUNK_SIZE    = 2048;    // ScriptProcessor buffer size

// ── DOM refs ────────────────────────────────────────────────────────────────
const btnTalk      = document.getElementById("btn-talk");
const btnStop      = document.getElementById("btn-stop");
const elStation    = document.getElementById("station-name");
const elDocks      = document.getElementById("dock-count");
const elStatus     = document.getElementById("monitor-status");
const elLog        = document.getElementById("log");

// ── State ───────────────────────────────────────────────────────────────────
let ws              = null;
let audioCtx        = null;
let micStream       = null;
let processorNode   = null;
let isTalking       = false;
let playbackQueue   = [];   // Array of AudioBuffer waiting to play
let isPlaying       = false;
let playbackCtx     = null;
let currentPlaybackSource = null; // active BufferSource; stopped on rider interrupt
let shouldReconnect = true; // set to false when the session is explicitly ended
let isConnected     = false; // true while WebSocket is open

// ── WebSocket ────────────────────────────────────────────────────────────────

function connect() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    log("Connected.");
    isConnected = true;
    btnTalk.disabled = false;
    btnStop.style.display = "block";
  };

  ws.onclose = () => {
    isConnected = false;
    if (!shouldReconnect) {
      log("Session ended. Reload the page to start a new trip.");
      return;
    }
    log("Disconnected. Reconnecting…");
    btnTalk.disabled = true;
    setTimeout(connect, 3000);
  };

  ws.onerror = (e) => {
    log("WebSocket error — check console.");
    console.error("WS error", e);
  };

  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      handleStatusEvent(JSON.parse(event.data));
    } else {
      handleAudioFrame(event.data);
    }
  };
}

// ── Status events ─────────────────────────────────────────────────────────

function handleStatusEvent(data) {
  if (data.type === "session_ended") {
    // Server confirmed the session is over (rider said cancel / returned bike).
    // Stop reconnecting so we don't spin up a new Gemini Live session.
    shouldReconnect = false;
    btnTalk.disabled = true;
    btnStop.style.display = "none";
    elStatus.textContent = "SESSION ENDED";
    log("Session ended. Reload the page to start a new trip.");
    return;
  }
  if (data.type === "status") {
    elStation.textContent = data.target_station_name || "No station selected";
    elDocks.textContent   = data.docks != null
      ? `${data.docks} open docks`
      : "—";
    elStatus.textContent  = (data.monitor_status || "NOT STARTED").replace(/_/g, " ");
  }
}

// ── Playback ──────────────────────────────────────────────────────────────

function ensurePlaybackCtx() {
  if (!playbackCtx) {
    playbackCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 24000, // Gemini Live outputs at 24 kHz
    });
  }
  // Resume if the browser suspended the context (autoplay policy).
  // Must be called inside a user-gesture handler to succeed.
  if (playbackCtx.state === "suspended") {
    playbackCtx.resume().catch(() => {});
  }
}

function handleAudioFrame(arrayBuffer) {
  ensurePlaybackCtx();
  const pcm16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(pcm16.length);
  for (let i = 0; i < pcm16.length; i++) {
    float32[i] = pcm16[i] / 32768;
  }
  const buf = playbackCtx.createBuffer(1, float32.length, 24000);
  buf.copyToChannel(float32, 0);
  playbackQueue.push(buf);
  if (!isPlaying) playNext();
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
  // Keep button enabled so the rider can press to interrupt the agent.
  if (!isTalking) {
    btnTalk.disabled = false;
    btnTalk.textContent = "Hold to interrupt";
  }
  const buf = playbackQueue.shift();
  const src = playbackCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playbackCtx.destination);
  src.onended = playNext;
  src.start();
  currentPlaybackSource = src;
}

// ── Microphone capture ────────────────────────────────────────────────────

async function startMic() {
  // navigator.mediaDevices is undefined on non-HTTPS pages in mobile browsers.
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    log("⚠️ Microphone unavailable. Open this page over HTTPS or use localhost.");
    btnTalk.disabled = true;
    return false;
  }
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
  }
  if (audioCtx.state === "suspended") await audioCtx.resume();
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  } catch (err) {
    log(`⚠️ Microphone access denied: ${err.message}`);
    btnTalk.disabled = true;
    return false;
  }
  const source = audioCtx.createMediaStreamSource(micStream);
  processorNode = audioCtx.createScriptProcessor(CHUNK_SIZE, 1, 1);

  processorNode.onaudioprocess = (e) => {
    if (!isTalking || !ws || ws.readyState !== WebSocket.OPEN) return;
    const float32 = e.inputBuffer.getChannelData(0);
    const pcm16 = floatToPCM16(float32);
    ws.send(pcm16.buffer);
  };

  source.connect(processorNode);
  processorNode.connect(audioCtx.destination);
  log("Mic ready. Hold the button to talk.");
  return true;
}

function stopMic() {
  if (processorNode) { processorNode.disconnect(); processorNode = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
}

function floatToPCM16(float32) {
  const pcm = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const clamped = Math.max(-1, Math.min(1, float32[i]));
    pcm[i] = clamped < 0 ? clamped * 32768 : clamped * 32767;
  }
  return pcm;
}

// ── Button handlers ───────────────────────────────────────────────────────

function stopTalking() {
  if (!isTalking) return;
  isTalking = false;
  btnTalk.textContent = "Hold to talk";
  log("Processing…");
  // Tell Gemini Live the rider has finished speaking.
  // With automatic VAD disabled this is what triggers the model to respond.
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "end_of_speech" }));
  }
}

btnTalk.addEventListener("pointerdown", async (e) => {
  e.preventDefault();
  btnTalk.setPointerCapture(e.pointerId); // ensure pointerup always fires on this element
  if (!micStream) {
    const ok = await startMic();
    if (!ok) return;  // mic unavailable — error already shown
  }
  // Resume the playback AudioContext inside this user gesture so the browser
  // autoplay policy allows audio to play when Gemini responds.
  ensurePlaybackCtx();
  // If the agent is still speaking, interrupt it: stop the current audio
  // source, drain the queue, and let Gemini know we are starting new speech.
  if (isPlaying) {
    if (currentPlaybackSource) {
      try { currentPlaybackSource.stop(); } catch (_) {}
      currentPlaybackSource = null;
    }
    playbackQueue = [];
    isPlaying = false;
    log("Interrupted. Listening…");
  }
  // Signal Gemini Live that speech is starting (VAD is disabled; it waits for
  // this signal before it starts listening to the incoming audio stream).
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "start_of_speech" }));
  }
  isTalking = true;
  btnTalk.textContent = "Listening…";
  if (!isPlaying) log("Listening…");
});

// Listen on both button and document to guarantee we catch the release
btnTalk.addEventListener("pointerup", stopTalking);
btnTalk.addEventListener("pointercancel", stopTalking);
document.addEventListener("pointerup", stopTalking);
document.addEventListener("pointercancel", stopTalking);

btnStop.addEventListener("click", async () => {
  log("Stopping…");
  await fetch(`/session/${SESSION_ID}/stop`, { method: "POST" });
  btnStop.disabled = true;
  elStatus.textContent = "STOPPED";
});

// ── Helpers ───────────────────────────────────────────────────────────────

function log(msg) {
  elLog.textContent = msg;
}

// ── Init ──────────────────────────────────────────────────────────────────

btnTalk.disabled = true;
connect();
