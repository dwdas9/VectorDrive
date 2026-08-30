// Local Ollama chat with optional retrieval from VectorDrive's existing
// hybrid index. The webview never performs network requests: every Ollama,
// retrieval, and filesystem action crosses the native GuiApi bridge.
"use strict";

const CHAT_STATE = {
  messages: [],
  models: [],
  selectedModel: "",
  useIndexedDocuments: true,
  activeChatId: null,
  streaming: false,
  stopping: false,
  resetAfterStop: false,
  modelState: "idle",
  modelError: "",
  status: "",
  statusKind: "status",
  draft: "",
};

let _chatMain = null;

function _chatFilename(path) {
  var parts = String(path || "Unknown document").split(/[\\/]/);
  return parts[parts.length - 1] || "Unknown document";
}

function _chatModelLabel(model) {
  if (!model || typeof model.size !== "number" || model.size <= 0) return model.name;
  var gib = model.size / (1024 * 1024 * 1024);
  return model.name + " · " + (gib >= 10 ? gib.toFixed(0) : gib.toFixed(1)) + " GB";
}

function _chatActiveAssistant() {
  for (var i = CHAT_STATE.messages.length - 1; i >= 0; i -= 1) {
    if (CHAT_STATE.messages[i].role === "assistant") return CHAT_STATE.messages[i];
  }
  return null;
}

function _chatSourceCard(source, index) {
  var rank = source.rank || index + 1;
  var filename = _chatFilename(source.path);
  var meta = [];
  if (source.page_number) meta.push("Page " + source.page_number);
  if (source.section) meta.push(source.section);

  return el("article", {
    class: "chat-source-card",
    "aria-label": "Source " + rank + ": " + filename,
  }, [
    el("p", { class: "chat-source-title", text: rank + ". " + filename }),
    meta.length ? el("p", { class: "chat-source-meta", text: meta.join(" · ") }) : null,
    el("p", { class: "chat-source-excerpt", text: source.excerpt || "No excerpt available." }),
    el("div", { class: "chat-source-actions" }, [
      el("button", {
        type: "button", class: "sm secondary", text: "Open",
        onclick: function () { api("open_document", source.path, source.page_number); },
      }),
      el("button", {
        type: "button", class: "sm secondary", text: "Reveal",
        onclick: function () { api("reveal_document", source.path); },
      }),
    ]),
  ]);
}

function _chatMessage(message) {
  var roleLabel = message.role === "user" ? "You" : "VectorDrive";
  var bubble = el("div", { class: "chat-bubble" });
  // Model output, prompts, and excerpts are untrusted plain text. Never
  // parse them as markup; this remains safe even if a document contains
  // HTML or prompt-injection-like instructions.
  bubble.textContent = message.content || (message.pending ? "Thinking…" : "");

  var children = [
    el("p", { class: "chat-role", text: roleLabel }),
    bubble,
  ];
  if (message.sources && message.sources.length) {
    children.push(el("section", {
      class: "chat-sources",
      "aria-label": "Sources for this response",
    }, [
      el("p", { class: "chat-sources-title", text: "Sources" }),
    ].concat(message.sources.map(_chatSourceCard))));
  }

  return el("div", { class: "chat-message " + message.role }, [
    el("div", { class: "chat-message-body" }, children),
  ]);
}

function _chatHistoryForRequest() {
  return CHAT_STATE.messages.map(function (message) {
    var content = message.content;
    if (message.role === "assistant" && content.length > 32000) content = content.slice(-32000);
    return { role: message.role, content: content };
  }).filter(function (message) { return Boolean(message.content); }).slice(-12);
}

function _chatSetStatus(message, kind) {
  CHAT_STATE.status = message;
  CHAT_STATE.statusKind = kind || "status";
}

function _chatRenderIfVisible() {
  if (_chatMain && document.body.contains(_chatMain) && currentRoute() === "chat") {
    _renderChatView(_chatMain);
  }
}

async function _loadChatModels() {
  if (CHAT_STATE.modelState === "loading") return;
  CHAT_STATE.modelState = "loading";
  CHAT_STATE.modelError = "";
  _chatRenderIfVisible();

  var res = await api("list_chat_models");
  if (!res.ok) {
    CHAT_STATE.models = [];
    CHAT_STATE.modelState = "unreachable";
    CHAT_STATE.modelError = (res.error && res.error.message) || "Could not connect to the local Ollama service.";
    _chatRenderIfVisible();
    return;
  }

  CHAT_STATE.models = (res.data && res.data.models) || [];
  if (!CHAT_STATE.models.length) {
    CHAT_STATE.selectedModel = "";
    CHAT_STATE.modelState = "empty";
  } else {
    var available = CHAT_STATE.models.map(function (model) { return model.name; });
    var preferred = (res.data && res.data.selected_model) || CHAT_STATE.selectedModel;
    CHAT_STATE.selectedModel = available.indexOf(preferred) >= 0 ? preferred : "";
    CHAT_STATE.modelState = "ready";
  }
  _chatRenderIfVisible();
}

