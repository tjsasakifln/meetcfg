// Capture mic + meeting-tab audio, downsample via the worklet, stream 16 kHz
// PCM to the backend over two WebSockets, and show the latest orientation.
//
// Adapted from live-meeting-assistant's app.js (MIT, Ben Linford): the
// capture/reconnect path is kept as-is; export, pinning, enrollment, the
// mode/party toggles and the card grid were removed.

const $ = (id) => document.getElementById(id);
const transcriptEl = $("transcript");
const statusEl = $("status");

let audioCtx = null;
const sources = {};       // name -> { stream, node, srcNode, ws, ... }
// One meeting per page load, unless ?meeting=<id> pins it — which is how the
// test mode (tools/inject_transcript.py --meeting test) drives this page.
const meetingId = new URLSearchParams(location.search).get("meeting") || crypto.randomUUID();
const lineEls = {};       // transcript line id -> DOM element (echo retraction)
let copilotWs = null;

function setStatus(msg) { statusEl.textContent = msg; }

function wsUrl(path, params) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({ ...params, meeting: meetingId });
  return `${proto}://${location.host}${path}?${q}`;
}

async function ensureContext() {
  if (audioCtx) return audioCtx;
  // Don't cache until the worklet module is actually loaded — caching a
  // context whose addModule() failed would make every later retry throw on
  // `new AudioWorkletNode(...)` forever, with no way to recover but reload.
  const ctx = new AudioContext();
  try {
    await ctx.audioWorklet.addModule("/static/pcm-worklet.js");
  } catch (err) {
    ctx.close();
    throw err;
  }
  audioCtx = ctx;
  return audioCtx;
}

function makeAudioWs(name) {
  const ws = new WebSocket(wsUrl("/ws/audio", { source: name }));
  ws.binaryType = "arraybuffer";
  return ws;
}

// Wire one MediaStream's audio into the worklet -> WS pipeline.
async function startSource(name, stream) {
  try {
    const ctx = await ensureContext();
    if (ctx.state === "suspended") await ctx.resume();

    const ws = makeAudioWs(name);
    ws.onopen = () => setStatus(`capturando: ${activeNames().join(" + ")}`);
    ws.onclose = () => scheduleReconnect(name);
    ws.onerror = () => setStatus(`erro de websocket (${name})`);

    const srcNode = ctx.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(ctx, "pcm-worklet");
    worklet.port.onmessage = (ev) => {
      // Look the socket up each time: reconnects swap it out under us.
      const s = sources[name];
      if (s && s.ws.readyState === WebSocket.OPEN) s.ws.send(ev.data);
    };
    srcNode.connect(worklet);
    // Do NOT connect to destination — we don't want to play the audio back.

    sources[name] = { stream, node: worklet, srcNode, ws, userStopped: false, reconnecting: false };
  } catch (err) {
    // Not registered in `sources` yet, so stop()'s cleanup can't reach this
    // stream's tracks — stop them here or the mic/tab-share stays hot.
    try { stream.getTracks().forEach((t) => t.stop()); } catch {}
    throw err;
  }
}

// The backend dropped (e.g. watchdog self-restart). The media stream is still
// alive in this page, so keep it and re-attach when the server comes back.
function scheduleReconnect(name) {
  const s = sources[name];
  if (!s || s.userStopped || s.reconnecting) return;
  s.reconnecting = true;
  setStatus("backend caiu — reconectando… (a captura continua)");
  const attempt = () => {
    if (!sources[name] || s.userStopped) return;
    const nw = makeAudioWs(name);
    nw.onopen = () => {
      s.ws = nw;
      s.reconnecting = false;
      nw.onclose = () => scheduleReconnect(name);
      setStatus(`reconectado — capturando: ${activeNames().join(" + ")}`);
      connectCopilot();
    };
    nw.onclose = () => { if (s.reconnecting) setTimeout(attempt, 3000); };
    nw.onerror = () => {};
  };
  attempt();
}

function activeNames() { return Object.keys(sources); }

function stopSource(name) {
  const s = sources[name];
  if (!s) return;
  s.userStopped = true;
  try { s.srcNode.disconnect(); } catch {}
  try { s.node.disconnect(); } catch {}
  try { s.stream.getTracks().forEach((t) => t.stop()); } catch {}
  try { if (s.ws.readyState === WebSocket.OPEN) s.ws.close(); } catch {}
  delete sources[name];
  if (activeNames().length === 0) {
    setStatus("parado");
    $("startBtn").disabled = false;
    $("stopBtn").disabled = true;
  }
}

