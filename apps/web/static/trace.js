(() => {
  "use strict";

  const root = document.getElementById("trace-pane-root");
  const shell = document.getElementById("case-shell");
  if (!root || !shell) return;

  const streamUrl = shell.dataset.caseStreamUrl;
  if (!streamUrl) return;

  const caseRef = streamUrl.split("/").filter(Boolean).at(-2) || "case";
  const lastEventKey = `rebound:${caseRef}:last-event-id`;
  const confirmationKey = `rebound:${caseRef}:confirmation-nonce`;
  const previousEventId = sessionStorage.getItem(lastEventKey);
  if (previousEventId) root.dataset.previousEventId = previousEventId;

  const elements = {
    connection: document.getElementById("trace-connection-status"),
    traceList: document.getElementById("trace-list"),
    traceCount: document.getElementById("trace-step-count"),
    sandboxGrid: document.getElementById("sandbox-grid"),
    sandboxSummary: document.getElementById("sandbox-grid-summary"),
    candidateList: document.getElementById("candidate-list"),
    candidateSummary: document.getElementById("candidate-list-summary"),
    guardianCap: document.getElementById("guardian-effective-cap"),
    guardianConfirmation: document.getElementById("guardian-confirmation-state"),
    guardianRedactions: document.getElementById("guardian-redactions"),
  };

  const state = {
    traces: new Map(),
    sandboxes: [],
    candidates: [],
    recommendedOfferId: null,
    confirmation: null,
    confirmationLocked: false,
    status: null,
    redactions: new Set(),
    dropNextSandboxSnapshot: false,
    droppedSandboxSnapshot: null,
  };

  const eventSource = new EventSource(streamUrl);

  eventSource.addEventListener("open", () => {
    setConnection("Live", "live");
  });
  eventSource.addEventListener("error", () => {
    // The same EventSource reconnects automatically and the browser supplies
    // its last received SSE id in the Last-Event-ID request header.
    setConnection("Reconnecting", "reconnecting");
  });
  eventSource.addEventListener("trace", (event) => {
    const snapshot = parseEvent(event);
    if (!snapshot) return;
    rememberEventId(event);
    renderTrace(snapshot);
    collectRedactions(snapshot.data);
  });
  eventSource.addEventListener("sandboxes", (event) => {
    const snapshot = parseEvent(event);
    if (!snapshot) return;
    rememberEventId(event);
    if (state.dropNextSandboxSnapshot) {
      state.dropNextSandboxSnapshot = false;
      state.droppedSandboxSnapshot = structuredClone(snapshot.slots || []);
      root.dataset.droppedSandboxEventId = event.lastEventId || "";
      return;
    }
    state.sandboxes = Array.isArray(snapshot.slots) ? snapshot.slots : [];
    renderSandboxes();
  });
  eventSource.addEventListener("candidates", (event) => {
    const snapshot = parseEvent(event);
    if (!snapshot) return;
    rememberEventId(event);
    state.candidates = Array.isArray(snapshot.candidates) ? snapshot.candidates : [];
    state.recommendedOfferId = snapshot.recommended_offer_id || null;
    renderCandidates();
  });
  eventSource.addEventListener("confirmation", (event) => {
    const snapshot = parseEvent(event);
    if (!snapshot || !snapshot.request) return;
    rememberEventId(event);
    state.confirmation = snapshot.request;
    state.confirmationLocked =
      sessionStorage.getItem(confirmationKey) === String(snapshot.request.nonce);
    renderGuardian();
    renderCandidates();
  });
  eventSource.addEventListener("status", (event) => {
    const snapshot = parseEvent(event);
    if (!snapshot) return;
    rememberEventId(event);
    state.status = snapshot.status || null;
    if (["executing", "recovered", "failed"].includes(state.status)) {
      state.confirmationLocked = true;
      disableConfirmButton();
      renderGuardian();
    }
  });

  window.addEventListener("pagehide", () => eventSource.close(), { once: true });

  function parseEvent(event) {
    try {
      return JSON.parse(event.data);
    } catch (error) {
      console.warn("Ignored malformed Rebound SSE event", error);
      return null;
    }
  }

  function rememberEventId(event) {
    if (!event.lastEventId) return;
    root.dataset.lastEventId = event.lastEventId;
    const prior = Number(sessionStorage.getItem(lastEventKey) || 0);
    if (Number(event.lastEventId) >= prior) {
      sessionStorage.setItem(lastEventKey, event.lastEventId);
    }
  }

  function setConnection(label, kind) {
    elements.connection.textContent = label;
    elements.connection.dataset.connection = kind;
    elements.connection.className =
      kind === "live"
        ? "rounded-full border border-emerald-300/50 bg-emerald-300/10 px-3 py-1.5 text-sm font-extrabold uppercase tracking-wide text-emerald-200"
        : "rounded-full border border-amber-300/50 bg-amber-300/10 px-3 py-1.5 text-sm font-extrabold uppercase tracking-wide text-amber-200";
  }

  function renderTrace(snapshot) {
    const key = String(snapshot.id);
    if (state.traces.has(key)) return;
    state.traces.set(key, snapshot);

    const item = document.createElement("li");
    item.className = "grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-3 py-2.5";
    item.dataset.eventId = key;
    item.dataset.step = snapshot.step || "";

    const copy = document.createElement("p");
    copy.className = "min-w-0 text-sm leading-snug text-slate-200";

    const step = document.createElement("strong");
    step.className = traceStepClass(snapshot.status);
    step.textContent = snapshot.step || "unnamed.step";

    const separator = document.createTextNode(" — ");
    const summary = document.createElement("span");
    summary.textContent = snapshot.summary || "No summary";
    copy.append(step, separator, summary);

    const elapsed = document.createElement("time");
    elapsed.className = "whitespace-nowrap font-mono text-sm font-bold text-slate-400";
    elapsed.textContent = formatElapsed(snapshot.elapsed_ms);

    item.append(copy, elapsed);
    elements.traceList.append(item);
    elements.traceCount.textContent = `${state.traces.size} ${state.traces.size === 1 ? "step" : "steps"}`;
  }

  function traceStepClass(status) {
    if (status === "failed") return "font-mono font-black text-rose-300";
    if (status === "started") return "font-mono font-black text-amber-200";
    return "font-mono font-black text-emerald-300";
  }

  function formatElapsed(value) {
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds)) return "+—";
    if (milliseconds < 1000) return `+${Math.max(0, Math.round(milliseconds))}ms`;
    return `+${(milliseconds / 1000).toFixed(1)}s`;
  }

  function renderSandboxes() {
    elements.sandboxGrid.replaceChildren();
    const counts = { pending: 0, active: 0, done: 0, failed: 0 };

    for (const sandbox of state.sandboxes) {
      const stateName = String(sandbox.state || "pending").toLowerCase();
      if (stateName === "done") counts.done += 1;
      else if (stateName === "failed") counts.failed += 1;
      else if (stateName === "starting" || stateName === "running") counts.active += 1;
      else counts.pending += 1;

      const tile = document.createElement("div");
      tile.className = `rounded-lg border p-2.5 ${sandboxClass(stateName)}`;
      tile.dataset.slot = String(sandbox.slot);
      tile.dataset.state = stateName;
      tile.setAttribute("role", "listitem");

      const title = document.createElement("p");
      title.className = "font-mono text-sm font-black";
      title.textContent = `Slot ${Number(sandbox.slot) + 1}`;

      const status = document.createElement("p");
      status.className = "mt-1 text-xs font-black uppercase tracking-wide";
      status.textContent = stateName;

      const elapsed = document.createElement("p");
      elapsed.className = "mt-1 font-mono text-xs opacity-80";
      elapsed.textContent = formatElapsed(sandbox.elapsed_ms);

      tile.append(title, status, elapsed);
      elements.sandboxGrid.append(tile);
    }

    elements.sandboxSummary.textContent = state.sandboxes.length
      ? `${counts.active} active · ${counts.done} done · ${counts.failed} failed`
      : "Waiting";
    root.dataset.sandboxStates = state.sandboxes
      .map((sandbox) => `${sandbox.slot}:${sandbox.state}`)
      .join(",");
  }

  function sandboxClass(stateName) {
    if (stateName === "done") {
      return "border-emerald-300/50 bg-emerald-300/15 text-emerald-100";
    }
    if (stateName === "failed") {
      return "border-rose-300/50 bg-rose-300/15 text-rose-100";
    }
    if (stateName === "starting" || stateName === "running") {
      return "border-amber-300/50 bg-amber-300/15 text-amber-100";
    }
    return "border-slate-600 bg-slate-800 text-slate-300";
  }

  function renderCandidates() {
    elements.candidateList.replaceChildren();
    const ranked = [...state.candidates].sort((left, right) => {
      const leftScore = Number(left.score);
      const rightScore = Number(right.score);
      if (Number.isFinite(leftScore) && Number.isFinite(rightScore)) {
        return rightScore - leftScore;
      }
      if (Number.isFinite(leftScore)) return -1;
      if (Number.isFinite(rightScore)) return 1;
      return 0;
    });

    ranked.forEach((candidate, index) => {
      const recommended = candidate.offer_id === state.recommendedOfferId;
      const card = document.createElement("article");
      card.className = recommended
        ? "rounded-xl border-2 border-cyan-300/60 bg-cyan-300/5 p-4"
        : "rounded-xl border border-slate-700 bg-slate-950/50 p-4";
      card.dataset.candidateId = String(candidate.candidate_id || "");
      card.dataset.offerId = candidate.offer_id || "";
      card.dataset.verified = String(Boolean(candidate.verified));
      card.dataset.recommended = String(recommended);

      const heading = document.createElement("div");
      heading.className = "flex items-start justify-between gap-3";

      const rank = document.createElement("div");
      const eyebrow = document.createElement("p");
      eyebrow.className = "text-xs font-black uppercase tracking-[0.14em] text-slate-400";
      eyebrow.textContent = `Option ${index + 1} · ${humanize(candidate.strategy || "replacement")}`;
      const price = document.createElement("h4");
      price.className = "mt-1 text-2xl font-black text-white";
      price.textContent = formatMoney(
        candidate.verified_price || candidate.price,
        candidate.currency,
      );
      rank.append(eyebrow, price);

      const badges = document.createElement("div");
      badges.className = "flex flex-wrap justify-end gap-2";
      if (recommended) badges.append(makeBadge("Recommended", "cyan"));
      badges.append(
        makeBadge(candidate.verified ? "Verified" : "Not verified", candidate.verified ? "green" : "slate"),
      );
      heading.append(rank, badges);

      const facts = document.createElement("dl");
      facts.className = "mt-3 grid grid-cols-3 gap-2";
      facts.append(
        makeFact("Arrival", candidate.arrival || "Pending"),
        makeFact("Delay", formatDelay(candidate.arrival_delay_minutes)),
        makeFact("Score", formatScore(candidate.score)),
      );

      const componentTitle = document.createElement("p");
      componentTitle.className = "mt-3 text-xs font-black uppercase tracking-wide text-slate-400";
      componentTitle.textContent = "Score components";

      const components = document.createElement("div");
      components.className = "mt-2 flex flex-wrap gap-2";
      const entries = Object.entries(candidate.components || {});
      if (entries.length === 0) {
        const waiting = document.createElement("span");
        waiting.className = "text-sm font-semibold text-slate-500";
        waiting.textContent = "Awaiting score";
        components.append(waiting);
      } else {
        for (const [name, value] of entries) {
          const chip = document.createElement("span");
          chip.className = "rounded-md bg-slate-800 px-2 py-1 font-mono text-xs font-bold text-slate-200";
          chip.textContent = `${humanize(name)} ${formatScore(value)}`;
          components.append(chip);
        }
      }

      card.append(heading, facts, componentTitle, components);

      if (candidate.rejected_reason) {
        const rejected = document.createElement("p");
        rejected.className = "mt-3 rounded-lg bg-rose-300/10 px-3 py-2 text-sm font-bold text-rose-200";
        rejected.textContent = `Rejected: ${humanize(candidate.rejected_reason)}`;
        card.append(rejected);
      }

      if (
        state.confirmation &&
        Number(candidate.candidate_id) === Number(state.confirmation.recommended_candidate_id)
      ) {
        card.append(makeConfirmButton(candidate));
      }

      elements.candidateList.append(card);
    });

    const verifiedCount = ranked.filter((candidate) => candidate.verified).length;
    elements.candidateSummary.textContent = ranked.length
      ? `${verifiedCount} verified · ${ranked.length} ranked`
      : "Waiting";
  }

  function makeBadge(label, color) {
    const badge = document.createElement("span");
    badge.className =
      color === "cyan"
        ? "rounded-md bg-cyan-300/15 px-2 py-1 text-xs font-black uppercase text-cyan-200"
        : color === "green"
          ? "rounded-md bg-emerald-300/15 px-2 py-1 text-xs font-black uppercase text-emerald-200"
          : "rounded-md bg-slate-700 px-2 py-1 text-xs font-black uppercase text-slate-300";
    badge.textContent = label;
    return badge;
  }

  function makeFact(label, value) {
    const item = document.createElement("div");
    item.className = "rounded-lg bg-slate-900 p-2";
    const term = document.createElement("dt");
    term.className = "text-xs font-bold uppercase tracking-wide text-slate-500";
    term.textContent = label;
    const description = document.createElement("dd");
    description.className = "mt-1 font-mono text-sm font-bold text-slate-200";
    description.textContent = value;
    item.append(term, description);
    return item;
  }

  function makeConfirmButton(candidate) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = "confirm";
    button.className =
      "mt-4 w-full rounded-xl bg-cyan-300 px-4 py-3 text-lg font-black text-slate-950 transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400";
    button.textContent = state.confirmationLocked ? "Confirmation sent" : "Confirm";
    button.disabled = state.confirmationLocked;
    button.addEventListener("click", () => confirmCandidate(button, candidate), { once: true });
    return button;
  }

  async function confirmCandidate(button, candidate) {
    if (state.confirmationLocked || !state.confirmation) return;

    // Lock synchronously, before fetch starts, so a rapid second click cannot
    // enqueue another request in the browser.
    state.confirmationLocked = true;
    sessionStorage.setItem(confirmationKey, String(state.confirmation.nonce));
    button.disabled = true;
    button.textContent = "Confirming…";
    renderGuardian();

    try {
      root.dataset.confirmRequestCount = String(
        Number(root.dataset.confirmRequestCount || 0) + 1,
      );
      const response = await fetch(streamUrl.replace(/\/stream$/, "/confirm"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_id: Number(candidate.candidate_id),
          nonce: state.confirmation.nonce,
        }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Confirmation failed (${response.status}): ${detail}`);
      }
      button.textContent = "Confirmation sent";
      elements.guardianConfirmation.textContent = "Confirmed · execution queued";
    } catch (error) {
      button.textContent = "Confirmation not accepted";
      elements.guardianConfirmation.textContent = "Confirmation failed · locked";
      elements.guardianConfirmation.className = "mt-1 text-lg font-black text-rose-200";
      console.error(error);
    }
  }

  function disableConfirmButton() {
    const button = elements.candidateList.querySelector("[data-action='confirm']");
    if (!button) return;
    button.disabled = true;
    button.textContent = "Confirmation sent";
  }

  function renderGuardian() {
    if (state.confirmation) {
      elements.guardianCap.textContent = formatMoney(
        state.confirmation.effective_cap_sgd,
        "SGD",
      );
    }

    if (state.status === "executing") {
      elements.guardianConfirmation.textContent = "Confirmed · executing";
      elements.guardianConfirmation.className = "mt-1 text-lg font-black text-emerald-200";
    } else if (state.status === "recovered") {
      elements.guardianConfirmation.textContent = "Confirmed · recovered";
      elements.guardianConfirmation.className = "mt-1 text-lg font-black text-emerald-200";
    } else if (state.confirmationLocked) {
      elements.guardianConfirmation.textContent = "Confirmation sent";
      elements.guardianConfirmation.className = "mt-1 text-lg font-black text-emerald-200";
    } else if (state.confirmation) {
      elements.guardianConfirmation.textContent = "Pending operator tap";
      elements.guardianConfirmation.className = "mt-1 text-lg font-black text-amber-200";
    }
  }

  function collectRedactions(data) {
    if (!data || typeof data !== "object") return;
    const candidates = [
      data.kinds_found,
      data.redaction_kinds,
      data.redactions?.kinds_found,
    ];
    for (const kinds of candidates) {
      if (!Array.isArray(kinds)) continue;
      for (const kind of kinds) {
        if (typeof kind === "string" && kind.trim()) state.redactions.add(kind.trim());
      }
    }
    renderRedactions();
  }

  function renderRedactions() {
    elements.guardianRedactions.replaceChildren();
    if (state.redactions.size === 0) {
      const empty = document.createElement("li");
      empty.className = "text-sm font-semibold text-slate-400";
      empty.textContent = "No redactions reported";
      elements.guardianRedactions.append(empty);
      return;
    }
    for (const kind of state.redactions) {
      const item = document.createElement("li");
      item.className = "rounded-md bg-violet-300/10 px-2 py-1 text-sm font-bold text-violet-200";
      item.textContent = humanize(kind);
      elements.guardianRedactions.append(item);
    }
  }

  function formatMoney(value, currency) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "Pending";
    try {
      return new Intl.NumberFormat("en-SG", {
        style: "currency",
        currency: currency || "SGD",
        maximumFractionDigits: 2,
      }).format(amount);
    } catch (_error) {
      return `${currency || "SGD"} ${amount.toFixed(2)}`;
    }
  }

  function formatDelay(value) {
    const minutes = Number(value);
    if (!Number.isFinite(minutes)) return "Pending";
    if (minutes === 0) return "On time";
    const sign = minutes > 0 ? "+" : "−";
    const absolute = Math.abs(Math.round(minutes));
    const hours = Math.floor(absolute / 60);
    const remainder = absolute % 60;
    if (hours === 0) return `${sign}${remainder}m`;
    return `${sign}${hours}h ${remainder}m`;
  }

  function formatScore(value) {
    const score = Number(value);
    return Number.isFinite(score) ? score.toFixed(3) : "Pending";
  }

  function humanize(value) {
    return String(value).replaceAll("_", " ").replaceAll(".", " ");
  }

  // Browser verification hook: skip exactly one full grid snapshot, then
  // inspect that the following snapshot self-heals every tile.
  window.__reboundTrace = {
    dropNextSandboxSnapshot() {
      state.dropNextSandboxSnapshot = true;
    },
    getState() {
      return {
        lastEventId: root.dataset.lastEventId || null,
        traceCount: state.traces.size,
        sandboxes: structuredClone(state.sandboxes),
        droppedSandboxSnapshot: structuredClone(state.droppedSandboxSnapshot),
        candidateCount: state.candidates.length,
        confirmRequestCount: Number(root.dataset.confirmRequestCount || 0),
      };
    },
  };
})();