async function _startChatMessage() {
  var message = CHAT_STATE.draft.trim();
  if (!message || CHAT_STATE.streaming || CHAT_STATE.modelState !== "ready" || !CHAT_STATE.selectedModel) return;

  var history = _chatHistoryForRequest();
  CHAT_STATE.messages.push({ role: "user", content: message });
  CHAT_STATE.messages.push({ role: "assistant", content: "", sources: [], pending: true });
  CHAT_STATE.draft = "";
  CHAT_STATE.streaming = true;
  CHAT_STATE.activeChatId = null;
  _chatSetStatus("Starting local chat…");
  _chatRenderIfVisible();

  var res = await api("start_chat",
    message,
    history,
    CHAT_STATE.selectedModel,
    CHAT_STATE.useIndexedDocuments
  );
  if (!res.ok) {
    if (CHAT_STATE.resetAfterStop) {
      _clearChatSession();
    } else {
      CHAT_STATE.streaming = false;
      var assistant = _chatActiveAssistant();
      if (assistant) {
        assistant.pending = false;
        assistant.content = (res.error && res.error.message) || "Chat could not be started.";
      }
      _chatSetStatus("Chat failed to start.", "alert");
    }
  } else if (res.data) {
    CHAT_STATE.activeChatId = res.data.chat_id;
    if (CHAT_STATE.resetAfterStop) _stopChat();
  }
  _chatRenderIfVisible();
}

async function _stopChat() {
  var chatId = CHAT_STATE.activeChatId;
  if (!CHAT_STATE.streaming || !chatId || CHAT_STATE.stopping) return;
  CHAT_STATE.stopping = true;
  _chatSetStatus("Stopping response…");
  _chatRenderIfVisible();
  var res = await api("cancel_chat", chatId);
  if (!res.ok || (res.data && !res.data.cancelled)) {
    if (CHAT_STATE.resetAfterStop && res.ok) {
      _clearChatSession();
      _chatRenderIfVisible();
      return;
    }
    CHAT_STATE.stopping = false;
    CHAT_STATE.resetAfterStop = false;
    _chatSetStatus("The response could not be stopped.", "alert");
  }
  _chatRenderIfVisible();
}

function _clearChatSession() {
  CHAT_STATE.activeChatId = null;
  CHAT_STATE.streaming = false;
  CHAT_STATE.stopping = false;
  CHAT_STATE.resetAfterStop = false;
  CHAT_STATE.messages = [];
  CHAT_STATE.draft = "";
  _chatSetStatus("New chat started.");
}

async function _newChat() {
  if (CHAT_STATE.streaming) {
    CHAT_STATE.resetAfterStop = true;
    if (CHAT_STATE.activeChatId) {
      await _stopChat();
    } else {
      _chatSetStatus("Stopping response…");
      _chatRenderIfVisible();
    }
    return;
  }
  _clearChatSession();
  _chatRenderIfVisible();
}

