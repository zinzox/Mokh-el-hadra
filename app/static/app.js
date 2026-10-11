const byId = (id) => document.getElementById(id);
const statusEl = byId("status");
const sourceModeEl = byId("sourceMode");
const deviceAddressEl = byId("deviceAddress");
const startBtn = byId("startBtn");
const scanBtn = byId("scanBtn");
const scanResultsEl = byId("scanResults");
const applySimBtn = byId("applySimBtn");
const simProfileEl = byId("simProfile");
const simSpeedEl = byId("simSpeed");
const ollamaStatusEl = byId("ollamaStatus");
const triageBtn = byId("triageBtn");
const triageResultEl = byId("triageResult");
const MAX_POINTS = 120;
const labels = [];
const hrSeries = [];
const phaseNames = { calm: "Calme", active: "Activité", activity: "Activité", stress: "Stress", recovery: "Récupération", sleep: "Sommeil", mixed: "Mixte", fever: "Fièvre", respiratory: "Désaturation", hypotension: "Hypotension" };
const priorityTitles = { P1: "Prise en charge immédiate", P2: "Évaluation prioritaire", P3: "Consultation standard", P4: "Orientation en soins primaires" };
let lastAlertKey = "";
let baseStatus = "Chargement de la source…";
let currentPhase = "";
let lastMeasurementId = null;
let lastSampleTs = "";
let lastStreamAt = 0;
let metricsPollBusy = false;
let wsConnected = false;
let triageTimeoutMs = 100000;
let activeEmergency = null;
let lastOpenedEmergencyId = "";
let emergencyHistoryBusy = false;
const pendingNotifications = new Set();
const emergencyDialog = byId("emergencyDialog");
const chartCanvas = byId("hrChart");
const chart = null;

// The chart is drawn locally so a slow external script cannot block live data.
function drawLocalChart() {
  const context = chartCanvas.getContext("2d");
  if (!context) return;
  const width = chartCanvas.clientWidth || 600;
  const height = chartCanvas.clientHeight || 228;
  const pixelRatio = window.devicePixelRatio || 1;
  chartCanvas.width = width * pixelRatio;
  chartCanvas.height = height * pixelRatio;
  context.scale(pixelRatio, pixelRatio);
  const left = 34, right = width - 10, top = 12, bottom = height - 24;
  const min = Math.min(45, ...hrSeries) - 5;
  const max = Math.max(160, ...hrSeries) + 5;
  context.font = "10px Segoe UI, sans-serif";
  context.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = top + ((bottom - top) * i) / 4;
    context.strokeStyle = "#eef2ef";
    context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
    context.fillStyle = "#9ba79f";
    context.fillText(String(Math.round(max - ((max - min) * i) / 4)), 0, y + 3);
  }
  if (hrSeries.length === 0) return;
  context.strokeStyle = "#408966";
  context.lineWidth = 2;
  context.beginPath();
  hrSeries.forEach((value, i) => {
    const x = left + (i / Math.max(hrSeries.length - 1, 1)) * (right - left);
    const y = bottom - ((value - min) / (max - min)) * (bottom - top);
    if (i === 0) context.moveTo(x, y); else context.lineTo(x, y);
  });
  context.stroke();
  context.fillStyle = "#9ba79f";
  context.fillText(labels[0], left, height - 5);
  if (labels.length > 1) {
    const lastLabel = labels[labels.length - 1];
    context.fillText(lastLabel, right - context.measureText(lastLabel).width, height - 5);
  }
}
if (!chart) {
  drawLocalChart();
  window.addEventListener("resize", drawLocalChart);
}

function setStatus(text) {
  baseStatus = text;
  statusEl.textContent = `${baseStatus}${currentPhase ? ` · ${currentPhase}` : ""}`;
}

async function requestJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`Le serveur a retourné une erreur (${res.status}). Réessayez.`);
  return res.json();
}

async function refreshStatus() {
  const data = await requestJson("/api/status");
  sourceModeEl.value = data.active_source;
  setStatus(data.active_source === "ble" ? "Garmin · Connexion Bluetooth" : "Données du simulateur");
  return data;
}

