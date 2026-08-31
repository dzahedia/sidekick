const rootInput = document.getElementById("root");
const filesInput = document.getElementById("files");
const taskInput = document.getElementById("task");
const runBtn = document.getElementById("run-btn");
const llmBtn = document.getElementById("llm-btn");
const errorEl = document.getElementById("error");
const approvalEl = document.getElementById("approval");
const approvalJson = document.getElementById("approval-json");
const approveBtn = document.getElementById("approve-btn");
const rejectBtn = document.getElementById("reject-btn");
const statusEl = document.getElementById("status");
const statusLabel = document.getElementById("status-label");
const logsEl = document.getElementById("logs");
const resultsEl = document.getElementById("results");
const summaryEl = document.getElementById("summary");
const changedFilesEl = document.getElementById("changed-files");
const llmAnswerEl = document.getElementById("llm-answer");
const llmTextEl = document.getElementById("llm-text");
const metricsEl = document.getElementById("metrics");
const userNameEl = document.getElementById("user-name");
const logoutBtn = document.getElementById("logout-btn");

let currentThreadId = null;
let pollTimer = null;
let logOffset = 0; // cursor: how many log lines we've already rendered
let metricsTimer = null;

function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
}

function clearError() {
    errorEl.textContent = "";
    errorEl.hidden = true;
}

function setRunning(disabled) {
    runBtn.disabled = disabled;
    llmBtn.disabled = disabled;
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function appendLogs(newLogs) {
    // Append only the new lines (cursor-based) instead of re-rendering the
    // whole log list on every poll.
    if (!newLogs || !newLogs.length) return;
    const text = newLogs.join("\n");
    if (logsEl.textContent) {
        logsEl.textContent += "\n" + text;
    } else {
        logsEl.textContent = text;
    }
    logsEl.scrollTop = logsEl.scrollHeight;
}

function clearResults() {
    // Fully reset the agent results panel so a new run starts fresh:
    // clear the text and hide the panel. It is shown again by renderResults.
    summaryEl.textContent = "";
    changedFilesEl.innerHTML = "";
    resultsEl.hidden = true;
}

function clearLLMAnswer() {
    // Reset the dedicated LLM answer panel so a new LLM call starts fresh.
    llmTextEl.textContent = "";
    llmAnswerEl.hidden = true;
}

function resetRunUI() {
    stopPolling();
    setRunning(false);
    clearError();
    approvalEl.hidden = true;
    approvalJson.textContent = "";
    logsEl.textContent = "";
    statusEl.hidden = true;
    resultsEl.hidden = true;
    summaryEl.textContent = "";
    changedFilesEl.innerHTML = "";
    clearLLMAnswer();
}

function renderResults(status) {
    summaryEl.textContent = status.summary || "Agent finished without a text summary.";
    changedFilesEl.innerHTML = "";
    if (status.changed_files && status.changed_files.length) {
        for (const path of status.changed_files) {
            const div = document.createElement("div");
            div.className = "file";
            div.textContent = path;
            changedFilesEl.appendChild(div);
        }
    } else {
        const div = document.createElement("div");
        div.className = "empty";
        div.textContent = "No files were edited.";
        changedFilesEl.appendChild(div);
    }
    resultsEl.hidden = false;
}

function renderMetrics(metrics) {
    metricsEl.innerHTML = "";
    // The API returns metrics newest first; only show the most recent one.
    const m = metrics && metrics.length ? metrics[0] : null;
    if (!m) {
        const div = document.createElement("div");
        div.className = "empty";
        div.textContent = "No metrics recorded yet.";
        metricsEl.appendChild(div);
        return;
    }
    const card = document.createElement("div");
    card.className = "metric";

    const meta = document.createElement("div");
    meta.className = "metric-meta";
    const parts = [];
    if (m.duration_seconds != null) parts.push(`duration: ${m.duration_seconds.toFixed(2)}s`);
    if (m.token_usage) {
        const t = m.token_usage;
        parts.push(`tokens: ${t.total_tokens ?? "?"} (in ${t.input_tokens ?? "?"} / out ${t.output_tokens ?? "?"})`);
    }
    meta.textContent = parts.join(" · ");

    card.appendChild(meta);
    metricsEl.appendChild(card);
}

async function refreshMetrics() {
    try {
        const res = await fetch("/api/metrics");
        if (!res.ok) return;
        const body = await res.json();
        renderMetrics(body.metrics || []);
    } catch {
        // Best effort: metrics are non-critical.
    }
}

function startMetricsPolling() {
    if (metricsTimer) return;
    metricsTimer = setInterval(refreshMetrics, 3000);
}

function renderApproval(pending) {
    if (pending) {
        approvalJson.textContent = JSON.stringify(pending, null, 2);
        approvalEl.hidden = false;
    } else {
        approvalEl.hidden = true;
        approvalJson.textContent = "";
    }
}

function updateStatusLabel(status) {
    statusLabel.className = "status-label " + status;
    const labels = {
        running: "Running agent…",
        waiting_approval: "Waiting for approval",
        complete: "Finished",
        error: "Error",
    };
    statusLabel.textContent = labels[status] || status;
}

function pollStatus(threadId) {
    stopPolling();
    logOffset = 0;
    pollTimer = setInterval(async () => {
        try {
            const res = await fetch(`/api/status/${threadId}?since=${logOffset}`);
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                throw new Error(body.detail || `Status request failed (${res.status})`);
            }
            const status = await res.json();
            // Only new lines are returned; advance the cursor accordingly.
            appendLogs(status.logs || []);
            logOffset = status.log_offset !== undefined ? status.log_offset : logOffset + (status.logs || []).length;
            updateStatusLabel(status.status);
            renderApproval(status.pending_interrupt);

            if (status.status === "error") {
                stopPolling();
                setRunning(false);
                showError(status.error || "Unknown error.");
                refreshMetrics();
            } else if (status.status === "complete") {
                stopPolling();
                setRunning(false);
                renderResults(status);
                refreshMetrics();
            }
        } catch (err) {
            stopPolling();
            setRunning(false);
            showError(err.message);
        }
    }, 1000);
}

