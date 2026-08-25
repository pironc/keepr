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
  chatPanel: document.querySelector(".chat-panel"),
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
  settingsBtn: document.getElementById("settings-btn"),
  zoomToast: document.getElementById("zoom-toast"),
};

// -- zoom (Cmd/Ctrl + Plus/Minus/0) --------------------------------------
// VS-Code-style whole-layout zoom via the root font-size (see app.css's
// `html { font-size: ... }` rule and its --zoom custom property), not the
// CSS `zoom` property (WebKit's getBoundingClientRect() etc. return
// unscaled values under `zoom` on most currently-deployed Safari/WKWebView
// versions — only fixed in Safari 26.4, ~March 2026) and not
// `transform: scale()` (any non-`none` transform on an ancestor becomes the
// containing block for `position: fixed` descendants, which would break
// every one of app.css's fixed-positioned sidebar/panel/modal rules).
// --zoom is the single multiplier every zoom-aware size in app.css, and
// the root font-size itself, are wrapped in — this is what makes Cmd+/
// Cmd- scale the whole app (rem-based text/spacing scale via the root
// font-size cascade; the handful of raw-px chrome sizes are each
// explicitly wrapped in calc(... * var(--zoom)) in app.css).
// NOTE: this array is duplicated in index.html's pre-paint inline <script>
// (no build step/module system to share it across the two files) — keep
// both in sync if either is edited.
var ZOOM_LEVELS = [0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0];
var _ZOOM_DEFAULT_INDEX = ZOOM_LEVELS.indexOf(1.0);

function _loadZoomIndex() {
  var raw = parseInt(localStorage.getItem("zoom-index"), 10);
  if (Number.isInteger(raw) && raw >= 0 && raw < ZOOM_LEVELS.length) return raw;
  return _ZOOM_DEFAULT_INDEX;
}

// The "real" runtime zoom state. Its pre-paint counterpart is the --zoom
// custom property index.html's inline <script> already stamped onto
// <html> before first paint — unlike sidebar/sources collapse, zoom has
// nothing further to "promote" at init() time: both read the exact same
// localStorage key, and the CSS custom property IS the runtime state, not
// just a flash guard for it.
var _zoomIndex = _loadZoomIndex();

function _showZoomToast(pct) {
  var toast = el.zoomToast;
  if (!toast) return;
  toast.textContent = pct + "%";
  toast.hidden = false;
  void toast.offsetHeight; // force reflow so re-triggering restarts the transition
  toast.classList.add("show");
  clearTimeout(_showZoomToast._timer);
  _showZoomToast._timer = setTimeout(function () {
    toast.classList.remove("show");
  }, 1200);
}

function _setZoomIndex(newIndex) {
  _zoomIndex = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, newIndex));
  var level = ZOOM_LEVELS[_zoomIndex];
  document.documentElement.style.setProperty("--zoom", String(level));
  localStorage.setItem("zoom-index", String(_zoomIndex));
  _showZoomToast(Math.round(level * 100));
  // None of these fire on their own for a zoom change (only on `resize`/
  // init, and changing root font-size doesn't trigger `resize`) — re-run
  // them explicitly so the 900px auto-collapse threshold, the toast-stack
  // sizing, and the composer placeholder all catch up immediately instead
  // of waiting for the next real window resize (which may never happen).
  _applyResponsiveState();
  _syncDownloadToastLayout();
  _updatePromptPlaceholder();
  _updateTextareaHeight();
  _syncEmptyPromptGlow();
}

const STATUS_LABELS = {
  staged: "Staged",
  queued: "Queued",
  uploading: "Uploading…",
  extracting: "Extracting…",
  chunking: "Chunking…",
  embedding: "Embedding…",
  indexed: "Indexed ✓",
  error: "Error",
  unsupported: "Not supported yet",
};

const MESSAGE_STATUS_LABELS = {
  queued: "Queued",
  "processing-documents": "Processing documents…",
  retrieving: "Retrieving…",
};

// Shown in place of the bubble content when a message ends in "error" with
// nothing else saved — an unhandled crash before any real answer text (or
// partial streamed tokens) existed to preserve. Without this, the bubble
// renders as genuinely empty with only the red "Error" status label above
// it, reading as the UI itself being broken rather than the generation
// having failed. A message that *does* have partial content (interrupted
// mid-stream, or a handled error like a missing model with its own
// explanatory text) keeps that real content instead — this only fills the
// gap when there's truly nothing else to show.
const GENERATION_ERROR_FALLBACK = "Something went wrong while generating this response. Please try asking again.";

function _displayContentFor(status, content) {
  return status === "error" && !content ? GENERATION_ERROR_FALLBACK : content;
}

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
let _promptCycleInFlight = false; // true while a _cyclePrompt() fade is still running

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
  // Re-entrancy guard: if a previous cycle's fade is still in flight (it
  // shouldn't be — 5s between ticks is far longer than one cycle's ~600ms —
  // but this is cheap insurance against whatever could call this again
  // early, e.g. a throttled/backgrounded tab replaying queued timers in a
  // burst), just skip this tick rather than starting a second fade on the
  // same elements a first one hasn't finished with yet.
  if (_promptCycleInFlight) return;
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

  // Force `next` fully hidden *before* touching anything else, with its own
  // transition briefly suppressed so this snaps instead of animating.
  // Doesn't assume `next` is already at opacity 0 from a prior cycle's
  // fade-out — an interrupted prior cycle (dropped frame, tab losing
  // focus) could leave it at any opacity, and both prompts fading
  // independently without this could both be visible at once.
  next.style.transition = "none";
  next.style.opacity = "0";
  void next.offsetHeight; // flush so transition:none above actually applies
  next.style.transition = "";

  // Fade out the visible text, then swap the text underneath and fade the
  // next one in — but only once the fade-out has *actually* finished
  // (transitionend), not after a guessed delay. A fallback timeout covers
  // the case where transitionend never fires at all (e.g. current was
  // already at 0 for some reason, so setting it to "0" again is a no-op
  // that triggers no transition).
  let swapped = false;
  _promptCycleInFlight = true;
  function swap() {
    if (swapped) return;
    swapped = true;
    current.removeEventListener("transitionend", swap);
    clearTimeout(fallback);
    next.textContent = newText;
    next.style.opacity = "1";
    _promptIsCurrent = !_promptIsCurrent;
    _promptCycleInFlight = false;
  }
  current.addEventListener("transitionend", swap);
  const fallback = setTimeout(swap, 600);
  current.style.opacity = "0";
}

function _removeEmptyChatPrompt() {
  _stopPromptCycle();
  const existing = document.getElementById("empty-chat-prompt");
  if (existing) existing.remove();
}

// Keeps the ambient glow (app.css: .chat-panel:has(.empty-chat-prompt)::before)
// centred on the *text*, not on .chat-panel's own box. The glow is hosted on
// .chat-panel rather than #messages specifically to escape #messages' own
// clipping (overflow-y: auto forces overflow-x to compute to auto too, per
// the CSS Overflow spec, which was cutting off the glow's blur bleed) — but
// that means its resting position, centred top:0/bottom:0 across the whole
// of .chat-panel, sits too low by roughly half of whatever height #composer
// is currently taking, which .empty-chat-prompt's own centring (inside
// #messages alone) never accounts for.
//
// Measured live rather than hardcoded: the composer is always at its
// baseline (single-line, un-grown) height whenever the empty prompt can
// even be showing, since that only happens with zero messages — but that
// baseline height still varies with zoom and window width (the icon/padding
// tokens it's built from are vw- and zoom-scaled), so it has to be
// re-measured on the same triggers as every other layout-refresh function,
// not computed once.
function _syncEmptyPromptGlow() {
  var composer = document.querySelector(".composer");
  if (!composer) return;
  document.documentElement.style.setProperty("--composer-h", composer.offsetHeight + "px");
}