function _chatModelControls() {
  var children = [el("label", { for: "chat-model", text: "Chat model" })];
  var options = [];
  var selectDisabled = false;
  if (CHAT_STATE.modelState === "loading" || CHAT_STATE.modelState === "idle") {
    options.push(el("option", { value: "", text: "Loading…" }));
    selectDisabled = true;
    children.push(el("span", { id: "chat-model-note", class: "chat-model-status", text: "Loading local models…", "aria-busy": "true" }));
  } else if (CHAT_STATE.modelState === "unreachable") {
    options.push(el("option", { value: "", text: "Ollama unavailable" }));
    selectDisabled = true;
    children.push(el("span", {
      id: "chat-model-note", class: "chat-model-status error", text: "Ollama is unavailable. " + CHAT_STATE.modelError,
    }));
    children.push(el("button", { type: "button", class: "sm secondary", text: "Retry", onclick: _loadChatModels }));
  } else if (CHAT_STATE.modelState === "empty") {
    options.push(el("option", { value: "", text: "No models installed" }));
    selectDisabled = true;
    children.push(el("span", { id: "chat-model-note", class: "chat-model-status error", text: "No chat models installed in Ollama." }));
    children.push(el("button", { type: "button", class: "sm secondary", text: "Refresh", onclick: _loadChatModels }));
  } else {
    options.push(el("option", { value: "", text: "Choose a model…", disabled: "disabled" }));
    options = options.concat(CHAT_STATE.models.map(function (model) {
      return el("option", { value: model.name, text: _chatModelLabel(model) });
    }));
    children.push(el("span", {
      id: "chat-model-note",
      class: "chat-model-status",
      text: CHAT_STATE.selectedModel ? "Runs locally through Ollama" : "Choose an installed local model to begin.",
    }));
  }
  var select = el("select", { id: "chat-model", "aria-describedby": "chat-model-note" }, options);
  select.value = CHAT_STATE.selectedModel;
  if (selectDisabled) select.setAttribute("disabled", "disabled");
  select.addEventListener("change", function () {
    var previous = CHAT_STATE.selectedModel;
    CHAT_STATE.selectedModel = select.value;
    _chatRenderIfVisible();
    api("set_chat_model", select.value).then(function (res) {
      if (!res.ok) {
        CHAT_STATE.selectedModel = previous;
        _chatRenderIfVisible();
      }
    });
  });
  children.splice(1, 0, select);
  return el("div", { class: "chat-model-row" }, children);
}

function _renderChatView(main) {
  main.replaceChildren();
  var sendDisabled = CHAT_STATE.streaming || CHAT_STATE.modelState !== "ready" || !CHAT_STATE.selectedModel;

  var header = el("header", { class: "chat-header" }, [
    el("div", {}, [
      el("h1", { text: "Chat with your documents" }),
      el("p", { class: "chat-subtitle", text: "Private local chat powered by Ollama and your VectorDrive index." }),
    ]),
    el("button", { type: "button", class: "secondary", text: "New Chat", onclick: _newChat }),
  ]);

  var transcript = el("div", {
    class: "chat-transcript", role: "log", "aria-live": "polite", "aria-relevant": "additions text",
    "aria-label": "Chat conversation",
  });
  if (!CHAT_STATE.messages.length) {
    transcript.appendChild(el("div", { class: "chat-empty" }, [
      el("p", { text: "Ask about your indexed documents or chat directly with a local model.", style: "font-weight:600;margin:0 0 4px" }),
      el("p", { text: "Sources appear beneath answers when indexed-document context is enabled.", style: "margin:0" }),
    ]));
  } else {
    CHAT_STATE.messages.forEach(function (message) { transcript.appendChild(_chatMessage(message)); });
  }

  var prompt = el("textarea", {
    id: "chat-prompt",
    rows: "3",
    placeholder: "Message your local model…",
    "aria-describedby": "chat-prompt-help chat-live-status",
  });
  prompt.value = CHAT_STATE.draft;
  if (sendDisabled) prompt.setAttribute("disabled", "disabled");
  prompt.addEventListener("input", function () { CHAT_STATE.draft = prompt.value; });
  prompt.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      CHAT_STATE.draft = prompt.value;
      _startChatMessage();
    }
  });

  var useIndex = el("input", { id: "chat-use-index", type: "checkbox" });
  useIndex.checked = CHAT_STATE.useIndexedDocuments;
  useIndex.addEventListener("change", function () { CHAT_STATE.useIndexedDocuments = useIndex.checked; });

  var sendButton = el("button", { type: "button", text: "Send", onclick: _startChatMessage });
  if (sendDisabled) sendButton.setAttribute("disabled", "disabled");
  var stopButton = el("button", { type: "button", class: "secondary", text: "Stop", onclick: _stopChat });
  if (!CHAT_STATE.streaming || !CHAT_STATE.activeChatId || CHAT_STATE.stopping) stopButton.setAttribute("disabled", "disabled");

  var status = el("p", {
    id: "chat-live-status",
    class: "chat-live-status",
    role: "status",
    "aria-live": CHAT_STATE.statusKind === "alert" ? "assertive" : "polite",
    text: CHAT_STATE.status,
  });
  if (CHAT_STATE.statusKind === "alert") status.setAttribute("role", "alert");

  var composer = el("form", { class: "chat-composer" }, [
    el("label", { for: "chat-prompt", class: "visually-hidden", text: "Message" }),
    prompt,
    el("p", { id: "chat-prompt-help", class: "visually-hidden", text: "Press Enter to send. Press Shift and Enter for a new line." }),
    el("div", { class: "chat-composer-row" }, [
      el("label", { class: "chat-index-toggle", for: "chat-use-index" }, [
        useIndex,
        document.createTextNode("Use indexed documents"),
      ]),
      el("div", { class: "chat-actions" }, [stopButton, sendButton]),
    ]),
    status,
  ]);
  composer.addEventListener("submit", function (e) { e.preventDefault(); _startChatMessage(); });

  main.appendChild(el("section", { class: "chat-view", "aria-labelledby": "chat-title" }, [
    header,
    _chatModelControls(),
    transcript,
    composer,
  ]));
  header.querySelector("h1").setAttribute("id", "chat-title");

  requestAnimationFrame(function () {
    transcript.scrollTop = transcript.scrollHeight;
  });
}

