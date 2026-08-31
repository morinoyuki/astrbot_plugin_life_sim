/* 转生模拟器 · 插件数据管理页逻辑(bridge SDK → 本插件 Web API) */
/* global AstrBotPluginPage */
(function () {
  "use strict";

  const P = window.AstrBotPluginPage;
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const fmtSize = (n) => {
    if (!n && n !== 0) return "-";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(2) + " MB";
  };
  const fmtTime = (v) => {
    if (!v) return "-";
    let t = typeof v === "number" ? new Date(v * 1000) : new Date(v);
    if (isNaN(t)) return String(v);
    const pad = (x) => String(x).padStart(2, "0");
    return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`;
  };

  // ── toast / 确认弹窗(sandbox iframe 里原生 confirm/alert 被禁,必须自绘)──
  let SCOPES = [];
  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function openModal(title, bodyHtml, buttons) {
    $("#modal-title").textContent = title || "";
    $("#modal-body").innerHTML = bodyHtml || "";
    const foot = $("#modal-foot");
    foot.innerHTML = "";
    (buttons || []).forEach((b) => {
      const btn = document.createElement("button");
      btn.className = "btn " + (b.style || "");
      btn.textContent = b.label;
      btn.onclick = async () => {
        try {
          if ((await b.onClick?.()) === false) return; // 返回 false 表示校验失败不关窗
        } catch (e) {
          toast("❌ " + e.message);
          return;
        }
        closeModal();
      };
      foot.appendChild(btn);
    });
    $("#modal-mask").classList.add("show");
  }
  function closeModal() { $("#modal-mask").classList.remove("show"); }

  function confirmAction(title, text, onOk) {
    openModal(
      title,
      `<p style="margin:4px 0">${esc(text)}</p>`,
      [
        { label: "取消" },
        { label: "确认执行", style: "danger", onClick: onOk },
      ],
    );
  }

  function viewCode(title, code) {
    openModal(title, `<pre class="code">${esc(code)}</pre>`, [{ label: "关闭" }]);
  }

  // ── API 封装 ──
  async function apiGet(ep, params) {
    if (!P?.apiGet) throw new Error("bridge SDK 未加载");
    return await P.apiGet(ep, params);
  }
  async function apiPost(ep, body) {
    if (!P?.apiPost) throw new Error("bridge SDK 未加载");
    return await P.apiPost(ep, body);
  }

  // ── Tab 切换 ──
  const TAB_LOADERS = {};
  function switchTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    (TAB_LOADERS[name] || (() => {}))().catch((e) => toast("❌ " + e.message));
  }

  // ── 总览 ──
  async function loadOverview() {
    const o = await apiGet("/api/overview");
    const cards = [
      ["会话 scope", o.scope_count],
      ["模拟会话", o.sessions.count, `${fmtSize(o.sessions.size)} · ${o.sessions.files} 文件`],
      ["剧情历史 scope", o.narrative.scopes, `${fmtSize(o.narrative.size)} · ${o.narrative.files} 文件`],
      ["分支快照", o.branches.files, fmtSize(o.branches.size)],
      ["RPG 角色", o.rpg.chars],
      ["RPG 会话", o.rpg.sessions],
      ["头像文件", o.avatars.files],
      ["向量记忆", o.memory?.entries ?? 0, `${fmtSize(o.memory?.size)} · ${o.memory?.files} 文件`],
    ];
    $("#ov-cards").innerHTML = cards.map(([lbl, num, sub]) => `
      <div class="card"><div class="num">${esc(num)}</div>
      <div class="lbl">${esc(lbl)}</div>${sub ? `<div class="lbl muted">${esc(sub)}</div>` : ""}</div>`).join("");
    $("#ov-datadir").textContent = "数据目录:" + o.data_dir;
  }

  // ── 模拟会话 ──
  async function loadSessions() {
    const data = await apiGet("/api/sessions");
    const tbody = $("#sessions-tbl tbody");
    if (!data.sessions.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="muted">暂无会话</td></tr>`;
      return;
    }
    tbody.innerHTML = data.sessions.map((s) => `
      <tr data-key="${esc(s.key)}">
        <td><code>${esc(s.key)}</code></td>
        <td>${esc(s.mode_name || s.mode)}</td>
        <td>${esc(s.owner || "-")}</td>
        <td>${s.turn}</td>
        <td>${s.msg_count}</td>
        <td>${s.lore_entries}</td>
        <td><div class="cell-pre" title="${esc(s.world_setting)}">${esc(s.world_setting || "-")}</div></td>
        <td>${fmtSize(s.size)}</td>
        <td>${fmtTime(s.mtime)}</td>
        <td style="white-space:nowrap">
          <button class="btn tiny act-detail">详情</button>
          <button class="btn tiny act-export">导出</button>
          <button class="btn tiny danger act-del">删除</button>
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("tr[data-key]").forEach((tr) => {
      const key = tr.dataset.key;
      tr.querySelector(".act-detail").onclick = () => openDrawer(key).catch((e) => toast("❌ " + e.message));
      tr.querySelector(".act-export").onclick = () => exportSession(key);
      tr.querySelector(".act-del").onclick = () =>
        confirmAction("删除会话", `将删除会话 ${key} 及其分支快照、头像。是否同时删除该 scope 的全部剧情记录?`, async () => {
          const purge = true;
          const r = await apiPost("/api/session/delete", { key, purge_narrative: purge });
          toast(`🗑️ 已删除${r.deleted.session ? " 会话" : ""}${r.deleted.branches ? `,分支快照 ${r.deleted.branches} 条` : ""}${r.deleted.records ? `,剧情记录 ${r.deleted.records} 条` : ""}`);
          loadSessions().catch(() => {});
        });
    });
  }

  function exportSession(key) {
    if (!P?.download) throw new Error("bridge SDK 未加载");
    P.download("/api/export/" + encodeURIComponent(key), {}, `life_sim_${key}.json`)
      .then(() => toast("📦 已开始下载"))
      .catch((e) => toast("❌ " + e.message));
  }

  // ── 会话详情抽屉 ──
  let drawerSession = null; // 当前抽屉里的 session 数据

  async function openDrawer(key) {
    const d = await apiGet("/api/session/" + encodeURIComponent(key), { messages: 1 });
    drawerSession = d.session;
    $("#drawer-title").textContent = drawerSession.mode_name ? `[模式 ${drawerSession.mode} - ${drawerSession.mode_name}]` : "";
    $("#drawer-sub").textContent = `${key} · ${drawerSession.lore_turn} 轮 · ${drawerSession.message_count} 条消息`;
    $("#e-owner").value = drawerSession.owner || "";
    $("#e-world").value = drawerSession.world_setting || "";
    renderWorldLoreEditor();
    renderCharLoreEditor();
    renderMsgList();
    $("#drawer-mask").classList.add("show");
    $("#drawer").classList.add("show");
  }
  function closeDrawer() {
    $("#drawer-mask").classList.remove("show");
    $("#drawer").classList.remove("show");
    drawerSession = null;
  }

  // 设定 / 世界观
  $("#save-basic").onclick = () => saveBasic().catch((e) => toast("❌ " + e.message));
  async function saveBasic() {
    const r = await apiPost("/api/session/update", {
      key: drawerSession.key,
      owner: $("#e-owner").value.trim(),
      world_setting: $("#e-world").value,
    });
    toast("✅ 已保存:" + (r.changed || []).join("、"));
  }

  // Lore 编辑器(world_lore)
  function renderWorldLoreEditor() {
    const list = $("#wl-list");
    list.innerHTML = "";
    (drawerSession.world_lore || []).forEach((entry) => list.appendChild(loreItemEl(entry)));
    if (!(drawerSession.world_lore || []).length)
      list.innerHTML = `<p class="hint muted">暂无条目</p>`;
  }
  function loreItemEl(entry = {}) {
    const div = document.createElement("div");
    div.className = "lore-item";
    div.innerHTML = `
      <div class="li-head">
        <span class="muted">#</span><input class="seq" value="${esc(entry.seq ?? "")}" placeholder="seq">
        <input class="sec" value="${esc(entry.section || "")}" placeholder="分类">
        <button class="btn tiny danger li-del">删除</button>
      </div>
      <textarea rows="3">${esc(entry.content || "")}</textarea>`;
    div.querySelector(".li-del").onclick = () => div.remove();
    return div;
  }
  $("#wl-add").onclick = () => {
    const empty = $("#wl-list").querySelector(".hint.muted");
    if (empty) empty.remove();
    $("#wl-list").appendChild(loreItemEl({}));
  };

  // Lore 编辑器(character_lore)
  function charBlockEl(name, entries) {
    const wrap = document.createElement("div");
    wrap.className = "char-block";
    const h5 = document.createElement("h5");
    h5.innerHTML = `👤 <input class="cname" value="${esc(name)}"> <span class="ops"></span>`;
    const del = document.createElement("button");
    del.className = "btn tiny danger"; del.textContent = "删除角色";
    del.onclick = () => wrap.remove();
    h5.querySelector(".ops").appendChild(del);
    wrap.appendChild(h5);
    entries.forEach((e) => wrap.appendChild(loreItemEl(e)));
    const addBtn = document.createElement("button");
    addBtn.className = "btn tiny"; addBtn.textContent = "+ 新增条目";
    addBtn.onclick = () => wrap.appendChild(loreItemEl({}));
    wrap.appendChild(addBtn);
    return wrap;
  }
  function renderCharLoreEditor() {
    const list = $("#cl-list");
    list.innerHTML = "";
    const cl = drawerSession.character_lore || {};
    Object.entries(cl).forEach(([name, entries]) => list.appendChild(charBlockEl(name, entries)));
  }
  $("#cl-add").onclick = () => {
    const name = $("#cl-new-name").value.trim();
    if (!name) return toast("请输入角色名");
    $("#cl-list").appendChild(charBlockEl(name, []));
    $("#cl-new-name").value = "";
  };

  $("#save-lore").onclick = () => saveLore().catch((e) => toast("❌ " + e.message));
  async function saveLore() {
    const collect = (containerSel) =>
      $$(containerSel + " .lore-item").map((item) => ({
        seq: parseInt(item.querySelector(".seq").value, 10) || undefined,
        section: item.querySelector(".sec").value.trim(),
        content: item.querySelector("textarea").value,
      })).filter((e) => e.content);

    const world_lore = collect("#wl-list");
    const character_lore = {};
    $$("#cl-list .char-block").forEach((block) => {
      const name = block.querySelector(".cname").value.trim();
      if (!name) return;
      character_lore[name] = $$(null, block);
      function $$(sel, root) {
        return Array.from(root.querySelectorAll(".lore-item")).map((item) => ({
          seq: parseInt(item.querySelector(".seq").value, 10) || undefined,
          section: item.querySelector(".sec").value.trim(),
          content: item.querySelector("textarea").value,
        })).filter((e) => e.content);
      }
    });

    const r = await apiPost("/api/session/update", {
      key: drawerSession.key, world_lore, character_lore,
    });
    toast("✅ 已保存:" + (r.changed || []).join("、"));
  }

  // 消息历史
  function msgText(m) {
    if (typeof m.content === "string") return m.content;
    if (Array.isArray(m.content)) {
      return m.content.map((p) => {
        if (typeof p === "string") return p;
        if (p.type === "text") return p.text || "";
        if (p.type === "image_url") return "[图片]";
        if (p.type === "reasoning") return "[思考]" + (p.reasoning || "");
        return JSON.stringify(p);
      }).filter(Boolean).join("\n");
    }
    return JSON.stringify(m.content);
  }
  function renderMsgList() {
    const ol = $("#msg-list");
    ol.innerHTML = "";
    (drawerSession.messages || []).forEach((m, idx) => {
      const li = document.createElement("li");
      const role = esc(m.role || "?");
      const turn = m.turn ? ` <span class="muted">#turn${esc(m.turn)}</span>` : "";
      li.innerHTML = `
        <span class="ops"><button class="btn tiny danger">⏪ 回滚到此前</button></span>
        <span class="role ${role}">${role}</span>${turn}
        <div class="txt">${esc(msgText(m)).slice(0, 2000)}${msgText(m).length > 2000 ? " …" : ""}</div>`;
      li.querySelector("button").onclick = () =>
        confirmAction("回滚消息", `删除第 ${idx + 1} 条及之后的全部消息(共 ${(drawerSession.messages || []).length - idx} 条)?`, async () => {
          const r = await apiPost("/api/messages/truncate", { key: drawerSession.key, keep_messages: idx });
          toast(`⏪ 已移除 ${r.removed} 条消息,turn 重置为 ${r.lore_turn}`);
          await openDrawer(drawerSession.key);
        });
      ol.appendChild(li);
    });
    if (!(drawerSession.messages || []).length)
      ol.innerHTML = `<li class="muted">暂无消息</li>`;
  }

  // ── 剧情历史 ──
  async function fillScopeSelect(selId, needBranch) {
    const scopes = (await apiGet("/api/scopes")).scopes;
    const sel = $(selId);
    sel.innerHTML = scopes.map((s) => `<option value="${esc(s.key)}">${esc(s.key)}</option>`).join("")
      || `<option value="">(无)</option>`;
    return scopes;
  }
  function fillBranchSelect(scopesForSel) {
    const selScope = $("#n-scope").value;
    const s = scopesForSel.find((x) => x.key === selScope);
    const lines = [["(主线)", ""]];
    (s?.branches || []).forEach((b) => lines.push([b, b]));
    $("#n-branch").innerHTML = lines.map(([label, v]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
  }

  async function loadNarrative() {
    const scope = $("#n-scope").value;
    const branch = $("#n-branch").value;
    if (!scope) { $("#narr-tbl tbody").innerHTML = `<tr><td colspan="7" class="muted">无 scope</td></tr>`; return; }
    const data = await apiGet("/api/narrative", { scope, branch });
    const tbody = $("#narr-tbl tbody");
    if (!data.records.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">该线暂无剧情记录</td></tr>`;
      return;
    }
    tbody.innerHTML = data.records.map((r) => `
      <tr data-id="${esc(r.id)}">
        <td><code>${esc(r.id)}</code></td>
        <td>${fmtTime(r.created_at)}</td>
        <td><div class="cell-pre" title="${esc(r.summary)}">${esc(r.summary || "-")}</div></td>
        <td><div class="cell-pre">${esc(r.user_action || "-")}</div></td>
        <td>${r.narrative_len}</td>
        <td>${r.revised_count ? `✎${r.revised_count}` : "-"}</td>
        <td style="white-space:nowrap">
          <button class="btn tiny act-view">查看</button>
          <button class="btn tiny danger act-del">删除</button>
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
      const id = tr.dataset.id;
      tr.querySelector(".act-view").onclick = () => editNarrative(scope, branch, id).catch((e) => toast("❌ " + e.message));
      tr.querySelector(".act-del").onclick = () =>
        confirmAction("删除剧情记录", `确定删除记录 ${id}?`, async () => {
          await apiPost("/api/narrative/delete", { scope, branch, id });
          toast("🗑️ 已删除");
          loadNarrative().catch(() => {});
        });
    });
  }

  async function editNarrative(scope, branch, id) {
    const d = await apiGet("/api/narrative/detail", { scope, branch, id });
    const rec = d.record;
    openModal(
      `修订剧情 · ${rec.id}`,
      `<p class="hint muted">${esc(rec.created_at)} · 用户:${esc(rec.user_action || "-")} · 修订 ${rec.revised_count} 次</p>
       <textarea id="ne-text" rows="16">${esc(rec.narrative)}</textarea>`,
      [
        { label: "取消" },
        {
          label: "💾 保存修订", style: "primary",
          onClick: async () => {
            const text = $("#ne-text").value;
            if (!text.trim()) { toast("内容不能为空"); return false; }
            await apiPost("/api/narrative/update", { scope, branch, id, narrative: text });
            toast("✅ 已修订");
          },
        },
      ],
    );
  }

  // ── 分支存档 ──
  let branchScopes = [];
  async function loadBranches() {
    branchScopes = (await apiGet("/api/scopes")).scopes.filter((s) => s.branches.length);
    const sel = $("#b-scope");
    sel.innerHTML = branchScopes.map((s) => `<option value="${esc(s.key)}">${esc(s.key)}</option>`).join("")
      || `<option value="">(无含分支的 scope)</option>`;
    await fetchBranchList();
  }
  async function fetchBranchList() {
    const scope = $("#b-scope").value;
    const tbody = $("#branch-tbl tbody");
    if (!scope) { tbody.innerHTML = `<tr><td colspan="3" class="muted">无 scope</td></tr>`; return; }
    const data = await apiGet("/api/branches", { scope });
    if (!data.branches.length) {
      tbody.innerHTML = `<tr><td colspan="3" class="muted">无分支</td></tr>`;
      return;
    }
    tbody.innerHTML = "";
    data.branches.forEach((b) => {
      const fields = Object.entries(b.fields).map(([k, v]) => `${esc(k)}=${esc(v)}`).join(" · ");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><strong>${esc(b.name)}</strong></td><td class="muted">${fields}</td>
        <td><button class="btn tiny danger">删除</button></td>`;
      tr.querySelector("button").onclick = () =>
        confirmAction("删除分支", `确定删除分支「${b.name}」(${scope})?`, async () => {
          await apiPost("/api/branch/delete", { scope, name: b.name });
          toast("🌿 已删除分支");
          fetchBranchList().catch(() => {});
        });
      tbody.appendChild(tr);
    });
  }

  // ── 向量记忆 ──
  let MEM_DATA = { scopes: [] };
  async function loadMemory() {
    MEM_DATA = await apiGet("/api/memory");
    const sel = $("#m-scope");
    sel.innerHTML =
      MEM_DATA.scopes.map((s) => `<option value="${esc(s.scope)}">${esc(s.scope)} (${s.count})</option>`).join("")
      || `<option value="">(无记忆)</option>`;
    await fetchMemoryList();
  }
  async function fetchMemoryList() {
    const scope = $("#m-scope").value;
    const tbody = $("#mem-tbl tbody");
    $("#m-checkall").checked = false;
    if (!scope) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">无向量记忆</td></tr>`;
      $("#m-meta").textContent = ""; return;
    }
    const s = MEM_DATA.scopes.find((x) => x.scope === scope);
    $("#m-meta").textContent = `${scope} · 共 ${s?.count ?? 0} 条 · 嵌入源:${s?.embed_source ?? "-"}`;
    if (!s?.entries?.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted">该 scope 暂无记忆(每轮 /do 自动写入,或用 life_sim_memorize 保存)</td></tr>`;
      return;
    }
    tbody.innerHTML = s.entries.map((e) => `
      <tr data-id="${esc(e.id)}">
        <td><input type="checkbox" class="mem-chk" value="${esc(e.id)}"></td>
        <td><code class="mute-id" title="${esc(e.id)}">${esc(String(e.id).slice(0, 8))}…</code></td>
        <td>${esc((e.importance || 1) >= 3 ? "★★★" : (e.importance || 1) === 2 ? "★★" : "★")}</td>
        <td>${e.turn ?? "-"}</td>
        <td class="muted">${fmtTime(e.created_at)}</td>
        <td><div class="cell-pre" title="${esc(e.content)}">${esc(e.content || "-")}</div></td>
        <td style="white-space:nowrap"><button class="btn tiny danger mem-del">删除</button></td>
      </tr>`).join("");

    tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
      tr.querySelector(".mem-del").onclick = () =>
        confirmAction("删除记忆", `确定删除该条记忆?`, async () => {
          const r = await apiPost("/api/memory/delete", { scope, mode: "ids", ids: [tr.dataset.id] });
          toast(`🗑️ 已删除 ${r.removed} 条`);
          await loadMemory();
        });
    });
  }
  $("#m-load").onclick = () => fetchMemoryList().catch((e) => toast("❌ " + e.message));
  $("#m-scope").onchange = () => fetchMemoryList().catch((e) => toast("❌ " + e.message));
  $("#m-checkall").onchange = (ev) =>
    $$(".mem-chk").forEach((c) => (c.checked = ev.target.checked));
  $("#m-del-sel").onclick = () => {
    const ids = $$(".mem-chk").filter((c) => c.checked).map((c) => c.value);
    if (!ids.length) return toast("未勾选任何条目");
    confirmAction("删除选中", `确定删除选中的 ${ids.length} 条记忆?`, async () => {
      const r = await apiPost("/api/memory/delete", { scope: $("#m-scope").value, mode: "ids", ids });
      toast(`🗑️ 已删除 ${r.removed} 条`);
      await loadMemory();
    });
  };
  $("#m-del-kw").onclick = () => {
    const kw = $("#m-keyword").value.trim();
    if (!kw) return toast("请输入关键字");
    confirmAction("按关键字删除", `删除所有内容含「${kw}」的记忆?`, async () => {
      const r = await apiPost("/api/memory/delete", { scope: $("#m-scope").value, mode: "keyword", keyword: kw });
      toast(`🗑️ 已删除 ${r.removed} 条`);
      await loadMemory();
    });
  };
  $("#m-del-all").onclick = () => {
    const scope = $("#m-scope").value;
    if (!scope) return;
    confirmAction("清空记忆", `确定清空 ${scope} 的全部向量记忆?`, async () => {
      const r = await apiPost("/api/memory/delete", { scope, mode: "all" });
      toast(`🗑️ 已清空 ${r.removed} 条`);
      await loadMemory();
    });
  };
  $("#m-export").onclick = () => {
    const scope = $("#m-scope").value;
    if (!scope) return toast("无 scope");
    if (!P?.download) return toast("bridge SDK 未加载");
    P.download("/api/memory/export", { scope }, `life_sim_memory_${scope}.json`)
      .then(() => toast("📦 已开始下载"))
      .catch((e) => toast("❌ " + e.message));
  };

  // ── RPG 存档 ──
  async function loadRpg() {
    const data = await apiGet("/api/rpg");
    const cbody = $("#rpg-char-tbl tbody");
    cbody.innerHTML = data.chars.length ? data.chars.map((c) => `
      <tr data-uid="${esc(c.uid)}">
        <td><code>${esc(c.uid)}</code></td>
        <td>${esc(c.name || "-")}</td>
        <td>${esc(c.level ?? "-")}</td>
        <td>${esc(c.hp ?? "-")}</td>
        <td>${esc(c.group_id || "-")}</td>
        <td style="white-space:nowrap">
          <button class="btn tiny act-view">JSON</button>
          <button class="btn tiny danger act-del">删除</button>
        </td>
      </tr>`).join("") : `<tr><td colspan="6" class="muted">无角色存档</td></tr>`;

    cbody.querySelectorAll("tr[data-uid]").forEach((tr) => {
      const uid = tr.dataset.uid;
      const row = data.chars.find((c) => c.uid === uid);
      tr.querySelector(".act-view").onclick = () => viewCode("角色存档 " + uid, prettyJson(row.raw));
      tr.querySelector(".act-del").onclick = () =>
        confirmAction("删除 RPG 角色", `确定删除 ${uid}?`, async () => {
          await apiPost("/api/rpg/char/delete", { uid });
          toast("⚔️ 已删除"); loadRpg().catch(() => {});
        });
    });

    const sbody = $("#rpg-sess-tbl tbody");
    sbody.innerHTML = data.sessions.length ? data.sessions.map((s) => `
      <tr data-sid="${esc(s.sid)}">
        <td><code>${esc(s.sid)}</code></td>
        <td>${esc(s.game_system || "-")}</td>
        <td>${esc(s.group_id || "-")}</td>
        <td>${esc(Array.isArray(s.members) ? s.members.join(", ") : "-")}</td>
        <td style="white-space:nowrap">
          <button class="btn tiny act-view">JSON</button>
          <button class="btn tiny danger act-del">删除</button>
        </td>
      </tr>`).join("") : `<tr><td colspan="5" class="muted">无会话存档</td></tr>`;

    sbody.querySelectorAll("tr[data-sid]").forEach((tr) => {
      const sid = tr.dataset.sid;
      const row = data.sessions.find((s) => s.sid === sid);
      tr.querySelector(".act-view").onclick = () => viewCode("RPG 会话 " + sid, prettyJson(row.raw));
      tr.querySelector(".act-del").onclick = () =>
        confirmAction("删除 RPG 会话", `确定删除 ${sid}?`, async () => {
          await apiPost("/api/rpg/session/delete", { sid });
          toast("⚔️ 已删除"); loadRpg().catch(() => {});
        });
    });
  }

  function prettyJson(rawOrObj) {
    if (typeof rawOrObj !== "string") return JSON.stringify(rawOrObj, null, 2);
    try { return JSON.stringify(JSON.parse(rawOrObj), null, 2); } catch { return rawOrObj; }
  }

  // ── 初始化 ──
  TAB_LOADERS.overview = loadOverview;
  TAB_LOADERS.sessions = loadSessions;
  TAB_LOADERS.narrative = loadNarrativeTab;
  TAB_LOADERS.memory = loadMemory;
  TAB_LOADERS.branches = loadBranches;
  TAB_LOADERS.rpg = loadRpg;

  async function loadNarrativeTab() {
    SCOPES = await fillScopeSelect("#n-scope");
    fillBranchSelect(SCOPES);
    await loadNarrative();
  }

  function applyTheme(ctx) {
    if (ctx?.isDark) document.body.classList.add("dark");
  }

  async function boot() {
    if (!P) { document.body.textContent = "bridge SDK 未加载,无法使用管理页面"; return; }
    await P.ready();
    applyTheme(P.getContext());
    $$(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));
    $$(".dt").forEach((t) => (t.onclick = () => {
      $$(".dt").forEach((x) => x.classList.toggle("active", x === t));
      $$(".dpane").forEach((p) => p.classList.toggle("active", p.id === "dp-" + t.dataset.dt));
    }));
    $("#btn-refresh").onclick = () =>
      ($$(".tab.active")[0]?.dataset.tab ? switchTab($$(".tab.active")[0].dataset.tab) : loadOverview())
        .catch((e) => toast("❌ " + e.message));

    $("#drawer-close").onclick = closeDrawer;
    $("#drawer-mask").onclick = closeDrawer;
    $("#modal-mask").addEventListener("click", (ev) => { if (ev.target === ev.currentTarget) closeModal(); });
    $("#n-load").onclick = () => loadNarrative().catch((e) => toast("❌ " + e.message));
    $("#n-scope").onchange = () => fillBranchSelect(SCOPES);
    $("#b-load").onclick = () => fetchBranchList().catch((e) => toast("❌ " + e.message));

    await loadOverview().catch((e) => toast("❌ " + e.message));
  }

  boot();
})();
