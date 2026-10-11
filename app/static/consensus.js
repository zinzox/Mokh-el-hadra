// This independent research panel sends only its own question to the server.
const consensusForm = byId("consensusForm");
const consensusQuestion = byId("consensusQuestion");
const consensusSendBtn = byId("consensusSendBtn");
const consensusMessages = byId("consensusMessages");
const consensusPopup = byId("consensusPopup");
const consensusLauncher = byId("consensusLauncher");
let consensusBusy = false;

function openConsensusChat() {
  if (emergencyDialog.open) return;
  if (!consensusPopup.open) consensusPopup.show();
  consensusLauncher.setAttribute("aria-expanded", "true");
  consensusLauncher.setAttribute("aria-label", "Fermer l’assistant Consensus");
  consensusQuestion.focus();
  consensusMessages.scrollTop = consensusMessages.scrollHeight;
}

function closeConsensusChat() {
  if (consensusPopup.open) consensusPopup.close();
}

async function refreshConsensusStatus() {
  const response = await requestJson("/api/consensus/status");
  byId("consensusStatus").textContent = response.configured ? "Consensus configuré" : "Consensus à configurer";
  byId("consensusStatus").classList.toggle("status-pill-ok", Boolean(response.configured));
}

function researchText(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function researchLink(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function appendResearchMessage(kind, text) {
  const message = document.createElement("article");
  message.className = `research-message research-${kind}`;
  message.appendChild(researchText("p", kind === "user" ? "VOUS" : "CONSENSUS", "research-speaker"));
  message.appendChild(researchText("p", text, "research-answer"));
  consensusMessages.appendChild(message);
  // Bound page memory while retaining recent questions and their sources.
  while (consensusMessages.children.length > 17) consensusMessages.firstElementChild.remove();
  consensusMessages.scrollTop = consensusMessages.scrollHeight;
  return message;
}

function renderResearchAnswer(message, data) {
  if (!data || typeof data.answer !== "string" || !Array.isArray(data.papers)) throw new Error("La réponse de recherche est invalide. Réessayez.");
  message.dataset.state = "success";
  message.replaceChildren(researchText("p", "CONSENSUS", "research-speaker"), researchText("p", data.answer, "research-answer"));
  const sources = document.createElement("ol");
  sources.className = "research-sources";
  for (const paper of data.papers.slice(0, 5)) {
    if (!paper || typeof paper.title !== "string") continue;
    const card = document.createElement("li");
    card.className = "research-source";
    const title = researchText("h4", paper.title);
    card.appendChild(title);
    const meta = [paper.journal_name || "Revue non renseignée", paper.publish_year || "Année non renseignée", paper.study_type].filter(Boolean).join(" · ");
    card.appendChild(researchText("p", meta, "research-source-meta"));
    if (typeof paper.takeaway === "string" && paper.takeaway.trim()) {
      card.appendChild(researchText("p", "Point clé fourni par Consensus", "research-takeaway-label"));
      card.appendChild(researchText("p", paper.takeaway, "research-takeaway"));
    } else card.appendChild(researchText("p", "Aucun point clé fourni. Consultez la publication ou son résumé.", "research-source-meta"));
    if (typeof paper.abstract === "string" && paper.abstract.trim()) {
      const details = document.createElement("details"); details.className = "research-abstract";
      details.appendChild(researchText("summary", "Lire le résumé disponible"));
      details.appendChild(researchText("p", paper.abstract));
      card.appendChild(details);
    }
    const href = typeof paper.url === "string" ? researchLink(paper.url) : null;
    if (href) {
      const link = researchText("a", "Ouvrir la publication ↗", "research-source-link");
      link.setAttribute("href", href); link.setAttribute("target", "_blank"); link.setAttribute("rel", "noopener noreferrer");
      card.appendChild(link);
    }
    sources.appendChild(card);
  }
  if (sources.children.length) message.appendChild(sources);
  if (data.cached) message.appendChild(researchText("p", "Recherche récente réutilisée · aucun nouvel appel à Consensus", "research-source-meta"));
  consensusMessages.scrollTop = consensusMessages.scrollHeight;
}

async function askConsensus(event) {
  event.preventDefault();
  if (consensusBusy) return;
  const question = consensusQuestion.value.trim();
  if (question.length < 3 || question.length > 1000) {
    byId("consensusFeedback").textContent = "Saisissez une question de 3 à 1 000 caractères.";
    if (consensusPopup.open) consensusQuestion.focus();
    return;
  }
  consensusBusy = true;
  consensusSendBtn.disabled = true;
  byId("consensusSendLabel").textContent = "Recherche…";
  byId("consensusFeedback").textContent = "Recherche de publications en cours…";
  appendResearchMessage("user", question);
  const answer = appendResearchMessage("assistant", "Je recherche les publications pertinentes dans Consensus…");
  answer.dataset.state = "loading";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 35000);
  try {
    const response = await fetch("/api/consensus/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ question }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "La recherche n’a pas pu aboutir. Réessayez.");
    renderResearchAnswer(answer, data);
    byId("consensusFeedback").textContent = `${data.papers.length} publication${data.papers.length === 1 ? "" : "s"} trouvée${data.papers.length === 1 ? "" : "s"}.`;
    if (consensusQuestion.value.trim() === question) consensusQuestion.value = "";
  } catch (error) {
    const text = error.name === "AbortError" ? "Le délai de réponse est dépassé. Réessayez dans quelques instants." : error.message || "Connexion au serveur impossible. Réessayez.";
    answer.dataset.state = "error";
    answer.replaceChildren(researchText("p", "CONSENSUS", "research-speaker"), researchText("p", text, "research-answer"));
    byId("consensusFeedback").textContent = text;
  } finally {
    clearTimeout(timeout);
    consensusBusy = false;
    consensusSendBtn.disabled = false;
    byId("consensusSendLabel").textContent = "Rechercher";
    if (consensusPopup.open) consensusQuestion.focus();
  }
}

consensusLauncher.addEventListener("click", () => { if (consensusPopup.open) closeConsensusChat(); else openConsensusChat(); });
byId("consensusCloseBtn").addEventListener("click", closeConsensusChat);
consensusPopup.addEventListener("close", () => {
  if (consensusPopup.open) return;
  consensusLauncher.setAttribute("aria-expanded", "false");
  consensusLauncher.setAttribute("aria-label", "Ouvrir l’assistant Consensus");
  consensusLauncher.focus();
});
consensusPopup.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); closeConsensusChat(); } });
consensusForm.addEventListener("submit", askConsensus);
consensusQuestion.addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) askConsensus(event); });
byId("consensusCreatineExample").addEventListener("click", () => { consensusQuestion.value = "Does creatine improve cognitive function?"; consensusQuestion.focus(); });
byId("consensusSleepExample").addEventListener("click", () => { consensusQuestion.value = "How does sleep affect heart rate variability and recovery?"; consensusQuestion.focus(); });
refreshConsensusStatus().catch(() => { byId("consensusStatus").textContent = "Statut de Consensus indisponible"; });