async function startRun() {
    // Start every run from a clean slate: drop the previous server-side
    // session (if any) and reset the whole UI so old results/logs don't
    // linger and the new run's output doesn't append to them.
    if (currentThreadId) {
        const oldThreadId = currentThreadId;
        currentThreadId = null;
        try {
            await fetch(`/api/clear/${oldThreadId}`, { method: "POST" });
        } catch {
            // Best effort: a stale session is harmless if it can't be removed.
        }
    }
    resetRunUI();
    statusEl.hidden = false;
    updateStatusLabel("running");

    const rawFiles = filesInput.value.split("\n").map((l) => l.trim()).filter(Boolean);
    const payload = {
        root: rootInput.value,
        files: rawFiles,
        task: taskInput.value,
    };

    setRunning(true);

    try {
        const res = await fetch("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(body.detail || `Run request failed (${res.status})`);
        }
        currentThreadId = body.thread_id;
        pollStatus(currentThreadId);
    } catch (err) {
        setRunning(false);
        showError(err.message);
    }
}

async function sendDecision(decision) {
    if (!currentThreadId) return;
    clearError();
    approvalEl.hidden = true;
    approvalJson.textContent = "";
    setRunning(true);
    updateStatusLabel("running");

    try {
        const res = await fetch(`/api/resume/${currentThreadId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ decision }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(body.detail || `Resume request failed (${res.status})`);
        }
        pollStatus(currentThreadId);
    } catch (err) {
        setRunning(false);
        showError(err.message);
    }
}

async function startLLM() {
    // Start every LLM call from a clean slate: drop the previous server-side
    // session (if any) and reset the whole UI so the LLM answer doesn't
    // appear to append to the previous agent run's results/logs.
    if (currentThreadId) {
        const oldThreadId = currentThreadId;
        currentThreadId = null;
        try {
            await fetch(`/api/clear/${oldThreadId}`, { method: "POST" });
        } catch {
            // Best effort: a stale session is harmless if it can't be removed.
        }
    }
    resetRunUI();
    llmAnswerEl.hidden = false;
    setRunning(true);

    try {
        const res = await fetch("/api/llm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt: taskInput.value }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || `LLM request failed (${res.status})`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            let idx;
            while ((idx = buffer.indexOf("\n\n")) !== -1) {
                const rawEvent = buffer.slice(0, idx);
                buffer = buffer.slice(idx + 2);
                for (const line of rawEvent.split("\n")) {
                    if (!line.startsWith("data: ")) continue;
                    let event;
                    try {
                        event = JSON.parse(line.slice(6));
                    } catch {
                        continue;
                    }
                    if (event.type === "token") {
                        llmTextEl.textContent += event.text;
                        llmAnswerEl.scrollTop = llmAnswerEl.scrollHeight;
                    } else if (event.type === "error") {
                        throw new Error(event.detail || "LLM stream error.");
                    }
                }
            }
        }
    } catch (err) {
        showError(err.message);
    } finally {
        setRunning(false);
    }
}

runBtn.addEventListener("click", startRun);
llmBtn.addEventListener("click", startLLM);
logoutBtn.addEventListener("click", handleLogout);
approveBtn.addEventListener("click", () => sendDecision(true));
rejectBtn.addEventListener("click", () => sendDecision(false));

// Restore the logged-in user's session (stored by login.js) and pre-fill the
// workspace root with the user's home folder so the app is ready to use.
function restoreSession() {
    try {
        const raw = sessionStorage.getItem("sidekick_user");
        if (!raw) {
            window.location.replace("/");
            return false;
        }
        const user = JSON.parse(raw);
        if (!user || !user.token) {
            sessionStorage.removeItem("sidekick_user");
            window.location.replace("/");
            return false;
        }
        if (user && user.folder) {
            rootInput.value = user.folder;
        }
        if (user && user.name) {
            userNameEl.textContent = user.name;
        }
        return true;
    } catch {
        sessionStorage.removeItem("sidekick_user");
        window.location.replace("/");
        return false;
    }
}

async function handleLogout() {
    // Read the stored session so we can send the token to the server and
    // clear every piece of user data we persisted locally.
    let user = null;
    try {
        const raw = sessionStorage.getItem("sidekick_user");
        if (raw) user = JSON.parse(raw);
    } catch {
        user = null;
    }

    try {
        const headers = {};
        if (user && user.token) {
            headers["Authorization"] = `Bearer ${user.token}`;
        }
        await fetch("/api/logout", { method: "POST", headers });
    } catch {
        // Best effort: clearing the local session is what matters.
    }

    // Clear all locally stored user data (session + local storage).
    sessionStorage.removeItem("sidekick_user");
    localStorage.removeItem("sidekick_user");
    window.location.href = "/";
}

const hasSession = restoreSession();

// Show any metrics recorded by previous runs (e.g. after a page reload),
// then keep the sidebar metrics fresh on a light interval.
if (hasSession) {
    refreshMetrics();
    startMetricsPolling();
}
