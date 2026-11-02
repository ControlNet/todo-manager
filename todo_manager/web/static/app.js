// Todo Manager Web UI: one page, plain DOM, no build step. Reads /api/*, writes through
// /api/tasks/<id>/act and /comment (docs/web-ui.md). Routes live in the hash:
//   #/tasks/<view>[/<task id>]  view: today | decide | agent | waiting | all | p:<project id>
//   #/projects[/<project id>]   #/delivery/<delivery id>   #/search   #/decide
"use strict";
(function () {
  const $app = document.getElementById("app");
  const $overlay = document.getElementById("overlay");

  // ------------------------------------------------------------------ text
  const TEXT = {
    "zh-CN": {
      today: "今天", decide: "决定", agent: "Agent", waiting: "在等", all: "全部", projects: "项目", search: "搜索",
      searchHint: "搜索任务、项目、邮件…", nextThing: "下一件事", noNext: "没有要马上做的事",
      g_decide: "需要决定", g_mine: "我的待办", g_agent_wait: "Agent 等你", g_agent_run: "Agent 在做", g_waiting: "在等别人",
      showAll: "全部 {n} 条", empty: "这里没有未完成的任务", decideOneByOne: "逐个决定",
      k_decision: "需要决定", k_todo: "待办", k_answer: "Agent 在问", k_review: "待验收", k_running: "Agent 在做",
      k_waiting: "在等", k_done: "已完成", k_cancelled: "已取消",
      p_status: "状态", p_actor: "由谁", p_due: "截止", p_follow: "跟进", p_project: "项目", p_source: "来源", p_snooze: "延后到",
      a_me: "我", a_agent: "Agent", a_external: "别人",
      none: "—", noProject: "无项目",
      agree: "同意", decline: "不同意", toAgent: "交给 Agent…", setTodo: "设为待办", markDone: "已完成", cancel: "取消",
      done: "完成", snooze: "延后…", waitFor: "等别人…", answer: "回答", selfDo: "我自己做", pause: "暂停", resume: "继续",
      accept: "验收通过", revise: "要求修改", viewDelivery: "查看交付", selfEdit: "我自己改", supplement: "补充",
      gotIt: "收到了", stillWaiting: "还在等…", chaser: "起草催办", reopen: "重新打开", more: "⋯ 更多",
      m_status: "状态", m_time: "时间", m_who: "谁来做", m_other: "其他",
      dueDate: "截止日期…", followDate: "跟进日期…", unsnooze: "取消延后", focusOn: "设为重点", focusOff: "取消重点",
      meDo: "我来做", pauseAgent: "暂停 Agent", resumeAgent: "继续 Agent", setProject: "项目…", openNotion: "在 Notion 打开",
      copyLink: "复制链接", copied: "链接已复制",
      nextStep: "下一步", doneCriteria: "做到什么算完成", summary: "摘要", sources: "来源", notes: "我的笔记", discussion: "讨论",
      noDiscussion: "还没有讨论", history: "改动记录", links: "相关任务",
      send: "发送",
      ph_hermes: "告诉 Hermes 要怎么处理…", ph_answer: "回答 Agent 的问题…", ph_review: "要改什么…", ph_running: "补充给 Agent…",
      st_pending: "等待处理", st_processing: "Hermes 处理中", st_applied: "已处理", st_clarify: "需要你补充", st_ignored: "没有执行", st_failed: "处理失败",
      who_me: "我", who_hermes: "H", who_agent: "A", who_other: "他",
      b_decision: "需要你决定", b_chosen: "已选择「{c}」，等 Hermes 处理", b_answer: "Agent 在问", b_review: "Agent 交付了 v{n}",
      b_running: "Agent 在做", b_preparing: "已交给 Agent，准备中", b_waiting: "在等 {who}", b_closed: "已关闭",
      b_change: "变化", b_paused: "已暂停", ps_active: "进行中", ps_paused: "已暂停", ps_done: "已完成", ps_archived: "已归档",
      agentTitle: "交给 Agent", agentNote: "补充说明", agentStart: "什么时候开始", agentDone: "做到什么算完成",
      optional: "可不填", now: "现在", tonight: "今晚 20:00", tomorrowAm: "明天 09:00", pickTime: "选时间…",
      snoozeTitle: "延后到", waitTitle: "等别人", waitWho: "等谁", followWhen: "什么时候跟进", followTitle: "下次跟进",
      dueTitle: "截止日期", clear: "清除", save: "保存", projectTitle: "项目",
      tomorrow: "明天", nextMonday: "下周一", inThree: "3 天后", inWeek: "一周后", pickDate: "选日期…",
      setWaiting: "设为等待", snoozeTo: "延后到 {d}",
      skip: "跳过", details: "详情", addLine: "补充一句再决定…", allDone: "都处理完了", backToday: "回到今天", ofN: "{i} / {n}",
      prevHanded: "上一件：{c}",
      delivery: "交付", deliveryNotes: "交付说明", files: "文件", preview: "预览", download: "下载", notCopied: "未保存",
      question: "问题", failure: "失败", version: "v{n}",
      updated: "数据 {m}前更新", updatedNow: "数据刚刚更新", justNow: "刚刚", minutes: "{n} 分钟", hours: "{n} 小时", days: "{n} 天",
      noData: "还没有数据", stale: "部分来源同步失败",
      theme: "外观", light: "亮色", dark: "暗色", system: "跟随系统", logout: "退出登录",
      dayToDue: "天后截止", dueToday: "今天截止", overdueBy: "已逾期 {n} 天",
      failed: "没有成功：{e}", offline: "连不上服务器",
      projGoal: "目标", projNext: "下一步", projBlockers: "卡在哪", projRecent: "最近动态", projRepos: "仓库",
      openTasks: "未完成", closedTasks: "已关闭", nOpen: "未完成", nDecide: "要决定", nAgent: "Agent",
      noResults: "没有找到", r_tasks: "任务", r_projects: "项目", r_sources: "来源",
      doneWeek: "本周完成", snoozedN: "已延后",
      weekday: ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],
    },
    en: {
      today: "Today", decide: "Decide", agent: "Agent", waiting: "Waiting", all: "All", projects: "Projects", search: "Search",
      searchHint: "Search tasks, projects, mail…", nextThing: "Next up", noNext: "Nothing pressing",
      g_decide: "To decide", g_mine: "My to-do", g_agent_wait: "Agent needs you", g_agent_run: "Agent working",
      g_waiting: "Waiting on others", showAll: "All {n}", empty: "No open tasks here", decideOneByOne: "One by one",
      k_decision: "To decide", k_todo: "To-do", k_answer: "Agent asks", k_review: "To review", k_running: "Agent working",
      k_waiting: "Waiting", k_done: "Done", k_cancelled: "Cancelled",
      p_status: "Status", p_actor: "Who", p_due: "Due", p_follow: "Follow up", p_project: "Project", p_source: "Source",
      p_snooze: "Snoozed to", a_me: "Me", a_agent: "Agent", a_external: "Someone else", none: "—", noProject: "No project",
      agree: "Agree", decline: "Decline", toAgent: "Assign to Agent…", setTodo: "My to-do", markDone: "Done", cancel: "Cancel",
      done: "Done", snooze: "Snooze…", waitFor: "Wait for…", answer: "Answer", selfDo: "I'll do it", pause: "Pause",
      resume: "Resume", accept: "Accept", revise: "Request changes", viewDelivery: "Open delivery", selfEdit: "I'll edit it",
      supplement: "Add info", gotIt: "Got it", stillWaiting: "Still waiting…", chaser: "Draft a chaser", reopen: "Reopen",
      more: "⋯ More", m_status: "Status", m_time: "Time", m_who: "Who", m_other: "Other",
      dueDate: "Due date…", followDate: "Follow-up date…", unsnooze: "Unsnooze", focusOn: "Focus", focusOff: "Unfocus",
      meDo: "I'll do it", pauseAgent: "Pause agent", resumeAgent: "Resume agent", setProject: "Project…",
      openNotion: "Open in Notion", copyLink: "Copy link", copied: "Link copied",
      nextStep: "Next step", doneCriteria: "Done when", summary: "Summary", sources: "Sources", notes: "My notes",
      discussion: "Discussion", noDiscussion: "No discussion yet", history: "Changes", links: "Related tasks",
      send: "Send",
      ph_hermes: "Tell Hermes what to do…", ph_answer: "Answer the agent…", ph_review: "What should change…",
      ph_running: "Add info for the agent…",
      st_pending: "Waiting", st_processing: "Hermes on it", st_applied: "Done", st_clarify: "Needs more from you", st_ignored: "Not done", st_failed: "Failed",
      who_me: "Me", who_hermes: "H", who_agent: "A", who_other: "?",
      b_decision: "Needs your decision", b_chosen: "Chose “{c}”; Hermes will handle it", b_answer: "The agent asks",
      b_review: "The agent delivered v{n}", b_running: "The agent is working", b_preparing: "Assigned to the agent, starting",
      b_waiting: "Waiting on {who}", b_closed: "Closed", b_change: "What changed", b_paused: "Paused", ps_active: "Active", ps_paused: "Paused", ps_done: "Done", ps_archived: "Archived",
      agentTitle: "Assign to Agent", agentNote: "Anything to add", agentStart: "Start", agentDone: "Done when",
      optional: "Optional", now: "Now", tonight: "Tonight 20:00", tomorrowAm: "Tomorrow 09:00", pickTime: "Pick a time…",
      snoozeTitle: "Snooze until", waitTitle: "Wait for someone", waitWho: "Waiting on", followWhen: "Follow up",
      followTitle: "Next follow-up", dueTitle: "Due date", clear: "Clear", save: "Save", projectTitle: "Project",
      tomorrow: "Tomorrow", nextMonday: "Next Monday", inThree: "In 3 days", inWeek: "In a week", pickDate: "Pick a date…",
      setWaiting: "Set waiting", snoozeTo: "Snooze to {d}",
      skip: "Skip", details: "Details", addLine: "Add a line first…", allDone: "All handled", backToday: "Back to today", ofN: "{i} / {n}",
      prevHanded: "Previous: {c}",
      delivery: "Delivery", deliveryNotes: "Delivery notes", files: "Files", preview: "Preview", download: "Download",
      notCopied: "not kept", question: "Question", failure: "Failure", version: "v{n}",
      updated: "Data updated {m} ago", updatedNow: "Data just updated", justNow: "just now", minutes: "{n} min", hours: "{n} h", days: "{n} d",
      noData: "No data yet", stale: "Some sources failed",
      theme: "Theme", light: "Light", dark: "Dark", system: "System", logout: "Sign out",
      dayToDue: "days to due", dueToday: "Due today", overdueBy: "Overdue by {n} d",
      failed: "Did not work: {e}", offline: "Cannot reach the server",
      projGoal: "Goal", projNext: "Next step", projBlockers: "Blockers", projRecent: "Recent", projRepos: "Repos",
      openTasks: "Open", closedTasks: "Closed", nOpen: "open", nDecide: "to decide", nAgent: "agent",
      noResults: "Nothing found", r_tasks: "Tasks", r_projects: "Projects", r_sources: "Sources",
      doneWeek: "Done this week", snoozedN: "Snoozed",
      weekday: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    },
  };
  let lang = "zh-CN";
  const t = (key, values) => {
    let s = (TEXT[lang] || TEXT.en)[key];
    if (s === undefined) s = TEXT.en[key] ?? key;
    if (values) for (const [k, v] of Object.entries(values)) s = s.replace("{" + k + "}", v);
    return s;
  };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // ------------------------------------------------------------------ state
  const S = {
    me: null, home: null, list: null, listKey: null, detail: null, detailId: null, more: false,
    target: "hermes", draft: "", stamp: null, projects: null, project: null, delivery: null, filePreview: null,
    search: { q: "", res: null, sel: 0 }, decide: null, sending: false, dialog: null,
    pressed: null, stale: false, outbox: null, shown: false, layer: "", opener: null, open: {},
  };

  // ------------------------------------------------------------------ server
  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-TM-Request": "1" },
    };
    let res;
    try {
      res = await fetch(path, { credentials: "same-origin", ...opts });
    } catch (e) {
      throw new Error(t("offline"));
    }
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("login");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || res.status);
    return data;
  }
  const rid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9]/g, "").slice(0, 32);
  // One request id per thing the user means to do: trying it again after a failure sends the same
  // id, so the server does it once even when only the answer was lost.
  const rids = new Map();
  const ridFor = (key) => { if (!rids.has(key)) rids.set(key, rid()); return rids.get(key); };

  function toast(text, error) {
    const el = document.createElement("div");
    el.className = "toast" + (error ? " error" : "");
    el.setAttribute("role", "status");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), error ? 5000 : 2200);
  }

  // ------------------------------------------------------------------ dates
  const DAY = 86400000;
  const dateOf = (iso) => new Date(iso + "T00:00:00Z");
  const isoOf = (d) => d.toISOString().slice(0, 10);
  const todayIso = () => (S.me ? S.me.today : isoOf(new Date()));
  const addDays = (n) => isoOf(new Date(dateOf(todayIso()).getTime() + n * DAY));
  const nextWeekday = (wd) => {
    const d = dateOf(todayIso());
    const gap = ((wd - d.getUTCDay() + 7) % 7) || 7;
    return addDays(gap);
  };
  const daysTo = (iso) => Math.round((dateOf(iso) - dateOf(todayIso())) / DAY);
  function day(iso) {
    if (!iso) return "";
    const n = daysTo(iso.slice(0, 10));
    if (lang === "zh-CN" && n === 0) return "今天";
    if (lang === "zh-CN" && n === 1) return "明天";
    if (lang === "zh-CN" && n === -1) return "昨天";
    if (lang !== "zh-CN" && n === 0) return "today";
    if (lang !== "zh-CN" && n === 1) return "tomorrow";
    const d = dateOf(iso.slice(0, 10));
    const md = `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
    return Math.abs(n) < 7 ? `${t("weekday")[d.getUTCDay()]} ${md}` : md;
  }
  function longDay(iso) {
    const d = dateOf(iso);
    return lang === "zh-CN" ? `${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${t("weekday")[d.getUTCDay()]}`
      : `${t("weekday")[d.getUTCDay()]} ${d.getUTCDate()} ${d.toLocaleString("en", { month: "short", timeZone: "UTC" })}`;
  }
  function ago(iso) {
    if (!iso) return "";
    const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (m < 1) return t("justNow");
    if (m < 60) return t("minutes", { n: m });
    if (m < 60 * 48) return t("hours", { n: Math.round(m / 60) });
    return t("days", { n: Math.round(m / 1440) });
  }
  function stamp(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const same = isoOf(new Date(d.getTime() - d.getTimezoneOffset() * 60000)) === todayIso();
    const hm = d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
    return same ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }

  // ------------------------------------------------------------------ routes
  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    switch (parts[0]) {
      case "projects": return { page: parts[1] ? "project" : "projects", id: parts[1] || null };
      case "delivery": return { page: "delivery", id: parts[1] };
      case "search": return { page: "search" };
      case "decide": return { page: "decide" };
      case "tasks": return { page: "tasks", view: parts[1] || "today", task: parts[2] || null };
      default: return { page: "tasks", view: "today", task: null };
    }
  }
  const go = (hash) => { if (location.hash !== hash) location.hash = hash; else render(); };
  const taskHash = (id, view) => `#/tasks/${encodeURIComponent(view || route().view || "today")}/${encodeURIComponent(id)}`;

  // ------------------------------------------------------------------ loading
  async function loadHome() { S.home = await api("/api/home"); }
  async function loadList(view) {
    if (view === "all" || view.startsWith("p:")) {
      const q = view === "all" ? "" : "?project=" + encodeURIComponent(view.slice(2));
      S.list = await api("/api/tasks" + q);
      S.listKey = view;
    }
  }
  async function loadDetail(id) {
    const data = await api("/api/tasks/" + encodeURIComponent(id));
    const r = route();
    if (r.page === "tasks" && r.task !== id) return;   // the user has moved on to another task
    if (S.detailId !== id) { S.more = false; S.bannerOpen = false; S.draft = ""; }
    S.detail = data; S.detailId = id;
  }

  async function load() {
    const r = route();
    const jobs = [loadHome()];
    if (r.page === "tasks") {
      jobs.push(loadList(r.view));
      if (r.task) jobs.push(loadDetail(r.task)); else { S.detail = null; S.detailId = null; }
    } else if (r.page === "projects") {
      jobs.push(api("/api/projects").then((p) => { S.projects = p; }));
    } else if (r.page === "project") {
      jobs.push(api("/api/projects/" + encodeURIComponent(r.id)).then((p) => { if (route().id === r.id) S.project = p; }));
    } else if (r.page === "delivery") {
      jobs.push(api("/api/deliveries/" + encodeURIComponent(r.id)).then(async (d) => {
        if (route().id !== r.id) return;
        if (!S.delivery || S.delivery.version.id !== d.version.id) S.filePreview = null;
        S.delivery = d;
        await loadDetail(d.task.id);
      }));
    }
    await Promise.all(jobs);
  }

  async function refresh() {
    S.stale = false;
    try {
      await load();
      render();
    } catch (e) {
      if (e.message !== "login") toast(t("failed", { e: e.message }), true);
    }
  }

  // A change from elsewhere waits while the user is in the middle of something: a dialog, the
  // search, a write, or typing. It shows when that ends (settle).
  const typing = () => {
    const el = document.activeElement;
    return !!el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && !["checkbox", "radio"].includes(el.type)));
  };
  const busy = () => !!(S.dialog || S.palette || S.pressed || S.sending || typing());
  async function poll() {
    if (document.hidden) return;
    try {
      const { stamp: now } = await api("/api/stamp");
      if (S.stamp !== null && now !== S.stamp) {
        if (busy()) S.stale = true; else await refresh();
      }
      S.stamp = now;
    } catch (e) { /* the next poll tries again */ }
  }
  function settle() {
    if (S.stale && !busy()) refresh();
  }

  // ------------------------------------------------------------------ pieces
  const kindLabel = (c) => (c.kind === "closed" ? t(c.status === "done" ? "k_done" : "k_cancelled") : t("k_" + c.kind));
  const kindTone = (c) => ({ decision: "warn", answer: "accent", review: "accent", running: "", waiting: "", todo: "", closed: "ok" })[c.kind] || "";
  const PROJECT_COLORS = ["#5B47E0", "#E0457B", "#0E9F6E", "#D97706", "#2F80ED", "#9B51E0", "#00A3A3", "#C2410C"];
  // The colours of Notion's built-in icons, so a project looks the same as on its Notion page.
  const NOTION_COLORS = { blue: "#337EA9", green: "#448361", red: "#D44C47", purple: "#9065B0", orange: "#D9730D",
    yellow: "#CB912F", brown: "#9F6B53", pink: "#C14C8A", gray: "#787774", lightgray: "#A3A29E" };
  const projectColor = (p) => {
    if (NOTION_COLORS[p.color]) return NOTION_COLORS[p.color];
    let h = 0;
    for (const ch of String(p.id || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return PROJECT_COLORS[h % PROJECT_COLORS.length];
  };
  // Notion's image of a built-in icon, the emoji, or else a square in the project's colour.
  const pdot = (p) => (!p ? "" : p.image ? `<img class="picon" src="${esc(p.image)}" alt="">`
    : p.icon ? `<span class="picon">${esc(p.icon)}</span>` : `<span class="pdot" style="background:${projectColor(p)}"></span>`);

  function metaOf(c, view) {
    const bits = [];
    if (c.project && view !== "p:" + c.project.id) bits.push(esc(c.project.title));
    if (c.kind === "waiting" && c.waiting_on) bits.push(esc(c.waiting_on));
    if (c.next_step && c.kind !== "waiting") bits.push(esc(c.next_step));
    return bits.join(" · ");
  }
  function badgeOf(c) {
    if (c.kind === "waiting" && c.follow_up) return [day(c.follow_up), c.follow_up_due ? "warn" : ""];
    if (c.due) return [day(c.due), c.overdue || daysTo(c.due) <= 1 ? "warn" : ""];
    if (["answer", "review"].includes(c.kind)) return [kindLabel(c), "accent"];
    if (c.focus) return ["★", "accent"];
    return null;
  }
  function row(c, view) {
    const on = route().task === c.id ? " on" : "";
    const badge = badgeOf(c);
    return `<button class="row${on}" data-go="${esc(taskHash(c.id, view))}">
      <span class="main"><span class="title">${esc(c.title)}</span>
      <span class="meta">${view === "p:" + (c.project && c.project.id) ? "" : pdot(c.project)}${metaOf(c, view) || esc(kindLabel(c))}</span></span>
      ${badge ? `<span class="pill ${badge[1]}">${esc(badge[0])}</span>` : ""}</button>`;
  }

  const GROUP_TONE = { decide: "warn", mine: "accent", agent_wait: "accent", agent_run: "", waiting: "" };
  function tile(key, cards, view, limit) {
    if (!cards.length) return "";
    const open = S.expanded && S.expanded[key];
    const shown = open || !limit ? cards : cards.slice(0, limit);
    const rest = cards.length - shown.length;
    return `<section class="tile" aria-label="${esc(t("g_" + key))}">
      <div class="tile-head"><b>${cards.length}</b><span class="mark ${GROUP_TONE[key]}"></span><span>${esc(t("g_" + key))}</span></div>
      ${shown.map((c) => row(c, view)).join("")}
      ${rest > 0 ? `<button class="tile-more" data-expand="${key}">${esc(t("showAll", { n: cards.length }))}</button>` : ""}
    </section>`;
  }

  function freshness() {
    const sources = (S.home && S.home.fresh.sources) || [];
    const at = sources.map((s) => s.at).filter(Boolean).sort().pop();
    const failing = sources.some((s) => s.failures > 0);
    const old = at ? (Date.now() - new Date(at).getTime()) / 60000 > 60 : true;
    const recent = at && Date.now() - new Date(at).getTime() < 60000;
    const text = !at ? t("noData") : recent ? t("updatedNow") : t("updated", { m: ago(at) });
    return `<span class="fresh" title="${failing ? esc(t("stale")) : ""}"><span class="dot${failing || old ? " stale" : ""}"></span>${esc(text)}</span>`;
  }

  // ------------------------------------------------------------------ navigation
  function navItems() {
    const n = (S.home && S.home.counts) || {};
    return [
      { key: "today", hash: "#/tasks/today", glyph: "◎", count: (n.decide || 0) + (n.mine || 0) + (n.agent_wait || 0) },
      { key: "decide", hash: "#/tasks/decide", glyph: "◆", count: n.decide || 0, badge: n.decide },
      { key: "agent", hash: "#/tasks/agent", glyph: "✦", count: (n.agent_wait || 0) + (n.agent_run || 0), badge: n.agent_wait },
      { key: "waiting", hash: "#/tasks/waiting", glyph: "◷", count: n.waiting || 0 },
      { key: "all", hash: "#/tasks/all", glyph: "≡", count: S.home ? S.home.open : 0 },
    ];
  }
  function currentNav(r) {
    if (r.page === "tasks") return r.view.startsWith("p:") ? r.view : r.view;
    if (r.page === "project" || r.page === "projects") return "projects";
    if (r.page === "decide") return "decide";
    if (r.page === "search") return "search";
    return "";
  }
  function sidebar(r) {
    const cur = currentNav(r);
    const projects = ((S.home && S.home.projects) || []).filter((p) => p.status === "active" || p.counts.open);
    return `<nav class="nav" aria-label="${esc(t("today"))}">
      <div class="brand"><span class="logo">TM</span><span>Todo Manager</span></div>
      <button class="nav-search" data-palette="1"><span>${esc(t("search"))}</span><span>⌘K</span></button>
      <div class="nav-group">${navItems().map((i) => `<button class="nav-item${cur === i.key ? " on" : ""}" data-go="${i.hash}">
        <span class="label">${esc(t(i.key))}</span><span class="count">${i.count === "" ? "" : esc(i.count)}</span></button>`).join("")}</div>
      ${projects.length ? `<div class="nav-group"><button class="nav-head${cur === "projects" ? " on" : ""}" data-go="#/projects">${esc(t("projects"))} ›</button>
        ${projects.map((p) => `<button class="nav-item${cur === "p:" + p.id ? " on" : ""}" data-go="#/tasks/p:${esc(p.id)}">
          ${pdot(p)}<span class="label">${esc(p.title)}</span>
          <span class="count">${p.counts.open || ""}</span></button>`).join("")}</div>` : ""}
      <div class="nav-foot">${freshness()}
        <button class="linkish" data-theme-cycle="1">${esc(t("theme"))}: ${esc(t(themeName()))}</button>
        <form method="post" action="/logout"><button class="linkish" type="submit">${esc(t("logout"))}</button></form></div>
    </nav>`;
  }
  function rail(r) {
    const cur = currentNav(r);
    return `<nav class="rail" aria-label="${esc(t("today"))}"><span class="logo">TM</span>
      ${navItems().concat([{ key: "projects", hash: "#/projects", glyph: "▦" }]).map((i) => `<button class="rail-item${cur === i.key ? " on" : ""}" data-go="${i.hash}" aria-label="${esc(t(i.key))}">
        <span class="g">${i.glyph}</span>${esc(t(i.key))}${i.badge ? `<span class="badge">${i.badge}</span>` : ""}</button>`).join("")}
      <button class="rail-item" data-palette="1" aria-label="${esc(t("search"))}"><span class="g">⌕</span>${esc(t("search"))}</button>
      <span class="grow"></span>
      <button class="rail-item" data-theme-cycle="1" aria-label="${esc(t("theme"))}"><span class="g">◐</span></button></nav>`;
  }
  function tabbar(r) {
    const cur = currentNav(r);
    const n = (S.home && S.home.counts) || {};
    const tabs = [["today", "#/tasks/today", "◎"], ["decide", "#/decide", "◆", n.decide], ["agent", "#/tasks/agent", "✦", n.agent_wait],
      ["projects", "#/projects", "▦"], ["search", "#/search", "⌕"]];
    return `<nav class="tabbar" aria-label="${esc(t("today"))}">${tabs.map(([k, h, g, b]) => `<button class="tab${cur === k ? " on" : ""}" data-go="${h}">
      <span class="g">${g}</span>${esc(t(k))}${b ? `<span class="badge">${b}</span>` : ""}</button>`).join("")}</nav>`;
  }

  // ------------------------------------------------------------------ task lists
  function viewTitle(view) {
    if (view.startsWith("p:")) {
      const p = ((S.home && S.home.projects) || []).find((x) => x.id === view.slice(2));
      return p ? p.title : t("projects");
    }
    return t(view);
  }
  function hero(view) {
    const mine = S.home.groups.mine;
    const next = mine.find((c) => c.focus || c.due) || mine[0];
    if (!next) return "";
    let n = "";
    if (next.due) {
      const d = daysTo(next.due);
      n = d > 0 ? `<b>${d}</b><span>${esc(t("dayToDue"))} · ${esc(day(next.due))}</span>`
        : `<span>${esc(d === 0 ? t("dueToday") : t("overdueBy", { n: -d }))}</span>`;
    }
    return `<button class="hero" data-go="${esc(taskHash(next.id, view))}"><span class="k">${esc(t("nextThing"))}</span>
      <span class="t">${esc(next.title)}</span>${n ? `<span class="n">${n}</span>` : ""}</button>`;
  }
  function listView(view) {
    const g = S.home.groups;
    let body = "";
    if (view === "today") {
      const waitDue = g.waiting.filter((c) => c.follow_up_due);
      body = hero(view)
        + `<div class="stats"><button class="stat warn" data-go="#/decide"><b>${g.decide.length}</b><span>${esc(t("g_decide"))}</span></button>
           <button class="stat" data-go="#/tasks/agent"><b>${g.agent_wait.length}</b><span>${esc(t("g_agent_wait"))}</span></button>
           <div class="stat"><b>${S.home.done_week}</b><span>${esc(t("doneWeek"))}</span></div></div>`
        + tile("decide", g.decide, view, 5) + tile("agent_wait", g.agent_wait, view, 5) + tile("mine", g.mine, view, 8)
        + tile("agent_run", g.agent_run, view, 4) + tile("waiting", waitDue.length ? waitDue : [], view, 5);
    } else if (view === "decide") {
      body = (g.decide.length ? `<button class="btn primary" data-go="#/decide">${esc(t("decideOneByOne"))}</button>` : "")
        + tile("decide", g.decide, view);
    } else if (view === "agent") {
      body = tile("agent_wait", g.agent_wait, view) + tile("agent_run", g.agent_run, view);
    } else if (view === "waiting") {
      body = tile("waiting", g.waiting, view);
    } else if (S.list && S.listKey === view) {
      const by = {};
      for (const c of S.list) (by[c.kind] = by[c.kind] || []).push(c);
      const order = [["decision", "decide"], ["answer", "agent_wait"], ["review", "agent_wait"], ["todo", "mine"],
        ["running", "agent_run"], ["waiting", "waiting"]];
      const seen = new Set();
      for (const [kind, key] of order) {
        if (seen.has(key)) continue;
        seen.add(key);
        const cards = order.filter((o) => o[1] === key).flatMap((o) => by[o[0]] || []);
        body += tile(key, cards, view, view === "all" ? 0 : 10);
      }
      if (by.closed && by.closed.length) {
        body += `<section class="tile"><div class="tile-head"><b>${by.closed.length}</b><span class="mark ok"></span><span>${esc(t("closedTasks"))}</span></div>
          ${by.closed.slice(0, 30).map((c) => row(c, view)).join("")}</section>`;
      }
    }
    const sub = view === "today" ? longDay(todayIso()) : "";
    return `<section class="list" id="list-scroll" aria-label="${esc(viewTitle(view))}">
      <div class="view-head"><h1>${esc(viewTitle(view))}</h1><span class="sub">${esc(sub)}</span></div>
      ${body.trim() ? body : `<div class="empty">${esc(t("empty"))}</div>`}</section>`;
  }

  // ------------------------------------------------------------------ task detail
  function actionsFor(d) {
    const work = d.agent_work;
    const active = work && !["ended", "cancelled", "quarantined"].includes(work.status);
    const pause = active ? (work.status === "paused" || work.pause_requested ? ["resume", "act:resume"] : ["pause", "act:pause"]) : null;
    const latest = work && work.versions.filter((v) => v.kind === "result").pop();
    const list = {
      decision: [["toAgent", "dlg:agent", "primary"], ["setTodo", "act:todo"], ["waitFor", "dlg:wait"], ["snooze", "dlg:snooze"],
        ["markDone", "act:done"], ["cancel", "act:cancel"]],
      todo: [["done", "act:done", "primary"], ["toAgent", "dlg:agent"], ["snooze", "dlg:snooze"], ["waitFor", "dlg:wait"],
        ["cancel", "act:cancel"]],
      answer: [["answer", "compose:agent", "primary"], ["selfDo", "act:todo"], pause, ["cancel", "act:cancel"]],
      review: [["accept", "act:done", "primary"], ["revise", "compose:agent"], latest ? ["viewDelivery", "go:#/delivery/" + latest.id] : null,
        ["selfEdit", "act:todo"], ["cancel", "act:cancel"]],
      running: [["supplement", "compose:agent"], pause, ["selfDo", "act:todo"], ["cancel", "act:cancel"]],
      waiting: [["gotIt", "act:todo", "primary"], ["stillWaiting", "dlg:followup"], ["chaser", "say:chaser"], ["done", "act:done"],
        ["cancel", "act:cancel"]],
      closed: [["reopen", "act:reopen", "primary"]],
    }[d.kind] || [];
    return list.filter(Boolean);
  }
  function menuFor(d) {
    const work = d.agent_work;
    const active = work && !["ended", "cancelled", "quarantined"].includes(work.status);
    const closed = d.kind === "closed";
    const groups = [
      ["m_status", closed ? [["reopen", "act:reopen"]] : [["setTodo", "act:todo"], ["markDone", "act:done"], ["cancel", "act:cancel", "danger"]]],
      ["m_time", closed ? [] : [d.snoozed ? ["unsnooze", "act:snooze"] : ["snooze", "dlg:snooze"], ["dueDate", "dlg:due"],
        ["followDate", "dlg:followup"], d.focus ? ["focusOff", "act:focus:0"] : ["focusOn", "act:focus:1"]]],
      ["m_who", closed ? [] : [active ? null : ["toAgent", "dlg:agent"], ["meDo", "act:todo"], ["waitFor", "dlg:wait"],
        active ? (work.status === "paused" || work.pause_requested ? ["resumeAgent", "act:resume"] : ["pauseAgent", "act:pause"]) : null]],
      ["m_other", [["setProject", "dlg:project"], d.notion_url ? ["openNotion", "url:" + d.notion_url] : null, ["copyLink", "copy"]]],
    ];
    return groups.map(([title, items]) => [title, items.filter(Boolean)]).filter(([, items]) => items.length);
  }
  const bannerText = (text) => (text.length > 160 && !S.bannerOpen
    ? `<span class="v clamp" data-unclamp="1">${esc(text)}</span>` : `<span class="v">${esc(text)}</span>`);
  // An action button in the colour of its Notion board column: Agent purple, to-do blue, done green,
  // cancel red, waiting yellow, later grey.
  const TONES = { "dlg:agent": "purple", "compose:agent": "purple", "act:pause": "purple", "act:resume": "purple",
    "act:todo": "blue", "act:reopen": "blue", "act:done": "green", "act:cancel": "red",
    "dlg:wait": "yellow", "dlg:followup": "yellow", "dlg:snooze": "gray" };
  const btnClass = (does, primary) => "btn" + (TONES[does] ? " tone-" + TONES[does] : "") + (primary ? " primary" : "");
  function button(item, cls) {
    const [label, does, tone] = item;
    const look = cls ? cls + (tone ? " " + tone : "") : btnClass(does, tone === "primary");
    return `<button class="${look}" data-do="${esc(does)}">${esc(t(label))}</button>`;
  }

  function banner(d) {
    const work = d.agent_work;
    const latest = work && work.versions.filter((v) => v.kind === "result").pop();
    if (d.kind === "closed") return ["ok", t("b_closed") + " · " + kindLabel(d), d.closed_reason || ""];
    if (d.kind === "answer") return ["accent", t("b_answer"), d.agent_question || (work && work.question) || ""];
    if (d.kind === "review" && latest) {
      return ["accent", t("b_review", { n: latest.seq }), latest.summary || "", "#/delivery/" + latest.id];
    }
    if (d.kind === "running") {
      if (!work || ["ended", "cancelled"].includes(work.status)) return ["", t("b_preparing"), ""];
      return ["", work.status === "paused" ? t("b_paused") : t("b_running"), work.goal || ""];
    }
    if (d.kind === "decision") {
      if (d.decision === "agree" || d.decision === "decline") return ["warn", t("b_chosen", { c: t(d.decision) }), ""];
      return ["warn", t("b_decision"), d.summary || ""];
    }
    if (d.kind === "waiting") return ["", t("b_waiting", { who: d.waiting_on || t("a_external") }), d.follow_up ? `${t("p_follow")} ${day(d.follow_up)}` : ""];
    return null;
  }

  function threadView(d) {
    const sending = S.outbox && S.outbox.task === d.id ? `<div class="msg me pending"><span class="avatar me">${esc(t("who_me"))}</span>
      <div class="body"><div class="bubble">${esc(S.outbox.text)}</div><div class="info"><span class="pill">${esc(t("st_pending"))}</span></div></div></div>` : "";
    if (!d.thread.length) return sending || `<span class="muted">${esc(t("noDiscussion"))}</span>`;
    return d.thread.map((m) => {
      const cls = m.who === "me" ? "me" : m.who === "agent" ? "agent" : "";
      let info = stamp(m.at);
      if (m.who === "me" && m.status) {
        const tone = { processing: "accent", applied: "ok", clarify: "warn" }[m.status] || "";
        info = `<span class="pill ${tone}">${esc(t("st_" + m.status))}</span><span>${esc(info)}</span>`;
      } else {
        info = `<span>${esc(info)}</span>`;
      }
      if (m.who === "agent" && m.delivery && m.kind === "result") {
        info += `<a href="#/delivery/${esc(m.delivery)}">${esc(t("version", { n: m.seq }))}</a>`;
      }
      if (m.to === "agent") info += `<span>→ ${esc(t("agent"))}</span>`;
      const choice = m.choice ? `<span class="pill accent">${esc(t({ agree: "agree", decline: "decline", agent: "agentTitle" }[m.choice] || "agentTitle"))}</span>` : "";
      const bubble = m.text ? `<div class="bubble">${esc(m.text)}</div>` : "";
      return `<div class="msg ${cls}"><span class="avatar ${cls}">${esc(t("who_" + m.who))}</span>
        <div class="body">${choice}${bubble}<div class="info">${info}</div></div></div>`;
    }).join("") + sending;
  }

  // One box: everything goes to Hermes, which relays to the agent what is meant for it.
  function composer(d) {
    const ph = t({ answer: "ph_answer", review: "ph_review", running: "ph_running" }[d.kind] || "ph_hermes");
    return `<div class="composer">
      <div class="compose-row"><label class="sr" for="draft">${esc(ph)}</label>
        <textarea id="draft" rows="2" placeholder="${esc(ph)}"></textarea>
        <button class="btn primary" data-send="1">${esc(t("send"))}</button></div>
    </div>`;
  }

  function prop(label, value, opts) {
    const o = opts || {};
    const v = value ? `<span class="v${o.warn ? " warn" : ""}">${esc(value)}</span>` : `<span class="v none">${esc(t("none"))}</span>`;
    return o.does ? `<button class="prop" data-do="${o.does}"><span class="k">${esc(label)}</span>${v}</button>`
      : `<div class="prop"><span class="k">${esc(label)}</span>${v}</div>`;
  }

  function detailBody(d, opts) {
    const compact = opts && opts.compact;
    const b = banner(d);
    const actor = { me: t("a_me"), agent: t("a_agent"), external: t("a_external") }[d.next_actor];
    const props = [
      prop(t("p_status"), kindLabel(d)), prop(t("p_actor"), actor),
      prop(t("p_due"), d.due ? day(d.due) : "", { warn: d.overdue, does: d.kind === "closed" ? "" : "dlg:due" }),
      d.kind === "waiting" || d.follow_up ? prop(t("p_follow"), d.follow_up ? day(d.follow_up) : "", { warn: d.follow_up_due, does: "dlg:followup" }) : "",
      d.snoozed ? prop(t("p_snooze"), day(d.snooze_until), { does: "dlg:snooze" }) : "",
      prop(t("p_project"), d.project ? d.project.title : "", { does: "dlg:project" }),
      d.source ? prop(t("p_source"), d.source) : "",
    ].join("");
    const more = S.more ? `<div class="menu">${menuFor(d).map(([title, items]) => `<div class="menu-group">
      <span class="menu-title">${esc(t(title))}</span>${items.map((i) => button(i, "menu-item")).join("")}</div>`).join("")}</div>` : "";
    const block = (label, text) => (text ? `<div class="block"><span class="label">${esc(label)}</span><span class="v">${esc(text)}</span></div>` : "");
    const sources = d.sources.length ? `<div class="block"><span class="label">${esc(t("sources"))}</span><div class="sources">
      ${d.sources.map((s) => `<div class="source"><span class="tag">${esc(s.source)}</span>
        ${s.url ? `<a class="text" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.author || s.id)}</a>`
          : `<span class="text">${esc(s.title || s.author || s.id)}</span>`}<span class="when">${esc(day((s.at || "").slice(0, 10)))}</span></div>`).join("")}</div></div>` : "";
    const history = d.history.length ? `<details data-keep="history"><summary>${esc(t("history"))}</summary><div class="history">
      ${d.history.slice().reverse().map((h) => `<span>${esc(stamp(h.at))} · ${esc(h.actor)}/${esc(h.origin)} · ${esc(Object.keys(h.changes).join(", "))}</span>`).join("")}</div></details>` : "";
    return `<div class="back"><button data-back="1" aria-label="back">‹</button><span class="muted">${esc(d.project ? d.project.title : "")}</span></div>
      <div class="detail-body" id="detail-scroll">
        ${compact ? "" : `<div class="crumb"><span class="project">${pdot(d.project)} ${esc(d.project ? d.project.title : t("noProject"))}</span>
          <span class="pill ${kindTone(d)}">${esc(kindLabel(d))}</span><span class="grow"></span>
          ${d.notion_url ? `<a href="${esc(d.notion_url)}" target="_blank" rel="noopener noreferrer">Notion ↗</a>` : ""}</div>`}
        <h2>${esc(d.title)}</h2>
        ${compact ? "" : `<div class="props">${props}</div>`}
        ${b ? `<section class="banner ${b[0]}"><span class="k">${esc(b[1])}</span>${b[2] ? bannerText(b[2]) : ""}
          ${b[3] ? `<a href="${esc(b[3])}">${esc(t("viewDelivery"))} →</a>` : ""}</section>` : ""}
        ${d.change_note && !compact && !["answer", "review", "running", "closed"].includes(d.kind) ? `<section class="banner accent"><span class="k">${esc(t("b_change"))} · ${esc(stamp(d.change_note_at))}</span>${bannerText(d.change_note)}</section>` : ""}
        <div class="actions">${actionsFor(d).map((a) => button(a)).join("")}
          <button class="btn ghost" data-more="1" aria-expanded="${S.more}">${esc(t("more"))}</button></div>
        ${more}
        ${compact ? "" : block(t("nextStep"), d.next_step) + block(t("doneCriteria"), d.done_criteria) + block(t("summary"), d.kind === "decision" ? "" : d.summary)}
        ${compact ? "" : sources}
        ${!compact && d.notes ? `<div class="block"><span class="label">${esc(t("notes"))}</span><div class="notes">${esc(d.notes)}</div></div>` : ""}
        ${!compact && d.links.length ? `<div class="block"><span class="label">${esc(t("links"))}</span>${d.links.map((l) =>
          `<a href="${esc(taskHash(l.task))}">${esc(l.title || l.task)}</a>`).join("")}</div>` : ""}
        <div class="thread"><span class="label">${esc(t("discussion"))}</span>${threadView(d)}</div>
        ${compact ? "" : history}
      </div>`;
  }

  // ------------------------------------------------------------------ pages
  function projectsPage() {
    const list = S.projects || [];
    return `<section class="page span" id="page-scroll"><div class="view-head"><h1>${esc(t("projects"))}</h1></div>
      <div class="tiles">${list.map((p) => `<button class="ptile" data-go="#/projects/${esc(p.id)}">
        <span class="name"><span class="icon" style="color:${projectColor(p)}">${p.image ? pdot(p) : esc(p.icon || "■")}</span>${esc(p.title)}</span>
        <span class="next">${esc(p.next_step || p.goal || "")}</span>
        <span class="nums"><span><b>${p.counts.open}</b>${esc(t("nOpen"))}</span>
          ${p.counts.decision ? `<span><b>${p.counts.decision}</b>${esc(t("nDecide"))}</span>` : ""}
          ${p.counts.agent ? `<span><b>${p.counts.agent}</b>${esc(t("nAgent"))}</span>` : ""}</span></button>`).join("")}</div></section>`;
  }
  function projectPage(r) {
    const p = S.project;
    if (!p || p.id !== r.id) return `<section class="page span"></section>`;
    const open = p.tasks.filter((c) => c.kind !== "closed");
    const closed = p.tasks.filter((c) => c.kind === "closed");
    const field = (label, text) => (text ? `<div class="block"><span class="label">${esc(label)}</span><span class="v">${esc(text)}</span></div>` : "");
    return `<section class="page span" id="page-scroll">
      <div class="crumb"><a href="#/projects">${esc(t("projects"))}</a><span>›</span></div>
      <div class="view-head"><h1>${p.image || p.icon ? pdot(p) + " " : ""}${esc(p.title)}</h1><span class="sub">${esc(t("ps_" + p.status))}</span></div>
      <div class="split"><div class="card"><span class="label">${esc(t("openTasks"))} · ${open.length}</span>
          ${open.map((c) => row(c, "p:" + p.id)).join("") || `<span class="muted">${esc(t("empty"))}</span>`}
          ${closed.length ? `<details data-keep="closed"><summary>${esc(t("closedTasks"))} · ${closed.length}</summary>${closed.slice(0, 50).map((c) => row(c, "p:" + p.id)).join("")}</details>` : ""}</div>
        <div class="card">${field(t("projGoal"), p.goal)}${field(t("projNext"), p.next_step)}${field(t("projBlockers"), p.blockers)}
          ${field(t("projRecent"), p.recent_activity)}${field(t("projRepos"), (p.repos || []).join("\n"))}</div></div></section>`;
  }

  function markdown(text) {
    // A small, safe subset: everything is escaped first, then a few marks are turned into tags.
    const lines = esc(text || "").split("\n");
    const out = [];
    let list = null, code = false, table = false, para = [];
    const link = (href, label) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    // A bare web address is a link too. It ends at a space, a quote or angle bracket, CJK punctuation,
    // or trailing ASCII punctuation (a closing parenthesis only when it has no opening one).
    const BARE = /https?:\/\/(?:(?!&lt;|&gt;|&quot;|&#39;)[^\s\uE000（）【】「」『』，。；：！？、])+/g;
    const inline = (s) => {
      const kept = [];   // code and links, out of reach of the later replacements
      const keep = (html) => `\uE000${kept.push(html) - 1}\uE001`;
      const bare = (url) => {
        let href = url;
        while (".,;:!?*".includes(href.slice(-1)) || (href.endsWith(")") && !href.includes("("))) href = href.slice(0, -1);
        return keep(link(href, href)) + url.slice(href.length);
      };
      return s.replace(/`([^`]+)`/g, (_, c) => keep(`<code>${c}</code>`))
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, href) => keep(link(href, label)))
        .replace(BARE, bare)
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\uE000(\d+)\uE001/g, (_, i) => kept[+i]);
    };
    const flush = () => {
      if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; }
      if (list) { out.push(`</${list}>`); list = null; }
    };
    for (const line of lines) {
      if (line.startsWith("```")) { flush(); out.push(code ? "</code></pre>" : "<pre><code>"); code = !code; continue; }
      if (code) { out.push(line + "\n"); continue; }
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const cells = line.trim().slice(1, -1).split("|").map((c) => c.trim());
        if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
        if (!table) { flush(); out.push("<table>"); table = true; out.push("<tr>" + cells.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr>"); }
        else out.push("<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
        continue;
      }
      if (table) { out.push("</table>"); table = false; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (h) { flush(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
      else if (li) {
        if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; }
        const kind = /^\s*\d+\./.test(line) ? "ol" : "ul";
        if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
        out.push(`<li>${inline(li[1])}</li>`);
      } else if (line.startsWith("&gt; ")) { flush(); out.push(`<blockquote>${inline(line.slice(5))}</blockquote>`); }
      else if (!line.trim()) flush();
      else { if (list) { out.push(`</${list}>`); list = null; } para.push(line); }
    }
    flush();
    if (table) out.push("</table>");
    if (code) out.push("</code></pre>");
    return out.join("");
  }

  function deliveryPage(r) {
    const x = S.delivery;
    const d = S.detail;
    if (!x || !d || x.version.id !== r.id) return `<section class="page span"></section>`;
    const results = x.delegation.versions.filter((v) => v.kind === "result" || v.id === x.version.id);
    const files = x.version.files;
    return `<section class="page span" id="page-scroll">
      <div class="crumb"><a href="${esc(taskHash(x.task.id))}">${esc(x.task.title)}</a><span>›</span><span>${esc(t("delivery"))}</span></div>
      <div class="view-head"><h1>${esc(x.delegation.title)}</h1></div>
      <div class="seg" role="tablist">${results.map((v) => `<button role="tab" class="${v.id === x.version.id ? "on" : ""}" data-go="#/delivery/${esc(v.id)}">
        ${esc(v.kind === "result" ? t("version", { n: v.seq }) : t(v.kind))}</button>`).join("")}</div>
      <div class="split"><div style="display:flex;flex-direction:column;gap:18px;min-width:0">
        <div class="card"><span class="label">${esc(t("deliveryNotes"))}</span>
          ${x.version.summary ? `<div class="banner accent"><span class="v">${esc(x.version.summary)}</span></div>` : ""}
          <div class="md">${markdown(x.body)}</div></div>
        ${files.length ? `<div class="card"><span class="label">${esc(t("files"))}</span><div class="files">
          ${files.map((f) => `<div class="file"><span class="name">${esc(f.name)}</span>
            ${f.available ? `<button class="btn small" data-preview="${f.index}">${esc(t("preview"))}</button>
              <a class="btn small" href="/api/deliveries/${esc(x.version.id)}/files/${f.index}" download="${esc(f.name)}">${esc(t("download"))}</a>`
              : `<span class="muted">${esc(t("notCopied"))}</span>`}</div>`).join("")}</div>
          ${S.filePreview ? `<pre class="preview">${esc(S.filePreview)}</pre>` : ""}</div>` : ""}
      </div><div class="card" style="padding:0;overflow:hidden">${detailBody(d, { compact: true }) + composer(d)}</div></div></section>`;
  }

  function searchResults(res, palette) {
    if (!res) return "";
    const hits = [];
    const add = (group, items, fn) => {
      if (items.length) hits.push(`<div class="group">${esc(t(group))}</div>` + items.map(fn).join(""));
    };
    let i = 0;
    const hit = (hash, label, meta) => {
      const on = palette && i++ === S.search.sel ? " on" : "";
      return `<button class="hit${on}" data-go="${esc(hash)}"><span class="t">${esc(label)}</span><span class="muted">${esc(meta || "")}</span></button>`;
    };
    add("r_tasks", res.tasks, (c) => hit(taskHash(c.id, "all"), c.title, kindLabel(c)));
    add("r_projects", res.projects, (p) => hit("#/projects/" + p.id, p.title, ""));
    add("r_sources", res.sources.filter((s) => s.task), (s) => hit(taskHash(s.task, "all"), s.title || s.author, s.source));
    return hits.length ? hits.join("") : `<div class="empty">${esc(t("noResults"))}</div>`;
  }
  function searchPage() {
    return `<section class="page span" id="page-scroll"><div class="view-head"><h1>${esc(t("search"))}</h1></div>
      <div class="card palette" style="max-width:none"><input id="search-input" type="search" placeholder="${esc(t("searchHint"))}" value="${esc(S.search.q)}" autocomplete="off">
      ${searchResults(S.search.res, false)}</div></section>`;
  }

  function decidePage() {
    const flow = S.decide;
    if (!flow) return `<section class="page span"></section>`;
    if (flow.i >= flow.cards.length) {
      return `<section class="page span"><div class="decide"><div class="done-hero"><b>${esc(t("ofN", { i: flow.done.length, n: flow.cards.length }))}</b>
        <span>${esc(t("allDone"))}</span></div>
        ${flow.done.map((x) => `<div class="row"><span class="main"><span class="title">${esc(x.title)}</span><span class="meta">${esc(x.choice)}</span></span></div>`).join("")}
        <button class="btn primary" data-go="#/tasks/today">${esc(t("backToday"))}</button></div></section>`;
    }
    const c = flow.cards[flow.i];
    const last = flow.done[flow.done.length - 1];
    const buttons = [["toAgent", "dlg:agent", "primary"], ["setTodo", "act:todo"], ["waitFor", "dlg:wait"], ["snooze", "dlg:snooze"],
      ["markDone", "act:done"], ["cancel", "act:cancel"]];
    return `<section class="page span"><div class="decide">
      <div class="view-head"><h1>${esc(t("g_decide"))}</h1><span class="sub">${esc(t("ofN", { i: flow.i + 1, n: flow.cards.length }))}</span></div>
      <div class="progress">${flow.cards.map((_, k) => `<span class="${k <= flow.i ? "on" : ""}"></span>`).join("")}</div>
      ${last ? `<span class="muted">${esc(t("prevHanded", { c: last.choice }))}</span>` : ""}
      <div class="decide-card"><span class="k">${esc([c.project && c.project.title, c.due && day(c.due)].filter(Boolean).join(" · "))}</span>
        <span class="t">${esc(c.title)}</span>${c.next_step ? `<span>${esc(c.next_step)}</span>` : ""}
        ${c.source ? `<span class="muted">${esc(c.source)}</span>` : ""}
        <a href="${esc(taskHash(c.id, "decide"))}">${esc(t("details"))} →</a></div>
      <div class="decide-grid">${buttons.map((b) => `<button class="${btnClass(b[1], b[2])}" data-decide="${b[1]}">${esc(t(b[0]))}</button>`).join("")}</div>
      <div class="compose-row"><label class="sr" for="decide-line">${esc(t("addLine"))}</label>
        <textarea id="decide-line" rows="1" placeholder="${esc(t("addLine"))}">${esc(flow.line || "")}</textarea>
        <button class="btn ghost" data-decide="skip">${esc(t("skip"))}</button></div></div></section>`;
  }

  // ------------------------------------------------------------------ dialogs
  function options(items, chosen) {
    return `<div class="options">${items.map(([value, label, sub]) => `<button class="option${chosen === value ? " on" : ""}" data-opt="${esc(value)}">
      ${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</button>`).join("")}</div>`;
  }
  const dateChoices = (kind) => {
    const list = kind === "snooze" ? [[addDays(1), t("tomorrow")], [nextWeekday(1), t("nextMonday")], [addDays(7), t("inWeek")]]
      : [[addDays(3), t("inThree")], [nextWeekday(1), t("nextMonday")], [addDays(7), t("inWeek")]];
    return list.map(([v, label]) => [v, label, day(v)]).concat([["pick", t("pickDate"), ""]]);
  };
  function dialogView() {
    const g = S.dialog;
    if (!g) return "";
    const d = g.task;
    let body = "", ok = "", okLabel = t("save");
    if (g.kind === "agent") {
      const start = g.start || "now";
      body = `<label class="field"><span class="label">${esc(t("agentNote"))}</span><textarea id="dlg-note" placeholder="${esc(t("optional"))}">${esc(g.note || "")}</textarea></label>
        <div class="field-row"><label class="field"><span class="label">${esc(t("agentStart"))}</span>
          <select id="dlg-start"><option value="now"${start === "now" ? " selected" : ""}>${esc(t("now"))}</option>
          <option value="tonight"${start === "tonight" ? " selected" : ""}>${esc(t("tonight"))}</option>
          <option value="tomorrow"${start === "tomorrow" ? " selected" : ""}>${esc(t("tomorrowAm"))}</option>
          <option value="pick"${start === "pick" ? " selected" : ""}>${esc(t("pickTime"))}</option></select>
          ${start === "pick" ? `<input id="dlg-at" type="datetime-local" value="${esc(g.at || "")}">` : ""}</label>
        <label class="field"><span class="label">${esc(t("agentDone"))}</span><input id="dlg-done" placeholder="${esc(t("optional"))}" value="${esc(g.done || "")}"></label></div>`;
      okLabel = t("agentTitle");
      ok = "agent";
    } else if (g.kind === "snooze" || g.kind === "followup") {
      body = options(dateChoices(g.kind), g.value) + (g.value === "pick" || g.picked
        ? `<label class="field"><span class="sr">${esc(t("pickDate"))}</span><input id="dlg-date" type="date" value="${esc(g.picked || "")}"></label>` : "");
      okLabel = g.kind === "snooze" && g.value && g.value !== "pick" ? t("snoozeTo", { d: day(g.value) }) : t("save");
      ok = g.kind;
    } else if (g.kind === "wait") {
      body = `<label class="field"><span class="label">${esc(t("waitWho"))}</span><input id="dlg-who" value="${esc(g.who ?? d.waiting_on ?? "")}"></label>
        <span class="label">${esc(t("followWhen"))}</span>${options(dateChoices("followup"), g.value)}
        ${g.value === "pick" ? `<input id="dlg-date" class="field" type="date" value="${esc(g.picked || "")}">` : ""}`;
      okLabel = t("setWaiting");
      ok = "wait";
    } else if (g.kind === "due") {
      body = `<label class="field"><span class="sr">${esc(t("dueTitle"))}</span><input id="dlg-date" type="date" value="${esc(g.picked ?? d.due ?? "")}"></label>`;
      ok = "due";
    } else if (g.kind === "project") {
      const list = (S.home.projects || []);
      body = `<div class="options" style="grid-template-columns:1fr">${[{ id: "", title: t("noProject") }].concat(list).map((p) =>
        `<button class="option${(d.project ? d.project.id : "") === p.id ? " on" : ""}" data-project="${esc(p.id)}">${esc(p.title)}</button>`).join("")}</div>`;
      ok = "";
    }
    const title = { agent: "agentTitle", snooze: "snoozeTitle", followup: "followTitle", wait: "waitTitle", due: "dueTitle", project: "projectTitle" }[g.kind];
    return `<div class="scrim" data-close="1"><div class="modal" role="dialog" aria-modal="true" aria-label="${esc(t(title))}">
      <h3>${esc(t(title))}</h3><span class="task">${esc(d.title)}</span>${body}
      <div class="modal-foot">${g.kind === "due" ? `<button class="btn" data-dlg-clear="1">${esc(t("clear"))}</button>` : ""}
        <button class="btn" data-close="1">${esc(t("cancel"))}</button>
        ${ok ? `<button class="btn primary" data-dlg-ok="${ok}">${esc(okLabel)}</button>` : ""}</div></div></div>`;
  }
  function paletteView() {
    return `<div class="scrim" data-close="1"><div class="modal palette" role="dialog" aria-modal="true" aria-label="${esc(t("search"))}">
      <input id="palette-input" type="search" placeholder="${esc(t("searchHint"))}" value="${esc(S.search.q)}" autocomplete="off">
      <div id="palette-results">${searchResults(S.search.res, true)}</div></div></div>`;
  }

  // ------------------------------------------------------------------ render
  // A selector that finds the same control after a render: its id, or its first data-* attribute
  // and its place among the controls that share it.
  function selOf(el) {
    if (!el || el === document.body || !el.tagName) return null;
    if (el.id) return { sel: "#" + CSS.escape(el.id), n: 0 };
    const a = [...el.attributes].find((x) => x.name.startsWith("data-"));
    if (!a) return null;
    const sel = `${el.tagName.toLowerCase()}[${a.name}="${CSS.escape(a.value)}"]`;
    return { sel, n: [...document.querySelectorAll(sel)].indexOf(el) };
  }
  const findSel = (s) => (s ? document.querySelectorAll(s.sel)[s.n] || null : null);
  let lastPressed = null;

  // The page is a few regions, each written only when its HTML changes: what the user is doing in
  // the others (focus, selection, scroll, an open <details>) is left alone, and a poll that brings
  // nothing new changes nothing.
  const written = new WeakMap();
  function patch(el, html) {
    if (written.get(el) === html) return false;
    el.innerHTML = html;
    written.set(el, html);
    return true;
  }
  let R = null;
  function regions() {
    if (R) return R;
    $app.innerHTML = `<div class="region" id="r-nav"></div><div class="region" id="r-main"></div>
      <article class="detail closed" id="r-detail"><div class="region" id="r-body"></div><div class="region" id="r-compose"></div></article>
      <div class="region" id="r-tabs"></div>`;
    R = {};
    for (const k of ["nav", "main", "detail", "body", "compose", "tabs"]) R[k] = document.getElementById("r-" + k);
    return R;
  }

  // ------------------------------------------------------------------ motion
  // Short and quiet, and only for what the user did: never for what a poll brought.
  const motion = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  // A class that runs its animation once; it is taken off when it ends so it can run again.
  function play(el, cls) {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
    const end = (e) => {
      if (e.target !== el) return;
      el.classList.remove(cls);
      el.removeEventListener("animationend", end);
    };
    el.addEventListener("animationend", end);
  }
  const leave = (el) => new Promise((done) => {
    if (!el) return done();
    el.addEventListener("animationend", (e) => { if (e.target === el) done(); });
    el.classList.add("leave");
    setTimeout(done, (parseFloat(motion("--t1")) || 0) + 60);
  });
  // Where the tiles and rows of the list are, so that after the user's own change they slide to
  // their new places (FLIP) rather than jump.
  const flipKey = (el) => (el.classList.contains("tile") ? "t:" + el.getAttribute("aria-label") : "r:" + el.dataset.go);
  function measure() {
    const list = document.getElementById("list-scroll");
    if (!list || !list.offsetParent || !parseFloat(motion("--t2"))) return null;
    const at = new Map();
    for (const el of list.querySelectorAll(".tile, .row")) at.set(flipKey(el), el.getBoundingClientRect().top);
    return at;
  }
  function flip(before) {
    const list = document.getElementById("list-scroll");
    if (!list || !before) return;
    const now = new Map();
    for (const el of list.querySelectorAll(".tile, .row")) now.set(el, el.getBoundingClientRect().top);
    for (const [el, top] of now) {
      const was = before.get(flipKey(el));
      if (was === undefined) continue;
      let dy = was - top;
      const tile = el.classList.contains("row") && el.closest(".tile");
      if (tile && now.has(tile) && before.has(flipKey(tile))) dy -= before.get(flipKey(tile)) - now.get(tile);   // it moves with its tile
      if (Math.abs(dy) < 1) continue;
      el.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }],
        { duration: parseFloat(motion("--t2")), easing: motion("--ease") || "ease" });
    }
  }
  let shownId = null;

  function render() {
    if (!S.home) return;
    const r = route();
    const keep = {};
    for (const id of ["list-scroll", "detail-scroll", "page-scroll"]) {
      const el = document.getElementById(id);
      if (el) keep[id] = el.scrollTop;
    }
    const active = document.activeElement;
    const focus = selOf(active);
    const range = active && typeof active.selectionStart === "number" ? [active.selectionStart, active.selectionEnd] : null;
    const layer = S.dialog ? "dialog" : S.palette ? "palette" : "";
    if (layer && !S.layer) S.opener = selOf(active && active !== document.body && !$overlay.contains(active) ? active : lastPressed);
    const $ = regions();
    let main = "";
    let d = null, loading = false;
    if (r.page === "tasks") {
      // Another task on its way keeps the one shown, dimmed, until it comes.
      d = S.detail && r.task && (S.detailId === r.task || S.shown) ? S.detail : null;
      loading = !!d && S.detailId !== r.task;
      main = listView(r.view);
    } else if (r.page === "projects") main = projectsPage();
    else if (r.page === "project") main = projectPage(r);
    else if (r.page === "delivery") main = deliveryPage(r);
    else if (r.page === "search") main = searchPage();
    else if (r.page === "decide") main = decidePage();
    S.shown = !!d;
    const opened = !!d && $.detail.classList.contains("closed");
    patch($.nav, sidebar(r) + rail(r));
    patch($.main, main);
    $.detail.hidden = r.page !== "tasks";
    $.detail.classList.toggle("closed", !d);
    $.detail.setAttribute("aria-label", d ? d.title : "");
    if (loading) $.detail.setAttribute("aria-busy", "true"); else $.detail.removeAttribute("aria-busy");
    patch($.body, d ? detailBody(d) : r.page === "tasks" ? `<div class="empty-detail"></div>` : "");
    patch($.compose, d ? composer(d) : "");
    patch($.tabs, tabbar(r));
    patch($overlay, S.dialog ? dialogView() : S.palette ? paletteView() : "");
    // A task opened: on a phone the detail slides in, beside the list it rises into place.
    if (d && !loading && d.id !== shownId) {
      if (opened && window.matchMedia("(max-width: 767px)").matches) play($.detail, "slide");
      else play(document.getElementById("detail-scroll"), "enter");
    }
    if (!loading) shownId = d ? d.id : null;
    if (layer && !S.layer) play($overlay.querySelector(".scrim"), "opening");
    const box = document.getElementById("draft");
    if (box && box.value !== S.draft) box.value = S.draft;
    for (const el of document.querySelectorAll("details[data-keep]")) el.open = !!S.open[el.dataset.keep];
    document.body.style.overflow = window.matchMedia("(max-width: 767px)").matches && r.page === "tasks" && r.task ? "hidden" : "";
    for (const [id, top] of Object.entries(keep)) {
      const el = document.getElementById(id);
      if (el) el.scrollTop = top;
    }
    const el = findSel(focus);
    if (el && el !== document.activeElement) {
      el.focus({ preventScroll: true });
      if (range && el.setSelectionRange) try { el.setSelectionRange(range[0], range[1]); } catch (e) { /* not text */ }
    }
    if (layer === "dialog" && S.layer !== "dialog") {
      // A dialog opens on its first field.
      const modal = $overlay.querySelector(".modal");
      const first = modal && (modal.querySelector("textarea, input, select") || modal.querySelector(".option.on, .option"));
      if (first) first.focus();
    } else if (!layer && S.layer) {
      const back = findSel(S.opener);
      if (back) back.focus({ preventScroll: true });
      S.opener = null;
    }
    S.layer = layer;
    markPressed();
    // Beside the list (tablet and desktop) a task is always open: the first one, not an empty pane.
    if (r.page === "tasks" && !r.task && window.matchMedia("(min-width: 768px)").matches) {
      const first = [...$app.querySelectorAll("[data-go]")].find((x) => /^#\/tasks\/[^/]+\/./.test(x.dataset.go));
      if (first) location.replace(first.dataset.go);
    }
    settle();
  }

  // ------------------------------------------------------------------ doing things
  // One write at a time. The pressed button shows it is working; the others dim only when it
  // takes a while.
  let dimTimer = null;
  function hold(el) {
    S.pressed = el ? selOf(el) : null;
    clearTimeout(dimTimer);
    document.documentElement.classList.remove("writing");
    if (el) dimTimer = setTimeout(() => document.documentElement.classList.add("writing"), 150);
    for (const b of document.querySelectorAll("button[aria-busy]")) b.removeAttribute("aria-busy");
    markPressed();
  }
  function markPressed() {
    const el = findSel(S.pressed);
    if (el) el.setAttribute("aria-busy", "true");
  }

  async function act(action, params, taskId) {
    if (S.sending) return false;
    const id = taskId || S.detailId;
    const key = JSON.stringify(["act", id, action, params || {}]);
    let done = false;
    S.sending = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(id)}/act`, { action, params: params || {}, rid: ridFor(key) });
      rids.delete(key);
      done = true;
      S.more = false;
      S.dialog = null;
      await load();
      return true;
    } catch (e) {
      if (e.message !== "login") toast(t("failed", { e: e.message }), true);
      return done;
    } finally {
      S.sending = false;
      const before = done ? measure() : null;
      render();
      flip(before);
    }
  }
  async function send(text, to, taskId) {
    const body = (text || "").trim();
    if (!body || S.sending) return false;
    const id = taskId || S.detailId;
    const key = JSON.stringify(["comment", id, to, body]);
    S.sending = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(id)}/comment`, { text: body, to, rid: ridFor(key) });
      rids.delete(key);
      return true;
    } catch (e) {
      if (e.message !== "login") toast(t("failed", { e: e.message }), true);
      return false;
    } finally {
      S.sending = false;
    }
  }
  function setDraft(text) {
    S.draft = text;
    const box = document.getElementById("draft");
    if (box) box.value = text;
  }
  const toBottom = () => {
    const el = document.getElementById("detail-scroll");
    if (el) el.scrollTop = el.scrollHeight;
  };
  // The comment shows at once, marked as waiting; the box is empty again for the next one. If
  // sending fails the text goes back in the box.
  async function sendDraft() {
    const box = document.getElementById("draft");
    const text = (box ? box.value : S.draft).trim();
    const task = S.detailId;
    if (!text || !task) return;
    S.outbox = { task, text };
    setDraft("");
    render();
    const fresh = document.getElementById("draft");
    if (fresh) fresh.focus({ preventScroll: true });
    toBottom();
    const ok = await send(text, "hermes", task);
    if (ok) {
      try { await load(); } catch (e) { /* the next poll shows it */ }
    } else if (!S.draft && S.detailId === task) setDraft(text);
    S.outbox = null;
    render();
    if (ok) toBottom();
  }

  function startTime(g) {
    if (g.start === "tonight") return todayIso() + " 20:00";
    if (g.start === "tomorrow") return addDays(1) + " 09:00";
    if (g.start === "pick" && g.at) return g.at.replace("T", " ");
    return "";
  }

  async function dialogOk(kind) {
    const g = S.dialog;
    const d = g.task;
    const picked = () => (document.getElementById("dlg-date") || {}).value || "";
    const dateOrPick = () => (g.value === "pick" ? picked() : g.value || picked());
    if (kind === "agent") {
      g.note = document.getElementById("dlg-note").value;
      g.done = document.getElementById("dlg-done").value;
      const at = document.getElementById("dlg-at");
      if (at) g.at = at.value;
      const params = { note: g.note, done_criteria: g.done, start_after: startTime(g) };
      if (g.decideFlow) return decideChoice("act:agent", params);
      return act("agent", params, d.id);
    }
    if (kind === "snooze") {
      if (!dateOrPick()) return;
      return g.decideFlow ? decideChoice("act:snooze", { until: dateOrPick() }) : act("snooze", { until: dateOrPick() }, d.id);
    }
    if (kind === "followup") return dateOrPick() && act("follow_up", { date: dateOrPick() }, d.id);
    if (kind === "due") return act("due", { date: picked() }, d.id);
    if (kind === "wait") {
      const who = document.getElementById("dlg-who").value;
      if (g.decideFlow) return decideChoice("act:wait", { on: who, follow_up: dateOrPick() });
      return act("wait", { on: who, follow_up: dateOrPick() }, d.id);
    }
  }

  async function decideChoice(does, params) {
    const flow = S.decide;
    if (!flow || S.sending) return;
    const c = flow.cards[flow.i];
    if (!c) return;
    if (does === "skip") return advance(flow);
    if (does === "dlg:agent") {
      S.dialog = { kind: "agent", task: c, note: flow.line || "", decideFlow: true };
      return render();
    }
    if (does === "dlg:wait" || does === "dlg:snooze") {
      S.dialog = { kind: does.slice(4), task: c, value: null, decideFlow: true };
      return render();
    }
    const action = does.split(":")[1];
    const line = (flow.line || "").trim();
    if (line && action !== "agent" && !(await send(line, "hermes", c.id))) return;
    if (!(await act(action, params || {}, c.id))) return;
    const labels = { agent: "agentTitle", todo: "setTodo", wait: "waitFor", snooze: "snooze", done: "markDone", cancel: "cancel" };
    flow.done.push({ title: c.title, choice: t(labels[action]).replace(/…$/, "") });
    S.dialog = null;
    return advance(flow);
  }
  // The card handed over leaves, the next one comes in, and the progress bar fills.
  async function advance(flow) {
    await leave(document.querySelector(".decide-card"));
    flow.i += 1;
    flow.line = "";
    render();
    play(document.querySelector(".decide-card, .done-hero"), "enter");
    play(document.querySelectorAll(".progress span")[flow.i], "fill");
  }

  function startDecide() {
    if (!S.decide || S.decide.i >= S.decide.cards.length) {
      // Agree / Decline (from a Notion board set up before 2026-10-12) waiting for Hermes are not offered again.
      S.decide = { cards: S.home.groups.decide.filter((c) => !["agree", "decline"].includes(c.decision)), i: 0, done: [], line: "" };
    }
  }

  async function doIt(does) {
    const d = S.detail;
    if (does.startsWith("act:")) {
      const [, action, value] = does.split(":");
      if (action === "focus") return act("focus", { on: value === "1" });
      if (action === "snooze") return act("snooze", { until: "" });
      return act(action);
    }
    if (does.startsWith("dlg:")) {
      S.dialog = { kind: does.slice(4), task: d, value: null };
      S.more = false;
      return render();
    }
    if (does.startsWith("compose:")) {
      S.more = false;
      render();
      const box = document.getElementById("draft");
      if (box) box.focus();
      return;
    }
    if (does === "say:chaser") {
      const words = lang === "zh-CN" ? `起草一封催办${d.waiting_on ? "给 " + d.waiting_on : ""}` : `Draft a chaser${d.waiting_on ? " to " + d.waiting_on : ""}`;
      if (await send(words, "hermes")) await refresh();
      return;
    }
    if (does.startsWith("go:")) return go(does.slice(3));
    if (does.startsWith("url:")) return window.open(does.slice(4), "_blank", "noopener,noreferrer");
    if (does === "copy") {
      const link = location.origin + "/" + taskHash(d.id, "all");
      try { await navigator.clipboard.writeText(link); toast(t("copied")); } catch (e) { toast(link); }
      S.more = false;
      return render();
    }
  }

  let searchTimer = null;
  function searchFor(q, palette) {
    S.search.q = q;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      if (!q.trim()) { S.search.res = null; } else {
        try { S.search.res = await api("/api/search?q=" + encodeURIComponent(q)); } catch (e) { return; }
      }
      S.search.sel = 0;
      if (palette) {
        const box = document.getElementById("palette-results");
        if (box) box.innerHTML = searchResults(S.search.res, true);
      } else render();
    }, 180);
  }

  function themeName() {
    const theme = document.documentElement.dataset.theme;
    return theme === "light" || theme === "dark" ? theme : "system";
  }
  function cycleTheme() {
    const next = { system: "light", light: "dark", dark: "system" }[themeName()];
    if (next === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { if (next === "system") localStorage.removeItem("tm-theme"); else localStorage.setItem("tm-theme", next); } catch (e) { /* not kept */ }
    render();
  }

  // ------------------------------------------------------------------ events
  // What writes: one at a time, and the pressed one shows it is working.
  const WRITES = "[data-send],[data-dlg-ok],[data-dlg-clear],[data-project],[data-decide],[data-do^='act:'],[data-do^='say:']";
  async function write(el) {
    if (el.dataset.send !== undefined) return sendDraft();
    if (el.dataset.dlgOk !== undefined) return dialogOk(el.dataset.dlgOk);
    if (el.dataset.dlgClear !== undefined) return act("due", { date: "" }, S.dialog.task.id);
    if (el.dataset.project !== undefined) return act("project", { project: el.dataset.project }, S.dialog.task.id);
    if (el.dataset.decide !== undefined) return decideChoice(el.dataset.decide);
    return doIt(el.dataset.do);
  }

  document.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-go],[data-do],[data-more],[data-send],[data-back],[data-expand],[data-palette]," +
      "[data-close],[data-opt],[data-dlg-ok],[data-dlg-clear],[data-project],[data-decide],[data-preview],[data-theme-cycle],[data-unclamp]");
    if (!el) return;
    lastPressed = el;
    if (el.dataset.close !== undefined) {
      if (el.classList.contains("scrim") && e.target !== el) return;   // a click inside the modal
      S.dialog = null;
      S.palette = false;
      return render();
    }
    e.preventDefault();
    if (el.matches(WRITES)) {
      if (S.pressed) return;
      hold(el);
      try { await write(el); } finally { hold(null); settle(); }
      return;
    }
    if (el.dataset.go !== undefined) {
      S.palette = false;
      S.dialog = null;
      return go(el.dataset.go);
    }
    if (el.dataset.do !== undefined) return doIt(el.dataset.do);
    if (el.dataset.more !== undefined) { S.more = !S.more; return render(); }
    if (el.dataset.unclamp !== undefined) { S.bannerOpen = true; return render(); }
    if (el.dataset.back !== undefined) {
      const r = route();
      return go(r.page === "delivery" ? taskHash(S.detailId) : `#/tasks/${encodeURIComponent(r.view || "today")}`);
    }
    if (el.dataset.expand !== undefined) { S.expanded = { ...(S.expanded || {}), [el.dataset.expand]: true }; return render(); }
    if (el.dataset.palette !== undefined) { return openPalette(); }
    if (el.dataset.themeCycle !== undefined) return cycleTheme();
    if (el.dataset.opt !== undefined) {
      S.dialog.value = el.dataset.opt;
      return render();
    }
    if (el.dataset.preview !== undefined) {
      try {
        const res = await fetch(`/api/deliveries/${encodeURIComponent(S.delivery.version.id)}/files/${el.dataset.preview}`, { credentials: "same-origin" });
        const kind = res.headers.get("Content-Type") || "";
        S.filePreview = kind.startsWith("text/") ? (await res.text()).slice(0, 200000) : `(${kind})`;
      } catch (err) { toast(t("offline"), true); }
      return render();
    }
  });

  document.addEventListener("input", (e) => {
    if (e.target.id === "draft") S.draft = e.target.value;
    if (e.target.id === "decide-line" && S.decide) S.decide.line = e.target.value;
    if (e.target.id === "search-input") searchFor(e.target.value, false);
    if (e.target.id === "palette-input") searchFor(e.target.value, true);
    // What is typed in a dialog is kept, so nothing redrawn under it loses it.
    const field = { "dlg-note": "note", "dlg-done": "done", "dlg-who": "who", "dlg-at": "at", "dlg-date": "picked" }[e.target.id];
    if (field && S.dialog) S.dialog[field] = e.target.value;
    if (e.target.id === "dlg-start" && S.dialog && S.dialog.start !== e.target.value) {
      S.dialog.start = e.target.value;
      render();
    }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "dlg-start" && S.dialog && S.dialog.start !== e.target.value) {
      S.dialog.start = e.target.value;
      render();
    }
  });
  document.addEventListener("focusout", () => setTimeout(settle, 0));
  document.addEventListener("toggle", (e) => {
    if (e.target.dataset && e.target.dataset.keep) S.open[e.target.dataset.keep] = e.target.open;
  }, true);

  function openPalette() {
    S.palette = true;
    S.search.sel = 0;
    render();
    const box = document.getElementById("palette-input");
    if (box) box.focus();
  }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      lastPressed = null;
      return openPalette();
    }
    // Tab stays inside an open dialog.
    const modal = e.key === "Tab" && $overlay.querySelector(".modal");
    if (modal) {
      const list = [...modal.querySelectorAll("button, input, textarea, select, a[href]")].filter((x) => !x.disabled);
      const first = list[0], last = list[list.length - 1];
      const inside = modal.contains(document.activeElement);
      if (first && e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (first && !e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
    }
    if (e.key === "Escape") {
      if (S.dialog || S.palette) { S.dialog = null; S.palette = false; return render(); }
      if (S.more) { S.more = false; return render(); }
    }
    if (e.target.id === "draft" && e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.isComposing) {
      e.preventDefault();
      const btn = document.querySelector("[data-send]");
      if (btn) btn.click();
    }
    if (e.target.id === "palette-input" && S.search.res) {
      const hits = [...document.querySelectorAll("#palette-results .hit")];
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        S.search.sel = (S.search.sel + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % Math.max(hits.length, 1);
        document.getElementById("palette-results").innerHTML = searchResults(S.search.res, true);
      } else if (e.key === "Enter" && hits[S.search.sel] && !e.isComposing) {
        e.preventDefault();
        hits[S.search.sel].click();
      }
    }
  });

  window.addEventListener("hashchange", async () => {
    S.filePreview = route().page === "delivery" ? S.filePreview : null;
    if (route().page === "decide" && S.home) startDecide();
    render();
    await refresh();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  document.addEventListener("touchstart", () => {}, { passive: true });   // so iOS shows :active on a tap

  // ------------------------------------------------------------------ start
  (async function start() {
    try {
      S.me = await api("/api/me");
      lang = S.me.language && S.me.language.startsWith("zh") ? "zh-CN" : "en";
      document.documentElement.lang = lang;
      await load();
      if (route().page === "decide") startDecide();
      render();
      S.stamp = (await api("/api/stamp")).stamp;
    } catch (e) {
      if (e.message !== "login") $app.innerHTML = `<div class="empty">${esc(t("failed", { e: e.message }))}</div>`;
    }
    setInterval(poll, 15000);
  })();
})();
