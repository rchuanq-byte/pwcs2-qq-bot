"use strict";

(() => {
  const COMMANDS = [
    { command: "/数据", category: "战绩", description: "查看赛季数据汇总", keywords: "cs 完美 赛季 统计" },
    { command: "/数据 gp", category: "战绩", description: "查看官匹赛季汇总", keywords: "cs 官匹 赛季 统计" },
    { command: "/战绩", category: "战绩", description: "近十场完美比赛", keywords: "cs 完美 成绩" },
    { command: "/战绩 gp", category: "战绩", description: "近十场官匹比赛", keywords: "cs 官匹 成绩" },
    { command: "/排行", category: "战绩", description: "群内战绩排行榜", keywords: "排名 榜单" },
    { command: "/游戏状态", category: "战绩", description: "看看游戏状态", keywords: "在线 steam" },
    { command: "/观战", category: "战绩", description: "查看观战入口", keywords: "直播 对局" },
    { command: "/瓦战绩", category: "战绩", description: "查看无畏契约战绩", keywords: "瓦 valorant" },
    { command: "/发车", category: "开黑", description: "发起今晚的集合", keywords: "组队 队友 搭子" },
    { command: "/车队", category: "开黑", description: "查看组队入口", keywords: "组队 队友 搭子" },
    { command: "/分队", category: "开黑", description: "分好队，一起打", keywords: "组队 分组 内战" },
    { command: "/ts", category: "开黑", description: "获取群语音入口", keywords: "teamspeak 上麦 语音" },
    { command: "/签到", category: "牛币", description: "今天也来报个到", keywords: "每日 打卡" },
    { command: "/牛币", category: "牛币", description: "查看你的牛币", keywords: "余额 钱包" },
    { command: "/牛币排行", category: "牛币", description: "谁是群里的大户", keywords: "排名 榜单" },
    { command: "/商店", category: "牛币", description: "逛逛群内商店", keywords: "道具 购买" },
    { command: "/特惠", category: "牛币", description: "看看今日特惠", keywords: "商店 优惠" },
    { command: "/背包", category: "牛币", description: "查看已拥有的道具", keywords: "物品 库存" },
    { command: "/称号", category: "牛币", description: "看看你的称号", keywords: "荣誉 收集" },
    { command: "/挑战", category: "互动", description: "来点不一样的挑战", keywords: "任务 对决" },
    { command: "/投票", category: "互动", description: "群里的事，一起选", keywords: "选择 决定" },
    { command: "/选手", category: "互动", description: "CS 职业选手每日一题", keywords: "cs 职业 每日 知识 答题" },
    { command: "/黑历史", category: "互动", description: "回顾群里的名场面", keywords: "高光 故事 记录" },
    { command: "/复仇", category: "互动", description: "这次，把场子找回来", keywords: "对决 挑战" },
    { command: "/ai", category: "AI", description: "和 AI 聊两句", keywords: "问答 聊天 助手" },
    { command: "/帮助", category: "互动", description: "查看完整使用说明", keywords: "入门 新人 绑定 参数 指南" }
  ];
  const $ = (id) => document.getElementById(id);
  const list = $("command-list");
  const search = $("command-search");
  const filters = [...document.querySelectorAll("[data-category]")];
  let category = "all";

  function renderCommands() {
    const query = search.value.trim().toLocaleLowerCase("zh-CN");
    const matches = COMMANDS.filter((item) =>
      (category === "all" || item.category === category) &&
      `${item.command} ${item.category} ${item.description} ${item.keywords}`.toLocaleLowerCase("zh-CN").includes(query)
    );
    const fragment = document.createDocumentFragment();
    for (const item of matches) {
      const row = document.createElement("li");
      row.className = "command-row";
      const body = document.createElement("div");
      body.className = "command-body";
      const name = document.createElement("code");
      name.className = "command-name";
      name.textContent = item.command;
      const description = document.createElement("span");
      description.className = "command-description";
      description.textContent = item.description;
      const tag = document.createElement("span");
      tag.className = "command-tag";
      tag.textContent = item.category;
      description.append(tag);
      body.append(name, description);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "copy-button";
      button.textContent = "复制";
      button.dataset.copy = item.command;
      button.dataset.copyLabel = item.command;
      button.setAttribute("aria-label", `复制命令 ${item.command}`);
      row.append(body, button);
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    list.hidden = matches.length === 0;
    $("command-empty").hidden = matches.length !== 0;
    $("clear-search").hidden = !search.value;
    $("command-count").textContent = `${matches.length} / ${COMMANDS.length} 条命令`;
    for (const button of filters) button.setAttribute("aria-pressed", String(button.dataset.category === category));
  }

  filters.forEach((button) => button.addEventListener("click", () => {
    category = button.dataset.category;
    renderCommands();
  }));
  search.addEventListener("input", renderCommands);
  $("clear-search").addEventListener("click", () => {
    search.value = "";
    renderCommands();
    search.focus();
  });
  $("reset-filters").addEventListener("click", () => {
    category = "all";
    search.value = "";
    renderCommands();
    search.focus();
  });
  renderCommands();

  let toastTimer;
  let copyReturnFocus = null;
  function announce(message) {
    const toast = $("copy-toast");
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = setTimeout(() => {
      toast.classList.remove("is-visible");
      toast.textContent = "";
    }, 3600);
  }

  function legacyCopy(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.setAttribute("aria-label", "正在复制的文本");
    textarea.className = "clipboard-probe";
    const previousFocus = document.activeElement;
    document.body.append(textarea);
    let copied = false;
    try {
      textarea.focus({ preventScroll: true });
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      copied = typeof document.execCommand === "function" && document.execCommand("copy") === true;
    } catch (_) {
      copied = false;
    } finally {
      textarea.remove();
      if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus({ preventScroll: true });
    }
    return copied;
  }

  async function copyText(text, button) {
    if (button.dataset.copying === "true") return;
    button.dataset.copying = "true";
    let copied = false;
    try {
      if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        try {
          await navigator.clipboard.writeText(text);
          copied = true;
        } catch (_) {
          // HTTP, denied permissions, and unsupported browsers use the same honest fallback.
        }
      }
      if (!copied) copied = legacyCopy(text);
      if (copied) {
        $("manual-copy").hidden = true;
        const label = button.dataset.copyLabel || "文本";
        announce(`${label} 已复制${text.startsWith("/") ? "，回群发送即可。" : "。"}`);
        if (button.classList.contains("copy-button")) {
          button.textContent = "已复制";
          button.dataset.copied = "true";
          clearTimeout(button._copyResetTimer);
          button._copyResetTimer = setTimeout(() => {
            button.textContent = "复制";
            delete button.dataset.copied;
          }, 1800);
        }
      } else {
        copyReturnFocus = button;
        const panel = $("manual-copy");
        const textarea = $("manual-copy-text");
        textarea.value = text;
        panel.hidden = false;
        textarea.focus({ preventScroll: true });
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        announce("自动复制未成功，已选中文本，请手动复制。");
      }
    } finally {
      delete button.dataset.copying;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-copy]");
    if (button) void copyText(button.dataset.copy, button);
  });
  function closeManualCopy() {
    $("manual-copy").hidden = true;
    if (copyReturnFocus && copyReturnFocus.isConnected) copyReturnFocus.focus({ preventScroll: true });
  }
  $("close-manual-copy").addEventListener("click", closeManualCopy);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("manual-copy").hidden) closeManualCopy();
  });

  const SNAPSHOT_TTL = 5 * 60 * 1000;
  const BOT_TTL = 3 * 60 * 1000;
  const REFRESH_INTERVAL = 60 * 1000;
  const NUMBER_FORMAT = new Intl.NumberFormat("zh-CN");
  const TIME_FORMAT = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  let snapshot = null;
  let fetchFailed = false;
  let refreshTimer;
  let freshnessTimer;
  let activeRequest = null;

  function isoTime(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return NaN;
    return Date.parse(value);
  }

  function renderSnapshot() {
    clearTimeout(freshnessTimer);
    const now = Date.now();
    const updatedAt = snapshot ? isoTime(snapshot.updated_at) : NaN;
    const validTime = Number.isFinite(updatedAt) && updatedAt <= now;
    const fresh = validTime && now - updatedAt <= SNAPSHOT_TTL;
    const checkedAt = snapshot ? isoTime(snapshot.bot?.checked_at) : NaN;
    const checkedFresh = Number.isFinite(checkedAt) && checkedAt <= now && now - checkedAt <= BOT_TTL;
    const fields = [["stat-players", "bound_players"], ["stat-matches", "reported_matches"], ["stat-titles", "available_titles"]];
    for (const [id, key] of fields) {
      const value = validTime ? snapshot?.stats?.[key] : null;
      const isCount = Number.isSafeInteger(value) && value >= 0;
      $(id).textContent = isCount ? NUMBER_FORMAT.format(value) : "—";
      $(id).classList.toggle("compact-number", isCount && value >= 10000);
    }
    let state = "unknown";
    let label = "机器人 · 状态待确认";
    if (fresh && checkedFresh && !fetchFailed) {
      if (snapshot.bot?.status === "online") {
        state = "online";
        label = "机器人 · 在线";
      } else if (snapshot.bot?.status === "offline") {
        state = "offline";
        label = "机器人 · 暂离";
      }
    }
    $("bot-status").dataset.state = state;
    $("bot-status-label").textContent = label;
    let note;
    if (!snapshot) {
      note = fetchFailed ? "公共快照暂不可用；未提供的数据以 — 显示，机器人状态待确认。" : "正在获取公共快照；未提供的数据以 — 显示。";
    } else if (!validTime) {
      note = "快照时间缺失或异常，暂不展示统计；机器人状态待确认。";
    } else {
      const stamp = TIME_FORMAT.format(updatedAt);
      if (!fresh) note = `快照已过期 · 数据截至 ${stamp} · 超过 5 分钟未更新，机器人状态待确认。`;
      else if (fetchFailed) note = `暂时无法更新 · 上次快照 ${stamp} · 机器人状态待确认。`;
      else if (!checkedFresh || snapshot.bot?.status === "unknown" || !["online", "offline"].includes(snapshot.bot?.status)) note = `快照更新于 ${stamp} · 缺少有效的近期心跳，机器人状态待确认。`;
      else note = `快照更新于 ${stamp} · 机器人状态基于最近 3 分钟内的心跳 · 页面可见时每分钟刷新。`;
    }
    $("snapshot-note").textContent = note;
    // Expire an online badge at its exact boundary, not at the next network poll.
    if (!document.hidden) {
      const deadlines = [validTime ? updatedAt + SNAPSHOT_TTL + 1 : NaN, checkedFresh ? checkedAt + BOT_TTL + 1 : NaN].filter((time) => Number.isFinite(time) && time > now);
      if (deadlines.length) freshnessTimer = setTimeout(renderSnapshot, Math.min(...deadlines) - now);
    }
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (!document.hidden) refreshTimer = setTimeout(refreshSnapshot, REFRESH_INTERVAL);
  }

  async function refreshSnapshot() {
    if (document.hidden || activeRequest) return;
    const controller = new AbortController();
    activeRequest = controller;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 10000);
    try {
      const response = await fetch("./snapshot.json", { cache: "no-store", credentials: "omit", mode: "same-origin", redirect: "error", signal: controller.signal });
      if (!response.ok) throw new Error("Snapshot unavailable");
      const data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data) || data.version !== 1) throw new Error("Unsupported snapshot");
      snapshot = data;
      fetchFailed = false;
    } catch (error) {
      if (error.name !== "AbortError" || timedOut) fetchFailed = true;
    } finally {
      clearTimeout(timeout);
      if (activeRequest === controller) activeRequest = null;
      renderSnapshot();
      scheduleRefresh();
    }
  }

  document.addEventListener("visibilitychange", () => {
    clearTimeout(refreshTimer);
    clearTimeout(freshnessTimer);
    if (document.hidden) {
      if (activeRequest) activeRequest.abort();
    } else {
      renderSnapshot();
      void refreshSnapshot();
    }
  });
  renderSnapshot();
  void refreshSnapshot();
})();
