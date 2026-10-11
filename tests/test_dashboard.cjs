// Behaviour tests for the actual dashboard script with small DOM test doubles.
// This verifies data flow and state transitions; it is not visual browser QA.
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "app/static/index.html"), "utf8");
const code = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8") + "\n" + fs.readFileSync(path.join(root, "app/static/consensus.js"), "utf8");
const idNames = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(idNames).size, idNames.length, "HTML IDs are unique");
assert.ok(!html.includes("cdn.jsdelivr"), "Live dashboard needs no external chart script");

class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.style = {}; this.attributes = {}; this.value = ""; this.textContent = ""; this.disabled = false; this.hidden = false; this.open = false; this.openCount = 0; this.listeners = {};
    const classes = new Set();
    this.classList = { add: (name) => classes.add(name), toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) };
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  replaceChildren(...children) { this.children = children; }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  get firstElementChild() { return this.children[0]; }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  focus() { this.focused = true; }
  querySelector() { return this.label ??= new Element(); }
  closest() { return this.details ??= new Element(); }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  showModal() { this.open = true; this.openCount += 1; }
  show() { this.open = true; this.openCount += 1; }
  close() { const wasOpen = this.open; this.open = false; if (wasOpen) this.listeners.close?.(); }
  checkValidity() { return true; }
  reportValidity() { return true; }
}
const elements = new Map(idNames.map((name) => [name, new Element()]));
const clinicalTags = [new Element(), new Element(), new Element()];
const grids = [new Element(), new Element()];
const flags = ["chest", "breath", "weakness", "bleeding"].map((name) => { const el = new Element(); el.dataset.redFlag = name; el.checked = false; return el; });
const links = ["#overview", "#triage", "#configuration"].map((href) => { const el = new Element(); el.setAttribute("href", href); return el; });
const canvasContext = { scale() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillText() {}, measureText: () => ({ width: 42 }) };
elements.get("hrChart").getContext = () => canvasContext;
elements.get("triagePain").value = "7/10";
elements.get("triageLanguage").value = "fr";
elements.get("triageMotif").value = "Douleur abdominale";
elements.get("emergencyPhone").value = "51288215";
const latest = {
  available: true, fresh: true, simulated: true, source: "sim", ts: new Date().toISOString(), measurement_id: 1,
  raw: { hr: 72, spo2: 98, temperature: 36.7, respiration: 16, systolic: 120, diastolic: 78, phase: "calm", body_battery: 0 },
  derived: { rmssd: 0, stress_score: 0, recovery_score: 0 }, windows: {hr_1m:72}, alerts: [], summary: "Stable",
};
const requests = [];
let createdSockets = 0;
const context = vm.createContext({
  document: {
    getElementById: (id) => { assert.ok(elements.has(id), `Missing dashboard element ${id}`); return elements.get(id); },
    createElement: () => new Element(), createTextNode: (text) => ({ textContent: text }),
    querySelectorAll: (selector) => selector === "[data-red-flag]" ? flags : selector === ".nav-link" ? links : selector === ".clinical-source" ? clinicalTags : selector === ".metrics-grid, .clinical-metrics" ? grids : [],
  },
  window: { location: { protocol: "http:", host: "127.0.0.1:8000", hash: "" }, addEventListener() {}, devicePixelRatio: 1 },
  WebSocket: class { static OPEN = 1; constructor() { createdSockets += 1; } send() {} close() {} },
  fetch: async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => url === "/api/metrics/latest" ? latest : url === "/api/status" ? {active_source:"sim",websocket_available:false} : url === "/api/sim/config" ? {profile:"calm",speed:1} : url === "/api/emergency/config" ? {phone:"51288215"} : url === "/api/emergency/alerts" ? {alerts:[]} : url === "/api/consensus/status" ? {configured:true} : {available:false} };
  },
  AbortController, URL, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}, console, Date,
});
const run = (script) => vm.runInContext(script, context);
context.fixture = latest;
run(code);

