"use strict";

const state = {
  conversationId: null,
  stagedFiles: [],
  // Every active SSE connection (send + reconnect) registers its
  // AbortController here so navigation can cancel them all at once.
  // Without this, reconnection SSE streams accumulate across
  // navigations, exhaust the browser's per-origin connection limit
  // (~6), and block every new request — including /health polls,
  // making the whole app appear frozen while the LLM is working.
  activeSseControllers: new Set(),
};

const el = {
  newChatBtn: document.getElementById("new-chat-btn"),
  conversationList: document.getElementById("conversation-list"),
  messages: document.getElementById("messages"),
  stagedTray: document.getElementById("staged-tray"),
  composer: document.getElementById("composer"),
  dropZone: document.getElementById("drop-zone"),
  promptInput: document.getElementById("prompt-input"),
  fileInput: document.getElementById("file-input"),
  attachBtn: document.getElementById("attach-btn"),
  sendBtn: document.getElementById("send-btn"),
  sourcesList: document.getElementById("sources-list"),
  sourcesPanel: document.getElementById("sources-panel"),
  sourcesToggle: document.getElementById("sources-toggle"),
  sidebar: document.getElementById("sidebar"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  searchChatsBtn: document.getElementById("search-chats-btn"),
};

const STATUS_LABELS = {
  staged: "Staged",
  queued: "Queued…",
  uploading: "Uploading…",
  extracting: "Extracting…",
  chunking: "Chunking…",
  embedding: "Embedding…",
  indexed: "Indexed ✓",
  error: "Error",
  unsupported: "Not supported yet",
};

const MESSAGE_STATUS_LABELS = {
  queued: "Queued…",
  retrieving: "Retrieving…",
};

// Rotated through while the status is "generating" — a new word every
// ~3.5 seconds so it reads as a natural, unhurried cadence.
const GENERATING_PHRASES = [
  "Thinking…",
  "Tinkering…",
  "Reading…",
  "Connecting dots…",
  "Drafting…",
  "Checking sources…",
  "Polishing…",
  "Wrapping up…",
];

const MESSAGE_TERMINAL_STATUSES = new Set(["done", "error"]);

const _CHAT_PROMPTS = [
  "What's on your mind?",
  "Ask me anything about your documents.",
  "Drop a file and ask a question.",
  "What would you like to know?",
  "Paste a document to get started.",
  "Curious about something? Ask away.",
  "Upload a PDF and I'll answer your questions.",
  "What are you working on today?",
];

let _promptCycleTimer = null;
let _promptIsCurrent = true; // true = .prompt-text is showing; false = .prompt-text-next

function _pickPrompt() {
  return _CHAT_PROMPTS[Math.floor(Math.random() * _CHAT_PROMPTS.length)];
}

function _showEmptyChatPrompt() {
  _removeEmptyChatPrompt();
  const container = document.createElement("div");
  container.className = "empty-chat-prompt";
  container.id = "empty-chat-prompt";

  const current = document.createElement("span");
  current.className = "prompt-text";
  current.textContent = _pickPrompt();

  const next = document.createElement("span");
  next.className = "prompt-text-next";
  next.textContent = _pickPrompt();

  container.appendChild(current);
  container.appendChild(next);
  document.getElementById("messages").appendChild(container);

  _promptIsCurrent = true;
  _startPromptCycle();
}

function _startPromptCycle() {
  _stopPromptCycle();
  _promptCycleTimer = setInterval(_cyclePrompt, 5000);
}

function _stopPromptCycle() {
  if (_promptCycleTimer) {
    clearInterval(_promptCycleTimer);
    _promptCycleTimer = null;
  }
}

function _cyclePrompt() {
  const container = document.getElementById("empty-chat-prompt");
  if (!container) return;

  const current = container.querySelector(
    _promptIsCurrent ? ".prompt-text" : ".prompt-text-next"
  );
  const next = container.querySelector(
    _promptIsCurrent ? ".prompt-text-next" : ".prompt-text"
  );

  // Pick a new prompt that differs from the visible one.
  let newText;
  do {
    newText = _pickPrompt();
  } while (newText === current.textContent && _CHAT_PROMPTS.length > 1);

  // Fade out visible text, swap underneath, fade in.
  current.style.opacity = "0";
  setTimeout(() => {
    next.textContent = newText;
    next.style.opacity = "1";
    _promptIsCurrent = !_promptIsCurrent;
    // Reset the faded-out element so it's ready for the next cycle.
    setTimeout(() => {
      current.style.opacity = "0";
    }, 50);
  }, 500);
}

function _removeEmptyChatPrompt() {
  _stopPromptCycle();
  const existing = document.getElementById("empty-chat-prompt");
  if (existing) existing.remove();
}

function toggleSourcesPanel() {
  const app = document.querySelector(".app");
  const isCollapsed = el.sourcesPanel.classList.toggle("collapsed");
  app.setAttribute("data-sources-panel", isCollapsed ? "collapsed" : "expanded");
  localStorage.setItem("sources-collapsed", isCollapsed ? "1" : "0");

  // Same .settled gate as the sidebar — the hover-to-reveal-toggle behaviour
  // is held off until the 200ms collapse transition finishes.  Without this
  // the toggle icon swaps and centres instantly (teleporting) while the grid
  // column is still animating.
  el.sourcesPanel.classList.remove("settled");
  clearTimeout(_sourcesSettleTimer);
  if (isCollapsed) {
    _sourcesSettleTimer = setTimeout(() => {
      el.sourcesPanel.classList.add("settled");
    }, 200);
  }
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status}`);
  }
  if (response.status === 204) return response;
  return response;
}

// -- conversations --------------------------------------------------------

async function fetchConversations() {
  const response = await api("/conversations");
  return response.json();
}

async function renderConversationList() {
  const conversations = await fetchConversations();
  el.conversationList.innerHTML = "";

  // Invalidate the search-popup cache so it picks up changes.
  _searchTitlesCache = [];

  for (const conversation of conversations) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-item" + (conversation.id === state.conversationId ? " active" : "");
    item.dataset.conversationId = conversation.id;
    item.addEventListener("click", () => selectConversation(conversation.id));

    // Full title (visible when expanded).
    const titleSpan = document.createElement("span");
    titleSpan.className = "conv-title";
    titleSpan.textContent = conversation.title;
    item.appendChild(titleSpan);

    // Abbreviated title (first letter, visible when collapsed).
    const abbrevSpan = document.createElement("span");
    abbrevSpan.className = "conv-abbrev";
    abbrevSpan.textContent = (conversation.title || "?").charAt(0).toUpperCase();
    item.appendChild(abbrevSpan);

    // Pinned items show a pushpin icon that replaces the three-dot kebab —
    // it opens the same context menu on click.  Unpinned items show only
    // the three-dot kebab.
    if (conversation.pinned) {
      const pinTrigger = document.createElement("button");
      pinTrigger.type = "button";
      pinTrigger.className = "pin-trigger pinned";
      pinTrigger.title = "Pinned";
      pinTrigger.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" width="16" height="16" aria-hidden="true"><path d="M12 15v7M8 7.308v2.13c0 .209 0 .313-.02.412a1.1 1.1 0 0 1-.09.254 1.4 1.4 0 0 1-.24.334l-1.57 1.962c-.666.833-.999 1.249-1 1.599a.9.9 0 0 0 .377.782c.274.219.807.219 1.872.219h9.342c1.066 0 1.599 0 1.872-.219a.9.9 0 0 0 .376-.782c0-.35-.333-.766-1-1.599l-1.57-1.962a1.4 1.4 0 0 1-.24-.334 1.1 1.1 0 0 1-.09-.254 2.4 2.4 0 0 1-.02-.412V7.308c0-.115 0-.173.007-.23a.6.6 0 0 1 .028-.15.7.7 0 0 1 .08-.215l1.007-2.52c.294-.735.441-1.102.38-1.397a.76.76 0 0 0-.426-.63C16.825 2 16.429 2 15.637 2H8.364c-.792 0-1.188 0-1.44.166a.76.76 0 0 0-.426.63c-.061.295.086.663.38 1.398l1.008 2.52a.7.7 0 0 1 .079.215.6.6 0 0 1 .029.15c.006.056.006.114.006.23Z"/></svg>`;
      pinTrigger.addEventListener("click", (e) => {
        e.stopPropagation();
        showContextMenu(e, conversation);
      });
      item.appendChild(pinTrigger);
    } else {
      const menuTrigger = document.createElement("button");
      menuTrigger.type = "button";
      menuTrigger.className = "context-menu-trigger";
      menuTrigger.title = "More actions";
      menuTrigger.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18" aria-hidden="true"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>`;
      menuTrigger.addEventListener("click", (e) => {
        e.stopPropagation();
        showContextMenu(e, conversation);
      });
      item.appendChild(menuTrigger);
    }

    el.conversationList.appendChild(item);
  }
  return conversations;
}