async function refreshGarminStatus() {
  try {
    const data = await requestJson("/api/integrations/health-connect/status");
    const pill = byId("garminStatus");
    const connected = Boolean(data.garmin_connected && data.is_enabled);
    pill.classList.toggle("status-pill-ok", connected);
    pill.textContent = connected ? "Synchronisée" : "Non synchronisée";
    byId("garminHeartRate").textContent = data.last_heart_rate != null ? `${Number(data.last_heart_rate).toFixed(0)} bpm` : "— bpm";
    byId("garminRestingHr").textContent = data.last_resting_hr != null ? `${Number(data.last_resting_hr).toFixed(0)} bpm` : "— bpm";
    byId("garminSleep").textContent = data.last_sleep ? new Date(data.last_sleep).toLocaleString("fr-FR", { timeZone: "Africa/Tunis" }) : "—";
    byId("garminSteps").textContent = data.last_steps != null ? `${Number(data.last_steps).toLocaleString("fr-FR")} pas` : "—";
    byId("garminSource").textContent = `Source : ${data.source || "garmin_health_connect"} · ${connected ? "connectée" : "inactive"}`;
    byId("garminLastSync").textContent = data.last_sync ? `Dernière synchronisation : ${new Date(data.last_sync).toLocaleString("fr-FR", { timeZone: "Africa/Tunis" })}` : "Dernière synchronisation : —";
  } catch (error) {
    byId("garminStatus").textContent = "Erreur de sync";
    byId("garminSource").textContent = "Source : Garmin Connect / Health Connect · indisponible";
    byId("garminLastSync").textContent = "Dernière synchronisation : impossible à récupérer";
  }
}

async function loadSimConfig() {
  const cfg = await requestJson("/api/sim/config");
  simProfileEl.value = cfg.profile ?? "mixed";
  simSpeedEl.value = cfg.speed ?? 1;
}

async function setSource() {
  startBtn.disabled = true;
  currentPhase = "";
  setStatus("Changement de source…");
  try {
    const data = await requestJson("/api/source", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: sourceModeEl.value, device_address: deviceAddressEl.value.trim() || null }),
    });
    sourceModeEl.value = data.active_source;
    if (!data.ok) {
      setStatus(data.error || "Simulateur activé");
      showToast(data.error || "Le simulateur a pris le relais.");
    } else {
      setStatus(data.active_source === "ble" ? "Garmin · Connexion Bluetooth" : "Données du simulateur");
    }
  } catch (error) {
    setStatus("Connexion à la source impossible");
    showToast(error.message, "danger");
  } finally {
    startBtn.disabled = false;
    pollLatestMetrics();
  }
}

async function applySimConfig() {
  if (!simSpeedEl.checkValidity()) { simSpeedEl.reportValidity(); return; }
  applySimBtn.disabled = true;
  try {
    const data = await requestJson("/api/sim/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: simProfileEl.value, speed: Number(simSpeedEl.value || 1) }),
    });
    if (!data.ok) throw new Error("La simulation n’a pas pu être mise à jour.");
    sourceModeEl.value = "sim";
    await setSource();
  } catch (error) {
    showToast(error.message, "danger");
  } finally {
    applySimBtn.disabled = false;
  }
}

function showToast(message, kind = "warning") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.textContent = message;
  byId("toastContainer").appendChild(toast);
  setTimeout(() => { toast.classList.add("toast-hide"); setTimeout(() => toast.remove(), 220); }, 3600);
}

async function refreshOllamaStatus() {
  const data = await requestJson("/api/triage/status");
  if (Number.isFinite(data.analysis_timeout_seconds)) triageTimeoutMs = (data.analysis_timeout_seconds + 5) * 1000;
  ollamaStatusEl.textContent = data.available ? `Ollama · ${data.model}` : "Moteur de repli local · Ollama indisponible";
  ollamaStatusEl.classList.toggle("status-pill-ok", Boolean(data.available));
}

function displayText(value, fallback = "Non renseigné") {
  if (value == null || !["string", "number"].includes(typeof value)) return fallback;
  const text = String(value).trim();
  return !text || /^(n\/?a|null|undefined|--|—)$/i.test(text) ? fallback : text;
}

function emergencyPhoneNumber() {
  const entered = byId("emergencyPhone").value.trim();
  if (!/^\+?[\d\s().-]+$/.test(entered)) return "";
  const number = entered.replace(/[\s().-]/g, "");
  return /^\+?\d{3,15}$/.test(number) ? number : "";
}

