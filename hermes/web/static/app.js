/* Hermes dashboard - dependency-free single page app. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v; // CSSOM - allowed under the strict CSP
      else if (k === "html") el.innerHTML = v; // only ever used with md()/escaped output
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtInt = (n) => Number(n || 0).toLocaleString();
  const fmtUsd = (n) => { n = Number(n || 0); return "$" + (n > 0 && n < 0.01 ? n.toFixed(4) : n.toFixed(2)); };
  // DOM replaceChildren() would render null/false as text - always filter first.
  const put = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  const short = (s, n = 90) => { s = String(s ?? "").replace(/\s+/g, " "); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  function ago(iso) {
    if (!iso) return "";
    const t = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
    if (isNaN(t)) return iso;
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function toast(msg, isErr) {
    const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
    $("#toasts").append(t);
    setTimeout(() => t.remove(), isErr ? 6000 : 3000);
  }

  // ------------------------------------------------------------------ markdown (escape first, then format)
  function inline(s) {
    s = esc(s);
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<code>" + codes[+i] + "</code>");
  }

  function md(text) {
    const out = [];
    const parts = String(text || "").split(/^```/m);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const nl = part.indexOf("\n");
        const body = nl === -1 ? part : part.slice(nl + 1);
        out.push("<pre><code>" + esc(body.replace(/\n$/, "")) + "</code></pre>");
        return;
      }
      const lines = part.split("\n");
      let i2 = 0;
      while (i2 < lines.length) {
        const line = lines[i2];
        if (!line.trim()) { i2++; continue; }
        let m;
        if ((m = line.match(/^(#{1,4})\s+(.*)/))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i2++; continue; }
        if (/^(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i2++; continue; }
        if (/^\s*\|.*\|\s*$/.test(line) && i2 + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i2 + 1])) {
          const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
          let html = "<table><thead><tr>" + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
          i2 += 2;
          while (i2 < lines.length && /^\s*\|.*\|\s*$/.test(lines[i2])) { html += "<tr>" + cells(lines[i2]).map((c) => `<td>${c}</td>`).join("") + "</tr>"; i2++; }
          out.push(html + "</tbody></table>");
          continue;
        }
        if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
          const ordered = /^\s*\d/.test(line);
          let html = ordered ? "<ol>" : "<ul>";
          while (i2 < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i2])) {
            html += "<li>" + inline(lines[i2].replace(/^\s*([-*+]|\d+[.)])\s+/, "").replace(/^\[ \]\s/, "☐ ").replace(/^\[x\]\s/i, "☑ ")) + "</li>";
            i2++;
          }
          out.push(html + (ordered ? "</ol>" : "</ul>"));
          continue;
        }
        if (line.startsWith(">")) {
          const q = [];
          while (i2 < lines.length && lines[i2].startsWith(">")) { q.push(lines[i2].replace(/^>\s?/, "")); i2++; }
          out.push("<blockquote>" + inline(q.join(" ")) + "</blockquote>");
          continue;
        }
        const para = [];
        while (i2 < lines.length && lines[i2].trim() && !/^(#{1,4}\s|\s*([-*+]|\d+[.)])\s|>|\s*\|)/.test(lines[i2])) { para.push(lines[i2]); i2++; }
        if (!para.length) { para.push(lines[i2]); i2++; }
        out.push("<p>" + para.map(inline).join("<br>") + "</p>");
      }
    });
    return out.join("");
  }

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (init.method !== "GET") init.headers["X-Hermes"] = "1";
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const res = await fetch(path, init);
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (res.status === 401) { showLogin(); throw new Error("not signed in"); }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function showLogin() {
    const main = $("#main");
    main.replaceChildren(h("div", { class: "page login" }, h("div", { class: "card" },
      h("h2", {}, "Sign in to the Hermes dashboard"),
      h("p", {}, "Open the private link printed in the terminal by ", h("code", {}, "hermes dashboard"), " (it contains a one-time token)."),
      h("p", { class: "muted" }, "Started on another machine? Run it with ", h("code", {}, "--host"), " and set ", h("code", {}, "HERMES_WEB_TOKEN"), ", then open ", h("code", {}, "http://HOST:PORT/?token=…"), "."))));
  }

  // ------------------------------------------------------------------ theme
  function applyTheme(choice) {
    if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
    else document.documentElement.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.classList.toggle("on", b.dataset.themeChoice === (choice || "system")));
    try { localStorage.setItem("hermes-theme", choice); } catch (_) { /* private mode */ }
  }
  let savedTheme = "system";
  try { savedTheme = localStorage.getItem("hermes-theme") || "system"; } catch (_) { /* ignore */ }
  applyTheme(savedTheme);
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeChoice)));
  $("#menu-btn").addEventListener("click", () => $("#app").classList.toggle("nav-open"));

  // ------------------------------------------------------------------ status
  const state = { status: null, session: null, run: null, es: null };
  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      state.status = s;
      $("#status-line").replaceChildren(
        h("div", {}, `${s.provider}:${short(s.model, 28)}`),
        h("div", {}, `${s.tools} tools · ${s.skills} skills`),
        h("div", {}, `profile: ${s.profile} · approvals: ${s.approvals}`));
      $("#brand-sub").textContent = "v" + s.version;
    } catch (e) {
      $("#status-line").textContent = e.message;
    }
  }

  // ------------------------------------------------------------------ router
  const pages = {};
  let leaveHook = null;
  async function route() {
    const hash = location.hash || "#/chat";
    const [, page, arg] = hash.match(/^#\/([^/]+)\/?(.*)$/) || [null, "chat", ""];
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
    $("#app").classList.remove("nav-open");
    if (leaveHook) { leaveHook(); leaveHook = null; }
    const main = $("#main");
    main.replaceChildren(h("div", { class: "page" }, h("span", { class: "spinner" })));
    try {
      await (pages[page] || pages.chat)(main, decodeURIComponent(arg || ""));
    } catch (e) {
      if (e.message !== "not signed in") main.replaceChildren(h("div", { class: "page" }, h("div", { class: "card error-text" }, e.message)));
    }
    main.focus({ preventScroll: true });
  }
  window.addEventListener("hashchange", route);

  // ================================================================== CHAT
  pages.chat = async (main, sessionId) => {
    const list = h("div", { class: "chat-items" });
    const search = h("input", { class: "input", type: "search", placeholder: "Search conversations…", "aria-label": "Search conversations" });
    const messages = h("div", { class: "messages", id: "messages", "aria-live": "polite" });
    const title = h("div", { class: "convo-title" }, "New conversation");
    const headMeta = h("div", { class: "sub" });
    const input = h("textarea", { rows: 1, placeholder: "Message Hermes…  (Enter to send, Shift+Enter for a new line, @file to attach)", "aria-label": "Message" });
    const sendBtn = h("button", { class: "btn primary", type: "button" }, "Send");
    const stopBtn = h("button", { class: "btn", type: "button", hidden: true }, "■ Stop");

    main.replaceChildren(h("div", { class: "chat" },
      h("div", { class: "chat-list" },
        h("div", { class: "chat-list-head" }, h("button", { class: "btn primary", type: "button", onclick: () => { location.hash = "#/chat"; } }, "+ New chat"), search),
        list),
      h("div", { class: "convo" },
        h("div", { class: "convo-head" }, h("div", { style: "min-width:0" }, title, headMeta), h("div", { class: "row" }, stopBtn)),
        messages,
        h("div", { class: "composer" }, h("div", { class: "composer-inner" }, input, sendBtn),
          h("div", { class: "hint" }, "Hermes can run tools on your machine. Risky commands will ask for your approval here.")))));

    state.session = sessionId || null;

    async function loadList(q) {
      const data = await api("/api/sessions" + (q ? "?q=" + encodeURIComponent(q) : "?limit=60"));
      list.replaceChildren(...(data.sessions.length ? data.sessions.map((s) => {
        const id = s.session_id || s.id;
        return h("button", { class: "chat-item" + (id === state.session ? " active" : ""), type: "button", onclick: () => { location.hash = "#/chat/" + encodeURIComponent(id); } },
          h("div", { class: "t" }, s.title || short(s.snippet, 40) || id),
          h("div", { class: "m" }, `${s.source || ""} · ${ago(s.started_at)}`));
      }) : [h("div", { class: "muted", style: "padding:10px" }, q ? "No matches." : "No conversations yet.")]));
    }
    let searchTimer;
    search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadList(search.value.trim()), 250); });

    const scrollDown = () => { messages.scrollTop = messages.scrollHeight; };
    const toolCards = new Map();

    function addUser(text) { messages.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, text))); }
    function addAssistant(text, meta, media) {
      const el = h("div", { class: "msg assistant" }, h("div", { class: "bubble md", html: md(text) }));
      if (media && media.length) {
        const box = h("div", { class: "media" });
        media.forEach((url) => {
          const p = decodeURIComponent(url).toLowerCase();
          if (/\.(png|jpe?g|webp|gif)$/.test(p)) box.append(h("img", { src: url, alt: "generated image" }));
          else if (/\.(mp3|ogg|wav|m4a|opus)$/.test(p)) box.append(h("audio", { src: url, controls: true }));
          else box.append(h("a", { href: url, target: "_blank" }, "attachment"));
        });
        el.append(box);
      }
      if (meta) el.append(h("div", { class: "meta" }, meta));
      messages.append(el);
    }
    function toolCard(id, name, args, done) {
      const argText = typeof args === "string" ? args : JSON.stringify(args ?? {});
      const preview = (() => {
        try { const o = typeof args === "string" ? JSON.parse(args) : args; const k = ["command", "query", "path", "url", "urls", "goal", "action", "name", "code"].find((x) => o && o[x] !== undefined); return k ? short(typeof o[k] === "string" ? o[k] : JSON.stringify(o[k]), 110) : ""; } catch (_) { return short(argText, 110); }
      })();
      const dot = h("span", { class: "dot " + (done ? "ok" : "run") });
      const stat = h("span", { class: "tstat" }, done ? "" : "running…");
      const out = h("pre", {}, "");
      let pretty = argText;
      try { pretty = JSON.stringify(typeof args === "string" ? JSON.parse(args) : args, null, 2); } catch (_) { /* keep raw */ }
      const card = h("details", { class: "tool" },
        h("summary", {}, dot, h("span", { class: "tname" }, name), h("span", { class: "targ" }, preview), stat),
        h("div", { class: "tbody" }, h("div", { class: "sub" }, "Arguments"), h("pre", {}, pretty), h("div", { class: "sub" }, "Result"), out));
      card._dot = dot; card._stat = stat; card._out = out;
      if (id) toolCards.set(id, card);
      messages.append(card);
      return card;
    }
    function finishTool(card, output, isErr, ms) {
      card._dot.className = "dot " + (isErr ? "err" : "ok");
      card._stat.textContent = (isErr ? "failed · " : "") + (ms !== undefined ? ms + " ms" : "");
      card._out.textContent = output || "";
      if (isErr) card._out.classList.add("error-text");
    }

    async function loadSession(id) {
      messages.replaceChildren();
      toolCards.clear();
      const data = await api("/api/sessions/" + encodeURIComponent(id));
      title.textContent = data.session.title || id;
      headMeta.textContent = `${data.session.source} · ${fmtInt(data.session.input_tokens)} in / ${fmtInt(data.session.output_tokens)} out · ${fmtUsd(data.session.cost_usd)}`;
      const pending = new Map();
      for (const m of data.messages) {
        if (m.role === "user") addUser(m.content.replace(/\n\n\[System reminder:[\s\S]*$/, ""));
        else if (m.role === "assistant") {
          if (m.content) addAssistant(m.content);
          for (const tc of m.tool_calls || []) pending.set(tc.name + pending.size, toolCard(null, tc.name, tc.arguments, true));
        } else if (m.role === "tool") {
          const key = [...pending.keys()].find((k) => k.startsWith(m.name));
          const card = key ? pending.get(key) : toolCard(null, m.name, {}, true);
          if (key) pending.delete(key);
          finishTool(card, m.content, m.is_error);
        }
      }
      if (!data.messages.length) showWelcome();
      scrollDown();
    }

    function showWelcome() {
      const ideas = ["What can you do? Give me a quick tour of your tools.", "Research the latest on the Model Context Protocol and write a short brief.",
        "Look around this project directory and summarise what it contains.", "Every weekday at 9am, send me a summary of top AI news."];
      messages.replaceChildren(h("div", { class: "welcome" },
        h("h2", {}, "What should we work on?"),
        h("p", {}, "Hermes can search the web, run commands, edit files, browse, remember things about you, and schedule work."),
        h("div", { class: "suggest" }, ideas.map((t) => h("button", { type: "button", onclick: () => { input.value = t; input.focus(); autosize(); } }, short(t, 60))))));
    }

    function setBusy(busy) {
      sendBtn.disabled = busy;
      stopBtn.hidden = !busy;
      input.disabled = false;
    }

    let working = null;
    function showWorking(text) {
      if (!working) { working = h("div", { class: "working" }, h("span", { class: "spinner" }), h("span", {}, "")); messages.append(working); }
      working.lastChild.textContent = text || "Thinking…";
      messages.append(working);
      scrollDown();
    }
    function hideWorking() { if (working) { working.remove(); working = null; } }

    function promptCard(ev, runId) {
      const isApproval = ev.type === "approval";
      const card = h("div", { class: "prompt-card" + (isApproval ? "" : " q") });
      const done = (label) => { card.querySelectorAll("button,input").forEach((b) => { b.disabled = true; }); card.append(h("div", { class: "sub" }, label)); };
      const send = async (path, body, label) => {
        try { await api(`/api/runs/${runId}/${path}`, { method: "POST", body }); done(label); } catch (e) { toast(e.message, true); }
      };
      if (isApproval) {
        card.append(h("strong", {}, "⚠ Approval needed: " + ev.reason), h("pre", {}, ev.command),
          h("div", { class: "row" },
            h("button", { class: "btn primary small", type: "button", onclick: () => send("approve", { prompt_id: ev.prompt_id, choice: "once" }, "Approved once") }, "Allow once"),
            h("button", { class: "btn small", type: "button", onclick: () => send("approve", { prompt_id: ev.prompt_id, choice: "session" }, "Allowed for this session") }, "Allow for session"),
            h("button", { class: "btn small", type: "button", onclick: () => send("approve", { prompt_id: ev.prompt_id, choice: "always" }, "Always allowed (added to allowlist)") }, "Always allow"),
            h("button", { class: "btn small danger", type: "button", onclick: () => send("approve", { prompt_id: ev.prompt_id, choice: "deny" }, "Denied") }, "Deny")));
      } else {
        const ans = h("input", { class: "input", placeholder: "Your answer…" });
        const submit = () => ans.value.trim() && send("answer", { prompt_id: ev.prompt_id, answer: ans.value.trim() }, "Answered: " + ans.value.trim());
        ans.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
        card.append(h("strong", {}, "❓ " + ev.question),
          (ev.choices || []).length ? h("div", { class: "row" }, ev.choices.map((c) => h("button", { class: "btn small", type: "button", onclick: () => send("answer", { prompt_id: ev.prompt_id, answer: c }, "Answered: " + c) }, c))) : null,
          h("div", { class: "row" }, ans, h("button", { class: "btn small primary", type: "button", onclick: submit }, "Reply")));
      }
      card.dataset.prompt = ev.prompt_id;
      messages.append(card);
    }

    function follow(runId) {
      if (state.es) state.es.close();
      let last = 0;
      const es = new EventSource(`/api/runs/${runId}/events`);
      state.es = es;
      state.run = runId;
      setBusy(true);
      showWorking("Thinking…");
      es.onmessage = (msg) => {
        const ev = JSON.parse(msg.data);
        if (ev.seq <= last) return;
        last = ev.seq;
        switch (ev.type) {
          case "tool_start": hideWorking(); toolCard(ev.id, ev.name, ev.args, false); showWorking(`Running ${ev.name}…`); break;
          case "tool_end": { const c = toolCards.get(ev.id); if (c) finishTool(c, ev.output, ev.is_error, ev.ms); showWorking("Thinking…"); break; }
          case "thinking": hideWorking(); messages.append(h("div", { class: "thinking" }, "💭 " + short(ev.text, 600))); showWorking("Thinking…"); break;
          case "interim": hideWorking(); addAssistant(ev.text); showWorking("Working…"); break;
          case "approval": case "clarify": hideWorking(); promptCard(ev, runId); break;
          case "final":
            hideWorking();
            addAssistant(ev.text || (ev.error ? "" : "(no response)"), `${ev.turns} turn(s) · ${ev.tool_calls} tool call(s) · ${fmtInt(ev.usage?.input)} in / ${fmtInt(ev.usage?.output)} out · ${fmtUsd(ev.cost_usd)} session`, ev.media);
            if (ev.error) messages.append(h("div", { class: "msg" }, h("div", { class: "error-text" }, ev.error)));
            break;
          case "error": hideWorking(); messages.append(h("div", { class: "msg" }, h("div", { class: "error-text" }, "⚠ " + ev.text))); break;
          default: break;
        }
        scrollDown();
      };
      es.addEventListener("end", () => {
        es.close(); state.es = null; state.run = null; hideWorking(); setBusy(false);
        loadList(search.value.trim()); refreshStatus();
      });
      es.onerror = () => { if (es.readyState === EventSource.CLOSED) { hideWorking(); setBusy(false); } };
    }

    async function send() {
      const text = input.value.trim();
      if (!text || sendBtn.disabled) return;
      if (messages.querySelector(".welcome")) messages.replaceChildren();
      input.value = ""; autosize();
      addUser(text); scrollDown();
      try {
        const r = await api("/api/chat", { method: "POST", body: { message: text, session_id: state.session } });
        if (!state.session) {
          state.session = r.session_id;
          history.replaceState(null, "", "#/chat/" + encodeURIComponent(r.session_id));
          title.textContent = short(text, 70);
        }
        follow(r.run_id);
      } catch (e) {
        toast(e.message, true);
        messages.append(h("div", { class: "msg" }, h("div", { class: "error-text" }, "⚠ " + e.message)));
      }
    }
    function autosize() { input.style.height = "auto"; input.style.height = Math.min(220, input.scrollHeight) + "px"; }
    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
    sendBtn.addEventListener("click", send);
    stopBtn.addEventListener("click", async () => { if (state.run) { try { await api(`/api/runs/${state.run}/stop`, { method: "POST", body: {} }); toast("Stopping…"); } catch (e) { toast(e.message, true); } } });
    leaveHook = () => { if (state.es) { state.es.close(); state.es = null; } };

    await loadList("");
    if (state.session) await loadSession(state.session); else showWelcome();
    input.focus();
  };

  // ================================================================== SESSIONS
  pages.sessions = async (main) => {
    const q = h("input", { class: "input", type: "search", placeholder: "Full-text search across every conversation…", style: "max-width:420px", "aria-label": "Search sessions" });
    const source = h("select", { class: "input", style: "width:auto", "aria-label": "Platform" }, ["", "cli", "web", "telegram", "discord", "slack", "whatsapp", "signal", "email", "teams", "google_chat", "cron", "api", "subagent", "kanban"].map((s) => h("option", { value: s }, s || "all platforms")));
    const body = h("div", { class: "card" });
    main.replaceChildren(h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Sessions"), h("div", { class: "sub" }, "Every conversation from every platform, stored in state.db")), h("div", { class: "row" }, q, source)),
      body));
    async function load() {
      const params = new URLSearchParams();
      if (q.value.trim()) params.set("q", q.value.trim()); else params.set("limit", "200");
      if (source.value) params.set("source", source.value);
      const data = await api("/api/sessions?" + params);
      if (!data.sessions.length) { body.replaceChildren(h("div", { class: "empty" }, "No sessions found.")); return; }
      if (data.search) {
        body.replaceChildren(h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Session"), h("th", {}, "Match"), h("th", {}, "Platform"), h("th", {}, "Started"))),
          h("tbody", {}, data.sessions.map((s) => h("tr", { class: "click", onclick: () => { location.hash = "#/chat/" + encodeURIComponent(s.session_id); } },
            h("td", {}, s.title || s.session_id), h("td", { html: esc(s.snippet).replace(/&gt;&gt;/g, "<mark>").replace(/&lt;&lt;/g, "</mark>") }), h("td", {}, s.source), h("td", {}, ago(s.started_at)))))));
        return;
      }
      body.replaceChildren(h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", {}, "Platform"), h("th", {}, "Started"), h("th", { class: "num" }, "Messages"), h("th", { class: "num" }, "Tokens"), h("th", { class: "num" }, "Cost"), h("th", {}, ""))),
        h("tbody", {}, data.sessions.map((s) => {
          const open = () => { location.hash = "#/chat/" + encodeURIComponent(s.id); };
          return h("tr", { class: "click" },
            h("td", { onclick: open }, h("div", {}, s.title || "(untitled)"), h("div", { class: "muted mono" }, s.id)),
            h("td", { onclick: open }, h("span", { class: "pill" }, s.source)),
            h("td", { onclick: open }, ago(s.started_at)),
            h("td", { class: "num", onclick: open }, fmtInt(s.message_count)),
            h("td", { class: "num", onclick: open }, fmtInt((s.input_tokens || 0) + (s.output_tokens || 0))),
            h("td", { class: "num", onclick: open }, fmtUsd(s.cost_usd)),
            h("td", {}, h("div", { class: "row" },
              h("button", { class: "btn small", type: "button", onclick: async () => {
                const t = prompt("Rename session", s.title || ""); if (t === null) return;
                try { await api("/api/sessions/" + encodeURIComponent(s.id), { method: "PUT", body: { title: t } }); load(); } catch (e) { toast(e.message, true); }
              } }, "Rename"),
              h("button", { class: "btn small danger", type: "button", onclick: async () => {
                if (!confirm("Delete this session permanently?")) return;
                try { await api("/api/sessions/" + encodeURIComponent(s.id), { method: "DELETE" }); toast("Deleted"); load(); } catch (e) { toast(e.message, true); }
              } }, "Delete"))));
        }))));
    }
    let t; q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 250); });
    source.addEventListener("change", load);
    await load();
  };

  // ================================================================== MEMORY
  pages.memory = async (main) => {
    const data = await api("/api/memory");
    const panel = (key, title, help) => {
      const info = data[key];
      const wrap = h("div", { class: "entries" });
      const meterSpan = h("span");
      const meter = h("div", { class: "meter", role: "meter", "aria-label": title + " usage" }, meterSpan);
      const usage = h("div", { class: "sub" });
      const collect = () => [...wrap.querySelectorAll("textarea")].map((t) => t.value.trim()).filter(Boolean);
      const update = () => {
        const used = collect().join("\n§\n").length;
        const pct = Math.min(100, (used / info.limit) * 100);
        meterSpan.style.width = pct + "%";
        meter.className = "meter" + (pct >= 100 ? " full" : pct > 85 ? " warn" : "");
        usage.textContent = `${fmtInt(used)} / ${fmtInt(info.limit)} characters · ${collect().length} ${collect().length === 1 ? "entry" : "entries"}`;
      };
      const addEntry = (text) => {
        const ta = h("textarea", { class: "input", rows: 2, "aria-label": "Memory entry" }, text);
        ta.addEventListener("input", update);
        wrap.append(h("div", { class: "entry" }, ta, h("button", { class: "btn small danger", type: "button", title: "Remove", onclick: (e) => { e.target.closest(".entry").remove(); update(); } }, "✕")));
      };
      info.entries.forEach(addEntry);
      update();
      return h("div", { class: "card" },
        h("div", { class: "page-head", style: "margin-bottom:8px" }, h("h2", { style: "margin:0" }, title), h("span", { class: "muted mono" }, info.path.split("/").slice(-2).join("/"))),
        h("p", { class: "sub", style: "margin-top:0" }, help),
        meter, usage, h("div", { style: "height:10px" }), wrap,
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", onclick: () => { addEntry(""); update(); wrap.lastChild.querySelector("textarea").focus(); } }, "+ Add entry"),
          h("button", { class: "btn primary", type: "button", onclick: async () => {
            try { const r = await api("/api/memory/" + key, { method: "PUT", body: { entries: collect() } }); toast(`Saved (${r.used}/${r.limit} chars). Active from the next session.`); } catch (e) { toast(e.message, true); }
          } }, "Save")));
    };
    main.replaceChildren(h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Memory"), h("div", { class: "sub" }, "Curated, bounded memory injected into every new session. Secrets and prompt-injection text are refused."))),
      h("div", { class: "grid-2" },
        panel("memory", "Agent notes · MEMORY.md", "Environment facts, conventions and lessons the agent has learned."),
        panel("user", "User profile · USER.md", "Who you are: preferences, role, projects, communication style."))));
  };

  // ================================================================== SKILLS
  pages.skills = async (main, selected) => {
    const filter = h("input", { class: "input", type: "search", placeholder: "Filter skills…", "aria-label": "Filter skills" });
    const listBox = h("div", { class: "skill-list" });
    const editor = h("div", { class: "card" });
    let skills = [];
    main.replaceChildren(h("div", { class: "page", style: "max-width:1400px" },
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Skills"), h("div", { class: "sub" }, "Reusable procedures (SKILL.md). The agent sees names + descriptions and loads a skill only when needed.")),
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", onclick: installDialog }, "Install from hub…"),
          h("button", { class: "btn", type: "button", onclick: async () => { try { const r = await api("/api/skills/sync", { method: "POST", body: {} }); toast(`Synced ${r.synced.length} bundled skill(s)`); load(); } catch (e) { toast(e.message, true); } } }, "Sync bundled"),
          h("button", { class: "btn primary", type: "button", onclick: () => openNew() }, "+ New skill"))),
      h("div", { class: "split" }, h("div", { class: "card", style: "padding:10px" }, filter, listBox), editor)));

    function renderList() {
      const f = filter.value.toLowerCase();
      const shown = skills.filter((s) => !f || (s.name + " " + s.description).toLowerCase().includes(f));
      const out = [];
      let cat = null;
      for (const s of shown) {
        if (s.category !== cat) { cat = s.category; out.push(h("div", { class: "skill-cat" }, cat || "general")); }
        out.push(h("button", { class: "skill-item" + (s.name === selected ? " active" : ""), type: "button", onclick: () => { location.hash = "#/skills/" + encodeURIComponent(s.name); } },
          h("div", {}, s.name, s.hub ? h("span", { class: "pill", style: "margin-left:6px" }, "hub") : null, s.issues.length ? h("span", { class: "pill warn", style: "margin-left:6px" }, s.issues.length + " issue" + (s.issues.length > 1 ? "s" : "")) : null),
          h("div", { class: "d" }, s.description)));
      }
      listBox.replaceChildren(...(out.length ? out : [h("div", { class: "muted", style: "padding:10px" }, "No skills.")]));
    }
    filter.addEventListener("input", renderList);

    async function load() {
      skills = (await api("/api/skills")).skills;
      renderList();
    }

    function editorFor(name, content, isNew, files, issues) {
      const nameIn = h("input", { class: "input", value: name, placeholder: "lowercase-with-dashes", disabled: !isNew, "aria-label": "Skill name" });
      const catIn = h("input", { class: "input", placeholder: "category (optional)", "aria-label": "Category" });
      const ta = h("textarea", { class: "input code", spellcheck: "false", "aria-label": "SKILL.md" }, content);
      put(editor,
        h("div", { class: "page-head", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, isNew ? "New skill" : name),
          h("div", { class: "row" },
            !isNew ? h("button", { class: "btn danger", type: "button", onclick: async () => {
              if (!confirm(`Delete skill "${name}"?`)) return;
              try { await api("/api/skills/" + encodeURIComponent(name), { method: "DELETE" }); toast("Deleted"); location.hash = "#/skills"; } catch (e) { toast(e.message, true); }
            } }, "Delete") : null,
            h("button", { class: "btn primary", type: "button", onclick: async () => {
              const n = nameIn.value.trim();
              try {
                await api("/api/skills/" + encodeURIComponent(n), { method: "PUT", body: { content: ta.value, category: catIn.value.trim() } });
                toast("Saved"); selected = n; await load(); if (isNew) location.hash = "#/skills/" + encodeURIComponent(n);
              } catch (e) { toast(e.message, true); }
            } }, "Save"))),
        isNew ? h("div", { class: "grid-2", style: "margin-bottom:10px" }, nameIn, catIn) : null,
        (issues || []).length ? h("div", { class: "prompt-card", style: "margin:0 0 10px" }, h("strong", {}, "Lint"), h("ul", {}, issues.map((i) => h("li", {}, i)))) : null,
        ta,
        files && files.length ? h("div", { class: "sub", style: "margin-top:8px" }, "Supporting files: " + files.join(", ")) : null);
    }

    function openNew() {
      selected = null; renderList();
      editorFor("", "---\nname: my-skill\ndescription: What this skill does and WHEN to use it.\nversion: 1.0.0\n---\n\n# My skill\n\n## When to use\n- ...\n\n## Steps\n1. ...\n\n## Pitfalls\n- ...\n\n## Verification\n- ...\n", true);
    }

    async function installDialog() {
      const src = prompt("Install a skill from a local path, a URL to SKILL.md, or a GitHub path (owner/repo/path/to/skill):");
      if (!src) return;
      try { const r = await api("/api/skills/install", { method: "POST", body: { source: src } }); toast("Installed " + r.name); await load(); location.hash = "#/skills/" + encodeURIComponent(r.name); }
      catch (e) { toast(e.message, true); }
    }

    await load();
    if (selected) {
      try { const s = await api("/api/skills/" + encodeURIComponent(selected)); editorFor(selected, s.content, false, s.files, (skills.find((x) => x.name === selected) || {}).issues); }
      catch (e) { editor.replaceChildren(h("div", { class: "error-text" }, e.message)); }
    } else {
      editor.replaceChildren(h("div", { class: "empty" }, "Select a skill to view or edit it, or create a new one."));
    }
  };

  // ================================================================== CRON
  pages.cron = async (main) => {
    const tableBox = h("div", { class: "card" });
    const outBox = h("div");
    const f = {
      name: h("input", { class: "input", placeholder: "Morning AI news" }),
      schedule: h("input", { class: "input", placeholder: "weekdays at 8am" }),
      deliver: h("input", { class: "input", value: "local", placeholder: "local | telegram | slack:C123 | email:me@x.com" }),
      skills: h("input", { class: "input", placeholder: "research-brief (optional)" }),
      prompt: h("textarea", { class: "input", rows: 3, placeholder: "A self-contained instruction - the job runs in a fresh session." }),
    };
    main.replaceChildren(h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Scheduled tasks"), h("div", { class: "sub" }, "Jobs run while the gateway is up (or via `hermes cron tick`). Results go to the delivery target; [SILENT] replies are saved but not sent."))),
      h("div", { class: "card" }, h("h2", {}, "New job"),
        h("div", { class: "grid-2" },
          h("label", { class: "field" }, "Name", f.name),
          h("label", { class: "field" }, "Schedule", f.schedule, h("span", { class: "muted" }, "30m · every 2h · daily at 9am · every monday at 8:30 · 0 9 * * 1-5")),
          h("label", { class: "field" }, "Deliver to", f.deliver),
          h("label", { class: "field" }, "Skills", f.skills)),
        h("label", { class: "field", style: "margin-top:12px" }, "Prompt", f.prompt),
        h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn primary", type: "button", onclick: create }, "Create job"))),
      tableBox, outBox));

    async function create() {
      try {
        const r = await api("/api/cron", { method: "POST", body: { name: f.name.value, schedule: f.schedule.value, deliver: f.deliver.value, skills: f.skills.value, prompt: f.prompt.value } });
        toast(`Created ${r.job.id}: ${r.job.schedule_text}`);
        f.name.value = f.schedule.value = f.skills.value = f.prompt.value = "";
        load();
      } catch (e) { toast(e.message, true); }
    }
    const act = async (id, action, method = "POST") => {
      try { await api(`/api/cron/${id}${action ? "/" + action : ""}`, { method, body: method === "POST" ? {} : undefined }); toast(action ? action + " ✓" : "removed"); load(); } catch (e) { toast(e.message, true); }
    };
    async function showOutputs(job) {
      const data = await api(`/api/cron/${job.id}/outputs`);
      outBox.replaceChildren(h("div", { class: "card" }, h("div", { class: "page-head", style: "margin-bottom:8px" }, h("h2", { style: "margin:0" }, `Recent output · ${job.name}`), h("button", { class: "btn small", type: "button", onclick: () => outBox.replaceChildren() }, "Close")),
        data.outputs.length ? data.outputs.map((o) => h("details", { class: "tool", style: "margin:6px 0" }, h("summary", {}, h("span", { class: "tname" }, o.name)), h("div", { class: "tbody" }, h("div", { class: "md", html: md(o.content) })))) : h("div", { class: "empty" }, "No runs yet.")));
      outBox.scrollIntoView({ behavior: "smooth" });
    }
    async function load() {
      const data = await api("/api/cron");
      if (!data.jobs.length) { tableBox.replaceChildren(h("h2", {}, "Jobs"), h("div", { class: "empty" }, "No scheduled jobs yet.")); return; }
      tableBox.replaceChildren(h("h2", {}, `Jobs (${data.status.enabled} active of ${data.status.total})`), h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, ["Job", "Schedule", "Next run", "Last", "Deliver", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, data.jobs.map((j) => h("tr", {},
          h("td", {}, h("div", {}, j.name), h("div", { class: "muted" }, short(j.prompt, 80))),
          h("td", {}, j.schedule_text, h("div", { class: "muted mono" }, j.id)),
          h("td", {}, j.enabled ? (j.next_run || "-").replace("T", " ") : h("span", { class: "pill" }, j.paused_reason || "paused")),
          h("td", {}, j.last_status ? h("span", { class: "pill " + (j.last_status === "ok" ? "good" : "bad") }, j.last_status === "ok" ? "✓ ok" : "✗ " + j.last_status) : h("span", { class: "muted" }, "never"),
            j.last_run ? h("div", { class: "muted" }, ago(j.last_run.replace("T", " "))) : null),
          h("td", {}, h("span", { class: "mono" }, j.deliver)),
          h("td", {}, h("div", { class: "row" },
            j.enabled ? h("button", { class: "btn small", type: "button", onclick: () => act(j.id, "pause") }, "Pause") : h("button", { class: "btn small", type: "button", onclick: () => act(j.id, "resume") }, "Resume"),
            h("button", { class: "btn small", type: "button", onclick: () => act(j.id, "run") }, "Run now"),
            h("button", { class: "btn small", type: "button", onclick: () => showOutputs(j) }, "Output"),
            h("button", { class: "btn small danger", type: "button", onclick: () => { if (confirm("Remove this job?")) act(j.id, "", "DELETE"); } }, "Remove"))))))));
    }
    await load();
  };

  // ================================================================== INSIGHTS
  let tipEl = null;
  function tip(e, text) {
    if (!tipEl) { tipEl = h("div", { class: "tip", role: "tooltip" }); document.body.append(tipEl); }
    if (!text) { tipEl.hidden = true; return; }
    tipEl.hidden = false; tipEl.textContent = text;
    const x = Math.min(e.clientX + 14, window.innerWidth - tipEl.offsetWidth - 8);
    tipEl.style.left = x + "px"; tipEl.style.top = (e.clientY + 14) + "px";
  }
  function barChart(rows, fmt, tipFmt) {
    // single series: one hue, value labels in text ink, rounded data-end anchored at the baseline
    const max = Math.max(...rows.map((r) => r.value), 0) || 1;
    if (!rows.length) return h("div", { class: "empty" }, "No data yet.");
    return h("div", { class: "bars", role: "table" }, rows.map((r) => {
      const hit = h("div", { class: "row-hit", role: "row" },
        h("div", { class: "lbl", role: "cell", title: r.label }, r.label),
        h("div", { class: "track", role: "cell" }, h("div", { class: "bar", style: `width:${Math.max(0.5, (r.value / max) * 100)}%` })),
        h("div", { class: "val", role: "cell" }, fmt(r.value)));
      [...hit.children].forEach((c) => {
        c.addEventListener("mousemove", (e) => tip(e, tipFmt(r)));
        c.addEventListener("mouseleave", (e) => tip(e, ""));
      });
      return hit;
    }));
  }
  pages.insights = async (main) => {
    const range = h("select", { class: "input", style: "width:auto", "aria-label": "Time range" }, [7, 30, 90, 365].map((d) => h("option", { value: d, selected: d === 30 }, `Last ${d} days`)));
    const body = h("div");
    main.replaceChildren(h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Insights"), h("div", { class: "sub" }, "Usage, tools and estimated cost (prices are estimates - override with `pricing:` in config.yaml).")), range),
      body));
    async function load() {
      const d = await api("/api/insights?days=" + range.value);
      const t = d.totals;
      const tile = (k, v, s) => h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), s ? h("div", { class: "s" }, s) : null);
      const cacheRate = t.input_tokens ? Math.round((t.cache_read_tokens / t.input_tokens) * 100) : 0;
      body.replaceChildren(
        h("div", { class: "tiles" },
          tile("Estimated cost", fmtUsd(t.cost_usd), `${d.days} days`),
          tile("Sessions", fmtInt(t.sessions), `${fmtInt(t.messages)} messages`),
          tile("Tool calls", fmtInt(t.tool_calls)),
          tile("Input tokens", fmtInt(t.input_tokens), `${cacheRate}% from cache`),
          tile("Output tokens", fmtInt(t.output_tokens))),
        h("div", { class: "grid-2" },
          h("div", { class: "card" }, h("h2", {}, "Estimated cost by model"),
            barChart(d.by_model.map((r) => ({ label: r.model || "unknown", value: r.cost_usd || 0, r })), fmtUsd,
              (x) => `${x.label}: ${fmtUsd(x.value)} · ${x.r.sessions} sessions · ${fmtInt(x.r.input_tokens)} in / ${fmtInt(x.r.output_tokens)} out`)),
          h("div", { class: "card" }, h("h2", {}, "Sessions by platform"),
            barChart(d.by_source.map((r) => ({ label: r.source || "unknown", value: r.sessions, r })), fmtInt,
              (x) => `${x.label}: ${fmtInt(x.value)} sessions · ${fmtUsd(x.r.cost_usd)}`))),
        h("div", { class: "card" }, h("h2", {}, "Tool calls by tool"),
          barChart(d.tools.map((r) => ({ label: r.tool_name, value: r.calls, r })), fmtInt,
            (x) => `${x.label}: ${fmtInt(x.value)} calls · ${Math.round((x.r.ok / x.value) * 100)}% ok · ~${x.r.avg_ms} ms avg`)),
        h("div", { class: "card" }, h("h2", {}, "Most expensive sessions"),
          d.top_sessions.length ? h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Session"), h("th", {}, "Platform"), h("th", { class: "num" }, "Tokens"), h("th", { class: "num" }, "Cost"))),
            h("tbody", {}, d.top_sessions.map((s) => h("tr", { class: "click", onclick: () => { location.hash = "#/chat/" + encodeURIComponent(s.id); } },
              h("td", {}, s.title || s.id), h("td", {}, s.source), h("td", { class: "num" }, fmtInt(s.tokens)), h("td", { class: "num" }, fmtUsd(s.cost_usd)))))) : h("div", { class: "empty" }, "No sessions yet.")));
    }
    range.addEventListener("change", load);
    await load();
  };

  // ================================================================== SETTINGS
  pages.settings = async (main) => {
    const data = await api("/api/settings");
    const e = data.editable;
    const inputs = {};
    const field = (key, label, kind, help) => {
      let el;
      if (data.choices[key]) el = h("select", { class: "input" }, data.choices[key].map((c) => h("option", { value: c, selected: c === (e[key] ?? "") }, c || "(default)")));
      else if (kind === "bool") el = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: !!e[key] }), h("span"));
      else el = h("input", { class: "input", type: kind === "int" ? "number" : "text", value: e[key] ?? "" });
      inputs[key] = { el, kind };
      return h("label", { class: "field" }, label, el, help ? h("span", { class: "muted" }, help) : null);
    };
    const provider = h("select", { class: "input" }, data.providers.map((p) => h("option", { value: p.id, selected: p.id === e["model.provider"] }, `${p.id} - ${p.label}`)));
    inputs["model.provider"] = { el: provider, kind: "str" };
    const modelField = field("model.default", "Model", "str", "e.g. anthropic/claude-opus-5-5 (OpenRouter) or claude-opus-5-5 (Anthropic)");
    provider.addEventListener("change", () => {
      const p = data.providers.find((x) => x.id === provider.value);
      if (p && p.default_model) inputs["model.default"].el.value = p.default_model;
    });
    const toolsBox = h("div", { class: "grid-2" });
    const doctorBox = h("div");
    const logsBox = h("pre", { class: "box" }, "");
    main.replaceChildren(h("div", { class: "page" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("div", { class: "sub mono" }, data.config_path)),
        h("button", { class: "btn primary", type: "button", onclick: save }, "Save settings")),
      h("div", { class: "grid-2" },
        h("div", { class: "card" }, h("h2", {}, "Model"),
          h("div", { style: "display:grid;gap:12px" }, h("label", { class: "field" }, "Provider", provider, h("span", { class: "muted" }, "API keys live in ~/.hermes/.env - set them with `hermes setup`.")), modelField,
            field("model.base_url", "Base URL (custom/local providers)", "str"), field("model.reasoning_effort", "Reasoning effort", "str"))),
        h("div", { class: "card" }, h("h2", {}, "Agent & safety"),
          h("div", { style: "display:grid;gap:12px" }, field("agent.personality", "Personality", "str"), field("agent.max_turns", "Max tool iterations per message", "int"),
            field("approvals.mode", "Approvals for risky commands", "str", "manual = always ask · smart = AI pre-screen, then ask · off = never ask"),
            h("div", { class: "grid-2" }, field("agent.verify_outputs", "Verify outputs", "bool"), field("checkpoints.enabled", "Checkpoints before edits", "bool")))),
        h("div", { class: "card" }, h("h2", {}, "Execution & memory"),
          h("div", { style: "display:grid;gap:12px" }, field("terminal.backend", "Terminal backend", "str"), field("terminal.cwd", "Working directory", "str"),
            field("memory.provider", "Memory provider", "str", "session_recall auto-recalls related past conversations")))),
      h("div", { class: "card" }, h("div", { class: "page-head", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, "Toolsets for the web dashboard"), h("span", { class: "sub" }, "Enable only what you need (least privilege).")), toolsBox),
      h("div", { class: "card" }, h("div", { class: "page-head", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, "Health check"), h("button", { class: "btn small", type: "button", onclick: runDoctor }, "Run doctor")), doctorBox),
      h("div", { class: "card" }, h("div", { class: "page-head", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, "Error log"), h("button", { class: "btn small", type: "button", onclick: loadLogs }, "Refresh")), logsBox),
      h("div", { class: "card" }, h("details", {}, h("summary", { style: "cursor:pointer;font-weight:600" }, "Effective configuration (secrets masked)"), h("pre", { class: "box", style: "margin-top:10px" }, JSON.stringify(data.config, null, 2))))));

    async function save() {
      const changes = {};
      for (const [k, { el, kind }] of Object.entries(inputs)) {
        const v = kind === "bool" ? el.querySelector("input").checked : kind === "int" ? Number(el.value) : el.value;
        if (v !== e[k] && !(v === "" && (e[k] === null || e[k] === undefined))) changes[k] = v;
      }
      if (!Object.keys(changes).length) { toast("No changes"); return; }
      try { await api("/api/settings", { method: "PUT", body: { changes } }); toast("Saved - applies to new messages"); refreshStatus(); pages.settings(main); } catch (err) { toast(err.message, true); }
    }
    async function loadTools() {
      const t = await api("/api/tools?platform=web");
      toolsBox.replaceChildren(...t.toolsets.map((ts) => {
        const cb = h("input", { type: "checkbox", checked: ts.enabled, "aria-label": "Enable " + ts.toolset });
        cb.addEventListener("change", async () => {
          try { await api("/api/tools", { method: "POST", body: { platform: "web", toolset: ts.toolset, enabled: cb.checked } }); toast(`${ts.toolset} ${cb.checked ? "enabled" : "disabled"}`); refreshStatus(); }
          catch (err) { cb.checked = !cb.checked; toast(err.message, true); }
        });
        return h("div", { class: "toolset" },
          h("div", { class: "toolset-head" }, h("strong", { class: "mono" }, ts.toolset), h("label", { class: "switch" }, cb, h("span"))),
          h("div", { class: "tools" }, ts.tools.map((x) => h("span", { class: "pill" + (x.available ? "" : " warn"), title: x.available ? x.description : x.reason }, x.name + (x.available ? "" : " ⚠")))));
      }));
    }
    async function runDoctor() {
      doctorBox.replaceChildren(h("span", { class: "spinner" }));
      const d = await api("/api/doctor");
      doctorBox.replaceChildren(...d.checks.map((c) => h("div", { class: "check" },
        h("span", { class: "pill " + { ok: "good", warn: "warn", fail: "bad" }[c.status] }, { ok: "✓ ok", warn: "⚠ warn", fail: "✗ fail" }[c.status]),
        h("div", {}, h("div", {}, c.check), c.hint && c.status !== "ok" ? h("div", { class: "hint" }, c.hint) : null))));
    }
    async function loadLogs() {
      const l = await api("/api/logs?kind=errors&lines=150");
      logsBox.textContent = l.lines.length ? l.lines.join("\n") : "No errors logged. 🎉";
    }
    await Promise.all([loadTools(), loadLogs()]);
  };

  // ------------------------------------------------------------------ boot
  refreshStatus();
  route();
})();