// A conversation is titled once, from its first exchange (see
// GenerationWorker._maybe_title_conversation) — updated in place here
// rather than re-fetching the whole list, matching updateDocumentStatus's
// existing in-place-update pattern below.
function updateConversationTitle(conversationId, title) {
  const item = el.conversationList.querySelector(`[data-conversation-id="${conversationId}"]`);
  if (!item) return;
  const titleSpan = item.querySelector(".conv-title");
  if (titleSpan) titleSpan.textContent = title;
  const abbrevSpan = item.querySelector(".conv-abbrev");
  if (abbrevSpan) abbrevSpan.textContent = (title || "?").charAt(0).toUpperCase();
}

function _filenameToTitle(filename) {
  var name = filename.replace(/\.[^.]+$/, "");
  return name
    .replace(/[-_]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
}

// -- URL routing (path-based, client-side) -----------------------------

function _setUrl(conversationId) {
  const path = conversationId ? `/chat/${conversationId}` : "/chat";
  if (location.pathname !== path) {
    history.pushState({ conversationId }, "", path);
  }
}

function _conversationIdFromPath() {
  const match = location.pathname.match(/^\/chat\/([a-f0-9-]+)$/);
  return match ? match[1] : null;
}

// -- conversations --------------------------------------------------------

// "New chat" enters draft mode — nothing is persisted until the user
// actually sends a message. Files are staged locally only.
async function createConversation() {
  if (!state.conversationId) return; // already in draft mode
  _enterDraftMode();
}

// Cancel every active SSE connection — both the primary send stream and
// any reconnection watchers.  Without this, reconnection SSE streams
// opened by reconnectToMessage() accumulate across navigations, exhaust
// the browser's per-origin connection limit (~6), and block every new
// request — /health polls, navigation API calls, everything — making the
// app appear completely frozen while the LLM is working serverside.
function _abortAllSse() {
  for (const controller of state.activeSseControllers) {
    controller.abort();
  }
  state.activeSseControllers.clear();
}

function _enterDraftMode() {
  _abortAllSse();

  state.conversationId = null;
  state.stagedFiles = [];
  renderStagedTray();
  // Ensure the composer is usable even if a background sendMessage in
  // another conversation still has it locked — this conversation's UI
  // must not be blocked by an unrelated SSE stream.
  setComposerEnabled(true);
  _updateSendButton();
  el.messages.innerHTML = "";
  el.sourcesList.innerHTML = "";
  _showEmptyChatPrompt();
  _setUrl(null);
  renderConversationList();
}

async function selectConversation(id) {
  _abortAllSse();

  state.conversationId = id;
  _setUrl(id);
  await renderConversationList();
  await _crossfadeMessages(() => Promise.all([loadMessages(id), loadDocuments(id)]));
  // Ensure the composer is usable even if a background sendMessage in
  // another conversation still has it locked — navigating to a different
  // conversation must not leave the composer dead.
  setComposerEnabled(true);
  _updateSendButton();
}

async function loadMessages(id) {
  const response = await api(`/conversations/${id}/messages`);
  const messages = await response.json();
  _clearMessages();
  if (messages.length === 0) {
    _showEmptyChatPrompt();
  }
  for (const message of messages) {
    const bubble = renderMessage(
      message.role,
      message.content,
      message.citations || [],
      message.status || "done",
      message.id
    );
    // A message still in flight when the page loaded (refresh mid-generation,
    // or mid-queue) — reattach to its live stream rather than leaving it
    // showing a permanently-frozen "Retrieving…"/"Generating…" bubble.
    if (message.role === "assistant" && !MESSAGE_TERMINAL_STATUSES.has(message.status)) {
      reconnectToMessage(id, message.id, bubble);
    }
  }
  scrollMessagesToBottom();
}

async function reconnectToMessage(conversationId, messageId, bubble) {
  // Register an AbortController so navigation away from this conversation
  // cancels this reconnection SSE stream — without this, every navigation
  // while a message is in-progress leaves behind a lingering SSE connection
  // that eats a browser origin slot (limit ~6) and eventually blocks every
  // new request, including /health polls.
  const controller = new AbortController();
  state.activeSseControllers.add(controller);

  const response = await fetch(
    `/conversations/${conversationId}/messages/${messageId}/stream`,
    { signal: controller.signal },
  );
  const citationsRef = { groups: new Map() };

  // The reconnection stream carries message events (status, token,
  // citations, done) and, since ingestion moved to the worker, may also
  // carry document_status events for files the worker finishes ingesting
  // as a fallback.  Poll document statuses every 2 s as a safety net so
  // the Sources panel stays live even if no document event arrives.
  var pollTimer = null;
  var stopPoll = function () {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };
  pollTimer = setInterval(async function () {
    // Only update the Sources panel if we're still viewing the conversation
    // this reconnection belongs to — otherwise documents from a previous
    // conversation pollute the current Sources panel after navigation.
    if (state.conversationId !== conversationId) return;
    try {
      var docsResp = await api("/conversations/" + conversationId + "/documents");
      var docs = await docsResp.json();
      for (var i = 0; i < docs.length; i++) {
        var doc = docs[i];
        updateDocumentStatus(
          { document_id: doc.id, status: doc.status, error_message: doc.error_message },
          doc.filename,
        );
      }
    } catch (_) { /* best-effort */ }
  }, 2000);

  try {
    await readSseStream(response, function (parsed) {
      if (parsed.event === "document_status") {
        if (state.conversationId !== conversationId) return;
        updateDocumentStatus(parsed.data, null);
      } else {
        handleMessageEvent(bubble, parsed, citationsRef);
      }
    });
  } catch (err) {
    // Aborted by navigation — not an error, just the user leaving the page.
    if (err.name !== "AbortError") throw err;
  } finally {
    stopPoll();
    state.activeSseControllers.delete(controller);
  }
}

async function loadDocuments(id) {
  const response = await api(`/conversations/${id}/documents`);
  const docs = await response.json();
  el.sourcesList.innerHTML = "";
  for (const doc of docs) {
    updateDocumentStatus({ document_id: doc.id, status: doc.status, error_message: doc.error_message }, doc.filename);
  }
}

// Clears all children from el.messages *except* the crossfade overlay
// (which is a temporary snapshot positioned absolute — removing it would
// break the animation).  When no overlay is present this is equivalent
// to innerHTML = "".
function _clearMessages() {
  var overlay = el.messages.querySelector(".messages-crossfade-overlay");
  while (el.messages.firstChild) {
    if (el.messages.firstChild === overlay) break;
    el.messages.removeChild(el.messages.firstChild);
  }
}

// Crossfades from old to new messages content.  The overlay captures
// a snapshot of what was visible, then after the content swap we fade
// the overlay out while simultaneously fading .messages in — both
// animate for the same 150ms so the transition reads as one continuous
// dissolve rather than a jump.
//
// The overlay is a direct child of .messages (positioned absolute so it
// stays out of flex flow); _clearMessages() is careful to leave it
// alone when loadMessages clears and rebuilds the bubble list.
function _crossfadeMessages(work) {
  return new Promise(function (resolve, reject) {
    var overlay = document.createElement("div");
    overlay.className = "messages-crossfade-overlay";

    // Snapshot the scroll position so the overlay matches what the user
    // is looking at rather than always showing the top of the chat.
    var scrollTop = el.messages.scrollTop;

    var children = el.messages.children;
    for (var i = 0; i < children.length; i++) {
      overlay.appendChild(children[i].cloneNode(true));
    }
    el.messages.appendChild(overlay);
    overlay.scrollTop = scrollTop;

    // New content starts invisible so it doesn't flash through at full
    // opacity while the overlay is still opaque.
    el.messages.classList.add("crossfading");

    // Force layout so the overlay is painted and .crossfading is applied
    // before we swap content underneath.
    overlay.getBoundingClientRect();

    Promise.resolve(work()).then(function () {
      // Kick off both transitions on the same animation frame:
      // overlay 1→0, messages 0→1.
      requestAnimationFrame(function () {
        overlay.classList.add("fading");
        el.messages.classList.remove("crossfading");
      });

      var done = false;
      function cleanup() {
        if (done) return;
        done = true;
        overlay.removeEventListener("transitionend", cleanup);
        clearTimeout(fallback);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        el.messages.classList.remove("crossfading");
        resolve();
      }
      overlay.addEventListener("transitionend", cleanup);
      var fallback = setTimeout(cleanup, 200);
    }).catch(function (err) {
      el.messages.classList.remove("crossfading");
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      reject(err);
    });
  });
}

// -- staged files (pre-submit, client-side only) ---------------------------

function addStagedFiles(fileList) {
  for (const file of fileList) {
    state.stagedFiles.push(file);
  }
  renderStagedTray();
  _updateSendButton();
}

function renderStagedTray() {
  el.stagedTray.innerHTML = "";
  state.stagedFiles.forEach((file, index) => {
    const chip = document.createElement("div");
    chip.className = "staged-chip";

    const label = document.createElement("span");
    label.textContent = file.name;

    const remove = document.createElement("button");
    remove.type = "button";
    remove.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
    remove.addEventListener("click", () => {
      state.stagedFiles.splice(index, 1);
      renderStagedTray();
      _updateSendButton();
    });

    chip.appendChild(label);
    chip.appendChild(remove);
    el.stagedTray.appendChild(chip);
  });
}

// -- messages / citations ----------------------------------------------
//
// The backend already collapses citations onto reader-facing "[1]", "[2]"
// markers numbered per source *document* (see RagEngine._renumber_citations)
// — five chunks from one file share a single number instead of implying
// five different sources. This layer groups the flat `citations` array the
// same way (by document_id, in first-appearance order) so the source badges
// below a message match that numbering exactly, and makes each inline "[N]"
// marker in the message body clickable too, jumping to that source's entry
// in the sidebar.

function groupCitationsByDocument(citations) {
  const groups = new Map();
  for (const citation of citations) {
    let group = groups.get(citation.document_id);
    if (!group) {
      group = {
        number: groups.size + 1,
        documentId: citation.document_id,
        filename: citation.document_filename || "source",
        entries: [],
      };
      groups.set(citation.document_id, group);
      _documentMeta[citation.document_id] = { filename: citation.document_filename || "source" };
    }
    group.entries.push(citation);
  }
  return groups;
}

function renderMessage(role, content, citations, status = "done", messageId = null) {
  const wrapper = document.createElement("div");
  wrapper.className = `message-wrapper ${role}`;
  if (messageId) {
    wrapper.dataset.messageId = messageId;
  }

  const groups = citations && citations.length ? groupCitationsByDocument(citations) : new Map();
  const contentEl = document.createElement("div");
  contentEl.className = "bubble-content";

  if (role === "user") {
    // Right-aligned compound stack: bubble + action buttons pinned beneath.
    const bubble = document.createElement("div");
    bubble.className = "message user";
    renderMessageContent(contentEl, content, groups);
    bubble.appendChild(contentEl);
    wrapper.appendChild(bubble);

    // Action buttons (Copy / Edit) — visible on hover.
    const actions = document.createElement("div");
    actions.className = "message-actions";
    actions.innerHTML =
      '<button type="button" class="action-btn copy-btn" title="Copy">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">' +
          '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>' +
          '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>' +
        '</svg>' +
      '</button>' +
      '<button type="button" class="action-btn edit-btn" title="Edit">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">' +
          '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>' +
        '</svg>' +
      '</button>';
    wrapper.appendChild(actions);

    // Wire copy button.
    actions.querySelector(".copy-btn").addEventListener("click", () => {
      navigator.clipboard.writeText(content).catch(() => {});
    });
    // Wire edit button — puts the text back in the input for editing.
    actions.querySelector(".edit-btn").addEventListener("click", () => {
      el.promptInput.value = content;
      el.promptInput.focus();
      el.promptInput.dispatchEvent(new Event("input"));
    });
  } else {
    // Assistant bubble — hide it when there's no content yet (queued /
    // retrieving / generating) so the status text doesn't sit on top of
    // a visible empty beige box.
    const bubble = document.createElement("div");
    bubble.className = "message assistant";
    renderMessageContent(contentEl, content, groups);
    bubble.appendChild(contentEl);
    if (!content && status !== "done" && status !== "error") {
      bubble.style.display = "none";
    }
    wrapper.appendChild(bubble);
  }

  setMessageStatus(wrapper, status);

  _removeEmptyChatPrompt();
  el.messages.appendChild(wrapper);
  scrollMessagesToBottom();
  return wrapper;
}

// Non-terminal statuses show a status line (reusing the same ::before
// spinner technique as the sources panel); "error" keeps whatever partial
// content the backend preserved and adds a distinct marker rather than
// hiding it.
let _generatingTimer = null;
let _generatingIndex = 0;

function setMessageStatus(bubble, status) {
  bubble.className = bubble.className.replace(/\bstatus-\S+/g, "").trim();
  bubble.classList.add(`status-${status}`);

  let statusEl = bubble.querySelector(".message-status");
  if (status === "done") {
    if (statusEl) statusEl.remove();
    _stopGeneratingRotation();
    return;
  }
  if (!statusEl) {
    statusEl = document.createElement("div");
    statusEl.className = "message-status";
    bubble.insertBefore(statusEl, bubble.firstChild);
  }

  if (status === "generating") {
    _startGeneratingRotation(statusEl);
  } else {
    _stopGeneratingRotation();
    statusEl.textContent = status === "error" ? "Error" : MESSAGE_STATUS_LABELS[status] || status;
  }
}

function _startGeneratingRotation(statusEl) {
  if (_generatingTimer) return; // already rotating
  _generatingIndex = 0;
  statusEl.textContent = GENERATING_PHRASES[0];
  _generatingTimer = setInterval(function () {
    _generatingIndex = (_generatingIndex + 1) % GENERATING_PHRASES.length;
    statusEl.textContent = GENERATING_PHRASES[_generatingIndex];
  }, 3500);
}

function _stopGeneratingRotation() {
  if (_generatingTimer) {
    clearInterval(_generatingTimer);
    _generatingTimer = null;
  }
}

// -- markdown rendering ---------------------------------------------------
//
// Hand-rolled and deliberately narrow — bold/italic/inline code/fenced code
// blocks/headings/lists/paragraphs, no link syntax, no raw HTML passthrough
// — rather than vendoring a general parser. Every node below is built via
// createElement/createTextNode, never innerHTML, so retrieved-document text
// or a model's output can never be parsed as markup: same rule the old
// text-node-only citation splitter followed, just extended to cover
// markdown spans too. Link syntax is skipped on purpose: "[N]" already
// means a citation marker here, and an arbitrary href is an XSS surface
// this app doesn't need for local RAG answers.
//
// Only called once, on the final content of a message (initial load, or
// the "done" SSE event) — live token streaming still appends raw text
// directly (see handleMessageEvent's "token" branch) rather than
// re-parsing markdown on every chunk.

// Underscore italics need a word-boundary guard (unlike `*.*.`) so that
// snake_case_identifiers — common in this app's own answers — don't get
// misread as emphasis; `_interrupted..._`-style markers (see
// RagEngine._finalize's error path) still match since those are always
// flanked by whitespace/string edges, never word characters.
const _INLINE_PATTERN = /`([^`]+)`|\*\*([^*]+?)\*\*|\*([^*]+?)\*|(?<!\w)_([^_]+?)_(?!\w)/g;
const _CITATION_MARKER_PATTERN = /\[(\d+)\]/g;
const _LIST_ITEM_PATTERN = /^\s*(?:[-*+]|\d+[.)])\s+/;

function parseMarkdownBlocks(text) {
  const lines = text.split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i++;
      continue;
    }
    if (line.trimStart().startsWith("```")) {
      i++;
      const codeLines = [];
      while (i < lines.length && lines[i].trim() !== "```") {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip the closing fence (harmless if the fence never closed — i.e. i === lines.length already)
      blocks.push({ type: "code", text: codeLines.join("\n") });
      continue;
    }
    const headingMatch = /^(#{1,6})\s+(.*)/.exec(line);
    if (headingMatch) {
      blocks.push({ type: "heading", level: headingMatch[1].length, text: headingMatch[2] });
      i++;
      continue;
    }
    if (_LIST_ITEM_PATTERN.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length) {
        if (_LIST_ITEM_PATTERN.test(lines[i])) {
          items.push(lines[i].replace(_LIST_ITEM_PATTERN, ""));
          i++;
          continue;
        }
        if (lines[i].trim() === "") {
          // "Loose" markdown lists put a blank line between items (models
          // do this constantly) — peek past it rather than ending the list,
          // but only if more list content is actually what follows; a
          // trailing blank line before an unrelated paragraph should still
          // end the list here and be left for the outer loop to skip.
          let j = i + 1;
          while (j < lines.length && lines[j].trim() === "") j++;
          if (j < lines.length && _LIST_ITEM_PATTERN.test(lines[j])) {
            i = j;
            continue;
          }
        }
        break;
      }
      blocks.push({ type: ordered ? "ordered-list" : "unordered-list", items });
      continue;
    }
    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !lines[i].trimStart().startsWith("```") &&
      !/^#{1,6}\s+/.test(lines[i]) &&
      !_LIST_ITEM_PATTERN.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push({ type: "paragraph", text: paraLines.join("\n") });
  }
  return blocks;
}