function buildEmergencySms(data, simulated) {
  const vitals = data.constantes_injectees || {};
  const cleanText = (value) => displayText(value, "non renseigné").replace(/\s+/g, " ").trim();
  const measurement = (value) => cleanText(value).replace(/(\d)\.(\d)/g, "$1,$2");
  const suppliedMotif = cleanText(data.motif_principal);
  const motif = /^(motif(?: principal)? non (?:précisé|renseigné)|non précisé)$/i.test(suppliedMotif) ? "non renseigné" : suppliedMotif.slice(0, 180);
  const date = data.analyse_ts ? new Date(data.analyse_ts) : null;
  const analysedAt = date && Number.isFinite(date.getTime())
    ? `${date.toLocaleDateString("fr-FR", { timeZone: "Africa/Tunis", day: "2-digit", month: "2-digit", year: "numeric" })} à ${date.toLocaleTimeString("fr-FR", { timeZone: "Africa/Tunis", hour: "2-digit", minute: "2-digit", hour12: false })} (heure de Tunis)`
    : "date non renseignée";
  return [
    ...(simulated ? ["DÉMONSTRATION — données simulées", ""] : []),
    "ALERTE P1 — ÉVALUATION IMMÉDIATE",
    "",
    `Motif : ${motif}`,
    `Fréquence cardiaque : ${measurement(vitals.frequence_cardiaque).replace(/\bbpm\b/gi, "battements/min")}`,
    `Saturation en oxygène : ${measurement(vitals.spo2)}`,
    `Tension artérielle : ${measurement(vitals.tension)}`,
    "",
    `Analyse : ${analysedAt}`,
    `${simulated ? "Action simulée" : "Action"} : prévenir immédiatement un soignant pour évaluer la situation.`,
  ].join("\n");
}

function updateEmergencyCall() {
  const number = emergencyPhoneNumber();
  const button = byId("emergencyCallBtn");
  button.setAttribute("aria-disabled", String(!number));
  if (number) button.setAttribute("href", `tel:${number}`); else button.removeAttribute("href");
  byId("emergencyCallLabel").textContent = number ? `Appeler le ${number}` : "Configurer un numéro d’appel";
  byId("emergencyCallHint").textContent = !number
    ? "Renseignez un numéro valide dans Configuration → Contact d’urgence."
    : activeEmergency?.simulated
      ? `Démonstration : un clic ouvre le téléphone pour un appel réel au ${number}.`
      : "Ouvre l’application téléphone de cet appareil.";
  const sms = byId("emergencySmsBtn");
  sms.setAttribute("aria-disabled", String(!number || !activeEmergency));
  if (number && activeEmergency) {
    const message = buildEmergencySms(activeEmergency.data, activeEmergency.simulated);
    sms.setAttribute("href", `sms:${number}?body=${encodeURIComponent(message)}`);
  } else sms.removeAttribute("href");
}

async function loadEmergencyConfig() {
  const data = await requestJson("/api/emergency/config");
  if (typeof data.phone === "string" && !byId("emergencyPhone").dataset.edited) byId("emergencyPhone").value = data.phone;
  updateEmergencyCall();
}

function updateNotificationAction() {
  if (!activeEmergency) return;
  const busy = pendingNotifications.has(activeEmergency.id);
  byId("notifyEmergencyBtn").disabled = busy || activeEmergency.notified || !activeEmergency.data.analyse_id;
  byId("notifyEmergencyLabel").textContent = activeEmergency.notified ? "Notification publiée" : busy ? "Publication en cours…" : "Notifier dans l’application";
  const status = byId("emergencyNotificationStatus");
  status.dataset.state = activeEmergency.notified ? "success" : busy ? "loading" : "idle";
  status.textContent = activeEmergency.notified
    ? "Alerte enregistrée dans le journal de cette application."
    : busy ? "Enregistrement de l’alerte P1…"
      : !activeEmergency.data.analyse_id ? "Identifiant d’analyse absent. Relancez le pré-triage pour notifier."
        : "La notification sera publiée dans le journal des alertes P1.";
}

function openEmergencyDialog() {
  if (!activeEmergency) return;
  const data = activeEmergency.data;
  byId("emergencyDemo").hidden = !activeEmergency.simulated;
  byId("emergencyMotif").textContent = displayText(data.motif_principal, "Motif non renseigné");
  byId("emergencyTimestamp").textContent = `Analyse du ${new Date(data.analyse_ts || Date.now()).toLocaleString("fr-FR", { timeZone: "Africa/Tunis" })}`;
  byId("emergencyReasons").replaceChildren();
  const reasons = Array.isArray(data.justification_tri) ? data.justification_tri : [];
  for (const reason of [...new Set(reasons.filter((value) => typeof value === "string"))].slice(0, 4)) {
    const li = document.createElement("li"); li.textContent = reason; byId("emergencyReasons").appendChild(li);
  }
  const vitals = data.constantes_injectees || {};
  for (const [id, key] of [["emergencyHr", "frequence_cardiaque"], ["emergencySpo2", "spo2"], ["emergencyPressure", "tension"]]) byId(id).textContent = displayText(vitals[key]);
  byId("emergencyAction").textContent = displayText(data.recommandation_action, "Prévenir un soignant et faire évaluer la situation sans délai.");
  updateEmergencyCall();
  updateNotificationAction();
  if (!emergencyDialog.open) emergencyDialog.showModal();
}