(async () => {
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(requests.some((request) => request.url === "/api/metrics/latest"));
  assert.equal(createdSockets, 0, "No unsupported WebSocket connection is attempted");
  assert.equal(elements.get("hr").children[0].textContent, "72 ");
  assert.equal(elements.get("temperature").children[0].textContent, "36.7 ");
  assert.equal(elements.get("bloodPressure").children[0].textContent, "120/78 ");
  assert.equal(elements.get("rmssd").children[0].textContent, "0.0 ");
  assert.equal(elements.get("bodyBattery").children[0].textContent, "0 ");
  assert.ok(elements.get("triageLiveValues").textContent.includes("120/78"));
  assert.ok(elements.get("connectionStatus").children[1].textContent.includes("HTTP"));
  assert.equal(run("labels.length"), 1);
  run('receiveMetrics(fixture, "WebSocket")');
  assert.equal(run("labels.length"), 1, "HTTP and WS duplicates produce one chart point");
  context.oldPacket = {...latest, ts: new Date(Date.now() - 60000).toISOString(), raw: {hr: 200}};
  run('receiveMetrics(oldPacket, "HTTP")');
  assert.equal(elements.get("hr").children[0].textContent, "72 ", "Out-of-order packets are ignored");
  run('updateLiveContext(oldPacket)');
  assert.equal(grids[0].dataset.stale, "true");

  context.analysis = {
    statut_tri:"P2", niveau_douleur_eva:"7/10", red_flags_detectes:false,
    constantes_injectees: {tension:"120/78 mmHg",frequence_cardiaque:"72 bpm",spo2:"98 %",temperature:"36.7 °C",frequence_respiratoire:"16 /min"},
    contexte_mesures:latest, moteur_analyse:"ollama", justification_tri:["Douleur signalée"], garde_fou_applique:true,
    recommandation_action:"Évaluation prioritaire", motif_principal:"<img src=x onerror=alert(1)>",
  };
  run("renderTriage(analysis)");
  assert.equal(elements.get("resultTemperature").textContent, "36.7 °C");
  assert.equal(elements.get("resultRespiration").textContent, "16 /min");
  assert.equal(elements.get("resultMotif").textContent, "<img src=x onerror=alert(1)>");
  assert.ok(elements.get("resultMeasurementsNote").textContent.includes("Simulation"));
  assert.equal(elements.get("resultReasons").children.length, 1);
  assert.ok(elements.get("resultEngine").textContent.includes("Ollama"));
  assert.equal(elements.get("painFill").style.width, "70%");
  const dialog = elements.get("emergencyDialog");
  assert.equal(dialog.open, false, "P2 does not open an emergency popup");
  assert.equal(elements.get("reopenEmergencyBtn").hidden, true);
  context.p1 = {...context.analysis, statut_tri:"P1", analyse_id:"server-analysis-1", analyse_ts:"2026-10-10T23:31:29.227073+00:00", analyse_prioritaire_immediate:true};
  run("renderTriage(p1)");
  assert.equal(dialog.open, true, "P1 opens a modal automatically");
  assert.equal(elements.get("emergencyDemo").hidden, false);
  assert.equal(elements.get("emergencyMotif").textContent, context.analysis.motif_principal, "Popup patient text is safe");
  assert.equal(elements.get("emergencyHr").textContent, "72 bpm");
  assert.equal(elements.get("emergencyCallBtn").getAttribute("href"), "tel:51288215");
  const smsHref = elements.get("emergencySmsBtn").getAttribute("href");
  assert.ok(smsHref.startsWith("sms:51288215?body="));
  const smsBody = decodeURIComponent(smsHref.split("?body=")[1]);
  assert.ok(smsBody.includes("DÉMONSTRATION") && smsBody.includes("Fréquence cardiaque : 72 battements/min") && smsBody.includes("Tension artérielle : 120/78 mmHg"), "SMS draft includes the simulated snapshot in plain language");
  assert.ok(smsBody.includes("ALERTE P1") && smsBody.includes("11/10/2026 à 00:31 (heure de Tunis)"), "SMS identifies P1 and formats the analysis time in Tunis, including the date change");
  assert.ok(elements.get("emergencyCallHint").textContent.includes("appel réel"), "Simulation makes manual calling explicit");
  assert.ok(elements.get("resultEngine").textContent.includes("immédiatement"));
  elements.get("closeEmergencyBtn").listeners.click();
  run("renderTriage(p1)");
  assert.equal(dialog.open, false, "A dismissed analysis does not reopen on duplicate rendering");
  assert.equal(dialog.openCount, 1);
  elements.get("reopenEmergencyBtn").listeners.click();
  assert.equal(dialog.open, true, "User can reopen emergency actions");
  elements.get("emergencyPhone").value = "javascript:alert(1)";
  run("updateEmergencyCall()");
  assert.equal(elements.get("emergencyCallBtn").getAttribute("href"), undefined);
  assert.equal(elements.get("emergencyCallBtn").getAttribute("aria-disabled"), "true");
  assert.equal(elements.get("emergencySmsBtn").getAttribute("href"), undefined);
  let preventedCall = false;
  elements.get("emergencyCallBtn").listeners.click({preventDefault(){preventedCall = true;}});
  assert.equal(preventedCall, true);
  elements.get("emergencyPhone").value = "+216 51 288 215";
  run("updateEmergencyCall()");
  assert.equal(elements.get("emergencyCallBtn").getAttribute("href"), "tel:+21651288215");
  context.fetch = async () => ({ok:false,status:500});
  await run("notifyEmergency()");
  assert.equal(elements.get("emergencyNotificationStatus").dataset.state, "error");
  assert.equal(elements.get("notifyEmergencyBtn").disabled, false, "Notification can be retried after a failure");
  const alert = {analyse_id:context.p1.analyse_id, motif:context.p1.motif_principal, created_at:new Date().toISOString(), simulated:true};
  let releaseNotification;
  let notificationCalls = 0;
  context.fetch = async (url, options) => {
    if (url === "/api/emergency/notify") {
      notificationCalls += 1;
      assert.deepEqual(JSON.parse(options.body), {analyse_id:context.p1.analyse_id}, "Only the analysis ID is sent");
      await new Promise((resolve) => { releaseNotification = resolve; });
      return {ok:true,json:async () => ({ok:true,created:true,alert})};
    }
    return {ok:true,json:async () => ({alerts:[alert]})};
  };
  const pendingNotification = run("notifyEmergency()");
  await run("notifyEmergency()");
  assert.equal(notificationCalls, 1, "Double click creates one request");
  assert.equal(elements.get("notifyEmergencyBtn").disabled, true);
  releaseNotification();
  await pendingNotification;
  assert.equal(elements.get("emergencyNotificationStatus").dataset.state, "success");
  assert.equal(elements.get("notificationHistory").children.length, 1);
  assert.ok(elements.get("notificationHistory").children[0].children[1].textContent.includes("Démonstration"));
  await run("notifyEmergency()");
  assert.equal(notificationCalls, 1, "Confirmed notification is not sent again");
  elements.get("dismissEmergencyBtn").listeners.click();
  context.p1Next = {...context.p1, analyse_id:"server-analysis-2", contexte_mesures:{source:"ble",simulated:false}};
  run("renderTriage(p1Next)");
  assert.equal(dialog.open, true, "A new P1 analysis opens another alert");
  assert.equal(elements.get("emergencyDemo").hidden, true);
  assert.ok(!decodeURIComponent(elements.get("emergencySmsBtn").getAttribute("href")).includes("DÉMONSTRATION"), "Real-source draft does not reuse the simulation label");
  assert.equal(elements.get("notifyEmergencyBtn").disabled, false);
  run("renderTriage(analysis)");
  assert.equal(dialog.open, false, "A new P2 result closes the previous P1 popup");
  assert.equal(elements.get("reopenEmergencyBtn").hidden, true);
  context.fetch = async () => ({ok:false,status:500});
  await run("analyseTriage({preventDefault(){}})");
  assert.equal(elements.get("triageResult").dataset.state, "error");
  assert.equal(elements.get("triageContent").hidden, true);
  assert.equal(elements.get("triageBtn").disabled, false);
  context.fetch = async () => ({ok:true,json:async () => context.analysis});
  await run("analyseTriage({preventDefault(){}})");
  assert.equal(elements.get("triageResult").dataset.state, "success");
  assert.equal(elements.get("triageContent").hidden, false);
  assert.equal(elements.get("consensusStatus").textContent, "Consensus configuré");
  const chatPopup = elements.get("consensusPopup");
  const chatLauncher = elements.get("consensusLauncher");
  assert.equal(chatPopup.open, false);
  chatLauncher.listeners.click();
  assert.equal(chatPopup.open, true);
  assert.equal(chatLauncher.getAttribute("aria-expanded"), "true");
  elements.get("consensusCreatineExample").listeners.click();
  assert.equal(elements.get("consensusQuestion").value, "Does creatine improve cognitive function?");
  let releaseResearch;
  let researchRequests = 0;
  const research = {answer:"Sources trouvées",papers:[
    {title:"<img src=x onerror=alert(1)>",journal_name:"Example Journal",publish_year:2023,takeaway:"<script>alert(1)</script>",abstract:"Study abstract",url:"https://consensus.app/papers/example"},
    {title:"Another study",url:"javascript:alert(1)"},
  ]};
  context.fetch = async (url, options) => {
    assert.equal(url, "/api/consensus/chat");
    researchRequests += 1;
    assert.deepEqual(JSON.parse(options.body), {question:"Does creatine improve cognitive function?"}, "No watch data, patient fields or API key are sent by the browser");
    await new Promise((resolve) => { releaseResearch = resolve; });
    return {ok:true,json:async () => research};
  };
  const pendingResearch = run("askConsensus({preventDefault(){}})");
  await run("askConsensus({preventDefault(){}})");
  assert.equal(researchRequests, 1, "Research double clicks produce one request");
  assert.equal(elements.get("consensusSendBtn").disabled, true);
  elements.get("consensusCloseBtn").listeners.click();
  assert.equal(chatPopup.open, false);
  assert.equal(chatLauncher.getAttribute("aria-expanded"), "false");
  releaseResearch();
  await pendingResearch;
  assert.equal(elements.get("consensusSendBtn").disabled, false);
  assert.equal(elements.get("consensusQuestion").value, "");
  const researchMessage = elements.get("consensusMessages").children.at(-1);
  assert.equal(researchMessage.dataset.state, "success");
  assert.equal(chatPopup.open, false, "A background response does not reopen a dismissed chat");
  chatLauncher.listeners.click();
  assert.equal(chatPopup.open, true);
  assert.equal(elements.get("consensusMessages").children.at(-1), researchMessage, "Reopening retains the response and sources");
  chatPopup.listeners.keydown({key:"Escape",preventDefault(){}});
  assert.equal(chatPopup.open, false);
  assert.equal(chatLauncher.getAttribute("aria-expanded"), "false");
  chatLauncher.listeners.click();
  const sourceCards = researchMessage.children[2].children;
  assert.equal(sourceCards[0].children[0].textContent, research.papers[0].title, "Paper titles are rendered as safe text");
  assert.equal(sourceCards[0].children[3].textContent, research.papers[0].takeaway, "Takeaways are rendered as safe text");
  assert.equal(sourceCards[0].children.at(-1).getAttribute("rel"), "noopener noreferrer");
  assert.ok(!sourceCards[1].children.some((child) => child.getAttribute("href")), "Unsafe source URLs are not clickable");
  elements.get("consensusQuestion").value = "sleep and recovery";
  context.fetch = async () => ({ok:false,json:async () => ({detail:"Limite Consensus atteinte."})});
  await run("askConsensus({preventDefault(){}})");
  assert.equal(elements.get("consensusMessages").children.at(-1).dataset.state, "error");
  assert.equal(elements.get("consensusFeedback").textContent, "Limite Consensus atteinte.");
  assert.equal(elements.get("consensusSendBtn").disabled, false);
  assert.equal(elements.get("consensusQuestion").value, "sleep and recovery", "A failed question remains available for retry");
  context.fetch = async () => ({ok:true,json:async () => ({answer:"Aucune publication trouvée",papers:[]})});
  await run("askConsensus({preventDefault(){}})");
  assert.equal(elements.get("consensusMessages").children.at(-1).dataset.state, "success");
  assert.equal(elements.get("consensusFeedback").textContent, "0 publications trouvées.");
  console.log("Dashboard: live metrics, safe text, P1 popup, call link, simulation label, notification failure/retry/deduplication, history and triage states passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