async function start() {
  // Browsers expose mic/screen capture only in secure contexts. In WSL that
  // means reaching the backend as http://localhost:5005 from Windows Chrome.
  if (!navigator.mediaDevices) {
    setStatus("captura bloqueada: abra esta página como http://localhost:5005");
    return;
  }
  $("startBtn").disabled = true;
  const wantMic = $("micToggle").checked;
  const wantSys = $("sysToggle").checked;
  if (!wantMic && !wantSys) {
    setStatus("marque pelo menos uma fonte de áudio");
    $("startBtn").disabled = false;
    return;
  }

  connectCopilot();
  try {
    if (wantMic) {
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
      await startSource("mic", mic);
    }
    if (wantSys) {
      // Browsers require a video track to grant tab audio; we capture it and
      // immediately drop the video, keeping only the audio.
      setStatus("escolha a aba do Meet e MARQUE 'compartilhar áudio'…");
      const disp = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      disp.getVideoTracks().forEach((t) => t.stop());
      if (disp.getAudioTracks().length === 0) {
        setStatus("nenhum áudio capturado — recompartilhe e marque 'compartilhar áudio'");
        disp.getTracks().forEach((t) => t.stop());
      } else {
        await startSource("system", disp);
      }
    }
    $("stopBtn").disabled = false;
  } catch (err) {
    console.error(err);
    // Any source already wired up (e.g. mic succeeded, tab-share was
    // cancelled) must be torn down here — otherwise a retry opens a second
    // stream/worklet/websocket on top of the still-live one. Do this before
    // setStatus: stopSource() itself sets status to "parado" once the last
    // source is gone, which would otherwise clobber the error message below.
    stop();
    setStatus("erro na captura: " + err.message);
    $("startBtn").disabled = false;
  }
}

function stop() { activeNames().forEach((n) => stopSource(n)); }

// --- transcript ---
function addLine(msg) {
  const text = (msg.text || "").trim();
  if (!text) return;
  if (msg.id !== undefined && lineEls[msg.id]) return; // audio ws + broadcast both deliver
  const line = document.createElement("div");
  line.className = "line " + (msg.source === "mic" ? "me" : "other");
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = msg.source === "mic" ? "Tiago" : "Lead";
  const body = document.createElement("span");
  body.className = "body";
  body.textContent = text;
  line.append(who, body);
  if (msg.id !== undefined) lineEls[msg.id] = line;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  // keep the DOM bounded — nothing here is meant to be a permanent record
  while (transcriptEl.children.length > 200) transcriptEl.removeChild(transcriptEl.firstChild);
}

// The server retracts a mic line when it turns out to be echo of a lead line.
function retractLine(id) {
  const el = lineEls[id];
  if (el) { el.remove(); delete lineEls[id]; }
}

// --- copilot ---
function connectCopilot() {
  if (copilotWs && (copilotWs.readyState === WebSocket.OPEN || copilotWs.readyState === WebSocket.CONNECTING)) return;
  copilotWs = new WebSocket(wsUrl("/ws/copilot", {}));
  copilotWs.onopen = () => { $("adviseBtn").disabled = false; };
  copilotWs.onclose = () => {
    $("adviseBtn").disabled = true;
    setCopilotStatus("");
    if (activeNames().length > 0) setTimeout(connectCopilot, 3000);
  };
  copilotWs.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "advice") renderAdvice(msg);
    else if (msg.type === "transcript") addLine(msg);
    else if (msg.type === "retract") retractLine(msg.id);
    else if (msg.type === "handraiser_context") renderLeadContext(msg);
    else if (msg.type === "copilot_status") {
      if (msg.state === "thinking") setCopilotStatus("pensando…", true);
      else if (msg.state === "error") setCopilotStatus("erro: " + (msg.msg || "falhou"));
      else if (msg.state === "silent") {
        // "--": nothing to add. The previous orientation stays, just dimmed.
        $("advice").classList.add("stale");
        setCopilotStatus(`sem intervenção · ${msg.at || ""}`);
      } else {
        setCopilotStatus(msg.last_ms ? `pronto · última ${(msg.last_ms / 1000).toFixed(1)}s` : "pronto");
      }
    }
  };
}

function setCopilotStatus(text, pulsing = false) {
  const el = $("copilotStatus");
  el.textContent = text;
  el.classList.toggle("pulsing", pulsing);
}