function handleEmergencyResult(data, priority) {
  byId("reopenEmergencyBtn").hidden = priority !== "P1";
  if (priority !== "P1") {
    if (emergencyDialog.open) emergencyDialog.close();
    activeEmergency = null;
    return;
  }
  const id = data.analyse_id || data.analyse_ts || JSON.stringify(data);
  if (activeEmergency?.id !== id) {
    activeEmergency = { id, data, simulated: Boolean(data.contexte_mesures?.simulated || data.contexte_mesures?.source === "sim"), notified: false };
  }
  // Dismissing an alert remains effective until a new P1 analysis arrives.
  if (lastOpenedEmergencyId !== id) { lastOpenedEmergencyId = id; openEmergencyDialog(); }
}

function renderEmergencyHistory(alerts) {
  const list = Array.isArray(alerts) ? alerts : [];
  byId("notificationCount").textContent = `${list.length} notification${list.length === 1 ? "" : "s"}`;
  byId("notificationHistoryStatus").textContent = list.length ? "Dernières alertes publiées dans l’application." : "Aucune notification publiée pour le moment.";
  byId("notificationHistory").replaceChildren();
  for (const alert of list) {
    const li = document.createElement("li");
    const title = document.createElement("strong"); title.textContent = `P1 · ${displayText(alert.motif, "Motif non renseigné")}`;
    const meta = document.createElement("p"); meta.textContent = `${alert.simulated ? "Démonstration · " : ""}${new Date(alert.created_at).toLocaleString("fr-FR", { timeZone: "Africa/Tunis" })} · Notification locale`;
    li.appendChild(title); li.appendChild(meta); byId("notificationHistory").appendChild(li);
  }
  if (activeEmergency && list.some((alert) => alert.analyse_id === activeEmergency.id)) {
    activeEmergency.notified = true;
    updateNotificationAction();
  }
}

async function refreshEmergencyHistory() {
  if (emergencyHistoryBusy) return;
  emergencyHistoryBusy = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const data = await requestJson("/api/emergency/alerts", { signal: controller.signal });
    renderEmergencyHistory(data.alerts);
  } catch {
    byId("notificationHistoryStatus").textContent = "Journal indisponible. Nouvelle tentative automatique…";
  } finally { clearTimeout(timeout); emergencyHistoryBusy = false; }
}

async function notifyEmergency() {
  const emergency = activeEmergency;
  if (!emergency?.data.analyse_id || emergency.notified || pendingNotifications.has(emergency.id)) return;
  pendingNotifications.add(emergency.id);
  updateNotificationAction();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const data = await requestJson("/api/emergency/notify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ analyse_id: emergency.data.analyse_id }), signal: controller.signal,
    });
    if (!data.ok || data.alert?.analyse_id !== emergency.id) throw new Error("La notification n’a pas été confirmée.");
    emergency.notified = true;
    if (activeEmergency?.id === emergency.id) updateNotificationAction();
    await refreshEmergencyHistory();
  } catch (error) {
    if (activeEmergency?.id === emergency.id && !emergency.notified) {
      byId("emergencyNotificationStatus").dataset.state = "error";
      byId("emergencyNotificationStatus").textContent = "Publication non confirmée. Réessayez ; une même analyse ne crée qu’une notification.";
    }
  } finally {
    clearTimeout(timeout);
    pendingNotifications.delete(emergency.id);
    if (activeEmergency?.id === emergency.id) {
      byId("notifyEmergencyBtn").disabled = emergency.notified;
      byId("notifyEmergencyLabel").textContent = emergency.notified ? "Notification publiée" : "Réessayer la notification";
    }
  }
}