function toggleSourcesPanel() {
  const app = document.querySelector(".app");
  const isCollapsed = el.sourcesPanel.classList.toggle("collapsed");
  app.setAttribute("data-sources-panel", isCollapsed ? "collapsed" : "expanded");
  localStorage.setItem("sources-collapsed", isCollapsed ? "1" : "0");
  _syncDownloadToastLayout();

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

// Make the download toasts live in the bottom-right of the Sources (right)
// rail, inside it, inset evenly from its bottom/left/right edges. Decides
// whether the rail is too narrow to host the full pill; if so, toggles
// .compact on the stack (the CSS drops every toast to an icon-only shimmer
// pill — in every download state), and drops each pill's inline width lock
// so the fixed 40px .compact pill wins (an inline min-width would keep the
// stacked pills stuck wide).
//
// The stack's width/position come from --sources-w-max (the rail's expanded
// clamp width), not --sources-w, so a rail that's merely narrower than
// usual doesn't squeeze the stack below what a non-compact pill needs.
//
// This checks the SAME two class conditions app.css keys the rail's own
// narrowing off (see .sources-panel), rather than measuring the rail's live
// offsetWidth against a px threshold: the rail's width transitions over
// 200ms (app.css: .sources-panel), so a synchronous offsetWidth read taken
// the instant a zoom change fires this function catches the OLD,
// pre-transition width — one zoom step permanently behind the rail's actual
// narrow/wide state. Class membership itself isn't animated, so checking it
// directly is immediate and exact, no transition timing to race.
function _syncDownloadToastLayout() {
  var stack = document.getElementById("toast-stack");
  var shouldCompact = document.documentElement.classList.contains("resp-sources-compact") ||
    el.sourcesPanel.classList.contains("collapsed");
  var wasCompact = stack.classList.contains("compact") ||
    stack.classList.contains("collapsed");
  stack.classList.toggle("compact", shouldCompact);
  stack.classList.remove("collapsed");
  if (!wasCompact && shouldCompact) {
    var pills = stack.querySelectorAll(".download-toast");
    for (var i = 0; i < pills.length; i++) pills[i].style.minWidth = "";
  }
}

// Keep the composer placeholder from shrinking the input when the window
// narrows.  The placeholder is forced to a single line (CSS clips it), so a
// long string would silently truncate; instead swap in the longest of a few
// variants that actually fits the textarea's current width.  Widths are
// measured with canvas.measureText (exact, layout-independent) against the
// textarea's content width, and the placeholder is only written when the
// chosen string changes, so a resize frame only touches the DOM when it
// must.
var _promptPlaceholderVariants = [
  "Ask a question, or drop a file to add it to this conversation",
  "Ask a question, or drop a file",
  "Ask a question",
];
// Same placeholder-as-explanation idea as above, but for when a required
// model is missing (app.css colours it red via .drop-zone.model-blocked),
// so there's no separate banner element. Unlike _promptPlaceholderVariants,
// this message is never shortened to fit — it's important enough to stay
// verbatim, so instead the box itself is resized to whatever the current
// app size (window width, zoom level) requires (see _updatePromptPlaceholder
// and the flex-shrink:0 rules in app.css that let that resize actually win
// over the row's own layout instead of being compressed back down).
var _modelGatePlaceholderVariants = {
  both: "No language model or embedding model installed",
  llm: "No language model installed",
  embedder: "No embedding model installed",
};
var _promptPlaceholderCanvas = null;
var _promptPlaceholderFontKey = "";

function _promptPlaceholderWidth(text) {
  if (!_promptPlaceholderCanvas) {
    _promptPlaceholderCanvas = document.createElement("canvas");
  }
  var ctx = _promptPlaceholderCanvas.getContext("2d");
  var font = getComputedStyle(el.promptInput).font;
  if (font !== _promptPlaceholderFontKey) {
    ctx.font = font;
    _promptPlaceholderFontKey = font;
  }
  return ctx.measureText(text).width;
}

function _updatePromptPlaceholder() {
  var input = el.promptInput;
  if (!input) return;
  var dz = el.dropZone;
  var blocked = _llmMissing || _embedderMissing;
  var wasBlocked = dz.classList.contains("model-blocked");
  // .drop-zone.model-blocked changes both the textarea's flex-grow AND its
  // own (wider) padding (see app.css) — the two measurements below each
  // need the state they're actually relevant to, not whatever the class
  // happens to be right now. rowWidth needs flex-grow OFF (the class
  // removed) so clientWidth reflects the row's real available space
  // instead of an arbitrary content-hugging default; padX needs the class
  // set to whatever `blocked` actually is *this* call, since the padding
  // that will really eat into the rendered box differs between the two
  // states — measuring against the wrong one under/over-counts it and
  // either clips the text or leaves the box needlessly small. All
  // measured and restored synchronously, so nothing paints in between and
  // there's no visible flicker.
  dz.classList.remove("model-blocked");
  input.style.width = "";
  var rowWidth = input.clientWidth;
  dz.classList.toggle("model-blocked", blocked);
  var padX =
    (parseInt(getComputedStyle(input).paddingLeft, 10) || 0) +
    (parseInt(getComputedStyle(input).paddingRight, 10) || 0);
  dz.classList.toggle("model-blocked", wasBlocked);
  var avail = rowWidth - padX;
  // canvas.measureText() slightly under-reports the real DOM-rendered width
  // (subpixel/hinting differences) — pad by the same proportional margin
  // the box-sizing step (below) applies, or a string could measure as
  // "fits" here and then, once padding is added back for the actual box
  // width, no longer fit after all — clipping the last character or two.
  function withMargin(w) {
    return w * 1.04 + 2;
  }
  var text;
  if (blocked) {
    // Never shortened — see _modelGatePlaceholderVariants. The box is
    // resized to this text below instead, regardless of `avail`.
    text =
      _llmMissing && _embedderMissing
        ? _modelGatePlaceholderVariants.both
        : _llmMissing
        ? _modelGatePlaceholderVariants.llm
        : _modelGatePlaceholderVariants.embedder;
  } else {
    if (avail <= 0) return;
    text = _promptPlaceholderVariants[_promptPlaceholderVariants.length - 1];
    for (var i = 0; i < _promptPlaceholderVariants.length; i++) {
      var w = _promptPlaceholderWidth(_promptPlaceholderVariants[i]);
      if (withMargin(w) <= avail) {
        text = _promptPlaceholderVariants[i];
        break;
      }
    }
  }
  if (input.placeholder !== text) {
    input.placeholder = text;
  }
  // While a model is missing, the box hugs the warning text instead of
  // filling the whole composer width — a short message in an otherwise
  // wide, empty-looking pill read as oversized next to the rest of the
  // UI. .drop-zone.model-blocked and its textarea (app.css) are both
  // flex: 0 0 auto so this explicit width always wins: no flex-grow to
  // pull it back out to fill the row, and no flex-shrink to compress it
  // back down below what the text needs, however app size or zoom change
  // the text's rendered width.
  //
  // `width` here is a border-box width (this file resets `* { box-sizing:
  // border-box }`), so it has to include padX or the content area ends up
  // padX narrower than the text — clipping the last character or two
  // rather than "hugging" it.
  //
  // Capped since the text is never shortened to fit anymore — at a high
  // enough zoom on a narrow enough window, "No language model or embedding
  // model installed" can need more room than actually exists. Without a
  // cap the box would grow unchecked (flex-shrink is 0), pushing the
  // settings gear off one edge of the window and the pill's own leading
  // text off the other. Capping lets the *box* stay on-screen and hands
  // the now-unavoidable overflow to the placeholder's existing CSS
  // ellipsis/clip (same graceful, contained degradation the ordinary
  // un-blocked placeholder already falls back to at this extreme).
  //
  // The cap is rowWidth (this row's own share of .composer) *plus*
  // whatever slack .composer itself has left over inside .chat-panel — not
  // rowWidth alone. .composer is deliberately narrower than the chat
  // column (64%, see app.css), and a plain rowWidth cap would claw back
  // that entire deliberate margin, clipping the pill in ordinary cases (a
  // normal window, no zoom at all). .chat-panel is the real outer
  // boundary — the fixed sidebar/sources rails live outside it — so
  // growing into that margin first is safe.
  if (blocked) {
    var textWidth = _promptPlaceholderWidth(text);
    var desired = Math.ceil(withMargin(textWidth)) + padX;
    var slack =
      el.chatPanel && el.composer
        ? Math.max(0, el.chatPanel.clientWidth - el.composer.clientWidth)
        : 0;
    var maxSafeWidth = rowWidth + slack;
    input.style.width = (maxSafeWidth > 0 ? Math.min(desired, maxSafeWidth) : desired) + "px";
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

var _selectConversationSeq = 0;

async function selectConversation(id) {
  _abortAllSse();

  var seq = ++_selectConversationSeq;
  // Re-clicking the conversation you're ALREADY viewing must not discard
  // wherever you'd actually scrolled to — loadMessages() below always ends
  // with scrollMessagesToBottom(), with no exception for "this is the same
  // conversation, already open", so capture the current scroll position
  // here, before it's overwritten, whenever `id` is the conversation
  // already open. The "different conversation" case is intentionally
  // untouched — landing at that conversation's own bottom on first open is
  // the desired default.
  var preserveScrollTop = id === state.conversationId ? el.messages.scrollTop : null;
  state.conversationId = id;
  _setUrl(id);
  await renderConversationList();
  // Rapid sidebar clicking fires several of these calls concurrently;
  // loadMessages/loadDocuments' own state.conversationId guard stops the
  // WRONG conversation's content from being written, but skipping the
  // fetch entirely here for an already-superseded click also avoids
  // firing (and then discarding) a request, and an SSE reconnect, for a
  // conversation the user has already clicked past.
  if (seq !== _selectConversationSeq) return;
  await Promise.all([loadMessages(id, preserveScrollTop), loadDocuments(id)]);
  if (seq !== _selectConversationSeq) return;
  // Ensure the composer is usable even if a background sendMessage in
  // another conversation still has it locked — navigating to a different
  // conversation must not leave the composer dead.
  setComposerEnabled(true);
  _updateSendButton();
}

async function loadMessages(id, preserveScrollTop) {
  const response = await api(`/conversations/${id}/messages`);
  const messages = await response.json();
  // Rapid sidebar clicking fires several of these concurrently, and nothing
  // about fetch/await ordering guarantees they resolve in the order they
  // were called — a slower response for a conversation the user has since
  // navigated away from can land AFTER a faster response for the one now on
  // screen, clobbering it with stale content. Bail if we're no longer
  // looking at `id` by the time this resolves, matching the same guard
  // reconnectToMessage's poll loop already uses below.
  if (state.conversationId !== id) return;
  el.messages.innerHTML = "";
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
  // undefined/null (a genuinely different conversation) still defaults to
  // the bottom; 0 is a legitimate scrolled-to-top position and must not be
  // treated as falsy here.
  if (preserveScrollTop !== null && preserveScrollTop !== undefined) {
    el.messages.scrollTop = preserveScrollTop;
  } else {
    scrollMessagesToBottom();
  }
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
  // Same stale-response guard as loadMessages above, and for the same
  // reason: this conversation's Sources panel must not get overwritten by
  // a slow-to-resolve fetch for a conversation the user already left.
  if (state.conversationId !== id) return;
  el.sourcesList.innerHTML = "";
  for (const doc of docs) {
    updateDocumentStatus({ document_id: doc.id, status: doc.status, error_message: doc.error_message }, doc.filename);
  }
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
    // Assistant bubble — hide it when there's nothing to show yet (queued /
    // retrieving / generating) so the status text doesn't sit on top of a
    // visible empty beige box. An error with no partial content gets the
    // GENERATION_ERROR_FALLBACK text substituted in (see _displayContentFor),
    // so it's never actually empty and this stays revealed for that case.
    const bubble = document.createElement("div");
    bubble.className = "message assistant";
    const displayContent = _displayContentFor(status, content);
    renderMessageContent(contentEl, displayContent, groups);
    bubble.appendChild(contentEl);
    if (!displayContent && status !== "done") {
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
// re-parsing markdown on every chunk. Streamed text should never actually
// contain a "[N]"/"[chunk_N]" tag for this to strip in the first place —
// the backend (src/rag/engine.py's _stream_strip_citation_tags) already
// removes those from every token before it's ever sent, precisely so
// nothing here has to reconcile a raw marker appearing live against the
// differently-numbered "[N]" tag "done" resolves (and drops, per
// appendTextWithCitations) in its place.

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

// Splits on "[N]" markers and drops each verified one entirely — per-claim
// citation UI is intentionally not shown, only the sources panel is. An
// unmatched "[N]" (numberToGroup has no group for it, so it isn't actually
// a citation — e.g. literal bracketed text) still renders as plain text,
// since there's nothing to hide. Shared by every block/inline context
// (paragraphs, list items, bold/italic spans).
function appendTextWithCitations(parent, text, numberToGroup) {
  let lastIndex = 0;
  let match;
  _CITATION_MARKER_PATTERN.lastIndex = 0;
  while ((match = _CITATION_MARKER_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    if (!numberToGroup.get(match[1])) {
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
}

// -- citation modal & file preview --------------------------------------

// documentMeta: { documentId: { filename } } — populated by both
// groupCitationsByDocument (for citation-bearing messages) and
// updateDocumentStatus (for source-panel entries).
const _documentMeta = {};

// Blob cache — fetch once per document, reuse across modal opens, revoked
// on page unload. Using a Blob URL (rather than pointing the iframe/preview
// at the API endpoint directly) lets the browser's built-in PDF viewer work
// without a second network round-trip, and lets text/markdown previews read
// the same already-fetched Blob's bytes instead of fetching twice.
// contentType comes straight from the fetch Response, mirroring whatever
// _media_type_for() (routes_conversations.py) decided server-side, so this
// never needs its own extension list.
const _documentBlobs = {};

async function _fetchDocumentBlobUrl(documentId) {
  if (_documentBlobs[documentId]) return _documentBlobs[documentId];
  const response = await fetch(
    `/conversations/${state.conversationId}/documents/${documentId}/file`
  );
  if (!response.ok) return null;
  const blob = await response.blob();
  const entry = { url: URL.createObjectURL(blob), contentType: blob.type, blob };
  _documentBlobs[documentId] = entry;
  return entry;
}

// PDFs get the browser's built-in viewer via an iframe; any other text-ish
// type (text/markdown, text/plain — which _media_type_for now also returns
// for every other ingested extension: .py, .json, .csv, ...) is read as text
// and rendered directly, since embedding a non-PDF type in an iframe just
// hits whatever (inconsistent, often blank) native placeholder the webview
// has for it. Anything left over (no ingestor understands it yet, e.g.
// audio/video) gets an explicit "can't preview this" state instead of
// silently falling into that same broken iframe path.
function openCitationModal(documentId, filename) {
  const modal = document.getElementById("citation-modal");
  const modalBody = document.getElementById("modal-body");

  modal.classList.add("active");
  modal.setAttribute("aria-hidden", "false");
  modalBody.innerHTML =
    '<div style="padding:24px;text-align:center;color:var(--color-ink-mute)">Loading…</div>';
  document.body.style.overflow = "hidden";

  _fetchDocumentBlobUrl(documentId).then(async (entry) => {
    if (!entry) {
      modalBody.innerHTML =
        '<p class="message-status" style="padding:24px">Could not load this file.</p>';
      return;
    }
    if (entry.contentType === "application/pdf") {
      renderPdfPreview(modalBody, entry.url, filename);
    } else if (entry.contentType.startsWith("text/")) {
      const text = await entry.blob.text();
      renderTextPreview(modalBody, text, entry.contentType);
    } else {
      renderUnsupportedPreview(modalBody, entry.url, filename);
    }
  });
}

function renderPdfPreview(modalBody, url, filename) {
  const iframe = document.createElement("iframe");
  iframe.src = url;
  iframe.title = filename;
  iframe.style.cssText = "position:absolute;inset:0;width:100%;height:100%;border:none;";
  // Escape/Enter typed while focus is inside the PDF viewer never reach
  // document's own keydown listener above — frame boundaries stop DOM
  // event bubbling regardless of same-origin-ness. A listener on the
  // iframe's own contentWindow, once loaded, covers both keys via the
  // same closeCitationModal() the outer listener uses. Reliable in
  // WebKit (this app's actual Tauri target — the PDF viewer stays in the
  // same same-origin blob: document there). Chromium-family engines render
  // their built-in PDF viewer as an internal extension the iframe
  // effectively navigates to, making it genuinely cross-origin even
  // from a same-origin blob src — a hard security boundary, not fixable
  // from page JS — so this is wrapped in try/catch and silently falls
  // back to the existing document-level handling plus the always
  // present Close button in that case.
  iframe.addEventListener("load", () => {
    try {
      iframe.contentWindow.addEventListener("keydown", (evt) => {
        if (evt.key === "Escape" || (evt.key === "Enter" && !evt.shiftKey)) {
          evt.preventDefault();
          closeCitationModal();
        }
      });
    } catch (err) {
      /* cross-origin PDF viewer — can't attach; see comment above */
    }
  });
  modalBody.innerHTML = "";
  modalBody.appendChild(iframe);
}

// Plain DOM elements (not an iframe), so Escape/Enter reach the existing
// document-level keydown listener with no extra wiring — unlike the PDF
// case above, there's no frame boundary here to stop bubbling.
function renderTextPreview(modalBody, text, contentType) {
  const scroller = document.createElement("div");
  scroller.className = "modal-scroll";
  if (contentType === "text/markdown") {
    // Reusing bubble-content (see the markdown-rendering section above) —
    // same DOM-only renderer chat messages use, so a previewed .md file
    // gets real headings/lists/code, not raw "# " characters. Citation
    // markers have no meaning outside a chat message, so an empty map: any
    // literal "[3]" in the file's own text just renders as plain text
    // (appendTextWithCitations' no-match fallback).
    const content = document.createElement("div");
    content.className = "bubble-content file-preview-markdown";
    renderMessageContent(content, text, new Map());
    scroller.appendChild(content);
  } else {
    const pre = document.createElement("pre");
    pre.className = "file-preview-text";
    pre.textContent = text;
    scroller.appendChild(pre);
  }
  modalBody.innerHTML = "";
  modalBody.appendChild(scroller);
}

function renderUnsupportedPreview(modalBody, url, filename) {
  modalBody.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "file-preview-unsupported";
  const msg = document.createElement("p");
  msg.className = "message-status";
  msg.textContent = `Preview isn't available for ${filename}.`;
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Open in a new tab";
  wrap.appendChild(msg);
  wrap.appendChild(link);
  modalBody.appendChild(wrap);
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
    const entry = await _fetchDocumentBlobUrl(documentId);
    if (entry) window.open(entry.url, "_blank");
    return;
  }

  highlightSource(documentId);
  openCitationModal(documentId, meta.filename);
}

// Revoke blob URLs on unload to prevent memory leaks.
window.addEventListener("beforeunload", () => {
  for (const entry of Object.values(_documentBlobs)) {
    URL.revokeObjectURL(entry.url);
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
    entry.innerHTML =
      '<span class="source-name"></span>' +
      '<span class="source-initial" aria-hidden="true"></span>' +
      '<span class="source-status"></span>';
    // Make source entries clickable — opens the citation modal. Reads
    // entry.dataset.documentId at click time rather than closing over
    // data.document_id: sendMessage seeds this entry with an optimistic
    // "pending-N" id before the real one exists, then mutates
    // entry.dataset.documentId in place once the server assigns one (see
    // the document_status handler there) — a captured `data.document_id`
    // would keep referring to the long-gone "pending-N" id forever, 404ing
    // every click for the entry's whole lifetime.
    entry.style.cursor = "pointer";
    entry.addEventListener("click", (e) => {
      handleCitationClick(entry.dataset.documentId, e);
    });
    el.sourcesList.appendChild(entry);
  }
  if (filename) {
    entry.querySelector(".source-name").textContent = filename;
    // .source-initial is what the compact rail (app.css:
    // html.resp-sources-compact) shows instead of the full name — every
    // call site that creates a new entry already has the filename in hand
    // (see loadDocuments/sendMessage), so this is never left stale for a
    // freshly created entry the way a later status-only update would be.
    entry.querySelector(".source-initial").textContent = filename.charAt(0).toUpperCase();
    // A native tooltip so the filename is still one hover away once the
    // compact rail hides it — set here rather than unconditionally below
    // so the error-message title (below) keeps taking priority when both
    // apply.
    entry.title = filename;
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

function _confirmDelete(title, verb) {
  verb = verb || "Delete";
  return new Promise((resolve) => {
    const backdrop = document.getElementById("delete-confirm");
    const message = document.getElementById("confirm-message");
    const cancelBtn = document.getElementById("confirm-cancel");
    const deleteBtn = document.getElementById("confirm-delete");

    message.innerHTML =
        '<p class="confirm-copy">' + _esc(verb) + ' <strong>"' + _esc(title) + '"</strong>?</p>'
      + '<p class="confirm-subtle">This action cannot be undone.</p>';
    if (deleteBtn) deleteBtn.textContent = verb;
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

// True whenever the composer as a whole must stay grayed out: status not
// yet confirmed (see _modelStatusReady below) or a required model missing.
// The textarea and send button are always kept in lock-step on this — there
// is no "type but can't send" state; _updatePromptPlaceholder names exactly
// what's missing in the placeholder itself instead of a separate banner.
function _modelGateBlocked() {
  return !_modelStatusReady || _llmMissing || _embedderMissing;
}

function setComposerEnabled(enabled) {
  var blockedByModel = _modelGateBlocked();
  var allow = enabled && !blockedByModel;
  el.promptInput.disabled = !allow;
  // Attach is driven by blockedByModel alone, not the combined `allow` — it
  // stays usable during an ordinary in-flight send (queuing the next
  // upload while one generation is still running is fine and unrelated to
  // this gate), and the whole pill gets the same muted look (app.css) so
  // there's no lone active-looking icon inside an otherwise inert box.
  el.attachBtn.disabled = blockedByModel;
  el.dropZone.classList.toggle("model-blocked", blockedByModel);
  if (allow) {
    _updateSendButton();
  } else {
    el.sendBtn.disabled = true;
  }
}

function _updateSendButton() {
  // Never re-enable the button while the textarea itself is disabled —
  // whether that's the in-flight-send lock (setComposerEnabled(false) in
  // sendMessage) or the model gate (setComposerEnabled applies
  // _modelGateBlocked() to both controls together, above). This function
  // only ever handles the separate "is there text/files to send" concern.
  if (el.promptInput.disabled) return;
  var hasText = el.promptInput.value.trim().length > 0;
  var hasFiles = state.stagedFiles.length > 0;
  // A brand-new conversation's first message needs a file attached — text
  // alone isn't enough. Styled as disabled (.gated, matching :disabled's
  // look) but deliberately NOT the native disabled attribute: a disabled
  // button fires no click/submit at all, and sendMessage's own guard is
  // what surfaces the "No attached file" toast on an attempted send.
  var needsFirstAttachment = !state.conversationId && !hasFiles;
  el.sendBtn.classList.toggle("gated", needsFirstAttachment);
  el.sendBtn.disabled = !needsFirstAttachment && !hasText && !hasFiles;
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
    renderMessageContent(
      bubble.querySelector(".bubble-content"),
      _displayContentFor(parsed.data.status, parsed.data.content),
      citationsRef.groups
    );
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

  // The textarea/button are natively disabled whenever _modelGateBlocked()
  // is true, which already stops a click or Enter-to-submit from reaching
  // here in the ordinary case — this is a defense-in-depth repeat of that
  // same check, in case some other path (e.g. a future keyboard shortcut)
  // calls composer.requestSubmit() without going through a disabled check.
  if (_modelGateBlocked()) return;

  // A conversation's first message needs a file attached — otherwise
  // there's nothing to ground an answer in yet. Checked here (not just via
  // the composer's own disabled state) so an Enter-key submit or a click
  // on the intentionally-still-clickable .gated send button both surface
  // the same rejection instead of silently doing nothing.
  if (!state.conversationId && state.stagedFiles.length === 0) {
    _showActionToast("No attached file", true);
    return;
  }

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
  // Marks the window a background model-status refresh (_applyModelGate)
  // must not clobber — see its own comment for why.
  _sendInFlight = true;
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
  // is the "shows as sent but pending" state, since the first token can be
  // many seconds away on a real model.
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
    // "Queued" forever with the composer locked out.
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
    _sendInFlight = false;
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
    input.removeEventListener("keydown", onInputKeyDown);
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

// True whenever a modal/overlay is currently shown — used to gate new
// shortcuts that would otherwise act (or open) invisibly behind/under
// whatever's already on top, or double-fire while something's already open.
function _anyOverlayActive() {
  return (
    document.getElementById("citation-modal").classList.contains("active") ||
    document.getElementById("search-overlay").classList.contains("active") ||
    document.getElementById("rename-overlay").classList.contains("active") ||
    document.getElementById("settings-overlay").classList.contains("active") ||
    document.getElementById("delete-confirm").getAttribute("aria-hidden") === "false" ||
    document.getElementById("restart-confirm").getAttribute("aria-hidden") === "false"
  );
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
    // When the restart-confirm modal is stacked above Settings, a single
    // Escape should dismiss only the topmost popup (the modal) — never
    // also close Settings and drop straight back to chat. The modal owns
    // its own Escape handling, so skip Settings/context-menu here.
    const restartModal = document.getElementById("restart-confirm");
    const restartActive =
      restartModal && restartModal.classList.contains("active");
    if (!restartActive) {
      const settingsOverlay = document.getElementById("settings-overlay");
      if (settingsOverlay && settingsOverlay.classList.contains("active")) {
        closeSettings();
      }
      closeContextMenu();
    }
  }
  // Global Enter-to-submit: when the user hasn't clicked into the textarea
  // but there's content ready to send (staged files or typed text), Enter
  // submits just like it does when the textarea is focused.  Skip when
  // focus is in another input (search popup, rename dialog) or when a
  // modal/overlay is open.
  if (e.key === "Enter" && !e.shiftKey) {
    // Enter closes the citation-preview modal if it's open, same as
    // Escape does above — checked first, so this can't also fall through
    // to the submit logic below on the same keypress.
    var citationModal = document.getElementById("citation-modal");
    if (citationModal.classList.contains("active")) {
      e.preventDefault();
      closeCitationModal();
      return;
    }
    if (el.promptInput.disabled) return;
    var tag = document.activeElement ? document.activeElement.tagName : "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (document.getElementById("search-overlay").classList.contains("active")) return;
    if (document.getElementById("rename-overlay").classList.contains("active")) return;
    if (document.getElementById("settings-overlay").classList.contains("active")) return;
    if (document.getElementById("delete-confirm").getAttribute("aria-hidden") === "false") return;
    var hasText = el.promptInput.value.trim().length > 0;
    var hasFiles = state.stagedFiles.length > 0;
    if (hasText || hasFiles) {
      e.preventDefault();
      el.composer.requestSubmit();
    }
  }
  // Manual zoom (Cmd/Ctrl +/-/0) — see _setZoomIndex above. Deliberately no
  // input-focus guard (unlike the Enter-to-submit block above): a browser's
  // own Cmd+Plus/Minus/0 works regardless of what's focused, and this
  // should match that.
  if ((e.metaKey || e.ctrlKey) && (e.key === "=" || e.key === "+" || e.code === "NumpadAdd")) {
    e.preventDefault();
    _setZoomIndex(_zoomIndex + 1);
  }
  if ((e.metaKey || e.ctrlKey) && (e.key === "-" || e.key === "_" || e.code === "NumpadSubtract")) {
    e.preventDefault();
    _setZoomIndex(_zoomIndex - 1);
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "0") {
    e.preventDefault();
    _setZoomIndex(_ZOOM_DEFAULT_INDEX);
  }

  // Cmd/Ctrl+F — open chat search. Guarded: search-overlay's z-index (100)
  // sits below citation-modal/delete-confirm/restart-confirm (150), so
  // opening it under one of those would render invisibly and steal focus
  // into a hidden input; re-opening while already active would stack a
  // second set of listeners (openSearchPopup has no existing-instance guard).
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
    e.preventDefault();
    if (!_anyOverlayActive()) openSearchPopup();
  }

  // Cmd/Ctrl+N — new chat, same as clicking "New chat". createConversation()
  // already no-ops in draft mode; guarded on _anyOverlayActive() too so a
  // stray keystroke can't reset the current chat underneath an open dialog.
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
    e.preventDefault();
    if (!_anyOverlayActive()) createConversation();
  }

  // Cmd/Ctrl+P — pin/unpin the current conversation. No-op in draft mode.
  // togglePin() needs a full conversation object (.id, .pinned), not just
  // the id string in state.conversationId, and nothing in the app caches
  // conversation objects — fetch fresh via the single-row GET (exists
  // server-side already) rather than adding a new cache that could drift
  // out of sync with pin state changed elsewhere. Not gated on
  // _anyOverlayActive(): this only does a background PATCH + sidebar
  // re-render, no z-index/focus contention with anything.
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "p") {
    e.preventDefault();
    if (state.conversationId) {
      api(`/conversations/${state.conversationId}`)
        .then((response) => response.json())
        .then((conversation) => togglePin(conversation))
        .catch(() => {}); // conversation may have been deleted elsewhere
    }
  }
});

// Mouse-wheel scroll landing in the empty grid gutters (the leftover space
// between a collapsed/narrower sidebar or sources panel and the fixed-width
// centered chat column — see .app's grid-template-columns) or the slack
// within .chat-panel itself (#messages' own max-width is often narrower
// than --chat-w) did nothing: neither .app, .chat-panel, nor body
// (overflow: hidden) are scrollable, and nothing forwarded the input to
// #messages, the only thing actually worth scrolling there. Only acts when
// the wheel event's target is exactly one of these two background
// containers — never a more specific child (sidebar list, sources list,
// composer, modals, #messages itself, ...), so every existing scrollable
// region keeps its own native behavior untouched.
(function () {
  var appEl = document.querySelector(".app");
  var chatPanelEl = document.querySelector(".chat-panel");
  document.addEventListener(
    "wheel",
    (e) => {
      if (e.target === appEl || e.target === chatPanelEl) {
        el.messages.scrollTop += e.deltaY;
      }
    },
    { passive: true }
  );
})();

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
// While a required model is missing, the whole (shrink-to-fit, inert)
// pill instead opens Settings on click — checked first and unconditional
// on e.target, so it fires for a click anywhere in the pill, including on
// the disabled textarea/attach/send children, not just its own padding.
el.dropZone.addEventListener("click", (e) => {
  if (_llmMissing || _embedderMissing) {
    openSettings();
    return;
  }
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

// Auto-grow the textarea so every line is visible (max-height: 160px in
// CSS caps it, at which point the textarea scrolls internally). Pulled out
// of the "input" listener below so a resize/zoom change can also re-run
// it — scrollHeight depends on both the current font-size (root font-size
// is zoom- and window-width-responsive, see app.css) and the textarea's
// own width (a narrower box wraps onto more lines); without re-running
// this on those changes too, existing multi-line text keeps whatever pixel
// height was set the last time it was actually typed into, so the box can
// end up visibly too tall or too short for its own text after a resize.
function _updateTextareaHeight() {
  var input = el.promptInput;
  if (!input || !input.value) return; // empty box is sized by CSS, not this
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
}

el.promptInput.addEventListener("input", () => {
  _updateSendButton();
  _updateTextareaHeight();
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
// updating --sidebar-w (and toggling the resp-* classes) the moment the
// threshold is crossed — so the collapse is instant regardless of
// animation speed. The Sources rail has no equivalent JS-computed width:
// --sources-w is driven entirely by the CSS cascade (see :root and the
// resp-sources-compact/collapsed overrides in app.css), on purpose — see
// _syncDownloadToastLayout for why it must stay that way.
//
// For the sidebar we add .collapsed directly (rather than a parallel
// resp-* class) so EVERY existing .sidebar.collapsed CSS rule fires —
// brand clipping, label hiding, toggle suppression — with the same
// specificity the manual toggle uses.  A tracking flag ensures we only
// auto-un-collapse a sidebar that *we* collapsed, never one the user
// collapsed manually.
var _responsiveSidebarCollapsed = false;

function _applyResponsiveState() {
  // Divide by the current zoom multiplier so the threshold tracks the
  // *effective* footprint of the fixed sidebar/sources rails, not just the
  // raw window: zoom inflates their rendered width (see app.css's --zoom)
  // without changing window.innerWidth or firing `resize` — without this,
  // zooming in on an otherwise-roomy window could let the two fixed rails'
  // combined reserved width exceed the window itself.
  var zoom = ZOOM_LEVELS[_zoomIndex];
  var w = window.innerWidth / zoom;
  var html = document.documentElement;

  if (w <= 900) {
    html.classList.add("resp-sources-compact");

    html.style.setProperty("--sidebar-w", (64 * zoom) + "px");
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
    html.classList.remove("resp-sources-compact");

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
      _syncDownloadToastLayout();
      _updatePromptPlaceholder();
      _updateTextareaHeight();
      _syncEmptyPromptGlow();
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

  // Set the toast stack's compact/width state now that both panels are in
  // their final pre-paint state, and size the composer placeholder to the
  // current width so a narrow window never shows the long text clipped.
  _syncDownloadToastLayout();
  _updatePromptPlaceholder();
  _updateTextareaHeight();
  _syncEmptyPromptGlow();

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
  _checkModelStatus();
})();

el.settingsBtn.addEventListener("click", openSettings);

// ── model status & settings popup ──────────────────────────────────────

var _dropdownDocBound = false;

function _esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Cached /api/models/status so opening Settings renders the model sections
// immediately instead of flashing a "Loading…" placeholder that then expands
// once the fetch returns. Kept current by refreshes on open and after downloads.
var _cachedModelStatus = null;
var _llmMissing = false;
var _embedderMissing = false;
// True once the *first* /api/models/status attempt (success or failure) has
// completed. Until then, _modelGateBlocked() (see setComposerEnabled) holds
// the composer disabled with no banner shown, rather than optimistically
// treating an unconfirmed default as "available".
var _modelStatusReady = false;
// True for the span of an in-flight send (see sendMessage) — guards
// _applyModelGate's background refreshes from clobbering that lock.
var _sendInFlight = false;

async function _fetchModelStatus() {
  var data = null;
  try {
    var resp = await fetch("/api/models/status");
    if (resp.ok) data = await resp.json();
  } catch (_) {
    // Best-effort — don't break the app if the endpoint fails.
  }
  _modelStatusReady = true;
  if (data) {
    _cachedModelStatus = data;
    var anyMissing = (!data.available || data.available.length === 0);
    if (anyMissing) {
      el.settingsBtn.classList.add("warning");
    } else {
      el.settingsBtn.classList.remove("warning");
    }
    // Block sending entirely when a real driver is configured but no model
    // is downloaded/selected for that role. Mock mode needs no files, so it
    // stays enabled. Both roles are checked independently — an LLM present
    // with no embedding model (or vice versa) still blocks sending, since
    // RagEngine.answer() needs both to produce a grounded answer.
    _llmMissing = data.llm_driver === "llama_cpp" && !data.active_llm;
    _embedderMissing = data.embedder === "llama_cpp" && !data.active_embedding;
  }
  // On a failed fetch there's no fresh data, so this just re-applies
  // whatever _llmMissing/_embedderMissing last held (their initial false
  // defaults, on the very first attempt) — never leaves the composer stuck
  // on the "status still loading" hold forever.
  _applyModelGate();
  return data;
}

// Reflects current model availability in the composer: both the textarea
// and send button grayed out together while blocked, with the placeholder
// itself naming exactly what's missing (see _updatePromptPlaceholder) —
// there's no "type but can't send" state, and no separate banner; the
// blocked textarea's own placeholder is the explanation. File
// attach/ingestion stays available either way (untouched by this gate).
function _applyModelGate() {
  // Harmless to call unconditionally (unlike setComposerEnabled below):
  // neither writes anything but the placeholder/width/height of an
  // (at most) empty-of-real-content box, never the in-flight send lock,
  // so neither can clobber it. _updateTextareaHeight matters here too, not
  // just on resize — the shrink-to-fit width _updatePromptPlaceholder just
  // applied can drastically change how any already-typed text wraps.
  _updatePromptPlaceholder();
  _updateTextareaHeight();
  // Guarded so a background refresh (closeSettings, window focus while
  // Settings is open, post-download/-delete) can't clobber an in-flight
  // send's own lock — none of those triggers are themselves blocked by an
  // in-flight generation, and promptInput.disabled during a send is *only*
  // ever set by sendMessage()'s lock. Forcing it back to enabled here mid-
  // generation would let a second send fire before the first one finishes —
  // exactly the double-send failure mode this app's backend was redesigned
  // around (see GenerationWorker in ARCHITECTURE.md). sendMessage() restores
  // the composer itself once the stream ends.
  if (!_sendInFlight) {
    setComposerEnabled(true);
  }
}

async function _checkModelStatus() {
  await _fetchModelStatus();
}

// A cheap fingerprint of the parts of /api/models/status the settings panel
// renders, used to decide whether a re-render is actually needed on a refresh
// (avoiding a flicker when nothing on disk changed).
function _modelStatusSignature(data) {
  if (!data) return "";
  return JSON.stringify([
    data.available,
    data.types,
    data.active_llm,
    data.active_embedding,
    data.models,
  ]);
}

// The models folder is user-editable — a file can be dropped in or deleted
// from Finder while Settings is open. Refresh the panel whenever the window
// regains focus (e.g. returning from the OS file manager) so the dropdowns
// and active-model names reflect the folder's real contents.
window.addEventListener("focus", function () {
  var overlay = document.getElementById("settings-overlay");
  if (!overlay || !overlay.classList.contains("active")) return;
  _fetchModelStatus().then(function (data) {
    if (data) _renderSettingsBody(data);
  });
});

function closeSettings() {
  var overlay = document.getElementById("settings-overlay");
  if (!overlay) return;
  overlay.classList.remove("active");
  overlay.setAttribute("aria-hidden", "true");
  _collapseAllDropdowns();
  // Deliberately do NOT abort an in-flight model download here. Aborting
  // leaves a partial `.incomplete` file in huggingface_hub's cache that never
  // becomes a usable model, and the progress bar/stream would just vanish —
  // appearing to the user as "the download never happened." Letting it finish
  // in the background is safe: _refreshAfterDownload runs with the overlay
  // hidden and the next open of Settings (or the gear warning check) reflects
  // the installed model.
  // Refresh the send button's model gate now, in case a download/delete
  // happened while Settings was open and closed itself (rather than via
  // _refreshAfterDownload) — e.g. deleting the last active model then
  // dismissing the panel without triggering another download.
  _fetchModelStatus();
}

function openSettings() {
  var overlay = document.getElementById("settings-overlay");
  var closeBtn = document.getElementById("settings-close");
  if (!overlay || !closeBtn) return;

  overlay.classList.add("active");
  overlay.setAttribute("aria-hidden", "false");

  // Render immediately from the cached model status so the model sections are
  // fully drawn when the popup appears (no "Loading…" placeholder that then
  // expands). Refresh in the background so a later open is up to date.
  if (_cachedModelStatus) {
    _renderSettingsBody(_cachedModelStatus);
    _fetchModelStatus().then(function (data) {
      if (data) _renderSettingsBody(data);
    });
  } else {
    _renderSettingsBody();
  }

  function close() {
    closeSettings();
    closeBtn.removeEventListener("click", close);
    overlay.removeEventListener("click", onOverlayClick);
    document.removeEventListener("keydown", onKeyDown);
  }

  function onOverlayClick(e) {
    if (e.target === overlay) close();
  }

  function onKeyDown(e) {
    // A restart-confirm modal sits above Settings — let its own Escape
    // handler dismiss it first; don't also close Settings here.
    const restartModal = document.getElementById("restart-confirm");
    if (restartModal && restartModal.classList.contains("active")) return;
    if (e.key === "Escape") close();
  }

  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("keydown", onKeyDown);
}

async function _renderSettingsBody(data) {
  var body = document.getElementById("settings-body");
  if (!body) return;

  // No data passed in: fetch it now (first open before the cache is warm).
  if (!data) {
    try {
      var resp = await fetch("/api/models/status");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      data = await resp.json();
      _cachedModelStatus = data;
    } catch (_) {
      body.innerHTML = '<div class="settings-error">Could not load model status.</div>';
      return;
    }
  }

  var html = "";

  // Capability hint. We don't list the raw driver names ("LLM driver:
  // mock") because they can't be changed here — instead we surface only
  // what the user can act on: mock mode means the selection below has no
  // effect.
  var mockMode = data.llm_driver === "mock" || data.embedder === "mock";
  if (mockMode) {
    html += _noticeHtml('Mock mode — models aren’t loaded. Set '
      + '<code>LLM_DRIVER=llama_cpp</code> and <code>EMBEDDER=llama_cpp</code> '
      + 'in <code>.env</code> to use your selection.');
  }

  // Find the known catalog entry per role (for the "download default" action).
  // Build a filename -> type map for *every* available file so the menus show
  // only models of the matching kind (an embedding GGUF must never appear in
  // the LLM menu). The backend classifies each file from its GGUF metadata
  // (pooling-layer presence), so third-party models get the right type too.
  var llmCat = null, embCat = null, typeByName = {};
  for (var c = 0; c < data.models.length; c++) {
    if (data.models[c].key === "llm") llmCat = data.models[c];
    if (data.models[c].key === "embedding") embCat = data.models[c];
  }
  if (data.types && typeof data.types === "object") {
    for (var name in data.types) {
      if (Object.prototype.hasOwnProperty.call(data.types, name)) {
        typeByName[name] = data.types[name];
      }
    }
  }

  html += _modelCardHtml("Language model", "llm", data.available, data.active_llm, typeByName, llmCat);
  html += _modelCardHtml("Embedding model", "embedding", data.available, data.active_embedding, typeByName, embCat);

  html += '<button type="button" class="settings-open-folder-btn" id="open-models-folder">'
    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>'
    + '<span>Open models folder</span>'
    + '</button>';

  body.innerHTML = html;

  // Wire model dropdown triggers
  var triggers = body.querySelectorAll(".settings-dropdown-trigger");
  for (var j = 0; j < triggers.length; j++) {
    triggers[j].addEventListener("click", function () {
      _toggleModelDropdown(this);
    });
  }

  // Wire model options (select a model). Only the real, on-disk candidate rows
  // are selectable: they are the <button> options carrying a data-filename.
  // The not-yet-downloaded catalog-default row is a <button> too, but it has
  // no data-filename (it can't be picked until it's on disk) — it's a
  // download action wired separately below, so it stays out of this set.
  var options = body.querySelectorAll("button.settings-dropdown-option[data-filename]:not([disabled])");
  for (var o = 0; o < options.length; o++) {
    options[o].addEventListener("click", function () {
      _selectModel(this.dataset.role, this.dataset.filename);
    });
  }

  // Wire the "open models folder" button
  var openBtn = body.querySelector(".settings-open-folder-btn");
  if (openBtn) openBtn.addEventListener("click", _openModelsFolder);

  // Wire download rows (the catalog-default row is one whole download button)
  var buttons = body.querySelectorAll(".settings-dropdown-option-download");
  for (var k = 0; k < buttons.length; k++) {
    buttons[k].addEventListener("click", function () {
      var key = this.dataset.modelKey;
      var name = this.dataset.name || null;
      _startModelDownload(key, name);
    });
  }

  // Wire delete buttons (one per installed model: the active model on the
  // trigger row, and each non-active downloaded model in the dropdown).
  var deletes = body.querySelectorAll(".settings-model-delete");
  for (var d = 0; d < deletes.length; d++) {
    deletes[d].addEventListener("click", function () {
      _deleteModel(this.dataset.deleteTarget);
    });
  }
}

// A single muted warning banner (icon + message). `inner` is trusted markup
// built from string literals only — never pass user input through here.
function _noticeHtml(inner) {
  return '<div class="settings-notice">'
    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M12 9v4"/><path d="M12 17h.01"/>'
    + '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg>'
    + '<span>' + inner + '</span>'
    + '</div>';
}

// Hand-authored trash glyph (no icon font/CDN).
function _trashGlyph() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';
}

// Checkmark marking the currently-selected model in the dropdown.
function _checkGlyph() {
  return '<svg class="settings-dropdown-option-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
}

// A small uninstall button for an installed model (default or custom).
function _deleteButtonHtml(target) {
  return '<button type="button" class="settings-model-delete" data-delete-target="' + _esc(target) + '" title="Uninstall model" aria-label="Uninstall ' + _esc(target) + '">'
    + _trashGlyph()
    + '</button>';
}

// Uninstall an installed model, reusing the chat-delete confirmation modal.
async function _deleteModel(target) {
  var confirmed = await _confirmDelete(target, "Uninstall");
  if (!confirmed) return;
  try {
    var resp = await fetch("/api/models/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: target })
    });
    if (!resp.ok) {
      var err = {};
      try { err = await resp.json(); } catch (_) {}
      throw new Error(err.detail || "HTTP " + resp.status);
    }
  } catch (err) {
    console.warn("delete model failed:", err);
  }
  _refreshAfterDownload();
}


function _modelCardHtml(label, role, available, active, typeByName, cat) {
  // Options: every .gguf of the matching type (role), INCLUDING the currently
  // active one (marked with a checkmark so it's clear it's selected). Filtering
  // by type keeps an embedding GGUF out of the LLM menu and vice versa.
  var opts = [];
  for (var i = 0; i < available.length; i++) {
    var f = available[i];
    if (!typeByName[f] || typeByName[f] === role) opts.push(f);
  }
  opts.sort();

  var canDownload = !!(cat && !cat.exists && cat.repo_id);
  // With no downloaded candidates and nothing to download, the trigger is a
  // dead button — disable it so it reads as a label, not a clickable control.
  var nothingToSelect = opts.length === 0 && !canDownload;
  var disabledAttr = nothingToSelect ? ' disabled' : '';
  // Nothing downloaded for this role at all yet — the same condition that
  // drives the composer's "No language/embedding model installed" gate
  // (app.js: _updatePromptPlaceholder). Flags the card so its background
  // and the "Select a model..." placeholder read as a warning, not just an
  // empty default state.
  var missingClass = opts.length === 0 ? ' missing' : '';

  var html = '<div class="settings-model-card' + missingClass + '">';
  html += '<p class="settings-model-name">' + _esc(label) + '</p>';
  html += '<div class="settings-model-trigger-row">';
  html += '<button type="button" class="settings-dropdown-trigger" data-role="' + _esc(role) + '" data-active="' + _esc(active || '') + '" aria-haspopup="listbox" aria-expanded="false"' + disabledAttr + '>';
  html += '<span class="settings-dropdown-value" id="value-' + _esc(role) + '">'
    + (active ? _esc(active) : 'Select a model...') + '</span>';
  html += '<svg class="settings-dropdown-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  html += '</button>';
  html += '</div>';
  html += '<div class="settings-dropdown-list" id="list-' + _esc(role) + '" role="listbox" hidden>';
  if (opts.length === 0 && !canDownload) {
    html += '<div class="settings-dropdown-empty">No other models available</div>';
  } else {
    for (var i = 0; i < opts.length; i++) {
      var f = opts[i];
      var isActive = f === active;
      html += '<div class="settings-dropdown-option-row">';
      html += '<button type="button" class="settings-dropdown-option settings-dropdown-option-select"'
        + ' data-role="' + _esc(role) + '" data-filename="' + _esc(f) + '"'
        + ' role="option"' + (isActive ? ' aria-selected="true"' : '') + '>';
      if (isActive) html += _checkGlyph();
      html += '<span class="settings-dropdown-option-name">' + _esc(f) + '</span>';
      html += '</button>';
      html += _deleteButtonHtml(f);
      html += '</div>';
    }
    if (canDownload) {
      // The catalog default isn't on disk yet — offer it as a row that is
      // itself one whole download button (the only model type the backend can
      // fetch), rather than a one-off "download default" button in the card
      // foot. Being a single <button>, every pixel of it — padding included —
      // is clickable and the row stays the same height as a plain option.
      html += '<button type="button" class="settings-dropdown-option settings-dropdown-option-download" data-model-key="' + _esc(role) + '" data-name="' + _esc(cat.filename) + '" data-lock-key="' + _esc(role) + '"'
        + ' title="Download ' + _esc(cat.filename) + '" aria-label="Download ' + _esc(cat.filename) + '">';
      html += '<span class="settings-dropdown-option-name">' + _esc(cat.filename) + '</span>';
      // Hand-authored download-into-tray glyph so the row needs no icon font/CDN.
      // Sized below the text line-height so it never inflates the row either.
      html += '<svg class="settings-download-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="3" x2="12" y2="15"/></svg>';
      html += '</button>';
    }
  }
  html += '</div>';
  // Card foot: a quiet status line for the select/restart flow ("Saving…",
  // "Restarting…", errors). The download progress now lives in the toast,
  // so no download button lives here anymore.
  html += '<div class="settings-model-foot">';
  html += '<span class="settings-model-note" id="note-' + _esc(role) + '"></span>';
  html += '</div>';
  html += '</div>';
  return html;
}

// Selecting a model persists the choice but it only takes effect after the
// backend restarts. So clicking an option asks for confirmation first: on
// confirm we persist + quit (the new model loads next launch); on cancel we
// revert the dropdown to the currently-active model and persist nothing.
// No transient "Saved" note — the restart prompt *is* the confirmation.
function _selectModel(role, filename) {
  var trigger = document.querySelector('.settings-dropdown-trigger[data-role="' + role + '"]');
  var current = trigger ? (trigger.dataset.active || "") : "";

  // Close the dropdown before showing the modal — the list's high z-index
  // would otherwise float it above the confirm-backdrop.
  var list = document.getElementById("list-" + role);
  _closeDropdownList(list, trigger, true);

  if (filename === current) {
    // Clicking the already-active model is a no-op — just close.
    return;
  }

  // The modal drives the restart action (and the loading state) itself;
  // this callback only runs on the cancel path — revert to current.
  _confirmRestart(role, filename, function () {
    _updateDropdownValue(role, current || "");
  });
}

function _updateCurrentActive(role, filename) {
  var trigger = document.querySelector('.settings-dropdown-trigger[data-role="' + role + '"]');
  if (trigger) trigger.dataset.active = filename || "";
}

// Lock the restart modal into a non-dismissible "restarting" state: disable
// both buttons, swap the Restart label for a clockwise spinner in-place (its
// box is kept, so the button doesn't reflow), disable Escape/backdrop
// dismissal, and gray out the Settings "x". The confirm message text is left
// untouched.
function _lockRestartModal() {
  var backdrop = document.getElementById("restart-confirm");
  if (!backdrop) return;
  backdrop.classList.add("restart-locked");

  var okBtn = document.getElementById("restart-confirm-ok");
  var cancelBtn = document.getElementById("restart-confirm-cancel");
  if (cancelBtn) cancelBtn.disabled = true;

  if (okBtn) {
    okBtn.disabled = true;
    okBtn.classList.add("restart-spinner-on");

    // Measure the button *before* touching its content so the replacement
    // keeps the exact same footprint on screen.
    var width = okBtn.getBoundingClientRect().width;
    var height = okBtn.getBoundingClientRect().height;
    okBtn.textContent = "";

    var spinner = document.createElement("span");
    spinner.className = "restart-loading";
    spinner.setAttribute("aria-hidden", "true");
    okBtn.appendChild(spinner);

    // Pin the measured size so the pill neither expands nor collapses.
    if (width) okBtn.style.minWidth = width + "px";
    if (height) okBtn.style.minHeight = height + "px";
  }

  var closeBtn = document.getElementById("settings-close");
  if (closeBtn) {
    closeBtn.disabled = true;
    closeBtn.classList.add("restart-locked-btn");
  }
}

// Tell the Tauri shell to exit now that the backend has quit. Falls back to a
// no-op when running in a plain browser (no native shell to close).
function _quitAppNow() {
  try {
    var tauri = window.__TAURI__;
    if (tauri && tauri.core && tauri.core.invoke) {
      tauri.core.invoke("quit_app");
    }
  } catch (_) {
    // No Tauri runtime (e.g. testing in a browser) — nothing to do.
  }
}

async function _persistModelAndQuit(role, filename) {
  _lockRestartModal();
  // Stop any in-flight generation first (abort live SSE streams) so the UI
  // stops streaming before we hand off to the graceful backend shutdown.
  _abortAllSse();

  var note = document.getElementById("note-" + role);
  if (note) { note.textContent = "Saving…"; note.className = "settings-model-note"; }
  try {
    var resp = await fetch("/api/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: role, filename: filename })
    });
    if (!resp.ok) {
      var err = {};
      try { err = await resp.json(); } catch (_) {}
      throw new Error(err.detail || "HTTP " + resp.status);
    }
    _updateDropdownValue(role, filename);
    _updateCurrentActive(role, filename);
    if (note) { note.textContent = "Restarting…"; note.className = "settings-model-note"; }
    // Model persisted — now shut the backend down so the choice takes effect
    // on next launch. The response flushes before the process teardown.
    try {
      await fetch("/api/models/quit", { method: "POST" });
    } catch (_) {
      // The connection may drop as the backend shuts down — that's expected.
    }
    // The backend is now gone — close the Tauri window so the app visibly
    // quits instead of sitting on the locked restart spinner.
    _quitAppNow();
  } catch (err) {
    if (note) { note.textContent = "Error: " + err.message; note.className = "settings-model-note error"; }
  }
}

// Shows the restart confirm modal. `role`/`filename` are the pending model
// choice; `onCancel` fires when the user cancels (so the caller can revert
// the selection). Clicking OK runs `_persistModelAndQuit` directly and keeps
// the modal open in its locked, loading state — the caller does not need to
// act on restart. Mirrors `_confirmDelete`'s modal lifecycle (backdrop,
// Escape, aria state).
function _confirmRestart(role, filename, onCancel) {
    var backdrop = document.getElementById("restart-confirm");
    var message = document.getElementById("restart-confirm-message");
    var cancelBtn = document.getElementById("restart-confirm-cancel");
    var okBtn = document.getElementById("restart-confirm-ok");
    if (!backdrop || !message || !cancelBtn || !okBtn) { if (onCancel) onCancel(); return; }

    message.innerHTML =
        '<p class="confirm-copy">Switching model requires an app restart.</p>'
      + '<p class="confirm-copy">Any chat currently processing data will be stopped.</p>'
      + '<p class="confirm-subtle">Do you wish to proceed?</p>';

    backdrop.classList.add("active");
    backdrop.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    okBtn.focus();

    function locked() { return backdrop.classList.contains("restart-locked"); }

    function dismiss() {
      backdrop.classList.remove("active");
      backdrop.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      cancelBtn.removeEventListener("click", handleCancel);
      okBtn.removeEventListener("click", handleRestart);
      backdrop.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onEscape);
    }

    function handleCancel() {
      if (locked()) return;
      dismiss();
      if (onCancel) onCancel();
    }
    function handleRestart() {
      if (locked()) return;
      // Keep the modal visible; _persistModelAndQuit locks it (disables the
      // buttons, shows the spinner, blocks Escape/backdrop/x) then quits.
      _persistModelAndQuit(role, filename);
    }
    function onBackdrop(e) {
      if (e.target === backdrop && !locked()) handleCancel();
    }
    function onEscape(e) {
      if (e.key === "Escape" && !locked()) {
        var modal = document.getElementById("citation-modal");
        if (modal && modal.classList.contains("active")) return;
        handleCancel();
      }
    }

    cancelBtn.addEventListener("click", handleCancel);
    okBtn.addEventListener("click", handleRestart);
    backdrop.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onEscape);
}

function _updateDropdownValue(role, filename) {
  var valueEl = document.getElementById("value-" + role);
  if (valueEl) valueEl.textContent = filename;

  var list = document.getElementById("list-" + role);
  if (list) {
    var options = list.querySelectorAll(".settings-dropdown-option[data-filename]");
    for (var i = 0; i < options.length; i++) {
      var selected = options[i].dataset.filename === filename;
      options[i].classList.toggle("selected", selected);
      options[i].setAttribute("aria-selected", selected ? "true" : "false");
    }
    // Selecting an option collapses instantly (no animation).
    var trigger = document.querySelector('.settings-dropdown-trigger[data-role="' + role + '"]');
    _closeDropdownList(list, trigger, true);
    return;
  }

  var trigger = document.querySelector('.settings-dropdown-trigger[data-role="' + role + '"]');
  if (trigger) trigger.setAttribute("aria-expanded", "false");
}

// Open/close helpers. Open animates in (80ms). Close animates out by default
// (80ms) but collapses instantly when an option is clicked. Only one list is
// open at a time, so opening/leaning on another collapses the previous one
// animated.
function _openDropdownList(list, trigger) {
  if (list._collapseTimeout) {
    clearTimeout(list._collapseTimeout);
    list._collapseTimeout = null;
  }
  list.classList.remove("no-anim");
  list.hidden = false;
  // Force a reflow so the browser starts from the hidden (opacity 0) state
  // before the .open class transitions it in.
  void list.offsetHeight;
  list.classList.add("open");
  trigger.setAttribute("aria-expanded", "true");
}

function _closeDropdownList(list, trigger, instant) {
  if (!list || list.hidden) return;

  if (instant) {
    list.classList.add("no-anim");
    list.classList.remove("open");
    list.hidden = true;
    list.classList.remove("no-anim");
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    return;
  }

  list.classList.remove("open");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  if (list._collapseTimeout) clearTimeout(list._collapseTimeout);
  list._collapseTimeout = setTimeout(function () {
    list.hidden = true;
    list._collapseTimeout = null;
  }, 80);
}

// The options list floats *below* the trigger (fixed-positioned against its
// rect) so it overlays the card without reflowing it, and scrolls internally
// once the model list grows long. Only one list is open at a time.
function _toggleModelDropdown(trigger) {
  // A disabled trigger (no active model and no candidates) must not open — a
  // disabled button doesn't normally fire click, but guard anyway so an
  // assistive-tech or synthetic click can't pop open a pointless empty list.
  if (trigger.disabled) return;
  var role = trigger.dataset.role;

  // Refresh the catalog before opening: the models folder is user-editable,
  // so a file deleted from Finder must disappear from this list immediately.
  // Re-render only when the data actually changed (otherwise every open would
  // flicker), then re-resolve the trigger/list since the re-render rebuilt
  // the DOM.
  var before = _modelStatusSignature(_cachedModelStatus);
  _fetchModelStatus().then(function (data) {
    if (!data || _modelStatusSignature(data) === before) {
      _openResolvedDropdown(role);
      return;
    }
    _renderSettingsBody(data).then(function () {
      _openResolvedDropdown(role);
    });
  });
}

function _openResolvedDropdown(role) {
  var trigger = document.querySelector('.settings-dropdown-trigger[data-role="' + role + '"]');
  if (!trigger || trigger.disabled) return;
  var list = document.getElementById("list-" + role);
  var isOpen = trigger.getAttribute("aria-expanded") === "true";

  var open = document.querySelectorAll(".settings-dropdown-trigger[aria-expanded='true']");
  for (var i = 0; i < open.length; i++) {
    if (open[i] === trigger) continue;
    var other = document.getElementById("list-" + open[i].dataset.role);
    _closeDropdownList(other, open[i], false);
  }

  if (isOpen) {
    _closeDropdownList(list, trigger, false);
    return;
  }

  if (!list) return;
  var rect = trigger.getBoundingClientRect();
  list.style.left = rect.left + "px";
  list.style.width = rect.width + "px";
  // Anchored flush under the trigger (no gap) so it reads as one joined
  // control rather than a detached floating panel.
  list.style.top = rect.bottom + "px";
  list.style.bottom = "auto";
  // Cap the height to the room below so it scrolls rather than spilling off
  // the bottom of the viewport.
  var spaceBelow = window.innerHeight - rect.bottom - 8;
  list.style.maxHeight = (spaceBelow < 320 ? Math.max(120, spaceBelow) : 320) + "px";

  _openDropdownList(list, trigger);

  if (!_dropdownDocBound) {
    _dropdownDocBound = true;
    document.addEventListener("click", _onDropdownOutsideClick);
  }
}

function _onDropdownOutsideClick(e) {
  var open = document.querySelector(".settings-dropdown-trigger[aria-expanded='true']");
  if (!open) return;
  var list = document.getElementById("list-" + open.dataset.role);
  if (list && !list.contains(e.target) && !open.contains(e.target)) {
    _closeDropdownList(list, open, false);
  }
}

function _collapseAllDropdowns() {
  var open = document.querySelectorAll(".settings-dropdown-trigger[aria-expanded='true']");
  for (var i = 0; i < open.length; i++) {
    var list = document.getElementById("list-" + open[i].dataset.role);
    _closeDropdownList(list, open[i], true);
  }
}

async function _openModelsFolder() {
  var btn = document.getElementById("open-models-folder");
  if (btn) btn.disabled = true;
  try {
    var resp = await fetch("/api/models/open-folder", { method: "POST" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
  } catch (_) {
    if (btn) btn.textContent = "Couldn't open folder";
  }
  if (btn) btn.disabled = false;
}

/* -- one-off action toasts -------------------------------------------- */
// A short-lived, self-dismissing pill for surfacing why an attempted
// action (e.g. sending with no file attached) was rejected. Shares the
// bottom-right #toast-stack container with the model-download pills below
// for one consistent notification spot, but is otherwise a separate,
// simpler element — no retry button, no progress state, no compact-rail
// collapsing (it's gone long before a rail resize would matter).
function _showActionToast(message, isError) {
  var stack = _toastStackEl();
  if (!stack) return;
  var toast = document.createElement("div");
  toast.className = "action-toast" + (isError ? " error" : "");
  toast.setAttribute("role", "status");
  toast.textContent = message;
  stack.appendChild(toast);
  void toast.offsetHeight; // force a reflow so the .show transition plays
  toast.classList.add("show");
  setTimeout(function () {
    toast.classList.remove("show");
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 200);
  }, 2500);
}

/* -- download toast stack -------------------------------------------- */
// One toast pill per model download, stacked bottom-up.  A download is
// identified by a unique stream id (not the model key — a single "all" request
// streams several models through one SSE connection and one toast).

var _downloadToasts = new Map(); // streamId -> {toast, controller, modelKey, hideTimer, ...}
var _downloadSeq = 0;

function _toastStackEl() {
  return document.getElementById("toast-stack");
}

// Mount a fresh pill (cloned from the <template>) into the stack.  Because the
// stack is a bottom-anchored flex column, the newest toast lands at the bottom
// and earlier ones ride above it — the usual bottom-right stack behaviour.
function _makeDownloadToast() {
  var tpl = document.getElementById("download-toast-template");
  var stack = _toastStackEl();
  var node = tpl.content.firstElementChild.cloneNode(true);
  stack.appendChild(node);
  return node;
}

function _ensureToastIcon(toast) {
  // The static icon (a wrapper holding the download glyph + the hover X)
  // stays as the toast's leading flex child so both the expanded (icon +
  // label + %) and collapsed (icon-only) forms work off the same node.
  // Reorder it to the front if a prior clear left it mid-tree.
  var icon = toast.querySelector(".download-toast-icon");
  if (!icon) return;
  if (toast.firstChild !== icon) toast.insertBefore(icon, toast.firstChild);
}

function _setDownloadToast(entry, label, pct, isError) {
  var toast = entry.toast;
  if (!toast) return;
  clearTimeout(entry.hideTimer);
  entry.hideTimer = null;
  toast.classList.toggle("error", !!isError);
  _ensureToastIcon(toast);

  // Two-line text block: the *state* is the primary line (always visible),
  // the model name is a muted secondary line underneath (truncated, never
  // pushing the state out of view).
  var textEl = toast.querySelector(".download-toast-text");
  if (!textEl) {
    textEl = document.createElement("div");
    textEl.className = "download-toast-text";
    toast.appendChild(textEl);
  }

  var labelEl = textEl.querySelector(".download-toast-label");
  if (!labelEl || labelEl.textContent !== (label || "")) {
    var old = textEl.querySelectorAll(".download-toast-label, .download-toast-name");
    for (var i = 0; i < old.length; i++) old[i].remove();
    if (label) {
      labelEl = document.createElement("span");
      labelEl.className = "download-toast-label";
      labelEl.textContent = label;
      textEl.appendChild(labelEl);
    }
    if (entry.name) {
      var nameEl = document.createElement("span");
      nameEl.className = "download-toast-name";
      nameEl.textContent = entry.name;
      textEl.appendChild(nameEl);
    }
  }

  var pctEl = toast.querySelector(".download-toast-pct");
  if (pct !== null && pct !== undefined) {
    if (!pctEl) {
      pctEl = document.createElement("span");
      pctEl.className = "download-toast-pct";
      toast.appendChild(pctEl);
    }
    pctEl.textContent = pct;
  } else if (pctEl) {
    pctEl.remove();
  }

  toast.hidden = false;
  void toast.offsetHeight; // force a reflow so the add .show transitions in
  toast.classList.add("show");
}

function _appendDownloadRetry(entry) {
  var toast = entry.toast;
  if (!toast || toast.querySelector(".download-toast-retry")) return;
  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "download-toast-retry";
  btn.textContent = "Retry";
  btn.addEventListener("click", function () {
    var key = entry.modelKey;
    var name = entry.name;
    _removeDownloadToast(entry);
    if (key) _startModelDownload(key, name);
  });
  toast.appendChild(btn);
}

function _removeDownloadToast(entry) {
  var toast = entry.toast;
  if (!toast || !toast.parentNode) return;
  clearTimeout(entry.hideTimer);
  entry.hideTimer = null;
  toast.classList.remove("show", "cancellable");
  // Let the fade-out finish, then actually remove the node so the remaining
  // pills slide down into its place.  Release the download width lock at the
  // same moment so it can't cause a visible shrink.
  entry.hideTimer = setTimeout(function () {
    if (!toast.classList.contains("show") && toast.parentNode) {
      toast.parentNode.removeChild(toast);
      toast.style.minWidth = "";
    }
  }, 200);
  _downloadToasts.delete(entry.id);
}

// Cancel one in-flight model download via its AbortController.  Clicking a
// pill while its transfer is running is the one interaction a download offers;
// guard so a stray click on a success/error pill (no active controller or the
// stream already finished) does nothing.
function _cancelModelDownload(entry) {
  if (!entry || !entry.controller) return;
  entry.toast.classList.remove("cancellable");
  try {
    entry.controller.abort();
  } catch (e) { /* already aborted/closed — treat as cancelled */ }
  _setDownloadToast(entry, "Download cancelled", null, false);
  _hideDownloadToastAfter(entry, 2500);
  _releaseModelRow(entry.lockKey || entry.modelKey);
}

// Route a click anywhere in the stack to the pill that was clicked (only that
// download, not the whole stack).  The Retry button receives its own click, so
// this never double-fires on it.
_toastStackEl().addEventListener("click", function (event) {
  var pill = event.target.closest(".download-toast");
  if (!pill) return;
  var entry = _downloadToasts.get(pill.dataset.streamId);
  if (entry) _cancelModelDownload(entry);
});

// Auto-dismiss a terminal success toast after `ms` (a success is transient;
// only the error toast stays until the user acts or closes it).
function _hideDownloadToastAfter(entry, ms) {
  clearTimeout(entry.hideTimer);
  entry.hideTimer = setTimeout(function () {
    _removeDownloadToast(entry);
  }, ms);
}

// Lock/release a single model's download row(s) for the duration of its own
// transfer.  Unlike the old single-download behaviour (which disabled every
// row), per-model locking keeps other models clickable so a second download can
// queue its own toast.
// A row lock key identifies one download target. Each role has a single
// catalog download, so the key is simply the role name ("llm"/"embedding").
function _modelDownloadRows(lockKey) {
  var rows = document.querySelectorAll(
    '.settings-dropdown-option-download[data-lock-key="' + lockKey + '"]'
  );
  return Array.prototype.slice.call(rows);
}

function _lockModelRow(lockKey) {
  var rows = _modelDownloadRows(lockKey);
  for (var i = 0; i < rows.length; i++) rows[i].disabled = true;
}

function _releaseModelRow(lockKey) {
  var rows = _modelDownloadRows(lockKey);
  for (var i = 0; i < rows.length; i++) rows[i].disabled = false;
}

async function _startModelDownload(modelKey, name) {
  // Each download gets its own controller, toast, and row lock.  Downloads are
  // *sequential* on the backend (a shared asyncio.Lock serializes transfers so
  // two writers never race huggingface_hub's cache), but every request is still
  // its own SSE stream and its own toast — so starting a second model while one
  // is running mounts a second pill that waits ("Queued") until it's picked up.
  var entry = {
    id: "dl" + (++_downloadSeq),
    modelKey: modelKey,
    name: name || null,
    lockKey: modelKey,
    controller: new AbortController(),
    toast: _makeDownloadToast(),
    hideTimer: null,
  };
  entry.toast.dataset.streamId = entry.id;
  _downloadToasts.set(entry.id, entry);

  // Mark the pill cancellable so the whole pill reads as a cancel button (hover
  // morphs the glyph to an X; click aborts that download only).
  entry.toast.classList.add("cancellable");
  _lockModelRow(entry.lockKey);

  // Hold each pill's width constant for its (usually short) transfer so changing
  // content never resizes it (especially the shorter cancel label).  A min-width
  // (not a fixed width) survives the CSS max-width cap that would defeat it.
  // Measure against the widest content the toast will show — "Downloading model…
  // 100.00%" — then settle on the real opening label synchronously, so the
  // browser paints only that (never a flash of the temp content).
  var stack = _toastStackEl();
  // Skip the width lock while the stack is compact/icon-only (small window):
  // in that state pills render as a fixed 40px circle, so measuring their
  // natural wider width and locking it would fight the .compact CSS.
  if (!stack.classList.contains("collapsed")
      && !stack.classList.contains("compact")) {
    _setDownloadToast(entry, "Downloading model…", "100.00%", false);
    entry.toast.style.minWidth = entry.toast.offsetWidth + "px";
  }
  // The backend serializes downloads via its lock, so until this request is
  // picked up its SSE stream emits nothing.  Show a short queued state; the
  // first event ("verifying") replaces it when the transfer starts.
  _setDownloadToast(entry, "Queued", null, false);

  try {
    var response = await fetch("/api/models/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelKey }),
      signal: entry.controller.signal,
    });

    if (!response.ok) {
      throw new Error("Download request failed: " + response.status);
    }

    await readSseStream(response, function (parsed) {
      if (parsed.event === "model_download_status") {
        _handleDownloadProgress(entry, parsed.data);
      }
    });
  } catch (err) {
    if (err.name !== "AbortError") {
      _setDownloadToast(entry, "Download failed", null, true);
      _appendDownloadRetry(entry);
    }
  } finally {
    // No longer cancellable — the transfer ended (complete, failed, or
    // aborted); drop the hover-morph / cancel affordance.  Drop it from the
    // live-download map so a re-render won't re-lock its row, but keep the
    // pill mounted so the terminal toast (success / "Download cancelled" /
    // retryable error) stays visible until it's removed on hide.  The width
    // lock stays set until then so it never visibly resizes.
    entry.toast.classList.remove("cancellable");
    _releaseModelRow(entry.lockKey);
    _downloadToasts.delete(entry.id);
    _refreshAfterDownload();
  }
}

function _handleDownloadProgress(entry, data) {
  // Progress surfaces as that download's pill ("Downloading model… X.XX%"),
  // never as a bar or in-panel readout. progress is 0..1; clamp so a
  // downstream overshoot can't exceed 100.
  var pctRaw = (typeof data.progress === "number" ? data.progress : 0);
  var pct = Math.max(0, Math.min(100, pctRaw * 100));

  switch (data.status) {
    case "finalizing":
      // Network transfer is done but the backend is still reassembling the
      // file on disk (see download.py) — a real gap of its own, not the same
      // wait as "Verifying model…" (which is the separate SHA256 pass after
      // this finishes), so it gets its own label rather than reusing either.
      _setDownloadToast(entry, "Finalizing download…", null, false);
      break;
    case "verifying":
      _setDownloadToast(entry, "Verifying model…", null, false);
      break;
    case "downloading":
      // Two decimals so the readout reads as a real progress value.
      _setDownloadToast(entry, "Downloading model…", pct.toFixed(2) + "%", false);
      break;
    case "already_exists":
      _setDownloadToast(entry, "Model already installed", null, false);
      _hideDownloadToastAfter(entry, 2500);
      break;
    case "complete":
      _setDownloadToast(entry, "Model downloaded", null, false);
      _hideDownloadToastAfter(entry, 2500);
      break;
    case "error":
      _setDownloadToast(entry, "Download failed", null, true);
      _appendDownloadRetry(entry);
      break;
  }
}

async function _refreshAfterDownload() {
  var body = document.getElementById("settings-body");
  if (body) await _renderSettingsBody();
  await _checkModelStatus();
  // Re-rendering the settings body rebuilds the dropdown rows disabled=false,
  // so re-apply the lock for any model still downloading on another toast.
  _downloadToasts.forEach(function (entry) {
    if (entry && entry.lockKey) _lockModelRow(entry.lockKey);
  });
}