// Splits on "[N]" markers and renders each as its own clickable element —
// the one place plain text ever gets citation markers resolved, shared by
// every block/inline context (paragraphs, list items, bold/italic spans).
function appendTextWithCitations(parent, text, numberToGroup) {
  let lastIndex = 0;
  let match;
  _CITATION_MARKER_PATTERN.lastIndex = 0;
  while ((match = _CITATION_MARKER_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    const group = numberToGroup.get(match[1]);
    if (group) {
      const marker = document.createElement("button");
      marker.type = "button";
      marker.className = "citation-marker";
      marker.textContent = `[${group.number}]`;
      marker.title = group.filename;
      marker.addEventListener("click", (e) => handleCitationClick(group.documentId, e));
      parent.appendChild(marker);
    } else {
      parent.appendChild(document.createTextNode(match[0]));
    }
    lastIndex = _CITATION_MARKER_PATTERN.lastIndex;
  }
  if (lastIndex < text.length) {
    parent.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

// Inline spans (code/bold/italic) within one block's text; code spans are
// left completely unprocessed (no citation markers, no nested emphasis) —
// matching standard markdown semantics that code is always literal.
function renderInline(parent, text, numberToGroup) {
  let lastIndex = 0;
  let match;
  _INLINE_PATTERN.lastIndex = 0;
  while ((match = _INLINE_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      appendTextWithCitations(parent, text.slice(lastIndex, match.index), numberToGroup);
    }
    const [, code, bold, starItalic, underscoreItalic] = match;
    if (code !== undefined) {
      const codeEl = document.createElement("code");
      codeEl.textContent = code;
      parent.appendChild(codeEl);
    } else if (bold !== undefined) {
      const strong = document.createElement("strong");
      appendTextWithCitations(strong, bold, numberToGroup);
      parent.appendChild(strong);
    } else {
      const em = document.createElement("em");
      appendTextWithCitations(em, starItalic !== undefined ? starItalic : underscoreItalic, numberToGroup);
      parent.appendChild(em);
    }
    lastIndex = _INLINE_PATTERN.lastIndex;
  }
  if (lastIndex < text.length) {
    appendTextWithCitations(parent, text.slice(lastIndex), numberToGroup);
  }
}

function renderMarkdownBlock(block, numberToGroup) {
  if (block.type === "code") {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = block.text;
    pre.appendChild(code);
    return pre;
  }
  if (block.type === "heading") {
    // Chat bubbles are small — every heading level reads at the same modest
    // size (CSS), so the tag only needs to preserve semantic level, not look.
    const heading = document.createElement(`h${Math.min(block.level + 3, 6)}`);
    renderInline(heading, block.text, numberToGroup);
    return heading;
  }
  if (block.type === "ordered-list" || block.type === "unordered-list") {
    const list = document.createElement(block.type === "ordered-list" ? "ol" : "ul");
    for (const item of block.items) {
      const li = document.createElement("li");
      renderInline(li, item, numberToGroup);
      list.appendChild(li);
    }
    return list;
  }
  const p = document.createElement("p");
  renderInline(p, block.text, numberToGroup);
  return p;
}

function renderMessageContent(contentEl, text, groupsByDocumentNumber) {
  contentEl.textContent = "";
  const numberToGroup = new Map();
  for (const group of groupsByDocumentNumber.values()) {
    numberToGroup.set(String(group.number), group);
  }
  for (const block of parseMarkdownBlocks(text)) {
    contentEl.appendChild(renderMarkdownBlock(block, numberToGroup));
  }
}

function renderCitationBadges(bubble, groups) {
  let citationsEl = bubble.querySelector(".citations");
  if (!citationsEl) {
    citationsEl = document.createElement("div");
    citationsEl.className = "citations";
    bubble.appendChild(citationsEl);
  }
  citationsEl.innerHTML = "";
  for (const group of groups.values()) {
    const badge = document.createElement("button");
    badge.type = "button";
    badge.className = "citation-badge";
    badge.textContent = `[${group.number}] ${_truncateFilename(group.filename)}`;
    badge.title = group.entries.map((citation) => citation.snippet).filter(Boolean).join("\n\n");
    badge.addEventListener("click", (e) => handleCitationClick(group.documentId, e));
    citationsEl.appendChild(badge);
  }
}

// Keep the beginning and end of a filename visible with a middle ellipsis
// so the extension is never hidden — "very_long_docu…ent_name.pdf" on one line.
function _truncateFilename(filename, maxLen) {
  if (maxLen === undefined) maxLen = 34;
  if (filename.length <= maxLen) return filename;
  var extStart = filename.lastIndexOf(".");
  var ext = extStart > 0 ? filename.substring(extStart) : "";
  var name = extStart > 0 ? filename.substring(0, extStart) : filename;
  var budget = maxLen - ext.length - 1; // 1 for the ellipsis character
  if (budget < 6) return filename.substring(0, maxLen - 1) + "…";
  var headLen = Math.ceil(budget * 0.55);
  var tailLen = budget - headLen;
  return name.substring(0, headLen) + "…" + name.substring(name.length - tailLen) + ext;
}

function highlightSource(documentId) {
  const entry = el.sourcesList.querySelector(`[data-document-id="${documentId}"]`);
  if (!entry) return;
  entry.scrollIntoView({ behavior: "smooth", block: "center" });
  entry.classList.add("highlight");
  setTimeout(() => entry.classList.remove("highlight"), 1500);
}

// -- citation modal & PDF viewer ---------------------------------------

// documentMeta: { documentId: { filename } } — populated by both
// groupCitationsByDocument (for citation-bearing messages) and
// updateDocumentStatus (for source-panel entries).
const _documentMeta = {};

// Blob URL cache — fetch once per document, reuse across modal opens,
// revoked on page unload. Using a Blob URL (rather than pointing the
// iframe at the API endpoint) lets the browser's built-in PDF viewer
// work without a second network round-trip.
const _documentBlobUrls = {};

async function _fetchDocumentBlobUrl(documentId) {
  if (_documentBlobUrls[documentId]) return _documentBlobUrls[documentId];
  const response = await fetch(
    `/conversations/${state.conversationId}/documents/${documentId}/file`
  );
  if (!response.ok) return null;
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  _documentBlobUrls[documentId] = url;
  return url;
}

function openCitationModal(documentId, filename) {
  const modal = document.getElementById("citation-modal");
  const modalBody = document.getElementById("modal-body");

  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  modalBody.innerHTML =
    '<div style="padding:24px;text-align:center;color:var(--color-ink-mute)">Loading…</div>';
  document.body.style.overflow = "hidden";

  _fetchDocumentBlobUrl(documentId).then((url) => {
    if (!url) {
      modalBody.innerHTML =
        '<p class="message-status" style="padding:24px">Could not load this file.</p>';
      return;
    }
    const iframe = document.createElement("iframe");
    iframe.src = url;
    iframe.title = filename;
    iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:none;";
    modalBody.innerHTML = "";
    modalBody.appendChild(iframe);
  });
}

function closeCitationModal() {
  const modal = document.getElementById("citation-modal");
  modal.classList.remove("active");
  modal.setAttribute("aria-hidden", "true");
  document.getElementById("modal-body").innerHTML = "";
  document.body.style.overflow = "";
}

// Unified citation click handler — wired to inline [N] markers, bottom
// citation badges, and source-panel entries. Left-click opens the modal;
// Cmd+Click (Mac) / Ctrl+Click opens in a new browser tab.
async function handleCitationClick(documentId, event) {
  const meta = _documentMeta[documentId];
  if (!meta) return;

  if (event.metaKey || event.ctrlKey) {
    event.preventDefault();
    const url = await _fetchDocumentBlobUrl(documentId);
    if (url) window.open(url, "_blank");
    return;
  }

  highlightSource(documentId);
  openCitationModal(documentId, meta.filename);
}

// Revoke blob URLs on unload to prevent memory leaks.
window.addEventListener("beforeunload", () => {
  for (const url of Object.values(_documentBlobUrls)) {
    URL.revokeObjectURL(url);
  }
});

function scrollMessagesToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

// -- sources panel / document status animation --------------------------

function updateDocumentStatus(data, filename) {
  let entry = el.sourcesList.querySelector(`[data-document-id="${data.document_id}"]`);
  if (!entry) {
    entry = document.createElement("div");
    entry.className = "source-entry";
    entry.dataset.documentId = data.document_id;
    entry.innerHTML = '<span class="source-name"></span><span class="source-status"></span>';
    // Make source entries clickable — opens the citation modal.
    entry.style.cursor = "pointer";
    entry.addEventListener("click", (e) => {
      const fname = _documentMeta[data.document_id]?.filename
        || entry.querySelector(".source-name").textContent
        || "source";
      handleCitationClick(data.document_id, e);
    });
    el.sourcesList.appendChild(entry);
  }
  if (filename) {
    entry.querySelector(".source-name").textContent = filename;
    if (!_documentMeta[data.document_id]) {
      _documentMeta[data.document_id] = { filename };
    }
  }
  entry.querySelector(".source-status").textContent = STATUS_LABELS[data.status] || data.status;
  entry.className = "source-entry status-" + data.status;
  if (data.error_message) {
    entry.title = data.error_message;
  }
}

// -- sidebar collapse / expand -----------------------------------------

let _sidebarSettleTimer = null;
let _sourcesSettleTimer = null;

// The collapsing "keepr" -> "k" clip-down (app.css's .brand max-width
// transition) needs a base max-width equal to "keepr"'s own rendered
// width, not a generous guessed constant — CSS ease() moves slowly at
// first, so even a small pixel buffer beyond the text's real width eats
function toggleSidebar() {
  const app = document.querySelector(".app");
  const isCollapsed = el.sidebar.classList.toggle("collapsed");
  app.setAttribute("data-sidebar", isCollapsed ? "collapsed" : "expanded");
  localStorage.setItem("sidebar-collapsed", isCollapsed ? "1" : "0");

  // The hover-to-reveal-toggle behavior (CSS gates it on .settled) is
  // deliberately held off until the 200ms collapse transition actually
  // finishes. Without this, clicking the toggle button — while the
  // cursor is still hovering right where it is — would instantly match
  // ".collapsed .sidebar-header:hover" and hide "k" via display:none,
  // skipping straight past the smooth max-width clip animation.
  el.sidebar.classList.remove("settled");
  clearTimeout(_sidebarSettleTimer);
  if (isCollapsed) {
    _sidebarSettleTimer = setTimeout(() => {
      el.sidebar.classList.add("settled");
    }, 200);
  }
}

// -- context menu ------------------------------------------------------

function showContextMenu(event, conversation) {
  closeContextMenu();

  // Keep the trigger visible while the cursor travels from the item
  // to the popup — without this the :hover state drops on the item
  // the moment the cursor leaves it.
  var item = event.target.closest(".conversation-item");
  if (item) item.classList.add("menu-open");

  const menu = document.createElement("div");
  menu.className = "context-menu";
  menu.id = "context-menu";

  const pinLabel = conversation.pinned ? "Unpin" : "Pin";
  const items = [
    {
      label: pinLabel,
      icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" class="context-menu-icon"><path d="M12 15v7M8 7.308v2.13c0 .209 0 .313-.02.412a1.1 1.1 0 0 1-.09.254 1.4 1.4 0 0 1-.24.334l-1.57 1.962c-.666.833-.999 1.249-1 1.599a.9.9 0 0 0 .377.782c.274.219.807.219 1.872.219h9.342c1.066 0 1.599 0 1.872-.219a.9.9 0 0 0 .376-.782c0-.35-.333-.766-1-1.599l-1.57-1.962a1.4 1.4 0 0 1-.24-.334 1.1 1.1 0 0 1-.09-.254 2.4 2.4 0 0 1-.02-.412V7.308c0-.115 0-.173.007-.23a.6.6 0 0 1 .028-.15.7.7 0 0 1 .08-.215l1.007-2.52c.294-.735.441-1.102.38-1.397a.76.76 0 0 0-.426-.63C16.825 2 16.429 2 15.637 2H8.364c-.792 0-1.188 0-1.44.166a.76.76 0 0 0-.426.63c-.061.295.086.663.38 1.398l1.008 2.52a.7.7 0 0 1 .079.215.6.6 0 0 1 .029.15c.006.056.006.114.006.23Z"/></svg>`,
      action: () => togglePin(conversation),
    },
    {
      label: "Rename",
      icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" class="context-menu-icon"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>`,
      action: () => startRename(conversation),
    },
    {
      label: "Delete",
      icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" class="context-menu-icon"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>`,
      action: () => deleteConversationById(conversation),
      danger: true,
    },
  ];

  for (const item of items) {
    const btn = document.createElement("button");
    btn.className = item.danger ? "danger" : "";
    btn.innerHTML = item.icon + item.label;
    btn.addEventListener("click", () => {
      closeContextMenu();
      item.action();
    });
    menu.appendChild(btn);
  }

  menu.style.left = `${Math.min(event.clientX, window.innerWidth - 170)}px`;
  menu.style.top = `${event.clientY}px`;
  document.body.appendChild(menu);
  // Trigger the CSS opacity transition.
  requestAnimationFrame(() => menu.classList.add("show"));

  setTimeout(() => {
    document.addEventListener("click", closeContextMenu, { once: true });
  }, 0);
}

function closeContextMenu() {
  const existing = document.getElementById("context-menu");
  if (existing) existing.remove();
  const openItem = document.querySelector(".conversation-item.menu-open");
  if (openItem) openItem.classList.remove("menu-open");
}

// -- pin / rename / delete ---------------------------------------------

async function togglePin(conversation) {
  await api(`/conversations/${conversation.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned: !conversation.pinned }),
  });
  await renderConversationList();
}

function startRename(conversation) {
  const overlay = document.getElementById("rename-overlay");
  const input = document.getElementById("rename-input");
  const cancelBtn = document.getElementById("rename-cancel");
  const renameBtn = document.getElementById("rename-save");

  input.value = conversation.title;
  overlay.classList.add("active");
  overlay.setAttribute("aria-hidden", "false");
  input.focus();
  input.select();

  async function commit() {
    const newTitle = input.value.trim() || conversation.title;
    overlay.classList.remove("active");
    overlay.setAttribute("aria-hidden", "true");
    await api(`/conversations/${conversation.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle }),
    });
    await renderConversationList();
  }

  function cancel() {
    overlay.classList.remove("active");
    overlay.setAttribute("aria-hidden", "true");
  }

  function onKeydown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      cancel();
    }
  }

  // Clean up old listeners by cloning nodes.
  const newCancel = cancelBtn.cloneNode(true);
  const newRename = renameBtn.cloneNode(true);
  cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);
  renameBtn.parentNode.replaceChild(newRename, renameBtn);

  newCancel.addEventListener("click", cancel);
  newRename.addEventListener("click", commit);
  input.addEventListener("keydown", onKeydown, { once: false });

  // Store for cleanup.
  overlay._renameCleanup = function () {
    input.removeEventListener("keydown", onKeydown);
  };
}