function setTriageState(state, message = "") {
  triageResultEl.dataset.state = state;
  triageResultEl.setAttribute("aria-busy", String(state === "loading"));
  byId("triageContent").hidden = state !== "success";
  byId("triageEmpty").hidden = state === "success";
  if (state === "loading") {
    byId("emptyTitle").textContent = "Analyse en cours…";
    byId("emptyDescription").textContent = "La fiche de pré-triage est en préparation. Cela peut prendre quelques instants.";
    byId("resultTimestamp").textContent = "Analyse en cours";
    byId("triageFeedback").textContent = "Analyse du pré-triage en cours.";
  } else if (state === "error") {
    byId("emptyTitle").textContent = "L’analyse n’a pas pu aboutir";
    byId("emptyDescription").textContent = message;
    byId("resultTimestamp").textContent = "Analyse indisponible";
    byId("triageFeedback").textContent = message;
  }
}

function renderTriage(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("La réponse d’analyse est invalide. Réessayez.");
  const suppliedPriority = String(data.statut_tri ?? "").trim().toUpperCase();
  const priority = Object.hasOwn(priorityTitles, suppliedPriority) ? suppliedPriority : "unknown";
  byId("priorityBanner").dataset.priority = priority;
  byId("priorityCode").textContent = priority === "unknown" ? "—" : priority;
  byId("priorityTitle").textContent = priorityTitles[priority] || "Priorité à vérifier";
  byId("resultMotif").textContent = displayText(data.motif_principal, "Motif non renseigné");
  byId("resultSymptoms").textContent = displayText(data.duree_symptomes, "Symptômes et durée non renseignés");
  byId("resultPain").textContent = displayText(data.niveau_douleur_eva);
  const painMatch = String(data.niveau_douleur_eva ?? "").trim().match(/^(\d+(?:[.,]\d+)?)\s*(?:\/\s*10)?$/);
  const painValue = painMatch ? Number(painMatch[1].replace(",", ".")) : NaN;
  const painMeter = byId("painMeter");
  painMeter.hidden = !Number.isFinite(painValue) || painValue < 0 || painValue > 10;
  if (!painMeter.hidden) {
    painMeter.setAttribute("aria-valuenow", String(painValue));
    painMeter.dataset.level = painValue >= 7 ? "high" : "normal";
    byId("painFill").style.width = `${painValue * 10}%`;
  } else {
    painMeter.removeAttribute("aria-valuenow");
    byId("painFill").style.width = "0%";
  }
  const detected = data.red_flags_detectes === true || data.red_flags_detectes === "true";
  const absent = data.red_flags_detectes === false || data.red_flags_detectes === "false";
  byId("redFlagIndicator").dataset.state = detected ? "detected" : absent ? "absent" : "unknown";
  byId("resultRedFlags").textContent = detected ? "Détectés" : absent ? "Aucun signalé" : "Non renseignés";
  byId("redFlagDetail").textContent = detected ? "Signal d’alerte présent" : absent ? "Selon les informations fournies" : "À vérifier par l’IAO";
  const vitals = data.constantes_injectees && typeof data.constantes_injectees === "object" ? data.constantes_injectees : {};
  for (const [id, key] of [["resultTension", "tension"], ["resultHr", "frequence_cardiaque"], ["resultSpo2", "spo2"], ["resultTemperature", "temperature"], ["resultRespiration", "frequence_respiratoire"]]) {
    const value = displayText(vitals[key], "Non renseignée");
    byId(id).textContent = value;
    byId(id).dataset.missing = String(value === "Non renseignée");
  }
  byId("recommendationBox").dataset.priority = priority;
  const measurementContext = data.contexte_mesures || {};
  const contextSource = measurementContext.source === "sim" ? "Simulation" : measurementContext.source === "ble" ? "Garmin BLE" : "Sans flux";
  const measuredAt = measurementContext.ts ? new Date(measurementContext.ts).toLocaleTimeString("fr-FR", { timeZone: "Africa/Tunis" }) : "";
  byId("resultMeasurementsNote").textContent = measurementContext.fresh
    ? `${contextSource} · instantané ${measuredAt}${data.origine_temperature === "saisie" ? " · température saisie prioritaire" : ""}`
    : "Aucune mesure récente injectée. L’analyse utilise les informations saisies.";
  const reasons = Array.isArray(data.justification_tri) ? data.justification_tri : [];
  byId("resultReasons").replaceChildren();
  for (const reason of [...new Set(reasons.filter((value) => typeof value === "string"))]) {
    const li = document.createElement("li"); li.textContent = reason; byId("resultReasons").appendChild(li);
  }
  const modelReasons = Array.isArray(data.raisonnement_ia) ? data.raisonnement_ia.filter((value) => typeof value === "string") : [];
  byId("modelExplanation").hidden = modelReasons.length === 0 && !data.orientation_proposee_ia;
  byId("modelExplanation").open = false;
  byId("modelReasons").replaceChildren();
  for (const reason of modelReasons) { const li = document.createElement("li"); li.textContent = reason; byId("modelReasons").appendChild(li); }
  byId("modelOrientation").textContent = typeof data.orientation_proposee_ia === "string" ? data.orientation_proposee_ia : "";
  byId("resultEngine").textContent = `${data.analyse_prioritaire_immediate ? "P1 détecté immédiatement par les règles" : data.moteur_analyse === "ollama" ? "Analyse Ollama" : "Règles de repli locales"}${data.garde_fou_applique ? " · priorité renforcée par les constantes" : ""} · validation IAO requise`;
  byId("resultRecommendation").textContent = displayText(data.recommandation_action, "Aucune recommandation fournie. À vérifier par l’IAO.");
  byId("triageJson").textContent = JSON.stringify(data, null, 2);
  byId("triageJson").closest("details").open = false;
  byId("resultTimestamp").textContent = `Analyse du ${new Date(data.analyse_ts || Date.now()).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Africa/Tunis" })}`;
  setTriageState("success");
  byId("triageFeedback").textContent = `Analyse terminée. ${priority === "unknown" ? "Priorité à vérifier" : `${priority} : ${priorityTitles[priority]}`}. À valider par l’IAO.`;
  handleEmergencyResult(data, priority);
}

