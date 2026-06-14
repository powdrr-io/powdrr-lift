from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from powdrr_lift.core.blame_view import (
    blame_file_view_to_data,
    blame_view_state_to_data,
    build_blame_view_state,
)
from powdrr_lift.core.code_index import _current_branch
from powdrr_lift.core.pr_analysis import resolve_default_branch, resolve_repo_root


def build_server(
    repo_root: str | Path | None = None,
    branch_name: str | None = None,
    parent_branch: str | None = None,
    selected_file: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    repo_root_path = resolve_repo_root(repo_root)
    resolved_branch = branch_name or _current_branch(repo_root_path)
    resolved_parent = parent_branch or resolve_default_branch(repo_root_path)

    class _Handler(BaseHTTPRequestHandler):
        server_version = "powdrr-lift-blame-ui/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._serve_html()
                return

            if parsed.path == "/api/bootstrap":
                query = parse_qs(parsed.query)
                selected_file_query = _query_first(query, "file") or selected_file
                state = build_blame_view_state(
                    repo_root=repo_root_path,
                    branch_name=resolved_branch,
                    parent_branch=resolved_parent,
                    selected_file=selected_file_query,
                )
                self._serve_json(blame_view_state_to_data(state))
                return

            if parsed.path == "/api/file":
                query = parse_qs(parsed.query)
                file_path = _query_first(query, "path")
                if file_path is None:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "Missing `path` query parameter.",
                    )
                    return

                state = build_blame_view_state(
                    repo_root=repo_root_path,
                    branch_name=resolved_branch,
                    parent_branch=resolved_parent,
                    selected_file=file_path,
                )
                if state.selected_file != file_path:
                    self._send_error_json(
                        HTTPStatus.NOT_FOUND,
                        f"No tracked file matched {file_path!r}.",
                    )
                    return
                if state.file_view is None:
                    self._send_error_json(
                        HTTPStatus.NOT_FOUND,
                        f"No tracked file matched {file_path!r}.",
                    )
                    return

                self._serve_json(
                    {
                        "branch_name": state.branch_name,
                        "selected_file": state.selected_file,
                        "file_view": blame_file_view_to_data(state.file_view),
                    }
                )
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown route.")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

        def _serve_html(self) -> None:
            body = _render_html()
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _serve_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), _Handler)
    server.repo_root = repo_root_path  # type: ignore[attr-defined]
    server.branch_name = resolved_branch  # type: ignore[attr-defined]
    server.parent_branch = resolved_parent  # type: ignore[attr-defined]
    return server