function _confirmDelete(title) {
  return new Promise((resolve) => {
    const backdrop = document.getElementById("delete-confirm");
    const message = document.getElementById("confirm-message");
    const cancelBtn = document.getElementById("confirm-cancel");
    const deleteBtn = document.getElementById("confirm-delete");

    message.innerHTML = `Delete <strong>"${title.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")}"</strong>?<br>This cannot be undone.`;
    backdrop.classList.add("active");
    backdrop.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";

    function cleanup(result) {
      backdrop.classList.remove("active");
      backdrop.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      cancelBtn.removeEventListener("click", onCancel);
      deleteBtn.removeEventListener("click", onDelete);
      backdrop.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onEscape);
      resolve(result);
    }

    function onCancel() { cleanup(false); }
    function onDelete() { cleanup(true); }
    function onBackdrop(e) { if (e.target === backdrop) cleanup(false); }
    function onEscape(e) {
      if (e.key === "Escape") {
        const modal = document.getElementById("citation-modal");
        if (modal && modal.classList.contains("active")) return; // let citation modal close first
        cleanup(false);
      }
    }

    cancelBtn.addEventListener("click", onCancel);
    deleteBtn.addEventListener("click", onDelete);
    backdrop.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onEscape);
  });
}

async function deleteConversationById(conversation) {
  const confirmed = await _confirmDelete(conversation.title);
  if (!confirmed) return;

  await api(`/conversations/${conversation.id}`, { method: "DELETE" });

  if (state.conversationId === conversation.id) {
    state.conversationId = null;
  }

  const conversations = await renderConversationList();
  if (!state.conversationId) {
    _enterDraftMode();
    // Re-render so the sidebar highlights nothing and the URL is /chat.
    await renderConversationList();
  }
}