async function analyseTriage(event) {
  event.preventDefault();
  if (triageBtn.disabled) return;
  triageBtn.disabled = true;
  const buttonLabel = triageBtn.querySelector("span");
  buttonLabel.textContent = "Analyse en cours…";
  setTriageState("loading");
  const redFlags = {};
  document.querySelectorAll("[data-red-flag]").forEach((input) => { redFlags[input.dataset.redFlag] = input.checked; });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), triageTimeoutMs);
  try {
    const data = await requestJson("/api/triage/analyse", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ language: byId("triageLanguage").value, motif: byId("triageMotif").value, symptoms: byId("triageSymptoms").value, temp: byId("triageTemp").value || "N/A", pain: byId("triagePain").value || "0/10", red_flags: redFlags }),
    });
    renderTriage(data);
  } catch (error) {
    setTriageState("error", error.name === "AbortError" ? "Le délai de réponse est dépassé. Relancez l’analyse." : error.message || "Connexion au serveur impossible. Réessayez.");
  } finally {
    clearTimeout(timer);
    triageBtn.disabled = false;
    buttonLabel.textContent = "Analyser le pré-triage";
  }
}

async function scanGarmin() {
  scanBtn.disabled = true;
  setStatus("Scan Bluetooth en cours…");
  scanResultsEl.replaceChildren();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const data = await requestJson("/api/devices", { signal: controller.signal });
    if (data.error) throw new Error(data.error);
    if (!data.devices || data.devices.length === 0) {
      setStatus("Aucun périphérique Bluetooth détecté");
      const li = document.createElement("li"); li.textContent = "Aucun périphérique détecté. Vérifiez que la montre diffuse sa fréquence cardiaque."; scanResultsEl.appendChild(li);
      return;
    }
    for (const device of data.devices.slice(0, 12)) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button"; button.className = "scan-device";
      button.textContent = `${device.local_name || device.name || "Appareil Bluetooth"} · ${device.address}${device.has_hr_service ? " · Fréquence cardiaque" : ""}`;
      button.title = "Sélectionner l’adresse de cet appareil";
      button.addEventListener("click", () => { deviceAddressEl.value = device.address; setStatus(`Appareil sélectionné : ${device.address}`); });
      li.appendChild(button); scanResultsEl.appendChild(li);
    }
    deviceAddressEl.value = data.auto_pick || data.preferred_address || data.devices[0].address;
    scanResultsEl.closest("details").open = true;
    setStatus(`${data.count ?? data.devices.length} appareil(s) détecté(s) · Adresse proposée`);
  } catch (error) {
    const message = error.name === "AbortError" ? "Le scan Bluetooth a dépassé le délai de 15 secondes." : error.message;
    setStatus(message);
    const li = document.createElement("li"); li.textContent = message; scanResultsEl.appendChild(li);
  } finally {
    clearTimeout(timer); scanBtn.disabled = false;
  }
}