// Only ever ONE orientation on screen: the latest replaces the previous.
function renderAdvice(msg) {
  const panel = $("advice");
  panel.className = "advice";
  panel.textContent = "";
  const rows = [["SINAL", msg.sinal, "sinal"], ["FAÇA", msg.faca, "faca"], ["DIGA", msg.diga, "diga"]];
  for (const [label, value, cls] of rows) {
    if (!value) continue;
    const row = document.createElement("div");
    row.className = `advice-row row-${cls}`;
    const l = document.createElement("div");
    l.className = "advice-label";
    l.textContent = label;
    const t = document.createElement("div");
    t.className = "advice-text";
    t.textContent = cls === "diga" ? `“${value}”` : value;
    row.append(l, t);
    panel.appendChild(row);
  }
  $("adviceMeta").textContent =
    `${msg.at || ""}${msg.elapsed_ms ? ` · ${(msg.elapsed_ms / 1000).toFixed(1)}s` : ""}${msg.replay ? " · (anterior)" : ""}`;
}

const LEAD_CONTEXT_FIELDS = [
  ["resumo", "resumo"],
  ["nucleo_problema", "núcleo e problema"],
  ["o_que_ja_se_sabe", "o que já se sabe"],
  ["o_que_e_unknown", "o que é UNKNOWN"],
  ["perguntas_sugeridas", "perguntas sugeridas"],
  ["limites_conflito", "limites/conflito"],
  ["proximo_estado", "próximo estado"],
  ["evidencia_tecnica", "evidência técnica disponível"],
  ["empresa", "empresa"],
  ["por_que_chegou_agora", "por que chegou agora"],
  ["canal", "canal"],
  ["intencao", "intenção"],
  ["fatos_verificaveis", "fatos verificáveis"],
  ["o_que_nao_sabemos", "o que NÃO sabemos"],
  ["oportunidade_contrato", "oportunidade/contrato relevante"],
  ["ultimo_touch_outcome", "último touch/outcome"],
  ["proximo_estado_comercial", "próximo estado comercial"],
  ["freshness", "freshness"],
  ["status", "status"],
];

const LEAD_DETAIL_FIELDS = [
  ["handraiser_id", "ID técnico"],
  ["nucleus_id", "núcleo (id)"],
  ["receipt", "recibo"],
  ["schema", "contrato"],
  ["schema_hash", "hash do contrato"],
];

function fieldText(value) {
  if (Array.isArray(value)) {
    const items = value.filter((v) => typeof v === "string" && v.trim());
    return items.length ? items.join("; ") : "UNKNOWN";
  }
  if (value === null || value === undefined || value === "") return "UNKNOWN";
  return String(value);
}

function renderLeadContext(msg) {
  const box = $("leadContext");
  const fields = $("leadContextFields");
  const reasonEl = $("leadContextReason");
  const detailBox = $("leadContextDetail");
  const detailFields = $("leadContextIds");
  if (!box || !fields) return;
  const conv = msg.conversation || msg;
  const detalhe = conv.detalhe || {};
  if (msg.reason && !msg.ok && !conv.empresa && !conv.resumo) {
    box.classList.remove("hidden");
    reasonEl.classList.remove("hidden");
    reasonEl.textContent = msg.reason;
    return;
  }
  reasonEl.classList.add("hidden");
  fields.textContent = "";
  for (const [key, label] of LEAD_CONTEXT_FIELDS) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    const text = fieldText(conv[key] !== undefined ? conv[key] : msg[key]);
    dd.textContent = text;
    if (text === "UNKNOWN") dd.className = "unknown";
    fields.append(dt, dd);
  }
  const inbound = conv.inbound_only !== undefined ? conv.inbound_only : msg.inbound_only;
  if (inbound === true) {
    const dt = document.createElement("dt");
    dt.textContent = "inbound-only";
    const dd = document.createElement("dd");
    dd.textContent = "sim — não é elegibilidade outbound";
    fields.append(dt, dd);
  }
  if (detailBox && detailFields) {
    detailFields.textContent = "";
    for (const [key, label] of LEAD_DETAIL_FIELDS) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = fieldText(detalhe[key] !== undefined ? detalhe[key] : (conv[key] !== undefined ? conv[key] : msg[key]));
      detailFields.append(dt, dd);
    }
    detailBox.classList.remove("hidden");
  }
  box.classList.remove("hidden");
}

function setHrFetchState(text) {
  const el = $("hrFetchState");
  if (el) el.textContent = text || "";
}