// -- sending a message ----------------------------------------------------

function setComposerEnabled(enabled) {
  el.promptInput.disabled = !enabled;
  el.sendBtn.disabled = !enabled;
}

function _updateSendButton() {
  // Never re-enable the button while a send is in flight — the composer
  // is disabled via setComposerEnabled(false) during the SSE round-trip,
  // and _updateSendButton (called from addStagedFiles / renderStagedTray)
  // must not override that.
  if (el.promptInput.disabled) return;
  var hasText = el.promptInput.value.trim().length > 0;
  var hasFiles = state.stagedFiles.length > 0;
  el.sendBtn.disabled = !hasText && !hasFiles;
}

function parseSseEvent(raw) {
  let eventName = "message";
  let dataLine = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLine += line.slice(5).trim();
    }
  }
  if (!dataLine) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) };
  } catch {
    return null;
  }
}

// Shared low-level SSE reader — both the initial send (POST, may also carry
// document_status events) and a refresh-reconnect (GET, message-only) read
// their response through this same loop, so there is exactly one place that
// knows how to frame events off the wire.
async function readSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseSseEvent(rawEvent);
      boundary = buffer.indexOf("\n\n");
      if (parsed) onEvent(parsed);
    }
  }
}

// Shared handling for the message_status/token/citations/done/
// conversation_title events that both a live send and a post-refresh
// reconnect need to react to identically.
function handleMessageEvent(bubble, parsed, citationsRef) {
  if (parsed.event === "conversation_title") {
    updateConversationTitle(parsed.data.conversation_id, parsed.data.title);
  } else if (parsed.event === "message_status") {
    if (bubble.dataset.messageId === undefined) {
      bubble.dataset.messageId = parsed.data.message_id;
    }
    setMessageStatus(bubble, parsed.data.status);
  } else if (parsed.event === "token") {
    setMessageStatus(bubble, "generating");
    const msgBubble = bubble.querySelector(".message.assistant");
    // Reveal the bubble on the first token — it was hidden (display:none)
    // while queued/retrieving/generating to avoid an empty beige box.
    var wasHidden = msgBubble && msgBubble.style.display === "none";
    if (wasHidden) {
      msgBubble.style.display = "";
    }
    const contentEl = bubble.querySelector(".bubble-content");
    // Strip leading whitespace from the first token so the generated text
    // doesn't start with a visible blank line (the LLM often opens with \n).
    var text = parsed.data.text;
    if (wasHidden) {
      text = text.replace(/^\s+/, "");
      if (!text) return; // nothing visible yet, keep the bubble open
    }
    contentEl.textContent = (contentEl.textContent || "") + text;
    scrollMessagesToBottom();
  } else if (parsed.event === "citations") {
    citationsRef.groups = parsed.data.citations.length
      ? groupCitationsByDocument(parsed.data.citations)
      : new Map();
  } else if (parsed.event === "done") {
    // When a fast-path answer (greeting, refusal) arrives with no preceding
    // token events, the assistant bubble is still display:none — reveal it
    // before filling in the content.
    const msgBubble = bubble.querySelector(".message.assistant");
    if (msgBubble && msgBubble.style.display === "none") {
      msgBubble.style.display = "";
    }
    renderMessageContent(bubble.querySelector(".bubble-content"), parsed.data.content, citationsRef.groups);
    setMessageStatus(bubble, parsed.data.status);
    scrollMessagesToBottom();
    // Re-fetch the conversation list so the just-finished conversation
    // jumps to the top of the sidebar immediately (its updated_at was
    // bumped by the worker's touch_conversation).  Fire-and-forget —
    // the SSE reader must not block on this async work.
    renderConversationList().catch(function () { /* best-effort */ });
  }
}