function setMetric(id, value, unit, decimals = null) {
  const el = byId(id);
  const number = typeof value === "number" && Number.isFinite(value) ? (decimals == null ? String(value) : value.toFixed(decimals)) : "—";
  el.replaceChildren(document.createTextNode(`${number} `));
  const unitEl = document.createElement("span"); unitEl.textContent = unit; el.appendChild(unitEl);
}

function pushPoint(hr, ts) {
  labels.push(new Date(ts).toLocaleTimeString("fr-FR", { timeZone: "Africa/Tunis" }));
  hrSeries.push(hr);
  if (labels.length > MAX_POINTS) { labels.shift(); hrSeries.shift(); }
  byId("chartEmpty").hidden = true;
  if (chart) chart.update(); else drawLocalChart();
}

function render(payload) {
  const raw = payload.raw || {}, derived = payload.derived || {}, windows = payload.windows || {};
  setMetric("hr", raw.hr, "bpm");
  setMetric("rmssd", derived.rmssd, "ms", 1);
  setMetric("stress", derived.stress_score, "/100");
  setMetric("recovery", derived.recovery_score, "/100");
  setMetric("spo2", raw.spo2, "%");
  setMetric("bodyBattery", raw.body_battery, "/100");
  setMetric("temperature", raw.temperature, "°C", 1);
  setMetric("respiration", raw.respiration, "/min", 1);
  const pressureEl = byId("bloodPressure");
  pressureEl.replaceChildren(document.createTextNode(typeof raw.systolic === "number" && typeof raw.diastolic === "number" ? `${raw.systolic}/${raw.diastolic} ` : "— "));
  const pressureUnit = document.createElement("span"); pressureUnit.textContent = "mmHg"; pressureEl.appendChild(pressureUnit);
  document.querySelectorAll(".clinical-source").forEach((el) => { el.textContent = payload.source === "sim" ? "Simulée" : "Si disponible"; });
  currentPhase = phaseNames[raw.phase] || "";
  setStatus(baseStatus);
  for (const [id, key] of [["window1m", "hr_1m"], ["window5m", "hr_5m"], ["window1h", "hr_1h"], ["windowBaseline", "baseline_rest_hr"]]) setMetric(id, windows[key], "bpm", 1);
  const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
  byId("alertCount").textContent = String(alerts.length);
  byId("alerts").replaceChildren();
  if (!alerts.length) {
    const li = document.createElement("li"); li.className = "no-alert"; li.textContent = "Aucune alerte active."; byId("alerts").appendChild(li);
  } else {
    for (const alert of alerts) { const li = document.createElement("li"); li.textContent = alert; byId("alerts").appendChild(li); }
    if (alerts.join("|") !== lastAlertKey) showToast(alerts[0]);
  }
  lastAlertKey = alerts.join("|");
  byId("summary").textContent = payload.summary || "En attente d’une synthèse.";
  byId("disclaimer").textContent = payload.disclaimer || byId("disclaimer").textContent;
  if (typeof raw.hr === "number" && Number.isFinite(raw.hr)) pushPoint(raw.hr, payload.ts);
}

function setConnection(connected, label = "") {
  const el = byId("connectionStatus");
  el.classList.toggle("status-pill-ok", connected);
  el.replaceChildren();
  const dot = document.createElement("span"); dot.className = "small-dot"; el.appendChild(dot);
  el.appendChild(document.createTextNode(label || (connected ? "Flux connecté" : "Reconnexion en cours")));
}

function updateLiveContext(payload) {
  const raw = payload.raw || {};
  const fresh = Boolean(payload.available !== false && payload.ts && Date.now() - new Date(payload.ts).getTime() <= 10000 && payload.fresh !== false);
  const source = payload.source === "sim" ? "Simulation" : "Garmin BLE";
  byId("triageLiveSource").textContent = fresh ? `${source}${phaseNames[raw.phase] ? ` · ${phaseNames[raw.phase]}` : ""}` : "Mesures indisponibles";
  byId("triageLiveValues").textContent = fresh
    ? `FC ${raw.hr ?? "—"} bpm · SpO₂ ${raw.spo2 ?? "—"} % · Temp. ${raw.temperature ?? "—"} °C · Resp. ${raw.respiration ?? "—"}/min · TA ${typeof raw.systolic === "number" ? `${raw.systolic}/${raw.diastolic}` : "—"} mmHg`
    : "Pas de mesures récentes. Elles seront ajoutées automatiquement dès la reprise du flux.";
  byId("triageLiveMeta").textContent = fresh
    ? `Dernière mesure à ${new Date(payload.ts).toLocaleTimeString("fr-FR", {timeZone: "Africa/Tunis"})}. La fiche fige les constantes au lancement de l’analyse.`
    : "Les données périmées sont exclues de l’analyse ; les champs saisis restent disponibles.";
  document.querySelectorAll(".metrics-grid, .clinical-metrics").forEach((el) => { el.dataset.stale = String(!fresh); });
  return fresh;
}

