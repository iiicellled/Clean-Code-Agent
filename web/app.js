const { createApp, nextTick, markRaw } = Vue;

const CODE_BLOCK_RE = /```([^`\n]*)\n([\s\S]*?)```/g;
const LANGUAGE_EXTENSIONS = {
  python: "py",
  py: "py",
  javascript: "js",
  js: "js",
  typescript: "ts",
  ts: "ts",
  json: "json",
  html: "html",
  css: "css",
  bash: "sh",
  shell: "sh",
  sql: "sql",
  text: "txt",
};
const MONACO_LANGUAGE_ALIASES = {
  py: "python",
  python: "python",
  js: "javascript",
  javascript: "javascript",
  jsx: "javascript",
  ts: "typescript",
  typescript: "typescript",
  tsx: "typescript",
  json: "json",
  html: "html",
  css: "css",
  scss: "scss",
  less: "less",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  md: "markdown",
  markdown: "markdown",
  sql: "sql",
  sh: "shell",
  bash: "shell",
  shell: "shell",
  java: "java",
  c: "c",
  cpp: "cpp",
  cs: "csharp",
  go: "go",
  rs: "rust",
  rust: "rust",
  php: "php",
  rb: "ruby",
  ruby: "ruby",
  swift: "swift",
  kt: "kotlin",
  kotlin: "kotlin",
  txt: "plaintext",
  text: "plaintext",
};
const MONACO_THEMES = {
  auto: "Auto",
  "clean-dark": "Clean Dark",
  "github-light": "GitHub Light",
  "vs-dark": "VS Dark",
  vs: "VS Light",
};
let customMonacoThemesDefined = false;
let monacoPromise = null;

const LOCAL_TEXT_FILE_EXTENSIONS = new Set([
  "bat", "c", "cfg", "cpp", "cs", "css", "env", "go", "h", "html", "ini", "java", "js", "json", "jsx",
  "kt", "less", "md", "php", "py", "rb", "rs", "scss", "sh", "sql", "ts", "tsx", "txt", "vue", "xml", "yaml", "yml"
]);
const LOCAL_DIRECTORY_SKIP_NAMES = new Set([".git", ".idea", ".vscode", "__pycache__", "node_modules", "dist", "build", "venv", ".venv"]);
const LOCAL_FILE_SCAN_LIMIT = 300;

function normalizeLatexBlockMath(content) {
  return content.replace(/(\$\$|\\\[)([\s\S]*?)(\$\$|\\\])/g, (match, open, body, close) => {
    const normalizedBody = body.replace(/(^|[^\\])\\\s*$/gm, "$1\\\\");
    return `${open}${normalizedBody}${close}`;
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMarkdown(content) {
  if (!window.marked || !window.DOMPurify) {
    return `<p>${escapeHtml(content).replaceAll("\n", "<br>")}</p>`;
  }
  marked.setOptions({ breaks: true, gfm: true });
  const raw = marked.parse(normalizeLatexBlockMath(content || ""));
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true }, ADD_ATTR: ["class"] });
}

function languageLabelForCodeBlock(code) {
  const className = String(code.className || "");
  const match = className.match(/(?:^|\s)language-([^\s]+)/) || className.match(/(?:^|\s)lang-([^\s]+)/);
  return (match?.[1] || "text").replace(/^plaintext$/, "text");
}

function enhanceCodeBlocks(root = document) {
  root.querySelectorAll(".markdown-body pre").forEach((pre) => {
    if (pre.closest(".code-block-shell")) return;
    const code = pre.querySelector("code");
    if (!code) return;
    const shell = document.createElement("div");
    shell.className = "code-block-shell";
    const header = document.createElement("div");
    header.className = "code-block-header";
    const language = document.createElement("span");
    language.className = "code-block-language";
    language.textContent = languageLabelForCodeBlock(code);
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "code-block-copy";
    copyButton.innerHTML = '<i class="fa-regular fa-copy"></i>';
    copyButton.addEventListener("click", async () => {
      await navigator.clipboard.writeText(code.innerText || code.textContent || "");
      copyButton.innerHTML = '<i class="fa-solid fa-copy"></i>';
      window.setTimeout(() => {
        copyButton.innerHTML = '<i class="fa-regular fa-copy"></i>';
      }, 1200);
    });
    header.append(language, copyButton);
    pre.parentNode.insertBefore(shell, pre);
    shell.append(header, pre);
  });
}

function highlightCodeBlocks(root = document) {
  if (window.hljs) {
    root.querySelectorAll("pre code:not(.hljs)").forEach((block) => {
      window.hljs.highlightElement(block);
    });
  }
  enhanceCodeBlocks(root);
}
function stripCodeBlocks(content) {
  return String(content || "").replace(CODE_BLOCK_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

function parseCodeBlocks(content) {
  const files = [];
  let match;
  let index = 1;
  CODE_BLOCK_RE.lastIndex = 0;
  while ((match = CODE_BLOCK_RE.exec(content || ""))) {
    const info = (match[1] || "").trim();
    const code = (match[2] || "").replace(/^\n+|\n+$/g, "");
    if (!code.trim()) continue;
    const tokens = info.split(/\s+/).filter(Boolean);
    const language = (tokens[0] || "text").toLowerCase();
    const explicitPath = tokens.slice(1).find((token) => token.includes(".") || token.includes("/") || token.includes("\\"));
    const extension = LANGUAGE_EXTENSIONS[language] || "txt";
    files.push({
      path: explicitPath || `generated_${index}.${extension}`,
      language,
      content: code,
    });
    index += 1;
  }
  return files;
}

function parseSseEvents(buffer) {
  const events = [];
  let boundary;
  while ((boundary = buffer.indexOf("\n\n")) >= 0) {
    const rawEvent = buffer.slice(0, boundary);
    buffer = buffer.slice(boundary + 2);
    const dataLines = rawEvent
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());
    if (dataLines.length > 0) events.push(dataLines.join("\n"));
  }
  return { events, buffer };
}

function normalizeWorkspace(workspace) {
  const files = Array.isArray(workspace?.files) ? workspace.files : [];
  const normalized = files.map((file, index) => ({
    path: file.path || file.name || `generated_${index + 1}.txt`,
    language: file.language || "text",
    content: file.content || "",
  }));
  return {
    files: normalized,
    active_file: workspace?.active_file || normalized[0]?.path || null,
  };
}

function languageFromFile(file) {
  if (!file) return "plaintext";
  const explicit = String(file.language || "").toLowerCase();
  if (MONACO_LANGUAGE_ALIASES[explicit]) return MONACO_LANGUAGE_ALIASES[explicit];
  const suffix = String(file.path || "").split(".").pop()?.toLowerCase() || "text";
  return MONACO_LANGUAGE_ALIASES[suffix] || "plaintext";
}

function isLocalTextFilePath(path) {
  const name = String(path || "").split("/").pop() || "";
  if ([".env", ".gitignore"].includes(name)) return true;
  const suffix = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
  return LOCAL_TEXT_FILE_EXTENSIONS.has(suffix);
}

function languageFromPath(path) {
  return languageFromFile({ path, language: "" });
}

async function collectLocalTextFiles(directoryHandle, prefix = "", result = []) {
  if (result.length >= LOCAL_FILE_SCAN_LIMIT) return result;
  const entries = [];
  for await (const [name, handle] of directoryHandle.entries()) {
    entries.push([name, handle]);
  }
  entries.sort(([aName, aHandle], [bName, bHandle]) => {
    if (aHandle.kind !== bHandle.kind) return aHandle.kind === "directory" ? -1 : 1;
    return aName.localeCompare(bName);
  });
  for (const [name, handle] of entries) {
    if (result.length >= LOCAL_FILE_SCAN_LIMIT) break;
    const path = prefix ? `${prefix}/${name}` : name;
    if (handle.kind === "directory") {
      if (!LOCAL_DIRECTORY_SKIP_NAMES.has(name)) await collectLocalTextFiles(handle, path, result);
    } else if (isLocalTextFilePath(path)) {
      result.push({ path, name });
    }
  }
  return result;
}

async function getFileHandleByPath(directoryHandle, path) {
  const parts = String(path || "").split("/").filter(Boolean);
  if (!parts.length) throw new Error("No file selected");
  let current = directoryHandle;
  for (const part of parts.slice(0, -1)) {
    current = await current.getDirectoryHandle(part);
  }
  return current.getFileHandle(parts.at(-1));
}
function resolveMonacoTheme(theme) {
  if (theme && theme !== "auto") return theme;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "clean-dark" : "github-light";
}

function defineCustomMonacoThemes(monaco) {
  if (customMonacoThemesDefined || !monaco?.editor) return;
  monaco.editor.defineTheme("clean-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "8FA3A3", fontStyle: "italic" },
      { token: "keyword", foreground: "7DD3FC", fontStyle: "bold" },
      { token: "string", foreground: "A7F3D0" },
      { token: "number", foreground: "FDE68A" },
      { token: "type", foreground: "C4B5FD" },
      { token: "function", foreground: "F9A8D4" },
      { token: "variable", foreground: "E5E7EB" },
    ],
    colors: {
      "editor.background": "#0B1117",
      "editor.foreground": "#E6EDF3",
      "editorLineNumber.foreground": "#53606D",
      "editorLineNumber.activeForeground": "#A7F3D0",
      "editorCursor.foreground": "#34D399",
      "editor.selectionBackground": "#1F6F5B66",
      "editor.lineHighlightBackground": "#13202A",
      "editorGutter.background": "#0B1117",
    },
  });
  monaco.editor.defineTheme("github-light", {
    base: "vs",
    inherit: true,
    rules: [
      { token: "comment", foreground: "6A737D", fontStyle: "italic" },
      { token: "keyword", foreground: "D73A49", fontStyle: "bold" },
      { token: "string", foreground: "032F62" },
      { token: "number", foreground: "005CC5" },
      { token: "type", foreground: "6F42C1" },
      { token: "function", foreground: "6F42C1" },
      { token: "variable", foreground: "24292E" },
    ],
    colors: {
      "editor.background": "#FFFFFF",
      "editor.foreground": "#24292E",
      "editorLineNumber.foreground": "#959DA5",
      "editorLineNumber.activeForeground": "#24292E",
      "editorCursor.foreground": "#0969DA",
      "editor.selectionBackground": "#C8E1FF",
      "editor.lineHighlightBackground": "#F6F8FA",
      "editorGutter.background": "#FFFFFF",
    },
  });
  customMonacoThemesDefined = true;
}

function loadMonacoEditor() {
  if (window.monaco?.editor) return Promise.resolve(window.monaco);
  if (monacoPromise) return monacoPromise;
  monacoPromise = new Promise((resolve, reject) => {
    const start = () => {
      if (!window.require) {
        reject(new Error("Monaco editor loader is unavailable"));
        return;
      }
      window.require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs" } });
      window.require(["vs/editor/editor.main"], () => resolve(window.monaco), reject);
    };
    if (window.require) {
      start();
      return;
    }
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js";
    script.onload = start;
    script.onerror = () => reject(new Error("Failed to load Monaco editor"));
    document.head.appendChild(script);
  });
  return monacoPromise;
}

createApp({
  data() {
    return {
      busy: false,
      running: false,
      runOutput: "",
      prompt: "",
      modelStatus: "Connecting to models...",
      historyEnabled: false,
      historyError: "",
      currentConversationId: null,
      conversations: [],
      messages: [
        {
          localId: `welcome-${Date.now()}`,
          role: "assistant",
          content: "Clean Code Agent is ready. Open a local project file and ask for a code change; I will propose a function-level patch you can apply.",
        },
      ],
      workspace: normalizeWorkspace(null),
      codeEditor: null,
      codeEditorModel: null,
      codeEditorResizeObserver: null,
      suppressEditorChange: false,
      monacoLoadError: "",
      codeTheme: localStorage.getItem("coderAgent.codeTheme") || "auto",
      workspaceRoot: "",
      workspaceStatus: "",
      workspaceEntries: [],
      selectedWorkspaceFile: "",
      workspaceDirectoryHandle: null,
      fileHandlesByPath: markRaw(new Map()),
      openingWorkspace: false,
      openingFile: false,
      savingFile: false,
      patchProposal: null,
      patchStatus: "",
      supportsLocalDirectoryPicker: !!window.showDirectoryPicker,
    };
  },
  computed: {
    activeFile() {
      return this.workspace.files.find((file) => file.path === this.workspace.active_file) || null;
    },
    canRunActiveFile() {
      return ["python", "py"].includes((this.activeFile?.language || "").toLowerCase()) && !!this.activeFile?.content.trim();
    },
  },
  watch: {
    "workspace.active_file"() {
      this.syncEditorToActiveFile();
    },
    codeTheme() {
      localStorage.setItem("coderAgent.codeTheme", this.codeTheme);
      this.applyMonacoTheme();
    },
  },
  methods: {
    formatTime(value) {
      return value ? new Date(value).toLocaleString() : "";
    },
    renderAssistantContent(content) {
      return renderMarkdown(content || "");
    },
    scrollMessages() {
      nextTick(() => {
        const el = this.$refs.messagesEl;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    setWorkspace(workspace) {
      const next = normalizeWorkspace(workspace);
      this.workspace = next;
      this.runOutput = "";
      if (next.files.length) {
        this.syncEditorToActiveFile();
      } else {
        this.disposeMonacoEditor();
      }
    },
    updateActiveFile(content) {
      const file = this.activeFile;
      if (file) file.content = content;
    },
    mergeWorkspaceFile(file) {
      if (!file) return;
      const normalized = normalizeWorkspace({ files: [file], active_file: file.path });
      const nextFile = normalized.files[0];
      const existing = this.workspace.files.find((item) => item.path === nextFile.path);
      if (existing) {
        existing.language = nextFile.language;
        existing.content = nextFile.content;
      } else {
        this.workspace.files.push(nextFile);
      }
      this.workspace.active_file = nextFile.path;
      this.runOutput = "";
      this.syncEditorToActiveFile();
    },
    closeWorkspaceFile(path) {
      this.flushEditorToActiveFile();
      const index = this.workspace.files.findIndex((file) => file.path === path);
      if (index < 0) return;
      const wasActive = this.workspace.active_file === path;
      this.workspace.files.splice(index, 1);
      this.fileHandlesByPath.delete(path);
      if (wasActive) {
        const nextFile = this.workspace.files[index] || this.workspace.files[index - 1] || null;
        this.workspace.active_file = nextFile?.path || null;
        this.runOutput = "";
        if (nextFile) {
          this.syncEditorToActiveFile();
        } else {
          this.disposeMonacoEditor();
        }
      }
    },
    applyPatchProposal() {
      this.flushEditorToActiveFile();
      const patch = this.patchProposal;
      if (!patch || !this.activeFile || this.activeFile.path !== patch.file_path) {
        this.patchStatus = "Patch does not match the active file.";
        return;
      }
      const content = this.activeFile.content || "";
      const count = content.split(patch.old).length - 1;
      if (count !== 1) {
        this.patchStatus = `Patch expected one match, found ${count}.`;
        return;
      }
      this.activeFile.content = content.replace(patch.old, patch.new);
      this.patchProposal = null;
      this.patchStatus = "Patch applied in the editor. Save the file to write it to disk.";
      this.syncEditorToActiveFile();
    },
    discardPatchProposal() {
      this.patchProposal = null;
      this.patchStatus = "";
    },
    clearRunOutput() {
      this.runOutput = "";
    },
    currentFilesPayload() {
      this.flushEditorToActiveFile();
      return this.workspace.files.map((file) => ({ ...file }));
    },
    flushEditorToActiveFile() {
      if (this.codeEditor && this.activeFile) {
        this.activeFile.content = this.codeEditor.getValue();
      }
    },
    disposeMonacoEditor() {
      this.codeEditorResizeObserver?.disconnect();
      this.codeEditorResizeObserver = null;
      this.codeEditor?.dispose();
      this.codeEditor = null;
      this.codeEditorModel?.dispose();
      this.codeEditorModel = null;
    },
    async ensureMonacoEditor() {
      if (!this.activeFile) return;
      await nextTick();
      const host = this.$refs.codeEditorEl;
      if (!host) return;
      try {
        const monaco = await loadMonacoEditor();
        defineCustomMonacoThemes(monaco);
        this.monacoLoadError = "";
        if (this.codeEditor && this.codeEditor.getContainerDomNode() !== host) {
          this.disposeMonacoEditor();
        }
        if (!this.codeEditor) {
          this.codeEditorModel = markRaw(monaco.editor.createModel(
            this.activeFile.content || "",
            languageFromFile(this.activeFile),
          ));
          this.codeEditor = markRaw(monaco.editor.create(host, {
            model: this.codeEditorModel,
            theme: resolveMonacoTheme(this.codeTheme),
            automaticLayout: true,
            fontSize: 13,
            lineHeight: 21,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            wordWrap: "off",
            tabSize: 4,
            insertSpaces: true,
            renderWhitespace: "selection",
            smoothScrolling: true,
            padding: { top: 12, bottom: 12 },
          }));
          this.codeEditor.onDidChangeModelContent(() => {
            if (!this.suppressEditorChange) this.updateActiveFile(this.codeEditor.getValue());
          });
          this.codeEditorResizeObserver = new ResizeObserver(() => this.codeEditor?.layout());
          this.codeEditorResizeObserver.observe(host);
          requestAnimationFrame(() => this.codeEditor?.layout());
          window.setTimeout(() => this.codeEditor?.layout(), 200);
        }
      } catch (error) {
        this.monacoLoadError = "Failed to load Monaco editor";
      }
    },
    applyMonacoTheme() {
      const monaco = window.monaco;
      if (!monaco?.editor) return;
      defineCustomMonacoThemes(monaco);
      monaco.editor.setTheme(resolveMonacoTheme(this.codeTheme));
    },
    syncEditorToActiveFile() {
      nextTick(async () => {
        if (!this.activeFile) return;
        await this.ensureMonacoEditor();
        if (!this.codeEditor || !this.codeEditorModel) return;
        const monaco = window.monaco;
        const nextValue = this.activeFile.content || "";
        this.suppressEditorChange = true;
        if (this.codeEditor.getValue() !== nextValue) this.codeEditor.setValue(nextValue);
        if (monaco?.editor) {
          monaco.editor.setModelLanguage(this.codeEditorModel, languageFromFile(this.activeFile));
          this.applyMonacoTheme();
        }
        this.suppressEditorChange = false;
        this.codeEditor.layout();
      });
    },
    async loadConversations() {
      try {
        const response = await fetch("/api/conversations");
        if (!response.ok) throw new Error(`Failed to load conversation: ${response.status}`);
        this.conversations = await response.json();
        this.historyEnabled = true;
        this.historyError = "";
        if (!this.currentConversationId && this.conversations.length > 0) {
          await this.loadConversation(this.conversations[0].id);
        }
      } catch (error) {
        this.historyEnabled = false;
        this.historyError = `Conversation storage is unavailable: ${error.message || "Check DATABASE_URL and the backend service"}`;
      }
    },
    async loadConversation(conversationId) {
      const response = await fetch(`/api/conversations/${conversationId}`);
      if (!response.ok) throw new Error(`Failed to load conversation: ${response.status}`);
      const data = await response.json();
      this.currentConversationId = data.id;
      this.messages = (data.messages || []).map((message) => ({ ...message, localId: `msg-${message.id}` }));
      this.scrollMessages();
    },
    async deleteConversation(conversation) {
      const confirmed = window.confirm(`确认删除会话：${conversation.title || "New conversation"}？`);
      if (!confirmed) return;
      const response = await fetch(`/api/conversations/${conversation.id}`, { method: "DELETE" });
      if (!response.ok && response.status !== 404) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `Failed to delete conversation: ${response.status}`);
      }
      this.conversations = this.conversations.filter((item) => item.id !== conversation.id);
      if (this.currentConversationId === conversation.id) this.startNewConversation();
      if (!this.currentConversationId && this.conversations.length > 0) {
        await this.loadConversation(this.conversations[0].id);
      }
    },
    async ensureConversation() {
      if (this.currentConversationId) return { id: this.currentConversationId };
      const response = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!response.ok) throw new Error(`Failed to load conversation: ${response.status}`);
      const data = await response.json();
      this.currentConversationId = data.id;
      this.conversations = [data, ...this.conversations.filter((item) => item.id !== data.id)];
      return data;
    },
    startNewConversation() {
      this.currentConversationId = null;
      this.messages = [];
      this.setWorkspace(null);
      this.runOutput = "";
      this.prompt = "";
    },
    async loadWorkspaceStatus() {
      this.workspaceStatus = this.supportsLocalDirectoryPicker ? "" : "Current browser does not support folder selection. Use Chrome or Edge.";
    },
    async chooseWorkspaceDirectory() {
      if (!this.supportsLocalDirectoryPicker || this.openingWorkspace) return;
      this.openingWorkspace = true;
      this.workspaceStatus = "Opening folder...";
      try {
        const handle = await window.showDirectoryPicker({ mode: "readwrite" });
        this.workspaceDirectoryHandle = markRaw(handle);
        this.fileHandlesByPath = markRaw(new Map());
        this.workspaceRoot = handle.name || "Selected folder";
        this.workspaceEntries = await collectLocalTextFiles(handle);
        this.selectedWorkspaceFile = this.workspaceEntries[0]?.path || "";
        this.workspaceStatus = this.workspaceEntries.length
          ? `Selected: ${this.workspaceRoot} (${this.workspaceEntries.length} files)`
          : `Selected: ${this.workspaceRoot} (no files found)`;
      } catch (error) {
        this.workspaceStatus = error?.name === "AbortError" ? "Folder selection canceled." : `Error: ${error.message}`;
      } finally {
        this.openingWorkspace = false;
      }
    },
    async openSelectedWorkspaceFile() {
      const path = this.selectedWorkspaceFile;
      if (!path || !this.workspaceDirectoryHandle || this.openingFile) return;
      this.openingFile = true;
      this.workspaceStatus = "Opening file...";
      try {
        const handle = await getFileHandleByPath(this.workspaceDirectoryHandle, path);
        const file = await handle.getFile();
        const content = await file.text();
        this.fileHandlesByPath.set(path, markRaw(handle));
        this.mergeWorkspaceFile({ path, language: languageFromPath(path), content });
        this.workspaceStatus = `Opened file: ${path}`;
      } catch (error) {
        this.workspaceStatus = `Error: ${error.message}`;
      } finally {
        this.openingFile = false;
      }
    },
    async saveActiveFile() {
      this.flushEditorToActiveFile();
      if (!this.activeFile || this.savingFile) return;
      this.savingFile = true;
      this.workspaceStatus = "Saving file...";
      try {
        const handle = this.fileHandlesByPath.get(this.activeFile.path);
        if (!handle) throw new Error("This file was not opened from a selected folder.");
        const writable = await handle.createWritable();
        await writable.write(this.activeFile.content || "");
        await writable.close();
        this.workspaceStatus = `Saved file: ${this.activeFile.path}`;
      } catch (error) {
        this.workspaceStatus = `Error: ${error.message}`;
      } finally {
        this.savingFile = false;
      }
    },
    handleComposerKeydown(event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        this.submitMessage();
      }
    },
    async submitMessage() {
      const content = this.prompt.trim();
      if (!content || this.busy) return;
      this.prompt = "";
      await this.sendMessage(content);
    },
    async sendMessage(content) {
      const loading = { localId: `loading-${Date.now()}`, role: "assistant", content: "Thinking..." };
      this.messages.push({ localId: `user-${Date.now()}`, role: "user", content }, loading);
      this.busy = true;
      this.scrollMessages();
      try {
        if (!this.historyEnabled) {
          throw new Error(this.historyError || "Conversation storage is unavailable. Configure DATABASE_URL first.");
        }
        const conversation = await this.ensureConversation();
        await this.sendConversationChat(conversation.id, content, loading);
        await this.loadConversations();
      } catch (error) {
        loading.content = `Error: ${error.message}`;
      } finally {
        this.busy = false;
        this.scrollMessages();
        this.refreshStatus();
      }
    },
    async sendConversationChat(conversationId, content, loading) {
      const response = await fetch(`/api/conversations/${conversationId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          current_files: this.currentFilesPayload(),
          active_file: this.workspace.active_file,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Request failed: ${response.status}`);
      }
      if (data.conversation?.id) this.currentConversationId = data.conversation.id;
      loading.content = data.message?.content || "Empty response.";
      if (data.message?.id) loading.id = data.message.id;
      this.patchProposal = data.patch || null;
      this.patchStatus = this.patchProposal ? "Patch proposal is ready." : this.patchStatus;
      this.scrollMessages();
    },
    async readSse(response, loading, hasConversationEvents) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let answer = "";
      let doneEvent = null;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
        const parsed = parseSseEvents(buffer);
        buffer = parsed.buffer;
        for (const eventText of parsed.events) {
          const event = JSON.parse(eventText);
          if (event.type === "conversation" && event.conversation?.id) {
            this.currentConversationId = event.conversation.id;
          } else if (event.type === "delta") {
            answer += event.content || "";
            loading.content = answer || "Generating...";
            this.scrollMessages();
          } else if (event.type === "error") {
            throw new Error(event.detail || "Streaming request failed");
          } else if (event.type === "done") {
            doneEvent = event;
          }
        }
      }
      if (doneEvent?.message?.content) {
        loading.content = doneEvent.message.content;
        loading.id = doneEvent.message.id;
      } else {
        loading.content = answer || "Empty response.";
      }
      if (doneEvent?.conversation?.id) this.currentConversationId = doneEvent.conversation.id;
      this.patchProposal = doneEvent?.patch || null;
      this.patchStatus = this.patchProposal ? "Patch proposal is ready." : this.patchStatus;
    },
    async refreshStatus() {
      try {
        const response = await fetch("/api/model/status");
        const data = await response.json();
        const primaryName = data.primary_model_name || "Primary model not configured";
        const remoteName = "cleancode-qwen" || "Coder model";
        const orchestration = data.agent_orchestration || "legacy";
        this.modelStatus = data.configured ? `Primary: ${primaryName} | Coder: ${remoteName} | ${orchestration}` : `Model status unknown: ${primaryName} | ${remoteName} | ${orchestration}`;
      } catch {
        this.modelStatus = "Model status unavailable";
      }
    },
    async copyActiveFile() {
      this.flushEditorToActiveFile();
      if (!this.activeFile) return;
      await navigator.clipboard.writeText(this.activeFile.content);
    },
    async runActiveFile() {
      this.flushEditorToActiveFile();
      if (!this.canRunActiveFile) return;
      this.running = true;
      this.runOutput = "Running...";
      try {
        const response = await fetch("/api/code/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ language: "python", code: this.activeFile.content }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Run failed: ${response.status}`);
        const output = [data.stdout, data.stderr].filter(Boolean).join("\n").trim();
        this.runOutput = output || "No output.";
      } catch (error) {
        this.runOutput = `Error: ${error.message}`;
      } finally {
        this.running = false;
      }
    },
  },
  mounted() {
    this.loadConversations();
    this.loadWorkspaceStatus();
    this.refreshStatus();
  },
  beforeUnmount() {
    this.disposeMonacoEditor();
  },
  updated() {
    nextTick(() => {
      if (this.activeFile) this.ensureMonacoEditor();
      highlightCodeBlocks(document);
      if (window.renderMathInElement) {
        document.querySelectorAll(".markdown-body").forEach((node) => {
          renderMathInElement(node, {
            delimiters: [
              { left: "$$", right: "$$", display: true },
              { left: "\\[", right: "\\]", display: true },
              { left: "\\(", right: "\\)", display: false },
              { left: "$", right: "$", display: false },
            ],
            ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
            throwOnError: false,
          });
        });
      }
    });
  },
}).mount("#app");