async function sendMessage(event) {
  event.preventDefault();

  const prompt = el.promptInput.value.trim();
  if (!prompt && state.stagedFiles.length === 0) return;

  // If we're in draft mode, create the conversation now — not before.
  // This means an empty "New chat" click never hits the backend.
  const isNewConversation = !state.conversationId;
  if (!state.conversationId) {
    const response = await api("/conversations", { method: "POST" });
    const conversation = await response.json();
    state.conversationId = conversation.id;
    _setUrl(conversation.id);
    // Instantly show the new conversation in the sidebar — don't wait
    // for the SSE generation stream to finish.
    await renderConversationList();
  }

  // When the user drops a file without typing anything, fill in a
  // sensible default so the LLM has a question to answer (and so a user
  // bubble appears in the chat — otherwise it looks like nothing was sent).
  const effectivePrompt = prompt || (state.stagedFiles.length ? "Summarize the uploaded document." : "");

  if (effectivePrompt) {
    renderMessage("user", effectivePrompt, []);
  }
  el.promptInput.value = "";
  el.promptInput.style.height = "auto";
  _updateSendButton();

  // Snapshot the conversation we're sending TO (may have just been created
  // above) so the re-enable at the end of this function only fires if the
  // user is still viewing this conversation.  If they navigated to a
  // different chat or draft mode while this send was in flight, _enterDraftMode
  // / selectConversation already re-enabled the composer.
  const owningConversationId = state.conversationId;
  setComposerEnabled(false);

  const formData = new FormData();
  formData.append("prompt", effectivePrompt);
  for (const file of state.stagedFiles) {
    formData.append("files", file);
  }

  const fileQueue = [...state.stagedFiles];
  const documentFilenames = {};
  const pendingIds = [];
  state.stagedFiles = [];
  renderStagedTray();

  // If this is a brand-new conversation, set the title immediately from
  // the first user message (or the first file's name if there's no prompt).
  // This means "Hi" titles the chat "Hi" instantly, with zero LLM cost.
  // The backend's _maybe_title_conversation only runs the LLM titler when
  // documents are actually involved — text-only chats keep this title.
  if (isNewConversation) {
    const derivedTitle = prompt.trim() || (fileQueue.length ? _filenameToTitle(fileQueue[0].name) : "");
    if (derivedTitle) {
      const truncated = derivedTitle.length > 80 ? derivedTitle.slice(0, 77) + "..." : derivedTitle;
      updateConversationTitle(state.conversationId, truncated);
      api(`/conversations/${state.conversationId}`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: truncated}),
      }).catch(function () { /* fire-and-forget */ });
    }
  }

  // Optimistic: seed the Sources panel with each file immediately so the
  // user sees their documents are queued, rather than waiting for the
  // backend's first document_status SSE event (which can take seconds).
  for (let i = 0; i < fileQueue.length; i++) {
    const pendingId = "pending-" + i;
    pendingIds.push(pendingId);
    updateDocumentStatus(
      { document_id: pendingId, status: "queued", error_message: null },
      fileQueue[i].name,
    );
  }

  // Rendered immediately, before the network round-trip even starts — this
  // is the "shows as sent but pending" state; previously nothing appeared
  // here until the first token streamed back, which on a real model can be
  // many seconds away.
  const assistantBubble = renderMessage("assistant", "", [], "queued");
  const citationsRef = { groups: new Map() };

  // Create an AbortController so navigation away from this conversation
  // can cancel the SSE stream immediately rather than letting it run to
  // completion in the background, interfering with the new view.
  const controller = new AbortController();
  state.activeSseControllers.add(controller);

  try {
    const response = await fetch(`/conversations/${state.conversationId}/messages`, {
      method: "POST",
      body: formData,
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error("Server returned " + response.status);
    }

    await readSseStream(response, (parsed) => {
      if (parsed.event === "document_status") {
        // Only update the Sources panel if we're still viewing this send's
        // conversation — otherwise the old conversation's documents bleed
        // into the current conversation's Sources list (and vice versa).
        if (state.conversationId !== owningConversationId) return;
        const data = parsed.data;
        if (data.status === "uploading" && !documentFilenames[data.document_id] && fileQueue.length) {
          documentFilenames[data.document_id] = fileQueue.shift().name;
          // Replace the optimistic pending-{i} entry with the real document_id
          // so updateDocumentStatus finds and updates it in-place.
          const pendingId = pendingIds.shift();
          if (pendingId) {
            const pendingEntry = el.sourcesList.querySelector(
              '[data-document-id="' + pendingId + '"]',
            );
            if (pendingEntry) {
              pendingEntry.dataset.documentId = data.document_id;
            }
          }
        }
        updateDocumentStatus(data, documentFilenames[data.document_id]);
      } else {
        handleMessageEvent(assistantBubble, parsed, citationsRef);
      }
    });
  } catch (err) {
    // Aborted by navigation — not an error. The user moved to a different
    // conversation (or new chat) while this send was in flight; the backend
    // generation keeps running independently and a future page load will
    // reattach to it via the reconnect endpoint.
    if (err.name === "AbortError") return;

    // 404 means the conversation was deleted (e.g. DB wiped between
    // restarts).  Undo the optimistic UI, reset to draft mode, and
    // retry — the next call will create a fresh conversation.
    if (String(err.message) === "Server returned 404") {
      // Remove the user + assistant bubbles we just rendered so they
      // don't duplicate on the retry.
      while (el.messages.firstChild) {
        el.messages.removeChild(el.messages.firstChild);
      }
      _showEmptyChatPrompt();
      // Restore the staged files that were cleared above.
      state.stagedFiles = fileQueue;
      renderStagedTray();
      // Clear the optimistic source entries.
      el.sourcesList.innerHTML = "";
      // Reset conversation state so the retry creates a new one.
      state.conversationId = null;
      _setUrl(null);
      _updateSendButton();
      // Retry — this call will create a fresh conversation and re-send.
      return sendMessage(event);
    }

    // If the SSE connection fails before we get a single token, surface
    // it on the assistant bubble rather than leaving it stuck on
    // "Queued…" forever with the composer locked out.
    setMessageStatus(assistantBubble, "error");
    const contentEl = assistantBubble.querySelector(".bubble-content");
    if (contentEl && !contentEl.textContent.trim()) {
      contentEl.textContent = "Something went wrong. Please try again.";
    }
    var msgBubble = assistantBubble.querySelector(".message.assistant");
    if (msgBubble && msgBubble.style.display === "none") {
      msgBubble.style.display = "";
    }
  } finally {
    state.activeSseControllers.delete(controller);
  }

  // Only re-enable the composer if we're still viewing the conversation
  // this send belonged to.  If the user navigated away (New Chat, sidebar
  // click, back/forward), _enterDraftMode / selectConversation already
  // re-enabled it — and we must not steal focus back from the current view.
  if (state.conversationId === owningConversationId) {
    setComposerEnabled(true);
    _updateSendButton();
    el.promptInput.focus();
  }
  await renderConversationList();
}

