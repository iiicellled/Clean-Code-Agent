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
  auto: "跟随系统",
  "clean-dark": "清爽深色",
  "github-light": "GitHub 浅色",
  "vs-dark": "VS 深色",
  vs: "VS 浅色",
};
let customMonacoThemesDefined = false;
let monacoPromise = null;

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
    snapshot_id: workspace?.snapshot_id || null,
  };
}

function languageFromFile(file) {
  if (!file) return "plaintext";
  const explicit = String(file.language || "").toLowerCase();
  if (MONACO_LANGUAGE_ALIASES[explicit]) return MONACO_LANGUAGE_ALIASES[explicit];
  const suffix = String(file.path || "").split(".").pop()?.toLowerCase() || "text";
  return MONACO_LANGUAGE_ALIASES[suffix] || "plaintext";
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
        reject(new Error("代码编辑器加载器不可用"));
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
    script.onerror = () => reject(new Error("代码编辑器加载器加载失败"));
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
      modelStatus: "正在连接模型...",
      historyEnabled: false,
      historyError: "",
      currentConversationId: null,
      conversations: [],
      messages: [
        {
          localId: `welcome-${Date.now()}`,
          role: "assistant",
          content: "代码精简助手已就绪。你可以直接提出代码需求，生成的代码会显示在右侧代码区。",
        },
      ],
      workspace: normalizeWorkspace(null),
      codeEditor: null,
      codeEditorModel: null,
      codeEditorResizeObserver: null,
      suppressEditorChange: false,
      monacoLoadError: "",
      codeTheme: localStorage.getItem("coderAgent.codeTheme") || "auto",
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
      const files = parseCodeBlocks(content);
      const text = stripCodeBlocks(content) || (files.length ? "已生成代码，如右侧代码区所示。" : content);
      return renderMarkdown(text);
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
        this.monacoLoadError = "代码编辑器加载失败";
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
        if (!response.ok) throw new Error(`历史会话不可用：${response.status}`);
        this.conversations = await response.json();
        this.historyEnabled = true;
        this.historyError = "";
        if (!this.currentConversationId && this.conversations.length > 0) {
          await this.loadConversation(this.conversations[0].id);
        }
      } catch (error) {
        this.historyEnabled = false;
        this.historyError = `历史会话暂不可用：${error.message || "请检查后端服务或数据库"}`;
      }
    },
    async loadConversation(conversationId) {
      const response = await fetch(`/api/conversations/${conversationId}`);
      if (!response.ok) throw new Error(`加载会话失败：${response.status}`);
      const data = await response.json();
      this.currentConversationId = data.id;
      this.messages = (data.messages || []).map((message) => ({ ...message, localId: `msg-${message.id}` }));
      this.setWorkspace(data.workspace);
      this.scrollMessages();
    },
    async deleteConversation(conversation) {
      const confirmed = window.confirm(`确认删除会话：${conversation.title || "新会话"}？`);
      if (!confirmed) return;
      const response = await fetch(`/api/conversations/${conversation.id}`, { method: "DELETE" });
      if (!response.ok && response.status !== 404) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `删除会话失败：${response.status}`);
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
      if (!response.ok) throw new Error(`创建会话失败：${response.status}`);
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
      const loading = { localId: `loading-${Date.now()}`, role: "assistant", content: "正在思考..." };
      this.messages.push({ localId: `user-${Date.now()}`, role: "user", content }, loading);
      this.busy = true;
      this.scrollMessages();
      try {
        if (this.historyEnabled) {
          const conversation = await this.ensureConversation();
          await this.streamConversationChat(conversation.id, content, loading);
          await this.loadConversations();
        } else {
          await this.streamPlainChat(loading);
        }
      } catch (error) {
        loading.content = `错误：${error.message}`;
      } finally {
        this.busy = false;
        this.scrollMessages();
        this.refreshStatus();
      }
    },
    async streamConversationChat(conversationId, content, loading) {
      const response = await fetch(`/api/conversations/${conversationId}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content,
          current_files: this.currentFilesPayload(),
          active_file: this.workspace.active_file,
        }),
      });
      if (!response.ok || !response.body) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `请求失败：${response.status}`);
      }
      await this.readSse(response, loading, true);
    },
    async streamPlainChat(loading) {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: this.messages
            .filter((message) => message !== loading)
            .filter((message) => message.role === "user" || message.role === "assistant")
            .map(({ role, content }) => ({ role, content })),
          current_files: this.currentFilesPayload(),
          active_file: this.workspace.active_file,
        }),
      });
      if (!response.ok || !response.body) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `请求失败：${response.status}`);
      }
      await this.readSse(response, loading, false);
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
            loading.content = answer || "生成中...";
            this.scrollMessages();
          } else if (event.type === "error") {
            throw new Error(event.detail || "流式请求失败");
          } else if (event.type === "done") {
            doneEvent = event;
          }
        }
      }

      const clientFiles = parseCodeBlocks(answer);
      if (doneEvent?.message?.content) {
        loading.content = doneEvent.message.content;
        loading.id = doneEvent.message.id;
      } else {
        loading.content = stripCodeBlocks(answer) || (clientFiles.length ? "已生成代码，如右侧代码区所示。" : "模型返回为空。");
      }
      if (doneEvent?.conversation?.id) this.currentConversationId = doneEvent.conversation.id;
      if (doneEvent?.workspace) {
        this.setWorkspace(doneEvent.workspace);
      } else if (!hasConversationEvents && clientFiles.length) {
        this.setWorkspace({ files: clientFiles, active_file: clientFiles[0].path });
      }
    },
    async refreshStatus() {
      try {
        const response = await fetch("/api/model/status");
        const data = await response.json();
        const primaryName = data.primary_model_name || "主模型未配置";
        const remoteName = data.coder_model_name || "代码模型";
        this.modelStatus = data.configured ? `主模型：${primaryName} | Coder：${remoteName}` : `模型状态未知：${primaryName} | ${remoteName}`;
      } catch {
        this.modelStatus = "服务状态未知";
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
      this.runOutput = "运行中...";
      try {
        const response = await fetch("/api/code/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ language: "python", code: this.activeFile.content }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `运行失败：${response.status}`);
        const output = [data.stdout, data.stderr].filter(Boolean).join("\n").trim();
        this.runOutput = output || "无输出。";
      } catch (error) {
        this.runOutput = `错误：${error.message}`;
      } finally {
        this.running = false;
      }
    },
  },
  mounted() {
    this.loadConversations();
    this.refreshStatus();
  },
  beforeUnmount() {
    this.disposeMonacoEditor();
  },
  updated() {
    nextTick(() => {
      if (this.activeFile) this.ensureMonacoEditor();
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