function receiveMetrics(payload, transport) {
  if (payload.ts && lastSampleTs && new Date(payload.ts) < new Date(lastSampleTs)) return;
  const fresh = updateLiveContext(payload);
  if (payload.available === false || !payload.raw) { setConnection(false, "En attente de mesures"); return; }
  setConnection(fresh, fresh ? `Flux ${transport} · ${payload.source === "sim" ? "simulation" : "Garmin"}` : "Mesures périmées");
  if (payload.measurement_id === lastMeasurementId && payload.ts === lastSampleTs) return;
  lastMeasurementId = payload.measurement_id ?? null;
  lastSampleTs = payload.ts || "";
  render(payload);
}

async function pollLatestMetrics() {
  if (metricsPollBusy || (wsConnected && Date.now() - lastStreamAt < 2500)) return;
  metricsPollBusy = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    receiveMetrics(await requestJson("/api/metrics/latest", { signal: controller.signal }), "HTTP");
  } catch {
    if (Date.now() - lastStreamAt > 10000) {
      setConnection(false, "Connexion au serveur indisponible");
      updateLiveContext({ available: false });
    }
  } finally {
    clearTimeout(timeout);
    metricsPollBusy = false;
  }
}

function connectWs() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host || "127.0.0.1:8000"}/ws`);
  let pingTimer = null;
  ws.onopen = () => {
    wsConnected = true;
    ws.send("ping");
    pingTimer = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 15000);
  };
  ws.onmessage = (event) => {
    try { lastStreamAt = Date.now(); receiveMetrics(JSON.parse(event.data), "WebSocket"); } catch (error) { console.error("Données de suivi invalides", error); }
    if (ws.readyState === WebSocket.OPEN) ws.send("ping");
  };
  ws.onclose = () => { if (pingTimer) clearInterval(pingTimer); wsConnected = false; pollLatestMetrics(); setTimeout(connectWs, 10000); };
  ws.onerror = () => ws.close();
}

const navLinks = Array.from(document.querySelectorAll(".nav-link"));
function activateNav(hash) {
  for (const link of navLinks) {
    const active = link.getAttribute("href") === hash;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "location"); else link.removeAttribute("aria-current");
  }
}
navLinks.forEach((link) => link.addEventListener("click", () => activateNav(link.getAttribute("href"))));
window.addEventListener("hashchange", () => activateNav(window.location.hash || "#overview"));
activateNav(window.location.hash || "#overview");
startBtn.addEventListener("click", setSource);
scanBtn.addEventListener("click", scanGarmin);
applySimBtn.addEventListener("click", applySimConfig);
byId("triageForm").addEventListener("submit", analyseTriage);
byId("reopenEmergencyBtn").addEventListener("click", openEmergencyDialog);
for (const id of ["closeEmergencyBtn", "dismissEmergencyBtn"]) byId(id).addEventListener("click", () => emergencyDialog.close());
byId("notifyEmergencyBtn").addEventListener("click", notifyEmergency);
byId("emergencyPhone").addEventListener("input", () => { byId("emergencyPhone").dataset.edited = "true"; updateEmergencyCall(); });
for (const id of ["emergencyCallBtn", "emergencySmsBtn"]) byId(id).addEventListener("click", (event) => { if (byId(id).getAttribute("aria-disabled") === "true") event.preventDefault(); });
loadEmergencyConfig().catch(updateEmergencyCall);
refreshGarminStatus();
byId("refreshGarminBtn").addEventListener("click", refreshGarminStatus);
refreshEmergencyHistory();
setInterval(refreshEmergencyHistory, 5000);
loadSimConfig().catch(() => {});
refreshStatus().then((data) => { if (data.websocket_available !== false) connectWs(); }).catch(() => setStatus("API indisponible"));
refreshOllamaStatus().catch(() => { ollamaStatusEl.textContent = "Statut du moteur indisponible"; });
pollLatestMetrics();
setInterval(pollLatestMetrics, 1000);
setInterval(refreshGarminStatus, 15000);