def serve(
    repo_root: str | Path | None = None,
    branch_name: str | None = None,
    parent_branch: str | None = None,
    selected_file: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    server = build_server(
        repo_root=repo_root,
        branch_name=branch_name,
        parent_branch=parent_branch,
        selected_file=selected_file,
        host=host,
        port=port,
    )
    print(f"Serving powdrr-lift blame UI at http://{host}:{port}")
    server.serve_forever()


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None

    return values[0]


def _render_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>powdrr-lift blame UI</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f5f7fb;
        --panel: #ffffff;
        --panel-muted: #f0f4fa;
        --border: #d6deea;
        --text: #122033;
        --muted: #5d6b82;
        --accent: #2156e6;
        --accent-soft: rgba(33, 86, 230, 0.12);
        --code: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        --sans: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, #f8fbff 0%, var(--bg) 100%);
        color: var(--text);
        font-family: var(--sans);
      }
      .shell {
        display: grid;
        grid-template-rows: 60px 1fr;
        height: 100vh;
      }
      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 0 20px;
        background: rgba(255,255,255,0.78);
        backdrop-filter: blur(12px);
        border-bottom: 1px solid var(--border);
      }
      .brand {
        font-size: 14px;
        font-weight: 700;
        letter-spacing: 0.03em;
        text-transform: uppercase;
      }
      .meta {
        display: flex;
        gap: 12px;
        color: var(--muted);
        font-size: 12px;
      }
      .workspace {
        display: grid;
        grid-template-columns:
          minmax(220px, var(--tree-width, 280px))
          8px
          minmax(360px, var(--code-width, 1fr))
          8px
          minmax(280px, var(--context-width, 360px));
        min-height: 0;
      }
      .pane {
        min-height: 0;
        border-right: 1px solid var(--border);
        background: rgba(255,255,255,0.6);
      }
      .pane:last-child { border-right: none; }
      .splitter {
        position: relative;
        z-index: 3;
        cursor: col-resize;
        background:
          linear-gradient(
            180deg,
            rgba(21, 41, 71, 0.08),
            rgba(21, 41, 71, 0.03)
          );
        border-right: 1px solid rgba(18, 32, 51, 0.08);
        border-left: 1px solid rgba(18, 32, 51, 0.08);
        touch-action: none;
      }
      .splitter::before {
        content: "";
        position: absolute;
        left: 50%;
        top: 50%;
        width: 2px;
        height: 34px;
        transform: translate(-50%, -50%);
        border-radius: 999px;
        background: rgba(97, 111, 137, 0.55);
        box-shadow:
          -5px 0 0 rgba(97, 111, 137, 0.22),
          5px 0 0 rgba(97, 111, 137, 0.22);
      }
      .splitter:hover::before,
      .splitter.dragging::before {
        background: var(--accent);
        box-shadow:
          -5px 0 0 rgba(33, 86, 230, 0.22),
          5px 0 0 rgba(33, 86, 230, 0.22);
      }
      .pane-header {
        position: sticky;
        top: 0;
        z-index: 2;
        padding: 12px 16px;
        background: rgba(245,247,251,0.96);
        border-bottom: 1px solid var(--border);
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--muted);
      }
      .tree-list {
        padding: 12px;
        overflow: auto;
        height: calc(100vh - 60px);
      }
      .tree-branch {
        display: grid;
        gap: 2px;
      }
      .tree-row {
        padding-left: calc(8px + (var(--depth, 0) * 8px));
      }
      .tree-node {
        margin: 0;
        padding: 0;
        list-style: none;
      }
      .tree-folder > summary {
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 8px;
        border-radius: 8px;
        color: var(--muted);
      }
      .tree-folder[open] > summary {
        background: var(--panel-muted);
        color: var(--text);
      }
      .tree-folder summary::before,
      .tree-file::before {
        content: "";
        width: 10px;
        flex: none;
      }
      .tree-folder summary::before {
        content: "▸";
        color: var(--muted);
        font-size: 11px;
        transform: translateY(-1px);
      }
      .tree-folder[open] > summary::before {
        content: "▾";
      }
      .tree-children {
        margin-left: 6px;
        padding-left: 6px;
        border-left: 1px solid rgba(93, 107, 130, 0.18);
      }
      .tree-file {
        display: block;
        width: 100%;
        border: 0;
        border-radius: 8px;
        background: transparent;
        color: var(--text);
        text-align: left;
        padding: 6px 10px;
        font: inherit;
        cursor: pointer;
      }
      .tree-file:hover,
      .tree-file.active {
        background: var(--accent-soft);
        color: var(--accent);
      }
      .blame-pane {
        overflow: auto;
        height: calc(100vh - 60px);
        padding: 0 0 40px;
      }
      .file-header {
        position: sticky;
        top: 0;
        z-index: 2;
        padding: 12px 16px;
        background: rgba(245,247,251,0.96);
        border-bottom: 1px solid var(--border);
      }
      .file-path {
        font-size: 16px;
        font-weight: 700;
      }
      .file-subtitle {
        margin-top: 4px;
        color: var(--muted);
        font-size: 12px;
      }
      .chunk {
        display: grid;
        grid-template-columns: 240px minmax(0, 1fr);
        border-bottom: 1px solid var(--border);
        cursor: pointer;
      }
      .chunk:hover { background: rgba(33,86,230,0.03); }
      .chunk.selected { background: rgba(33,86,230,0.08); }
      .chunk-meta {
        padding: 14px 14px 14px 16px;
        border-right: 1px solid var(--border);
        background: rgba(255,255,255,0.88);
      }
      .chunk-badge {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 4px 8px;
        margin-bottom: 8px;
        border-radius: 999px;
        background: var(--accent-soft);
        color: var(--accent);
        font-size: 12px;
        font-weight: 700;
      }
      .chunk-summary {
        font-size: 13px;
        font-weight: 600;
        line-height: 1.35;
      }
      .chunk-range {
        margin-top: 8px;
        color: var(--muted);
        font-size: 12px;
      }
      .chunk-lines {
        padding: 10px 0;
        background: white;
      }
      .line {
        display: grid;
        grid-template-columns: 72px minmax(0, 1fr);
        gap: 12px;
        padding: 0 16px;
        min-height: 24px;
        align-items: start;
        font-family: var(--code);
        font-size: 13px;
        line-height: 1.45;
      }
      .line:hover { background: #f8fbff; }
      .line-number {
        color: #7b8798;
        text-align: right;
        user-select: none;
      }
      .line-code {
        white-space: pre-wrap;
        word-break: break-word;
      }
      .details {
        height: calc(100vh - 60px);
        overflow: auto;
      }
      .details-body {
        padding: 16px;
        display: grid;
        gap: 12px;
      }
      .detail-card {
        padding: 14px;
        border: 1px solid var(--border);
        border-radius: 14px;
        background: var(--panel);
      }
      .detail-label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
        margin-bottom: 6px;
      }
      .detail-title {
        font-size: 18px;
        font-weight: 800;
        line-height: 1.25;
        margin: 0 0 8px;
      }
      .detail-text {
        white-space: pre-wrap;
        line-height: 1.55;
      }
      .detail-grid {
        display: grid;
        grid-template-columns: 110px 1fr;
        gap: 8px 12px;
        font-size: 13px;
      }
      .detail-key {
        color: var(--muted);
      }
      .empty-state {
        padding: 20px;
        color: var(--muted);
      }
      .pill {
        display: inline-flex;
        align-items: center;
        padding: 4px 8px;
        border-radius: 999px;
        background: var(--panel-muted);
        color: var(--muted);
        font-size: 12px;
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <header class="topbar">
        <div>
          <div class="brand">powdrr-lift blame UI</div>
          <div class="meta" id="meta"></div>
        </div>
        <div class="pill">index-backed · local only after refresh</div>
      </header>
      <div class="workspace">
        <aside class="pane">
          <div class="pane-header">Files</div>
          <div class="tree-list" id="tree"></div>
        </aside>
        <div class="splitter" data-splitter="tree" aria-hidden="true"></div>
        <main class="pane">
          <div class="pane-header">CODE</div>
          <div class="blame-pane" id="blame"></div>
        </main>
        <div class="splitter" data-splitter="code" aria-hidden="true"></div>
        <aside class="pane">
          <div class="pane-header">Context</div>
          <div class="details" id="details"></div>
        </aside>
      </div>
    </div>
    <script>
      const state = {
        app: null,
        currentFileView: null,
        selectedChunkRef: null,
        treeWidth: 280,
        codeWidth: null,
        contextWidth: 360,
      };

      async function bootstrap() {
        const url = new URL(window.location.href);
        const selectedFile = url.searchParams.get("file");
        const response = await fetch(
          `/api/bootstrap${
            selectedFile ? `?file=${encodeURIComponent(selectedFile)}` : ""
          }`
        );
        state.app = await response.json();
        applyColumnWidths();
        installSplitters();
        renderMeta();
        renderTree();
        renderFileView(state.app.file_view);
      }

      function applyColumnWidths() {
        const workspace = document.querySelector(".workspace");
        if (!workspace) return;
        workspace.style.setProperty("--tree-width", `${state.treeWidth}px`);
        workspace.style.setProperty("--context-width", `${state.contextWidth}px`);
        if (state.codeWidth != null) {
          workspace.style.setProperty("--code-width", `${state.codeWidth}px`);
        } else {
          workspace.style.removeProperty("--code-width");
        }
      }

      function installSplitters() {
        const splitterMap = {
          tree: {
            element: document.querySelector('[data-splitter="tree"]'),
            left: "treeWidth",
            right: "codeWidth",
            minLeft: 220,
            minRight: 360,
          },
          code: {
            element: document.querySelector('[data-splitter="code"]'),
            left: "codeWidth",
            right: "contextWidth",
            minLeft: 360,
            minRight: 280,
          },
        };

        for (const config of Object.values(splitterMap)) {
          if (!config.element) continue;
          config.element.addEventListener("pointerdown", (event) => {
            event.preventDefault();
            const workspace = document.querySelector(".workspace");
            if (!workspace) return;
            const rect = workspace.getBoundingClientRect();
            const leftPane = config.element.previousElementSibling;
            const rightPane = config.element.nextElementSibling;
            if (!leftPane || !rightPane) return;

            const initialLeft = measurePaneWidth(leftPane);
            const initialRight = measurePaneWidth(rightPane);
            const startX = event.clientX;
            config.element.classList.add("dragging");
            config.element.setPointerCapture(event.pointerId);

            const onMove = (moveEvent) => {
              const delta = moveEvent.clientX - startX;
              const total = initialLeft + initialRight;
              const maxLeft = total - config.minRight;
              const nextLeft = clamp(initialLeft + delta, config.minLeft, maxLeft);
              const nextRight = total - nextLeft;
              state[config.left] = nextLeft;
              state[config.right] = nextRight;
              applyColumnWidths();
            };

            const onUp = () => {
              config.element.classList.remove("dragging");
              config.element.removeEventListener("pointermove", onMove);
              config.element.removeEventListener("pointerup", onUp);
              config.element.removeEventListener("pointercancel", onUp);
            };

            config.element.addEventListener("pointermove", onMove);
            config.element.addEventListener("pointerup", onUp, { once: true });
            config.element.addEventListener("pointercancel", onUp, { once: true });
          });
        }
      }

      function measurePaneWidth(element) {
        return Math.round(element.getBoundingClientRect().width);
      }

      function clamp(value, min, max) {
        return Math.min(Math.max(value, min), max);
      }

      function renderMeta() {
        const meta = document.getElementById("meta");
        meta.textContent = (
          `${state.app.branch_name} → ${state.app.parent_branch} · `
          + `${state.app.files.length} files`
        );
      }

      function renderTree() {
        const root = document.getElementById("tree");
        root.innerHTML = "";
        const list = document.createElement("div");
        list.appendChild(renderTreeNodes(state.app.tree, 0));
        root.appendChild(list);
      }

      function renderTreeNodes(nodes, depth) {
        const container = document.createElement("div");
        container.className = "tree-branch";
        for (const node of nodes) {
          const row = document.createElement("div");
          row.className = "tree-row";
          row.style.setProperty("--depth", depth.toString());
          if (node.kind === "dir") {
            const details = document.createElement("details");
            details.open = depth < 2;
            details.className = "tree-node tree-folder";
            const summary = document.createElement("summary");
            summary.textContent = node.name;
            details.appendChild(summary);
            const children = document.createElement("div");
            children.className = "tree-children";
            children.appendChild(renderTreeNodes(node.children, depth + 1));
            details.appendChild(children);
            row.appendChild(details);
          } else {
            const button = document.createElement("button");
            button.className =
              "tree-file" + (node.path === state.app.selected_file ? " active" : "");
            button.textContent = node.name;
            button.dataset.path = node.path;
            button.addEventListener("click", () => loadFile(node.path));
            row.appendChild(button);
          }
          container.appendChild(row);
        }
        return container;
      }

      async function loadFile(path) {
        const response = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(payload.error || `Failed to load ${path}`);
        }
        const payload = await response.json();
        state.app.selected_file = payload.selected_file;
        state.currentFileView = payload.file_view;
        state.selectedChunkRef = payload.file_view.selected_chunk_ref;
        renderTree();
        renderFileView(payload.file_view);
      }

      function renderFileView(fileView) {
        const blame = document.getElementById("blame");
        blame.innerHTML = "";
        if (!fileView) {
          blame.innerHTML = '<div class="empty-state">No tracked file selected.</div>';
          renderDetails(null);
          return;
        }

        const header = document.createElement("div");
        header.className = "file-header";
        header.innerHTML = `
          <div class="file-path">${escapeHtml(fileView.path)}</div>
          <div class="file-subtitle">
            ${fileView.line_count} lines · ${fileView.chunks.length} blame blocks
          </div>
        `;
        blame.appendChild(header);

        for (const chunk of fileView.chunks) {
          const chunkEl = document.createElement("section");
          chunkEl.className =
            "chunk" +
            (chunk.provenance_ref === state.selectedChunkRef ? " selected" : "");
          chunkEl.dataset.ref = chunk.provenance_ref ?? "";
          chunkEl.addEventListener(
            "click",
            () => selectChunk(fileView, chunk.provenance_ref),
          );

          const meta = document.createElement("div");
          meta.className = "chunk-meta";
          const provenance = findProvenance(fileView, chunk.provenance_ref);
          meta.innerHTML = `
            <div class="chunk-badge">
              ${provenance ? `PR #${provenance.pr_number ?? "?"}` : "Unattributed"}
            </div>
            <div class="chunk-summary">
              ${escapeHtml(
                provenance
                  ? provenance.summary || provenance.title || "Change"
                  : "No provenance recorded",
              )}
            </div>
            <div class="chunk-range">Lines ${chunk.start_line}–${chunk.end_line}</div>
          `;

          const lines = document.createElement("div");
          lines.className = "chunk-lines";
          for (const line of chunk.lines) {
            const row = document.createElement("div");
            row.className = "line";
            row.innerHTML = `
              <div class="line-number">${line.line_number}</div>
              <div class="line-code">${escapeHtml(line.text)}</div>
            `;
            lines.appendChild(row);
          }

          chunkEl.appendChild(meta);
          chunkEl.appendChild(lines);
          blame.appendChild(chunkEl);
        }

        renderDetails(findProvenance(fileView, state.selectedChunkRef));
      }

      function selectChunk(fileView, provenanceRef) {
        state.selectedChunkRef = provenanceRef;
        renderFileView(fileView);
      }

      function findProvenance(fileView, provenanceRef) {
        if (provenanceRef == null) return null;
        return (
          fileView.provenances.find((entry) => entry.ref === provenanceRef) || null
        );
      }

      function renderDetails(provenance) {
        const details = document.getElementById("details");
        if (!provenance) {
          details.innerHTML = (
            '<div class="details-body"><div class="empty-state">'
            + "Select a blame block to inspect its PR intent and rationale."
            + "</div></div>"
          );
          return;
        }

        details.innerHTML = `
          <div class="details-body">
            <div class="detail-card">
              <div class="detail-label">Change</div>
              <h2 class="detail-title">
                ${escapeHtml(provenance.title || provenance.summary || "Change")}
              </h2>
              <div class="detail-grid">
                <div class="detail-key">PR</div>
                <div>#${provenance.pr_number ?? "?"}</div>
                <div class="detail-key">Kind</div>
                <div>${escapeHtml(kindLabel(provenance.kind))}</div>
                <div class="detail-key">File</div>
                <div>${escapeHtml(provenance.file_path || "")}</div>
                <div class="detail-key">Source span</div>
                <div>
                  ${provenance.span_start ?? "?"}–${provenance.span_end ?? "?"}
                </div>
              </div>
              <div class="detail-text" style="margin-top: 10px; color: var(--muted);">
                The span is the source-file line range tied to this provenance.
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-label">Intent</div>
              <div class="detail-text">
                ${escapeHtml(
                  provenance.intent_problem || "No problem statement recorded.",
                )}
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-label">Goal</div>
              <div class="detail-text">
                ${escapeHtml(provenance.intent_goal || "No goal recorded.")}
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-label">Summary</div>
              <div class="detail-text">
                ${escapeHtml(provenance.summary || "No summary recorded.")}
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-label">Justification</div>
              <div class="detail-text">
                ${escapeHtml(provenance.rationale || "No rationale recorded.")}
              </div>
            </div>
            <div class="detail-card">
              <div class="detail-label">Entities</div>
              <div class="detail-text">
                ${
                  provenance.affects && provenance.affects.length
                    ? escapeHtml(provenance.affects.join(", "))
                    : "No entities recorded."
                }
              </div>
            </div>
          </div>
        `;
      }

      function escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
      }

      function kindLabel(kind) {
        if (kind === "declared") {
          return "declared: explicit changelog entry";
        }
        if (kind === "artifact") {
          return "artifact: PR changelog file itself";
        }
        if (kind === "implicit") {
          return "implicit: inferred from the diff";
        }
        if (kind === "commented") {
          return "commented: derived from the commit message body";
        }
        return kind;
      }

      bootstrap().catch((error) => {
        document.body.innerHTML = `
          <pre style="padding:20px;color:#b42318;">
            ${escapeHtml(error.stack || error.message)}
          </pre>
        `;
      });
    </script>
  </body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="powdrr-lift-blame-ui")
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root to use when building the blame index.",
    )
    parser.add_argument(
        "--branch-name",
        help="Branch to inspect. Defaults to the current branch.",
    )
    parser.add_argument(
        "--parent-branch",
        help="Reference parent branch. Defaults to the repository default branch.",
    )
    parser.add_argument(
        "--file",
        dest="selected_file",
        help="Initial file to show in the blame UI.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the UI server to.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the UI server to.",
    )
    args = parser.parse_args(argv)

    serve(
        repo_root=args.repo_root,
        branch_name=args.branch_name,
        parent_branch=args.parent_branch,
        host=args.host,
        port=args.port,
    )
    return 0