// -- wiring ---------------------------------------------------------------

el.newChatBtn.addEventListener("click", () => createConversation());

// Respond to browser back/forward — load the conversation named in the URL.
window.addEventListener("popstate", async () => {
  const id = _conversationIdFromPath();
  if (id && id !== state.conversationId) {
    await selectConversation(id);
  } else if (!id && state.conversationId) {
    _enterDraftMode();
  }
});
el.sidebarToggle.addEventListener("click", toggleSidebar);
el.sourcesToggle.addEventListener("click", toggleSourcesPanel);
// -- search popup ----------------------------------------------------------

el.searchChatsBtn.addEventListener("click", openSearchPopup);

function openSearchPopup() {
  const overlay = document.getElementById("search-overlay");
  const input = document.getElementById("search-popup-input");
  const results = document.getElementById("search-popup-results");
  const closeBtn = document.getElementById("search-popup-close");

  overlay.classList.add("active");
  overlay.setAttribute("aria-hidden", "false");
  input.value = "";
  input.focus();

  // Show all conversations as initial results.
  _renderSearchResults(input.value);

  function close() {
    overlay.classList.remove("active");
    overlay.setAttribute("aria-hidden", "true");
    input.removeEventListener("input", onInput);
    closeBtn.removeEventListener("click", close);
    overlay.removeEventListener("click", onOverlayClick);
    document.removeEventListener("keydown", onKeyDown);
  }

  function onInput() {
    _renderSearchResults(input.value);
  }

  function onOverlayClick(e) {
    if (e.target === overlay) close();
  }

  function onKeyDown(e) {
    if (e.key === "Escape") close();
  }

  function onInputKeyDown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      var first = document.querySelector("#search-popup-results .search-result-item");
      if (first) first.click();
    }
  }

  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onInputKeyDown);
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("keydown", onKeyDown);
}

// Cache for the current conversation-titles list so the popup's filter
// never needs to re-fetch.
var _searchTitlesCache = [];

function _renderSearchResults(query) {
  var results = document.getElementById("search-popup-results");
  results.innerHTML = "";

  // Populate cache from the current list if empty.
  if (!_searchTitlesCache.length) {
    var items = document.querySelectorAll("#conversation-list .conversation-item");
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      _searchTitlesCache.push({
        id: item.dataset.conversationId,
        title: (item.querySelector(".conv-title") || {}).textContent || "",
      });
    }
  }

  var filtered = _searchTitlesCache;
  if (query) {
    var q = query.toLowerCase();
    filtered = _searchTitlesCache.filter(function (entry) {
      return entry.title.toLowerCase().indexOf(q) !== -1;
    });
  }

  if (!filtered.length) {
    var empty = document.createElement("div");
    empty.className = "search-result-empty";
    empty.textContent = query ? "No chats match your search." : "No chats yet.";
    results.appendChild(empty);
    return;
  }

  for (var j = 0; j < filtered.length; j++) {
    var entry = filtered[j];
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "search-result-item";
    btn.textContent = entry.title;
    btn.addEventListener("click", function (id) {
      return function () {
        document.getElementById("search-overlay").classList.remove("active");
        document.getElementById("search-overlay").setAttribute("aria-hidden", "true");
        selectConversation(id).then(function () {
          _searchTitlesCache = [];
        });
      };
    }(entry.id));
    results.appendChild(btn);
  }
}

// Close context menu or citation modal on Escape.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const modal = document.getElementById("citation-modal");
    if (modal && modal.classList.contains("active")) {
      closeCitationModal();
    }
    const renameOverlay = document.getElementById("rename-overlay");
    if (renameOverlay && renameOverlay.classList.contains("active")) {
      renameOverlay.classList.remove("active");
      renameOverlay.setAttribute("aria-hidden", "true");
    }
    closeContextMenu();
  }
  // Global Enter-to-submit: when the user hasn't clicked into the textarea
  // but there's content ready to send (staged files or typed text), Enter
  // submits just like it does when the textarea is focused.  Skip when
  // focus is in another input (search popup, rename dialog) or when a
  // modal/overlay is open.
  if (e.key === "Enter" && !e.shiftKey) {
    if (el.promptInput.disabled) return;
    var tag = document.activeElement ? document.activeElement.tagName : "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (document.getElementById("search-overlay").classList.contains("active")) return;
    if (document.getElementById("rename-overlay").classList.contains("active")) return;
    if (document.getElementById("delete-confirm").getAttribute("aria-hidden") === "false") return;
    var hasText = el.promptInput.value.trim().length > 0;
    var hasFiles = state.stagedFiles.length > 0;
    if (hasText || hasFiles) {
      e.preventDefault();
      el.composer.requestSubmit();
    }
  }
});
el.composer.addEventListener("submit", sendMessage);
document.getElementById("citation-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeCitationModal();
});
document.getElementById("modal-close").addEventListener("click", closeCitationModal);
document.getElementById("rename-overlay").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) {
    var overlay = document.getElementById("rename-overlay");
    overlay.classList.remove("active");
    overlay.setAttribute("aria-hidden", "true");
  }
});
el.attachBtn.addEventListener("click", async () => {
  if (window.showOpenFilePicker) {
    try {
      var handles = await window.showOpenFilePicker({
        multiple: true,
        types: [
          {
            description: "Documents",
            accept: { "application/pdf": [".pdf"], "text/plain": [".txt"], "text/markdown": [".md"] }
          },
          {
            description: "Audio",
            accept: { "audio/mpeg": [".mp3"], "audio/wav": [".wav"], "audio/mp4": [".m4a"] }
          },
          {
            description: "Video",
            accept: { "video/mp4": [".mp4"], "video/quicktime": [".mov"], "video/webm": [".webm"] }
          }
        ]
      });
      var files = await Promise.all(handles.map(function(h) { return h.getFile(); }));
      addStagedFiles(files);
    } catch (err) {
      // User cancelled or API not available — ignore.
      if (err.name !== "AbortError") console.warn("showOpenFilePicker failed:", err);
    }
  } else {
    el.fileInput.click();
  }
});
el.fileInput.addEventListener("change", () => {
  addStagedFiles(el.fileInput.files);
  el.fileInput.value = "";
});
// Clicking anywhere in the drop-zone (including its padding) focuses
// the textarea — without this the user has to hit the text node itself.
el.dropZone.addEventListener("click", (e) => {
  if (e.target === el.dropZone) {
    el.promptInput.focus();
  }
});

el.promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.composer.requestSubmit();
  }
});

el.promptInput.addEventListener("input", () => {
  _updateSendButton();
  // Auto-grow the textarea so every line is visible (max-height: 160px
  // in CSS caps it, at which point the textarea scrolls internally).
  el.promptInput.style.height = "auto";
  el.promptInput.style.height = el.promptInput.scrollHeight + "px";
});

for (const eventName of ["dragenter", "dragover"]) {
  el.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    el.dropZone.classList.add("drag-over");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  el.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    el.dropZone.classList.remove("drag-over");
  });
}
el.dropZone.addEventListener("drop", (event) => {
  if (event.dataTransfer && event.dataTransfer.files.length) {
    addStagedFiles(event.dataTransfer.files);
  }
});

// -- speech-to-text (Web Speech API, hold to speak) ----------------------

el.micBtn = document.getElementById("mic-btn");
let _recognition = null;
let _isRecording = false;
var _micRetries = 0;
var _MIC_MAX_RETRIES = 5;

