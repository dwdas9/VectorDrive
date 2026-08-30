// Help screen: complete offline searchable documentation.
"use strict";

function _helpSections() {
  return [
    {
      id: "quick-start",
      title: "Quick Start",
      body: "Add a folder via Folders > Add folder and choose a directory containing your documents. VectorDrive scans for supported files and begins indexing automatically. Once indexing completes, switch to the Search view and type a query. Results are ranked by a hybrid of full-text and vector similarity. Ensure Ollama is running at localhost:11434 with the qwen3-embedding:8b model pulled for vector search to work.",
    },
    {
      id: "adding-removing-folders",
      title: "Adding and removing folders",
      body: "Open the Folders view and click Add folder to pick a directory. VectorDrive previews the folder contents before adding. To remove a folder, click Remove on its row and confirm. Removing deletes only VectorDrive's index data (extracted text, chunks, embeddings). Your original files on disk are never modified or deleted. You can re-add a removed folder at any time to rebuild its index from scratch.",
    },
    {
      id: "indexing-verifying-rebuilding",
      title: "Indexing, verifying and rebuilding",
      body: "Click Re-index on a folder row to run an incremental index (only new or changed files are processed; files deleted since the last run are reconciled out of the index automatically). If files previously failed, use Retry Failed Files from the folder's … menu to re-attempt them. To discard a folder's saved index and reprocess every file from scratch, use Full Rebuild… from the same … menu. Indexing progress is shown in the global job status card visible from any screen. You can cancel a running index job at any time without corrupting your database. Use Settings > Database integrity to verify the index is consistent.",
    },
    {
      id: "supported-formats",
      title: "Supported document and image formats",
      body: "VectorDrive indexes PDF, DOCX, plain text (.txt), and Markdown (.md) files. Image files (JPG, PNG) are processed through the OCR pipeline to extract embedded text. PDFs with embedded text are extracted directly; scanned PDFs are routed through OCR. DOCX files are parsed for all text content including headers, footers, and tables. Unsupported file types are skipped and reported in the folder preview.",
    },
    {
      id: "ocr-tiers",
      title: "OCR tiers and escalation",
      body: "VectorDrive uses a tiered OCR pipeline that escalates automatically when a lower tier produces insufficient text. Tier 2 (RapidOCR) is the default fast extractor for clear, well-scanned documents. Tier 3 (PaddleOCR-VL) handles complex layouts, tables, and mixed-language content. Tier 4 (Tesseract) is the fallback for edge cases where other tiers fail. Each tier reports a confidence score; escalation occurs when confidence falls below the threshold. Files that fail all tiers are marked with a warning.",
    },
    {
      id: "hybrid-fts-vector-search",
      title: "Hybrid, FTS and vector search",
      body: "The Search view offers three modes: Hybrid (default), FTS-only, and Vector-only. FTS (full-text search) uses SQLite FTS5 for exact keyword matching with BM25 ranking. Vector search embeds your query via the Ollama model and finds semantically similar chunks. Hybrid mode runs both and merges results using Reciprocal Rank Fusion (RRF). Use FTS when you know the exact terms; use Vector when searching by concept or meaning.",
    },
    {
      id: "search-result-interpretation",
      title: "Search result interpretation",
      body: "Each result shows the source file, page number (for PDFs), and matched chunk text. The score reflects relevance: in FTS mode it is BM25; in Vector mode it is cosine similarity; in Hybrid mode it is the RRF fusion score combining both. Higher scores indicate stronger matches. Results are grouped by chunk, so a single document may appear multiple times if different sections match. Page numbers help you locate the relevant passage in the original file.",
    },
    {
      id: "local-chat",
      title: "Local chat with indexed documents",
      body: "Open Chat, choose a locally installed Ollama chat model, and enter a message. Raw base and fill-in-the-middle completion models are hidden because they do not reliably follow chat roles. Use indexed documents is enabled by default: VectorDrive gives the local model at most four relevant passages and shows their ranked sources beneath the answer. A simple greeting or thanks skips document retrieval automatically; a message that also asks about a document remains grounded. Turn the option off for a direct conversation without document retrieval. Press Enter to send, Shift+Enter for a new line, Stop to cancel generation, or New Chat to clear the in-memory conversation. Chat history is kept only for the current app session.",
    },
    {
      id: "ollama-setup",
      title: "Ollama setup and model troubleshooting",
      body: "VectorDrive requires Ollama running locally at localhost:11434. Install Ollama from ollama.com, then pull the embedding model: ollama pull qwen3-embedding:8b. Verify the server is reachable by visiting localhost:11434 in a browser. If vector search returns no results, check that Ollama is running and the model is pulled. Common issues: Ollama not started after reboot (run 'ollama serve'), model name typo, or insufficient RAM for the model (qwen3-embedding:8b measured approximately 12.2 GB resident in the local VectorDrive pilot).",
    },
    {
      id: "file-permission-problems",
      title: "File permission problems and troubleshooting",
      body: "If files are skipped during indexing with permission errors, ensure your user account has read access to the target directory and files. On macOS, grant Full Disk Access to the VectorDrive app in System Settings > Privacy & Security if indexing folders outside your home directory. Files inside iCloud Drive may fail if not downloaded locally. Network-mounted volumes (SMB, NFS) require the mount to be active during indexing.",
    },
    {
      id: "unsupported-corrupted-files",
      title: "Unsupported and corrupted files",
      body: "Files with unrecognized extensions are skipped during indexing and counted as unsupported in the folder preview. Corrupted files (truncated PDFs, invalid DOCX archives, unreadable images) are attempted but marked as failed if extraction produces no usable text. Check the folder warnings (Show warnings button) for per-file failure reasons. Failed files do not block indexing of other files in the same folder.",
    },
    {
      id: "deleted-file-reconciliation",
      title: "Deleted-file reconciliation",
      body: "When you run an incremental index, VectorDrive detects files that have been deleted from disk since the last index. Their corresponding index entries (text, chunks, embeddings) are automatically removed from the database. This keeps search results accurate and prevents stale matches to documents that no longer exist. Reconciliation runs as part of every incremental index and requires no manual action.",
    },
    {
      id: "mcp-setup",
      title: "MCP setup and available tools",
      body: "VectorDrive provides a read-only MCP (Model Context Protocol) server that Claude Desktop can use to search your indexed documents. Go to the Claude Desktop view and click Preview connection to see the configuration change, then Apply. After restarting Claude Desktop, it can call VectorDrive's tools. Available MCP tools: search_drive (hybrid/FTS/vector query), read_document (a bounded preview, or a single page, of an already-indexed document), and index_status (summarize the latest indexing run and configuration).",
    },
    {
      id: "logs-diagnostics",
      title: "Logs and diagnostics",
      body: "VectorDrive writes structured logs to its data directory. View recent entries via Settings > Show recent logs. The log includes indexing events, OCR tier escalations, errors, and API calls. For deeper debugging, find the full log file path shown in the Settings data location section. If reporting a bug, include the last 50-100 log lines. The database integrity check in Settings verifies SQLite and FTS5 index consistency.",
    },
    {
      id: "keyboard-shortcuts",
      title: "Keyboard shortcuts",
      body: null,
      custom: function () {
        var shortcuts = [
          ["⌘K", "Focus search"],
          ["⌘1–5", "Switch views (Folders, Search, Chat, Overview, Logs)"],
          ["⌘O", "Add folder"],
          ["⌘,", "Settings"],
        ];
        var list = el("div", { style: "margin-top:8px" });
        shortcuts.forEach(function (pair) {
          list.appendChild(el("div", { style: "display:flex;gap:12px;align-items:baseline;margin-bottom:6px" }, [
            el("kbd", { text: pair[0], style: "font-family:system-ui;font-size:13px;background:var(--color-surface);border:1px solid var(--color-border);border-radius:4px;padding:2px 6px;min-width:48px;text-align:center" }),
            el("span", { text: pair[1] }),
          ]));
        });
        return list;
      },
    },
    {
      id: "privacy-local-processing",
      title: "Privacy and local processing",
      body: "VectorDrive processes all documents entirely on your machine. No file content, search queries, or metadata are sent to any remote server. The embedding model runs locally via Ollama. The MCP server only exposes data to Claude Desktop on localhost. The application enforces a strict Content Security Policy with connect-src 'none', making network egress impossible from the UI layer. Your indexed data is stored in a local SQLite database in your user data directory.",
    },
  ];
}