function renderConversationList(body) {
  const list = $("hrList");
  const empty = $("hrPickerEmpty");
  if (!list) return;
  list.textContent = "";
  const fetchState = (body && body.fetch) || {};
  if (!body || body.warmbly_configured === false || fetchState.reason === "PRODUCER_NOT_CONFIGURED") {
    setHrFetchState("modo manual — sem credencial Warmbly");
  } else if (fetchState.reason && fetchState.ok === false) {
    setHrFetchState(fetchState.reason);
  } else if (fetchState.at) {
    setHrFetchState("atualizado");
  }
  const rows = (body && body.conversations) || [];
  if (empty) empty.classList.toggle("hidden", rows.length > 0);
  const current = meetingId;
  rows.forEach((row) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    const title = row.resumo || row.titulo || row.empresa || "UNKNOWN";
    const nucleo = row.nucleo && row.nucleo !== "UNKNOWN" ? row.nucleo : "";
    const bits = [title, nucleo].filter(Boolean);
    btn.textContent = bits.join(" · ");
    btn.title = ""; // never use the technical id as the visible title
    if (row.inbound_only === true) {
      const meta = document.createElement("span");
      meta.className = "hr-list-meta";
      meta.textContent = " inbound-only";
      btn.appendChild(meta);
    }
    if (row.session_id === current || row.handraiser_id === current.replace(/^hr:/, "")) {
      btn.classList.add("active");
    }
    btn.onclick = () => selectConversation(row.handraiser_id);
    li.appendChild(btn);
    list.appendChild(li);
  });
}

function loadConversations() {
  return fetch("/api/handraiser/list")
    .then((r) => r.json())
    .then(renderConversationList)
    .catch(() => setHrFetchState("lista indisponível"));
}

function refreshConversas() {
  const btn = $("refreshConversasBtn");
  if (btn) btn.disabled = true;
  setHrFetchState("atualizando…");
  return fetch("/api/handraiser/refresh", { method: "POST" })
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
    .then(({ body }) => {
      const reason = (body && body.reason) || "";
      if (reason === "PRODUCER_NOT_CONFIGURED") setHrFetchState("modo manual — sem credencial Warmbly");
      else if (body && body.ok) setHrFetchState("atualizado");
      else setHrFetchState(reason || "falha ao atualizar");
      renderConversationList(body);
    })
    .catch(() => setHrFetchState("falha ao atualizar"))
    .finally(() => { if (btn) btn.disabled = false; });
}

function selectConversation(hid) {
  fetch("/api/handraiser/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handraiser_id: hid }),
  })
    .then((r) => r.json())
    .then((body) => {
      if (!body || !body.ok || !body.session_id) {
        setHrFetchState((body && body.reason) || "não encontrado");
        return;
      }
      const u = new URL(location.href);
      u.searchParams.set("meeting", body.session_id);
      u.searchParams.delete("handraiser");
      location.assign(u.toString());
    })
    .catch(() => setHrFetchState("falha ao selecionar"));
}

function loadLeadContext() {
  const params = new URLSearchParams(location.search);
  const hr = params.get("handraiser");
  const url = hr
    ? `/api/handraiser/${encodeURIComponent(hr)}`
    : `/api/session/context?meeting=${encodeURIComponent(meetingId)}`;
  fetch(url)
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
    .then(({ body }) => {
      if (body && (body.ok || body.conversation || body.empresa)) renderLeadContext(body);
      else if (body && body.reason) {
        const box = $("leadContext");
        const reasonEl = $("leadContextReason");
        if (box && reasonEl && (params.get("handraiser") || (params.get("meeting") || "").indexOf("hr:") === 0)) {
          box.classList.remove("hidden");
          reasonEl.classList.remove("hidden");
          reasonEl.textContent = body.reason;
        }
      }
    })
    .catch(() => {});
}

$("startBtn").onclick = start;
$("stopBtn").onclick = stop;
$("adviseBtn").onclick = () => {
  if (copilotWs && copilotWs.readyState === WebSocket.OPEN) {
    copilotWs.send(JSON.stringify({ type: "advise_now" }));
  }
};
const refreshBtn = $("refreshConversasBtn");
if (refreshBtn) refreshBtn.onclick = refreshConversas;
// Connect on load so the test mode (tools/inject_transcript.py) can drive the
// page without anyone clicking Iniciar.
connectCopilot();
loadLeadContext();
loadConversations();