function _initSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    el.micBtn.style.display = "none";
    return;
  }
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = true;
  _recognition.lang = "en-US";

  _recognition.addEventListener("result", (event) => {
    _micRetries = 0; // successful recognition — reset the retry counter
    let transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      transcript += event.results[i][0].transcript;
    }
    el.promptInput.value = transcript;
    el.promptInput.dispatchEvent(new Event("input"));
  });

  _recognition.addEventListener("error", (event) => {
    // "network" means the browser can't reach Google's speech servers
    // at all — this is persistent (offline, Brave shields, firewall),
    // not transient.  Stop immediately so the user sees the feedback.
    if (event.error === "network") {
      console.warn("Speech recognition unavailable: cannot reach speech servers. "
        + "Check your internet connection and whether Brave shields are blocking it.");
      el.micBtn.classList.add("recording"); // brief red flash
      el.micBtn.title = "Dictation unavailable — speech servers unreachable";
      _stopRecording();
      setTimeout(function() { el.micBtn.title = "Dictate"; }, 3000);
      return;
    }
    // Hard failures — no recovery possible.
    if (event.error === "not-allowed" || event.error === "audio-capture"
        || event.error === "service-not-allowed") {
      console.warn("Speech recognition error:", event.error);
      _stopRecording();
      return;
    }
    // "no-speech" and "aborted" are transient — the "end" handler
    // restarts, capped by _micRetries to avoid an infinite loop.
  });

  _recognition.addEventListener("end", () => {
    if (_isRecording) {
      _micRetries++;
      if (_micRetries > _MIC_MAX_RETRIES) {
        console.warn("Speech recognition: giving up after " + _MIC_MAX_RETRIES + " retries");
        _stopRecording();
        return;
      }
      try {
        _recognition.start();
      } catch {
        // ignore — may already be starting
      }
    } else {
      el.micBtn.classList.remove("recording");
    }
  });
}

function _startRecording() {
  if (!_recognition) return;
  _isRecording = true;
  _micRetries = 0;
  el.micBtn.classList.add("recording");
  el.micBtn.title = "Stop dictating";
  console.log("mic: starting");
  try {
    _recognition.start();
  } catch {
    // already started
  }
}

function _stopRecording() {
  _isRecording = false;
  _micRetries = 0;
  el.micBtn.classList.remove("recording");
  el.micBtn.title = "Dictate";
  console.log("mic: stopping");
  try {
    _recognition.stop();
  } catch {
    // already stopped
  }
}

// Toggle: first click starts, second click stops and sends.
el.micBtn.addEventListener("click", () => {
  if (_isRecording) {
    _stopRecording();
    el.composer.requestSubmit();
  } else {
    _startRecording();
  }
});

// If the user presses Enter or clicks Send while the mic is still
// recording, stop it first so the sent message captures the full
// transcript.
el.composer.addEventListener("submit", (event) => {
  if (_isRecording) {
    _stopRecording();
  }
}, { capture: true });

_initSpeechRecognition();

// -- global drag-and-drop -----------------------------------------------

const _globalDropOverlay = document.getElementById("global-drop-overlay");
let _dragCounter = 0;

window.addEventListener("dragenter", (event) => {
  event.preventDefault();
  _dragCounter++;
  if (_dragCounter === 1) {
    _globalDropOverlay.classList.add("active");
    _globalDropOverlay.setAttribute("aria-hidden", "false");
  }
});

window.addEventListener("dragleave", (event) => {
  event.preventDefault();
  _dragCounter--;
  if (_dragCounter === 0) {
    _globalDropOverlay.classList.remove("active");
    _globalDropOverlay.setAttribute("aria-hidden", "true");
  }
});

window.addEventListener("dragover", (event) => {
  event.preventDefault();
});

// Safety net: dragend fires when the drag ends for any reason (drop,
// cancelled, or released outside the window).  The dragenter/dragleave
// counter pattern can miss a leave event across browsers, leaving the
// overlay stuck at pointer-events:auto and blocking every click on the
// page — sidebar, composer, everything.
window.addEventListener("dragend", () => {
  _dragCounter = 0;
  _globalDropOverlay.classList.remove("active");
  _globalDropOverlay.setAttribute("aria-hidden", "true");
});

window.addEventListener("drop", (event) => {
  event.preventDefault();
  _dragCounter = 0;
  _globalDropOverlay.classList.remove("active");
  _globalDropOverlay.setAttribute("aria-hidden", "true");
  if (event.dataTransfer && event.dataTransfer.files.length) {
    addStagedFiles(event.dataTransfer.files);
  }
});

// -- responsive breakpoints (JS-driven) ---------------------------------
// CSS @media queries only evaluate at the *final* viewport width after a
// resize animation settles.  During a smooth resize (e.g. macOS
// double-click-title-bar snap) the sidebars would overlap the chat
// content for the entire animation and only collapse at the very end.
//
// This function runs on every animation frame of a resize (via rAF),
// updating --sidebar-w/--sources-w custom properties the moment the
// threshold is crossed — so the collapse is instant regardless of
// animation speed.
//
// For the sidebar we add .collapsed directly (rather than a parallel
// resp-* class) so EVERY existing .sidebar.collapsed CSS rule fires —
// brand clipping, label hiding, toggle suppression — with the same
// specificity the manual toggle uses.  A tracking flag ensures we only
// auto-un-collapse a sidebar that *we* collapsed, never one the user
// collapsed manually.
var _responsiveSidebarCollapsed = false;

function _applyResponsiveState() {
  var w = window.innerWidth;
  var html = document.documentElement;

  if (w <= 900) {
    html.style.setProperty("--sources-w", "0");
    html.classList.add("resp-sources-hidden");

    html.style.setProperty("--sidebar-w", "64px");
    html.classList.add("resp-sidebar-collapsed");
    if (!el.sidebar.classList.contains("collapsed")) {
      el.sidebar.classList.add("collapsed");
      _responsiveSidebarCollapsed = true;
    }
    // Remove .settled so the hover-to-reveal-toggle behaviour (which is
    // gated on .settled) never fires during responsive auto-collapse —
    // expanding is pointless at this width.
    el.sidebar.classList.remove("settled");
  } else {
    html.style.removeProperty("--sources-w");
    html.classList.remove("resp-sources-hidden");

    html.style.removeProperty("--sidebar-w");
    html.classList.remove("resp-sidebar-collapsed");
    if (_responsiveSidebarCollapsed) {
      el.sidebar.classList.remove("collapsed");
      _responsiveSidebarCollapsed = false;
    }
    // Restore .settled if the sidebar was already collapsed from a
    // manual toggle before the responsive collapse kicked in.
    if (el.sidebar.classList.contains("collapsed")
        && !el.sidebar.classList.contains("settled")) {
      el.sidebar.classList.add("settled");
    }
  }
}

var _resizeTimer = null;
var _resizeRAF = null;
window.addEventListener("resize", function () {
  // Kill transitions while the user is dragging the window edge so the
  // responsive collapse snaps instantly to the current viewport width.
  document.documentElement.classList.add("resizing");
  if (_resizeTimer !== null) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(function () {
    _resizeTimer = null;
    document.documentElement.classList.remove("resizing");
  }, 150);

  if (_resizeRAF === null) {
    _resizeRAF = requestAnimationFrame(function () {
      _resizeRAF = null;
      _applyResponsiveState();
    });
  }
});

(async function init() {
  const app = document.querySelector(".app");
  const html = document.documentElement;

  // Transfer collapse state from the pre-paint <html> classes (which
  // prevented layout flash) to the runtime data-* attributes on .app,
  // then drop the <html> classes so the data-* attributes are the
  // single source of truth for the CSS grid and toggle functions.
  if (localStorage.getItem("sidebar-collapsed") === "1") {
    el.sidebar.classList.add("collapsed");
    app.setAttribute("data-sidebar", "collapsed");
    // Loaded already-collapsed — there's no transition to wait out.
    el.sidebar.classList.add("settled");
  }
  html.classList.remove("sb-coll");

  if (localStorage.getItem("sources-collapsed") === "1") {
    el.sourcesPanel.classList.add("collapsed");
    app.setAttribute("data-sources-panel", "collapsed");
    // Loaded already-collapsed — there's no transition to wait out.
    el.sourcesPanel.classList.add("settled");
  }
  html.classList.remove("sp-coll");

  // Enable JS-driven responsive breakpoints (see app.css responsive
  // auto-collapse section).  The .js-resp class disables the CSS
  // @media-query fallback so the resize handler below is the single
  // source of truth for sidebar collapse/hide during smooth window
  // resize animations.
  html.classList.add("js-resp");

  // Apply responsive state immediately (before first paint of the
  // conversation list) so a narrow window doesn't flash expanded
  // sidebars even for a single frame.
  _applyResponsiveState();

  await renderConversationList();

  const conversationId = _conversationIdFromPath();
  if (conversationId) {
    // URL names a conversation — load it directly.
    await selectConversation(conversationId);
  } else {
    // No URL fragment or just #/chat — start in draft mode.
    _enterDraftMode();
  }

  _updateSendButton();
})();