function renderChat(main) {
  _chatMain = main;
  _renderChatView(main);
  if (CHAT_STATE.modelState === "idle") _loadChatModels();
}

window.addEventListener("vd:event", function (ev) {
  var evt = ev.detail || {};
  if (["chat_started", "chat_sources", "chat_delta", "chat_done", "chat_failed"].indexOf(evt.kind) < 0) return;
  var payload = evt.payload || {};
  var chatId = payload.chat_id || evt.job_id;
  if (CHAT_STATE.activeChatId && chatId !== CHAT_STATE.activeChatId) return;

  var assistant = _chatActiveAssistant();
  if (evt.kind === "chat_started") {
    CHAT_STATE.activeChatId = chatId;
    CHAT_STATE.streaming = true;
    _chatSetStatus("Generating response…");
    if (CHAT_STATE.resetAfterStop) _stopChat();
  } else if (evt.kind === "chat_sources" && assistant) {
    assistant.sources = (payload.sources || []).map(function (source, index) {
      return Object.assign({}, source, { rank: source.rank || index + 1 });
    });
  } else if (evt.kind === "chat_delta" && assistant) {
    assistant.pending = false;
    assistant.content += payload.text || payload.delta || "";
    _chatSetStatus("Generating response…");
  } else if (evt.kind === "chat_done") {
    if (assistant) {
      assistant.pending = false;
      // The final content is authoritative. It repairs any skipped live
      // deltas if the bounded native event ring ever advances past a slow
      // browser poll.
      if (typeof payload.content === "string") assistant.content = payload.content;
    }
    if (CHAT_STATE.resetAfterStop) {
      _clearChatSession();
    } else {
      CHAT_STATE.streaming = false;
      CHAT_STATE.stopping = false;
      CHAT_STATE.activeChatId = null;
      _chatSetStatus(payload.cancelled ? "Response stopped." : "Response complete.");
    }
  } else if (evt.kind === "chat_failed") {
    if (CHAT_STATE.resetAfterStop) {
      _clearChatSession();
      _chatRenderIfVisible();
      return;
    }
    if (assistant) {
      assistant.pending = false;
      if (!assistant.content) assistant.content = payload.error || payload.message || "The local chat request failed.";
    }
    CHAT_STATE.streaming = false;
    CHAT_STATE.stopping = false;
    CHAT_STATE.activeChatId = null;
    _chatSetStatus(payload.error || payload.message || "Chat failed.", "alert");
  }
  _chatRenderIfVisible();
});

window.addEventListener("vd:event-gap", function () {
  var chatId = CHAT_STATE.activeChatId;
  if (!CHAT_STATE.streaming || !chatId) return;
  _chatSetStatus("Some live updates were skipped; reconciling the response…");
  _chatRenderIfVisible();
  api("get_chat", chatId).then(function (res) {
    if (!res.ok || !res.data || chatId !== CHAT_STATE.activeChatId) return;
    var record = res.data;
    var assistant = _chatActiveAssistant();
    if (assistant) {
      if (typeof record.content === "string") assistant.content = record.content;
      assistant.pending = record.status === "running" && !assistant.content;
      assistant.sources = record.sources || assistant.sources;
    }
    if (record.status === "done" || record.status === "cancelled") {
      CHAT_STATE.streaming = false;
      CHAT_STATE.stopping = false;
      CHAT_STATE.activeChatId = null;
      _chatSetStatus(record.cancelled ? "Response stopped." : "Response complete.");
    } else if (record.status === "failed") {
      CHAT_STATE.streaming = false;
      CHAT_STATE.stopping = false;
      CHAT_STATE.activeChatId = null;
      if (assistant && !assistant.content) assistant.content = record.error || "The local chat request failed.";
      _chatSetStatus(record.error || "Chat failed.", "alert");
    }
    _chatRenderIfVisible();
  });
});

VIEW_RENDERERS.chat = renderChat;