function _renderHelpSection(section, open) {
  var details = el("details", { class: "card", style: "margin-bottom:12px" });
  if (open) details.setAttribute("open", "");

  var summary = el("summary", {
    text: section.title,
    style: "cursor:pointer;font-weight:600;padding:4px 0;user-select:none",
  });
  details.appendChild(summary);

  if (section.custom) {
    details.appendChild(section.custom());
  }
  if (section.body) {
    details.appendChild(el("p", { text: section.body, style: "margin:8px 0 0;line-height:1.5" }));
  }
  return details;
}

async function renderHelp(main) {
  main.appendChild(el("h1", { text: "Help" }));

  var searchInput = el("input", {
    id: "help-filter-input",
    type: "text",
    placeholder: "Filter help topics…",
    "aria-label": "Filter help topics",
    style: "width:100%;padding:8px 12px;border:1px solid var(--color-border);border-radius:6px;font-size:14px;background:var(--color-surface);color:var(--color-text);margin-bottom:16px;box-sizing:border-box",
  });
  main.appendChild(searchInput);

  var sectionsContainer = el("div", {});
  main.appendChild(sectionsContainer);

  var sections = _helpSections();

  function renderAll(filter) {
    sectionsContainer.innerHTML = "";
    var query = (filter || "").toLowerCase().trim();
    var hasResults = false;

    sections.forEach(function (section) {
      if (query) {
        var haystack = (section.title + " " + (section.body || "")).toLowerCase();
        if (haystack.indexOf(query) === -1) return;
      }
      hasResults = true;
      sectionsContainer.appendChild(_renderHelpSection(section, !!query));
    });

    if (!hasResults) {
      sectionsContainer.appendChild(el("p", {
        text: "No matching topics found.",
        style: "color:var(--color-text-muted);margin-top:12px",
      }));
    }
  }

  searchInput.addEventListener("input", function () {
    renderAll(searchInput.value);
  });

  renderAll("");

  // Version footer
  var versionEl = el("p", {
    text: "Loading version…",
    style: "margin-top:24px;font-size:12px;color:var(--color-text-muted);text-align:center",
  });
  main.appendChild(versionEl);

  var infoRes = await api("get_app_info");
  if (infoRes.ok && infoRes.data) {
    versionEl.textContent = "VectorDrive v" + (infoRes.data.version || "unknown");
  } else {
    versionEl.textContent = "VectorDrive (version unavailable)";
  }
}

VIEW_RENDERERS.help = renderHelp;
