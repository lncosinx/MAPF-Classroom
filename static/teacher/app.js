// 老师端：规则（含课例模板）→ 座位（下拉 / 拖拽）→ 开始 / 暂停 / 立即结算 / 结束，上帝视角看全图和建议；
// "地图工坊"标签页里生成、编辑、保存、导入导出地图（static/teacher/workshop.js）。
import { Connection, ACTIONS, MESSAGES, agentColor, agentAvatar, store, cellFromEvent, checkVersion } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";
import { createWorkshop, WorkshopTools, WorkshopCanvas } from "/static/teacher/workshop.js";
import { createReview, ReviewSide, ReviewMain } from "/static/teacher/review.js";

const { createApp, reactive, computed, ref, nextTick, watch, onMounted, onBeforeUnmount } = Vue;

const OPTIONS = {
  view_radius: [[null, "全图可见"], [1, "1 格"], [2, "2 格"], [3, "3 格"], [4, "4 格"], [5, "5 格"]],
  fog_mode: [["memory", "记忆地图（走过的地方记得墙）"], ["fog", "纯迷雾"]],
  goal_display: [["projection", "视野外只显示边上的投影（同 AI）"], ["visible", "始终看得见"]],
  reveal_mode: [["teacher", "只给老师看"], ["immediate", "每步结算后告诉学生"], ["review", "赛后复盘再看"]],
  fill_policy: [["model", "AI 模型"], ["expert", "专家"], ["idle", "原地不动"]],
  timeout_policy: [["wait", "原地不动"], ["ai", "由补位方代选"]],
  ui_mode: [["advanced", "进阶（高年级）"], ["simple", "简洁（低年级）"]],
  model_variant: [["big", "主模型 7.45M"], ["small", "轻量 2.16M（老电脑）"]],
  comm_delivery: [["now", "立即送达"], ["next", "下一步才送达"]],
  comm_budget: [[null, "不限"], [0, "0 次（禁止）"], [1, "1 次"], [2, "2 次"], [3, "3 次"]],
};
const STATUS = { lobby: "未开始", playing: "进行中", paused: "暂停", finished: "已结束" };

createApp({
  components: { GridView, WorkshopTools, WorkshopCanvas, ReviewSide, ReviewMain },
  setup() {
    const st = reactive({
      online: false, denied: false, players: [], maps: [], templates: [], defaults: null, joinUrl: "",
      // 分组比拼：groups[g] 是第 g 组的 teacher_state（_rx = 收到时刻，用来算倒计时）；sel = 正在看的组
      groups: [], sel: 0, overview: false, sidesOut: false, thumbsShown: false,
      // 布局：class = 规则/同学 | 中间 | 控制/排行榜；workshop = 工坊工具 | 大画布；review = 复盘明细 | 复盘地图 | 控制/排行榜
      mode: "class",
      // 右栏（控制 / 排行榜）可以往右收起，让出宽度（记在浏览器里）；rightOut = 正在滑出 / 滑入
      rightHidden: store.get("mapf.teacher.rightHidden") === "1", rightOut: false,
      leaderboard: [], references: {}, screenLive: false,
      tab: "class", templateName: "",
      // 规则卡片收起 / 展开（记在浏览器里）
      rulesCollapsed: store.get("mapf.teacher.rulesCollapsed") === "1",
      // "本步"卡片叠到"控制"卡片后面（记在浏览器里）
      stepStacked: store.get("mapf.teacher.stepStacked") === "1", ctrlH: 0, stackNat: { step: 0, comm: 0, result: 0 },
      form: null, dirty: false, pendingApply: false, applyError: "", toast: "", now: performance.now(),
      show: { expert: true, ai: false, boxes: true, edges: false, range: false, links: true },
      // 地图缩放："fit" = 整张图缩进窗口；数字 = 相对地图框宽度的倍数（放大后在框内滚动）
      zoom: store.get("mapf.teacher.zoom") === null || store.get("mapf.teacher.zoom") === "fit"
        ? "fit" : Number(store.get("mapf.teacher.zoom")) || "fit",
      lastResolved: null,
    });
    let toastTimer = null;
    const toast = (m) => { st.toast = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (st.toast = ""), 3000); };

    function setGroup(g) {
      nextTick(measure);   // 地图框第一次出现、或上方内容高度变化后重新测量
      st.groups[g.group] = { ...g, _rx: performance.now() };
      if (g.group === 0 && !st.dirty) st.form = JSON.parse(JSON.stringify(g.settings));
    }
    function onMessage(m) {
      if (m.type === "classroom") {
        checkVersion(m.version);
        // 应用规则成功后服务端会推一份新的 classroom 快照；这时才把表单换成服务端的设置
        if (st.pendingApply) { st.pendingApply = false; st.dirty = false; st.applyError = ""; }
        st.players = m.players; st.maps = m.maps; st.templates = m.templates; st.defaults = m.defaults;
        st.groups = [];
        m.groups.forEach(setGroup);
        if (st.sel >= st.groups.length) st.sel = 0;
        st.leaderboard = m.leaderboard; st.references = m.references; st.screenLive = m.screen_live;
        if (m.join_url) st.joinUrl = m.join_url;
      } else if (m.type === "teacher_state") {
        setGroup(m);
      } else if (m.type === "progress" && st.groups[m.group]) {
        st.groups[m.group].progress = { submitted: m.submitted, total: m.total };
        st.groups[m.group].choices = m.choices;
      } else if (m.type === "comm_event" && st.groups[m.group]) {
        const g = st.groups[m.group];
        if (g.view && m.event.t === g.view.t) g.comm_events.push(m.event);
      } else if (m.type === "leaderboard") {
        st.leaderboard = m.rows; st.references = m.references;
      } else if (m.type === "resolved") {
        st.lastResolved = m;
      } else if (m.type === "rpc_result" || m.type === "rpc_error") {
        const p = pending.get(m.id);
        if (p) { pending.delete(m.id); m.type === "rpc_result" ? p.resolve(m.result) : p.reject(new Error(m.message)); }
      } else if (m.type === "error") {
        // 应用规则失败：保留老师填的表单，错误常驻显示在按钮下方
        if (st.pendingApply) { st.pendingApply = false; st.applyError = m.message; }
        toast(m.message);
      }
    }
    const key = new URLSearchParams(location.search).get("key");
    const conn = new Connection("/ws/teacher" + (key ? `?key=${encodeURIComponent(key)}` : ""), {
      onMessage, onStatus: (ok, code) => { st.online = ok; if (code === 4403) st.denied = true; },
    });
    fetch("/api/join-info").then((r) => r.json()).then((j) => (st.joinUrl = j.url)).catch(() => {});

    const send = (o) => { if (!conn.send(o)) toast("和服务器的连接断了"); };
    // 地图工坊 / 地图库 / 模板的请求-应答
    const pending = new Map();
    let rpcId = 0;
    function rpc(method, params) {
      return new Promise((resolve, reject) => {
        const id = ++rpcId;
        if (!conn.send({ type: "rpc", id, method, params })) { reject(new Error("和服务器的连接断了")); return; }
        pending.set(id, { resolve, reject });
        setTimeout(() => { if (pending.delete(id)) reject(new Error("服务器没有回应")); }, 15000);
      });
    }
    function normalizedForm() {
      const s = { ...st.form };
      if (s.num_agents === "" || s.num_agents === 0) s.num_agents = null;
      if (s.num_agents != null) s.num_agents = Number(s.num_agents);
      s.countdown_sec = Number(s.countdown_sec); s.max_steps = Number(s.max_steps);
      s.comm_radius = Number(s.comm_radius);
      return s;
    }
    function applySettings() {
      st.applyError = "";
      st.pendingApply = true;
      send({ type: "configure", settings: normalizedForm() });
    }
    // ---- 地图选择：选了地图库里的图就不再用工坊的图
    function pickPreset() { st.form.custom_map = null; st.dirty = true; }
    function useWorkshopMap(spec) {
      st.form.custom_map = spec; st.dirty = true; setTab("class");
      if (status.value === "playing" || status.value === "paused") { toast("比赛进行中，先点「结束」"); return; }
      applySettings();
    }
    // ---- 课例模板：叠加在默认规则上（不是叠加在当前表单上），保证效果和模板说明一致
    const currentTemplate = computed(() => st.templates.find((t) => t.name === st.templateName));
    function loadTemplate() {
      const t = currentTemplate.value;
      if (!t) return;
      st.form = JSON.parse(JSON.stringify({ ...st.defaults, ...t.settings }));
      st.dirty = true;
      toast(`已载入「${t.title}」，点「应用规则」生效`);
    }
    async function saveTemplate() {
      const title = prompt("给这套规则（含地图）起个名字", "");
      if (!title) return;
      try { await rpc("template.save", { title, settings: normalizedForm() }); toast(`已存为模板「${title}」`); }
      catch (e) { toast(e.message); }
    }
    async function deleteTemplate() {
      const t = currentTemplate.value;
      if (!t || t.builtin || !confirm(`删除模板「${t.title}」？`)) return;
      try { await rpc("template.delete", { name: t.name }); st.templateName = ""; } catch (e) { toast(e.message); }
    }
    // ---- 拖拽分座位：把同学拖到课堂地图上的智能体上
    function onDragStart(ev, p) { ev.dataTransfer.setData("text/plain", String(p.id)); ev.dataTransfer.effectAllowed = "move"; }
    function onDropMap(ev) {
      const pid = Number(ev.dataTransfer.getData("text/plain"));
      const v = r.value && r.value.view;
      if (!pid || !v) return;
      const rc = cellFromEvent(ev, mapWrap.value, v.H, v.W);
      const agent = rc ? v.positions.findIndex((p) => p[0] === rc[0] && p[1] === rc[1]) : -1;
      if (agent < 0) { toast("要拖到地图上的某个圆点（智能体）上"); return; }
      send({ type: "assign", player_id: pid, agent, group: st.sel });
    }
    const cmd = (type, extra = {}) => send({ type, ...extra });
    // 座位下拉框的值："组:智能体"，智能体为空表示在该组观战
    const seatValue = (p) => (p.group == null ? "" : `${p.group}:${p.agent ?? ""}`);
    function assign(pid, v) {
      if (v === "") return;
      const [g, a] = v.split(":");
      send({ type: "assign", player_id: pid, group: Number(g), agent: a === "" ? null : Number(a) });
    }
    // ---- 叠卡片：本步 / 本步通信 / 本局结果依次滑到控制卡片后面，每张只在下沿露出一条摘要（像一叠卡片的边）。
    //      第 i 张用负的 margin-top 往上拉：拉的距离 = 它的高度 − 露出的高度 + 间距，于是它的下沿正好比上一张低 PEEK。
    //      叠起时高度封顶到控制卡片的高度（max-height），否则很高的卡片（同学多、通信多）会从控制卡片上方露头。
    //      高度量的是卡片里内容包装层（.stack-body）：卡片自己被 max-height 卡住时也能量到真实高度
    const PEEK = 34, GAP = 16, CARD_PAD = 32;   // 露出的高度；.stack 的 gap；.card 上下内边距之和
    const ctrlCard = ref(null);
    const stackBodies = {};
    let ro = null;
    function measureStack() {
      if (ctrlCard.value) st.ctrlH = ctrlCard.value.offsetHeight;
      for (const [k, el] of Object.entries(stackBodies)) if (el) st.stackNat[k] = el.offsetHeight + CARD_PAD;
    }
    function setBody(key, el) {
      const old = stackBodies[key];
      if (old === el) return;
      if (old && ro) ro.unobserve(old);
      stackBodies[key] = el;
      if (el && ro) ro.observe(el);
    }
    onMounted(() => {
      if (window.ResizeObserver) {
        ro = new ResizeObserver(measureStack);
        for (const el of Object.values(stackBodies)) if (el) ro.observe(el);
        watch(ctrlCard, (el, old) => { if (old) ro.unobserve(old); if (el) ro.observe(el); }, { immediate: true });
      }
    });
    onBeforeUnmount(() => ro && ro.disconnect());
    // 顶栏高度（窗口窄时会折行变高）写进 CSS 变量 --topbar-h：地图工坊的工具栏贴在顶栏下面、单独滚动
    const topbar = ref(null);
    let topRo = null;
    onMounted(() => {
      const setH = () => topbar.value && document.documentElement.style.setProperty("--topbar-h", `${topbar.value.offsetHeight}px`);
      setH();
      if (window.ResizeObserver && topbar.value) { topRo = new ResizeObserver(setH); topRo.observe(topbar.value); }
    });
    onBeforeUnmount(() => topRo && topRo.disconnect());
    function toggleStack() {
      measureStack();
      st.stepStacked = !st.stepStacked;
      store.set("mapf.teacher.stepStacked", st.stepStacked ? "1" : "0");
    }
    // 控制卡片后面现在有哪几张（本步通信、本局结果按条件出现）
    const stackKeys = computed(() => {
      const v = r.value, keys = ["step"];
      if (v && v.settings.comm_enabled && v.status !== "lobby") keys.push("comm");
      if (v && v.metrics) keys.push("result");
      return keys;
    });
    function underStyle(key) {
      const depth = stackKeys.value.indexOf(key) + 1, nat = st.stackNat[key];
      const style = { zIndex: 10 - depth };
      if (!nat) return style;
      if (!st.stepStacked) return { ...style, maxHeight: `${nat}px` };
      const h = Math.max(PEEK, Math.min(nat, st.ctrlH || nat));
      return { ...style, maxHeight: `${h}px`, marginTop: `${-(h - PEEK + GAP)}px`, transform: `scale(${1 - 0.035 * depth})` };
    }
    function toggleRules() {
      st.rulesCollapsed = !st.rulesCollapsed;
      store.set("mapf.teacher.rulesCollapsed", st.rulesCollapsed ? "1" : "0");
      setTimeout(measure, 540);   // 等过渡动画（0.5 s）走完再量地图框位置
    }
    // 收起时显示的一行摘要
    const rulesSummary = computed(() => {
      const s = st.form;
      if (!s) return "";
      const mapTitle = s.custom_map ? s.custom_map.title : ((st.maps.find((m) => m.name === s.preset) || {}).title || s.preset);
      const parts = [mapTitle, s.num_groups > 1 ? `${s.num_groups} 组` : null,
                     s.view_radius == null ? "全图可见" : `视野 ${s.view_radius} 格`,
                     s.comm_enabled ? "可以通信" : "不通信", s.ui_mode === "simple" ? "简洁界面" : "进阶界面"];
      return parts.filter(Boolean).join(" · ");
    });
    const groupLabel = (g) => (st.groups.length > 1 ? `第 ${g + 1} 组·` : "");
    const setScreenLive = (v) => send({ type: "screen", live_maps: v });
    function removePlayer(p) {
      const tip = p.online ? `把在线的「${p.nickname}」移出课堂？他的页面会回到加入页。` : `移除已离开的「${p.nickname}」？`;
      if (confirm(tip)) send({ type: "remove", player_id: p.id });
    }
    const removeOffline = () => send({ type: "remove", player_id: null });
    // 在线的排前面
    const sortedPlayers = computed(() => [...st.players].sort((a, b) => (b.online - a.online) || (a.id - b.id)));
    const offlineCount = computed(() => st.players.filter((p) => !p.online).length);

    let raf = null;
    const tick = () => { st.now = performance.now(); raf = requestAnimationFrame(tick); };
    onMounted(() => { raf = requestAnimationFrame(tick); });
    onBeforeUnmount(() => cancelAnimationFrame(raf));

    const r = computed(() => st.groups[st.sel] || st.groups[0]);
    // 整体状态（开始 / 暂停 / 结束 / 改规则都作用于所有组）：有组在进行就算进行中
    const status = computed(() => {
      const sts = new Set(st.groups.map((g) => g.status));
      for (const s of ["playing", "paused", "lobby"]) if (sts.has(s)) return s;
      return st.groups.length ? "finished" : "lobby";
    });
    const multi = computed(() => st.groups.length > 1);
    const simpleMode = computed(() => !!(r.value && r.value.settings.ui_mode === "simple"));
    const ava = (i) => (simpleMode.value ? " " + agentAvatar(i) : "");
    const STATUS_SHORT = { lobby: "未开始", playing: "进行中", paused: "暂停", finished: "已结束" };
    // "全部"视图里的缩略图
    function thumbScene(g) {
      const v = g.view;
      return { H: v.H, W: v.W, cells: v.map,
               agents: v.positions.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i) })),
               goals: v.targets.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i) })) };
    }
    const fmtMetric = (m) => (m ? `CSR ${m.CSR === 1 ? "✓" : "✗"} · ISR ${Math.round(m.ISR * 100)}% · SoC ${m.SoC} · 用时 ${m.makespan}` : "");
    const agentOfPlayer = computed(() => {
      const m = {};
      if (r.value) for (const [a, p] of Object.entries(r.value.seats)) m[p] = Number(a);
      return m;
    });
    const playerOfAgent = computed(() => {
      const m = {};
      if (r.value) for (const [a, p] of Object.entries(r.value.seats)) m[Number(a)] = st.players.find((x) => x.id === p);
      return m;
    });
    const nextTickMeasure = () => nextTick(measure);

    // ---- "全部"总览：两侧卡片收起，中间占满；算出列数和缩略图大小，让所有组一屏放下、不用滚动
    const overviewOn = computed(() => st.tab === "class" && multi.value && st.overview);
    const thumbsBox = ref(null);
    const THUMB_PAD = 19, THUMB_HEAD = 30, THUMB_GAP = 12;     // 卡片内边距 + 边框、标题行、卡片间距
    const OV_CONTROLS_H = 76;                                   // 底部控制栏（按钮 + 上边距 + 卡片底边距）
    const thumbFit = reactive({ cols: 3, w: 240 });
    function measureThumbs() {
      const el = thumbsBox.value, g0 = st.groups[0];
      if (!el || !g0) return;
      const k = st.groups.length, aspect = g0.view.H / g0.view.W;
      const Wc = el.clientWidth;
      const Hc = window.innerHeight - (el.getBoundingClientRect().top + window.scrollY) - 24 - OV_CONTROLS_H;
      let best = { cols: 1, w: 0 };
      for (let c = 1; c <= k; c++) {
        const rows = Math.ceil(k / c);
        const byW = (Wc - (c - 1) * THUMB_GAP) / c - THUMB_PAD;
        const byH = ((Hc - (rows - 1) * THUMB_GAP) / rows - THUMB_PAD - THUMB_HEAD) / aspect;
        const w = Math.floor(Math.min(byW, byH));
        if (w > best.w) best = { cols: c, w };
      }
      thumbFit.cols = best.cols; thumbFit.w = Math.max(80, best.w);
    }
    // 过渡分两段，任何时候都不在动画中压窄内容（压窄会让卡片里的字竖排、缩略图排两次版）：
    //   进入：两侧卡片保持原宽度向外滑出 + 淡出（SIDE_MS）→ 一次性切成全宽 → 按最终宽度排好缩略图 → 缩略图淡入
    //   退出：缩略图淡出（THUMB_MS）→ 恢复三栏 → 两侧卡片从外面滑回来
    const SIDE_MS = 300, THUMB_MS = 250;
    const raf2 = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));   // 等布局生效后再改样式，过渡才会播放
    let ovTimer = null;
    function enterOverview() {
      clearTimeout(ovTimer);
      st.sidesOut = true;
      ovTimer = setTimeout(() => {
        st.overview = true;
        nextTick(() => { measureThumbs(); raf2(() => (st.thumbsShown = true)); });
      }, SIDE_MS);
    }
    function exitOverview(g) {
      clearTimeout(ovTimer);
      st.thumbsShown = false;
      ovTimer = setTimeout(() => {
        if (g != null) st.sel = g;
        st.overview = false;
        nextTick(() => { measure(); raf2(() => { st.sidesOut = false; }); });
      }, st.overview ? THUMB_MS : 0);
    }
    // ---- 标签切换。地图工坊有自己的布局（左边工具卡片 + 中间大画布）；切换布局时和总览一样分两段，
    //      不在动画中压窄内容：当前两侧卡片滑出 → 换布局 → 新的侧边卡片滑入（画布另有一段往左滑到位的动画）
    let tabTimer = null;
    function setTab(t) {
      if (t === st.tab) return;
      const toMode = t === "workshop" || t === "review" ? t : "class";
      clearTimeout(ovTimer); clearTimeout(tabTimer);
      if (t === "review") review.enter(); else review.leave();      // 每次切到复盘都重新读课堂记录
      st.overview = false; st.thumbsShown = false;          // 离开课堂标签时退出总览
      if (toMode === st.mode) { st.tab = t; st.sidesOut = false; nextTick(measure); return; }
      st.sidesOut = true;
      tabTimer = setTimeout(() => {
        st.tab = t; st.mode = toMode;
        nextTick(() => { measure(); raf2(() => { st.sidesOut = false; }); });
      }, SIDE_MS);
    }
    // ---- 右栏收起 / 展开：和换布局一样分两段（先滑出再让出宽度；先占回宽度再滑进来），不在动画中压窄内容
    let rightTimer = null;
    function toggleRight() {
      clearTimeout(rightTimer);
      const hide = !st.rightHidden;
      store.set("mapf.teacher.rightHidden", hide ? "1" : "0");
      if (hide) {
        st.rightOut = true;
        rightTimer = setTimeout(() => { st.rightHidden = true; nextTick(measure); }, SIDE_MS);
      } else {
        st.rightOut = true; st.rightHidden = false;
        nextTick(() => { measure(); raf2(() => { st.rightOut = false; }); });
      }
    }
    // 组数变了（新开一局改了分组数）要重新排
    watch(() => st.groups.length, () => { if (overviewOn.value) nextTick(measureThumbs); });
    const onEsc = (e) => { if (e.key === "Escape" && overviewOn.value) exitOverview(); };
    onMounted(() => { window.addEventListener("keydown", onEsc); window.addEventListener("resize", measureThumbs); });
    onBeforeUnmount(() => { window.removeEventListener("keydown", onEsc); window.removeEventListener("resize", measureThumbs); });
    const remaining = computed(() => {
      const ms = r.value && r.value.countdown_ms;
      if (ms == null) return null;
      return r.value.status === "playing" ? Math.max(0, ms - (st.now - r.value._rx)) : ms;
    });

    const scene = computed(() => {
      const v = r.value && r.value.view;
      if (!v) return null;
      const agents = v.positions.map((p, i) => {
        const pl = playerOfAgent.value[i];
        // 简洁模式：编号旁加学生看到的动物，方便老师和学生对上号
        return { id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1), self: !!pl && pl.online,
                 badge: simpleMode.value ? agentAvatar(i) : null };
      });
      const goals = v.targets.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) }));
      const arrows = [];
      if (st.show.expert && v.expert_actions)
        v.expert_actions.forEach((a, i) => arrows.push({ r: v.positions[i][0], c: v.positions[i][1], action: a, color: "#0ea08c" }));
      if (st.show.ai && v.model_actions)
        v.model_actions.forEach((a, i) => arrows.push({ r: v.positions[i][0], c: v.positions[i][1], action: a, color: "#e0a030", dashed: true }));
      const boxes = [];
      const rad = r.value.settings.view_radius;
      if (st.show.boxes && rad != null) {
        for (const [a] of Object.entries(r.value.seats)) {
          const [pr, pc] = v.positions[Number(a)];
          boxes.push({ color: agentColor(Number(a)), box: [Math.max(0, pr - rad), Math.max(0, pc - rad), Math.min(v.H - 1, pr + rad), Math.min(v.W - 1, pc + rad)] });
        }
      }
      const circles = [];
      if (st.show.range && r.value.settings.comm_enabled) {
        for (const [a] of Object.entries(r.value.seats)) {
          const [pr, pc] = v.positions[Number(a)];
          circles.push({ r: pr, c: pc, radius: r.value.settings.comm_radius, color: agentColor(Number(a)) });
        }
      }
      // 学生通信图：本步的消息（橙色实线）和成功的视野交换（青色虚线），与"AI 通信"对比
      const links = [];
      if (st.show.links) {
        for (const e of r.value.comm_events || []) {
          if (e.to == null) continue;
          if (e.type === "msg") links.push({ a: v.positions[e.from], b: v.positions[e.to], color: "#e0a030" });
          else if (e.type === "view_accept") links.push({ a: v.positions[e.from], b: v.positions[e.to], color: "#00a3b4", dashed: true });
        }
      }
      const edges = st.show.edges && v.ai_edges ? v.ai_edges.filter(([i, j]) => i < j).map(([i, j]) => ({ a: v.positions[i], b: v.positions[j] })) : [];
      return { H: v.H, W: v.W, cells: v.map, agents, goals, arrows, boxes, edges, circles, links, stepKey: v.t };
    });
    const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);

    // ---- 地图缩放 ----
    const ZOOMS = [0.4, 0.5, 0.65, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];
    function setZoom(z) { st.zoom = z; store.set("mapf.teacher.zoom", String(z)); }
    const mapWrap = ref(null);
    const fitH = ref(480);       // 地图框可用高度（px）：窗口底部到地图框顶部，再留出图例的位置
    const LEGEND_SPACE = 60;
    // 左栏撑满到窗口底部（只在窗口缩放时量一次左栏的位置）。同学卡片用弹性布局填满规则卡片剩下的空间：
    // 规则卡片收起 / 展开的每一帧，同学卡片都跟着同步变高，不会"先小后长"（以前是动画结束后才用 JS 重算高度）
    const leftCol = ref(null);
    const leftColH = ref(600);
    function measurePlayers() {
      const el = leftCol.value;
      if (!el) return;
      const top = el.getBoundingClientRect().top + window.scrollY;
      leftColH.value = Math.max(480, window.innerHeight - top - 16);
    }
    function measure() {
      measurePlayers();
      const el = mapWrap.value;
      if (!el) return;
      // 用地图框在文档里的位置（不随页面滚动变化），保证页面滚到顶时整张图正好在窗口里
      const top = el.getBoundingClientRect().top + window.scrollY;
      fitH.value = Math.max(240, window.innerHeight - top - LEGEND_SPACE);
    }
    function fitRatio() {
      // "适应"模式下地图实际占地图框宽度的比例：受窗口高度限制时会小于 1
      const el = mapWrap.value, v = r.value && r.value.view;
      if (!el || !v) return 1;
      return Math.min(1, (fitH.value * v.W / v.H) / el.clientWidth);
    }
    function zoomStep(dir) {
      // 从"适应"出发时，按当前实际显示比例找相邻档位，保证"−"一定变小、"＋"一定变大
      const cur = st.zoom === "fit" ? fitRatio() : st.zoom;
      if (dir < 0) { const z = [...ZOOMS].reverse().find((x) => x < cur - 1e-6); setZoom(z ?? ZOOMS[0]); return; }
      const z = ZOOMS.find((x) => x > cur + 1e-6); setZoom(z ?? ZOOMS[ZOOMS.length - 1]); return;
    }
    // 适应：宽度取"地图框宽"和"可用高度按地图宽高比换算的宽"中较小的那个，高度随之确定，整张图不超出窗口
    // 缩放：按地图框宽度的倍数；比框窄时水平居中，比框宽时在框内滚动
    const mapStyle = computed(() => {
      const v = r.value && r.value.view;
      if (st.zoom === "fit" && v) return { width: `min(100%, ${Math.floor(fitH.value * v.W / v.H)}px)`, margin: "0 auto" };
      return { width: `${st.zoom * 100}%`, maxWidth: "none", margin: "0 auto" };
    });
    const wrapStyle = computed(() => (st.zoom === "fit" ? {} : { maxHeight: `${fitH.value}px` }));
    onMounted(() => { window.addEventListener("resize", measure); });
    onBeforeUnmount(() => window.removeEventListener("resize", measure));
    const zoomLabel = computed(() => (st.zoom === "fit" ? "适应" : `${Math.round(st.zoom * 100)}%`));
    function eventText(e) {
      const who = (i) => (i == null ? "" : `${i + 1} 号`);
      if (e.type === "msg") return `${who(e.from)} → ${who(e.to)}：${MESSAGES[e.payload.kind].icon} ${MESSAGES[e.payload.kind].text}`
        + (e.payload.delivery === "next" ? "（下一步送达）" : "");
      if (e.type === "view_request") return `${who(e.from)} 请求和 ${who(e.to)} 交换视野`;
      if (e.type === "view_accept") return `${who(e.from)} 同意了 ${who(e.to)}` + (e.payload && e.payload.auto ? "（自动）" : "");
      if (e.type === "view_reject") return `${who(e.from)} 拒绝了 ${who(e.to)}`;
      return e.type;
    }

    // 地图工坊的编辑状态：整页只建一份，工具卡片和画布共用
    const workshop = createWorkshop({ rpc, toast, getMaps: () => st.maps, onUse: useWorkshopMap });
    const review = createReview({ rpc, toast, keyParam: key || "" });
    watch(() => st.online, (ok) => { if (ok) workshop.revalidate(); });

    return { workshop, review, toggleRight, topbar, setTab, overviewOn, thumbsBox, thumbFit, THUMB_PAD, enterOverview, exitOverview, leftCol, leftColH, ctrlCard, setBody, toggleStack, underStyle, stackKeys, toggleRules, rulesSummary, ava, simpleMode, keyParam: key || "", nextTickMeasure, seatValue, groupLabel, setScreenLive, multi, STATUS_SHORT, thumbScene, fmtMetric, toast, rpc, pickPreset, useWorkshopMap, currentTemplate, loadTemplate, saveTemplate, deleteTemplate,
             onDragStart, onDropMap, eventText, zoomStep, setZoom, mapStyle, wrapStyle, zoomLabel, mapWrap, st, r, status, scene, remaining, OPTIONS, STATUS, applySettings, cmd, assign, agentOfPlayer,
             removePlayer, removeOffline, sortedPlayers, offlineCount, MESSAGES,
             playerOfAgent, agentColor, icon };
  },
  template: `
  <div>
    <div v-if="st.denied" class="conn-lost">老师页面只能在本机打开，或者使用启动时打印的带 key 的地址</div>
    <div v-else-if="!st.online" class="conn-lost">连接断开，正在重连……</div>
    <div ref="topbar" class="topbar">
      <h1>🧭 MAPF 课堂 · 老师</h1>
      <span :class="['badge', status]">{{ STATUS[status] }}</span>
      <span v-if="r" class="muted">{{ r.preset_title }} · {{ r.num_agents }} 个智能体</span>
      <div class="join">
        <div style="text-align:right"><div class="muted">学生加入地址</div><div class="url">{{ st.joinUrl }}</div></div>
        <img src="/api/join-qr.svg" alt="加入二维码">
      </div>
    </div>

    <div :class="['layout', { overview: overviewOn, 'sides-out': st.sidesOut, 'ws-mode': st.mode === 'workshop',
                  'rv-mode': st.mode === 'review', 'no-right': st.rightHidden || st.mode === 'workshop', 'right-out': st.rightOut }]" v-if="r && st.form">
      <!-- 各栏按布局模式用 v-show 显隐（行内 display:none，不会被别的 CSS 规则盖掉）。地图工坊的左栏：工具卡片 -->
      <div v-show="st.mode === 'workshop'" class="col side left ws-only">
        <WorkshopTools :w="workshop" :maps="st.maps" :can-use="status !== 'playing' && status !== 'paused'"/>
      </div>
      <!-- 复盘的左栏：每个智能体的对比、本步通信、教学指标、导出 -->
      <div v-show="st.mode === 'review'" class="col side left rv-only">
        <ReviewSide :v="review"/>
      </div>
      <div v-show="st.mode === 'class'" ref="leftCol" class="col side left class-only" :style="{ minHeight: leftColH + 'px' }">
        <div class="card rules-card">
          <h2 class="fold" @click="toggleRules" :title="st.rulesCollapsed ? '展开规则' : '收起规则'">
            规则 <span :class="['fold-icon', { closed: st.rulesCollapsed }]">▾</span>
            <span v-if="st.dirty" class="badge paused" style="margin-left:auto">有改动未应用</span>
          </h2>
          <!-- 收起 / 展开用 grid-template-rows 1fr ⇄ 0fr 过渡：内容高度不固定也能平滑动画 -->
          <div :class="['fold-wrap', 'fold-summary-wrap', { open: st.rulesCollapsed }]">
            <div class="fold-inner"><div class="muted small fold-summary" @click="toggleRules">{{ rulesSummary }}</div></div>
          </div>
          <div :class="['fold-wrap', { open: !st.rulesCollapsed }]">
          <div class="fold-inner">
          <div class="tpl">
            <select v-model="st.templateName">
              <option value="">课例模板……</option>
              <optgroup label="内置课例"><option v-for="t in st.templates.filter(x => x.builtin)" :value="t.name">{{ t.title }}</option></optgroup>
              <optgroup v-if="st.templates.some(x => !x.builtin)" label="我的模板">
                <option v-for="t in st.templates.filter(x => !x.builtin)" :value="t.name">{{ t.title }}</option></optgroup>
            </select>
            <button class="btn ghost" :disabled="!currentTemplate" @click="loadTemplate">载入</button>
            <button v-if="currentTemplate && !currentTemplate.builtin" class="btn ghost" @click="deleteTemplate">删除</button>
          </div>
          <p v-if="currentTemplate" class="muted small">{{ currentTemplate.note }}</p>
          <div v-if="r.ai_hint" class="ai-hint">🤖 {{ r.ai_hint }}</div>
          <div class="form" @input="st.dirty = true" @change="st.dirty = true">
            <label>地图</label>
            <div v-if="st.form.custom_map" class="custom-map">
              <b>工坊地图：{{ st.form.custom_map.title }}</b>
              <button class="btn ghost small" @click="pickPreset">改用地图库</button>
            </div>
            <select v-else v-model="st.form.preset" @change="pickPreset">
              <optgroup label="内置"><option v-for="m in st.maps.filter(x => x.builtin)" :value="m.name">{{ m.title }}</option></optgroup>
              <optgroup v-if="st.maps.some(x => !x.builtin)" label="我保存的">
                <option v-for="m in st.maps.filter(x => !x.builtin)" :value="m.name">{{ m.title }}（{{ m.n }} 人）</option></optgroup>
            </select>
            <label>智能体数</label>
            <input type="number" min="1" max="64" v-model="st.form.num_agents" placeholder="用预设默认值">
            <label>分组数</label>
            <input type="number" min="1" max="12" v-model.number="st.form.num_groups">
            <label>排名方式</label>
            <select v-model="st.form.rank_by">
              <option value="lex">综合（全员到达 → 到达比例 → 总步数 → 用时）</option>
              <option value="CSR">只看是否全员到达</option><option value="ISR">只看到达比例</option>
              <option value="SoC">只看总步数（少者胜）</option><option value="makespan">只看用时（少者胜）</option>
            </select>
            <label>倒计时（秒）</label>
            <input type="number" min="3" max="300" v-model="st.form.countdown_sec">
            <label>最多步数</label>
            <input type="number" min="1" max="512" v-model="st.form.max_steps">
            <label>视野半径</label>
            <select v-model="st.form.view_radius"><option v-for="o in OPTIONS.view_radius" :value="o[0]">{{ o[1] }}</option></select>
            <label>迷雾</label>
            <select v-model="st.form.fog_mode"><option v-for="o in OPTIONS.fog_mode" :value="o[0]">{{ o[1] }}</option></select>
            <label>自己的目标</label>
            <select v-model="st.form.goal_display"><option v-for="o in OPTIONS.goal_display" :value="o[0]">{{ o[1] }}</option></select>
            <label>专家/AI 建议</label>
            <select v-model="st.form.reveal_mode"><option v-for="o in OPTIONS.reveal_mode" :value="o[0]">{{ o[1] }}</option></select>
            <label>空位补位</label>
            <select v-model="st.form.fill_policy"><option v-for="o in OPTIONS.fill_policy" :value="o[0]">{{ o[1] }}</option></select>
            <label>超时未选</label>
            <select v-model="st.form.timeout_policy"><option v-for="o in OPTIONS.timeout_policy" :value="o[0]">{{ o[1] }}</option></select>
            <label>界面</label>
            <select v-model="st.form.ui_mode"><option v-for="o in OPTIONS.ui_mode" :value="o[0]">{{ o[1] }}</option></select>
            <label>AI 模型</label>
            <select v-model="st.form.model_variant"><option v-for="o in OPTIONS.model_variant" :value="o[0]">{{ o[1] }}</option></select>
            <label class="full sep">学生通信</label>
            <label>开启通信</label>
            <label><input type="checkbox" v-model="st.form.comm_enabled" style="width:auto"> 允许同学之间通信</label>
            <template v-if="st.form.comm_enabled">
              <label>通信半径</label>
              <input type="number" min="1" max="12" step="0.5" v-model="st.form.comm_radius">
              <label>交换视野</label>
              <label><input type="checkbox" v-model="st.form.comm_view_exchange" style="width:auto"> 允许
                <input type="checkbox" v-model="st.form.comm_auto_accept" style="width:auto;margin-left:10px"> 不用对方确认</label>
              <label>每步次数</label>
              <select v-model="st.form.comm_budget"><option v-for="o in OPTIONS.comm_budget" :value="o[0]">{{ o[1] }}</option></select>
              <label>消息送达</label>
              <select v-model="st.form.comm_delivery"><option v-for="o in OPTIONS.comm_delivery" :value="o[0]">{{ o[1] }}</option></select>
              <label>可用消息</label>
              <div class="kinds">
                <label v-for="(m, k) in MESSAGES" :key="k" :title="m.text">
                  <input type="checkbox" :value="k" v-model="st.form.comm_messages" style="width:auto"> {{ m.icon }}</label>
              </div>
            </template>
            <label class="full sep">其他</label>
            <label>精细模式</label>
            <label><input type="checkbox" v-model="st.form.tta" style="width:auto"> TTA（更准但更慢）</label>
            <button class="btn full" :disabled="status === 'playing' || status === 'paused'" @click="applySettings">
              {{ st.pendingApply ? '正在应用……' : st.dirty ? '应用规则（新开一局）' : '新开一局' }}</button>
            <div v-if="st.applyError" class="full apply-error">没有应用成功：{{ st.applyError }}</div>
            <div v-else-if="status === 'playing' || status === 'paused'" class="full muted" style="font-size:13px">
              比赛进行中不能改规则，先点「结束」</div>
            <button class="btn ghost full" @click="saveTemplate">把当前规则存为模板</button>
          </div>
          </div>
          </div>
        </div>
        <div class="card players-card">
          <h2>同学（{{ st.players.filter(p => p.online).length }} 人在线）</h2>
          <!-- 列表在自己的区域里滚动：鼠标放在上面滚轮只滚列表，不带着整个页面跑 -->
          <div class="players-scroll">
          <table class="players">
            <thead><tr><th>昵称</th><th>座位</th><th>{{ multi ? '组 · 智能体' : '智能体' }}</th><th></th></tr></thead>
            <tr v-for="p in sortedPlayers" :key="p.id" :class="{ offline: !p.online }" draggable="true"
                @dragstart="onDragStart($event, p)" title="可以把这一行拖到地图上的圆点上分配座位">
              <td><span :class="['online-dot', { yes: p.online }]"></span>{{ p.nickname }}<span v-if="!p.online" class="muted">（已离开）</span></td>
              <td>{{ p.seat }}</td>
              <td>
                <select :value="seatValue(p)" @change="assign(p.id, $event.target.value)" :disabled="status === 'finished'">
                  <option value="" disabled>未分组</option>
                  <template v-for="g in st.groups" :key="g.group">
                    <option :value="g.group + ':'">{{ groupLabel(g.group) }}观战</option>
                    <option v-for="i in g.num_agents" :value="g.group + ':' + (i - 1)">{{ groupLabel(g.group) }}{{ i }} 号{{ ava(i - 1) }}</option>
                  </template>
                </select>
              </td>
              <td><button class="x" title="移出课堂" @click="removePlayer(p)">✕</button></td>
            </tr>
          </table>
          <p v-if="!st.players.length" class="muted small" style="padding:6px 2px">还没有同学加入</p>
          </div>
          <button v-if="offlineCount" class="btn ghost" style="margin-top:10px;width:100%" @click="removeOffline">
            清除已离开的同学（{{ offlineCount }} 人）</button>
          <button class="btn ghost" style="margin-top:10px;width:100%" :disabled="status !== 'lobby'" @click="cmd('shuffle')">
            {{ multi ? '随机分组并分配座位' : '随机分配座位' }}</button>
        </div>
      </div>

      <div class="col center">
        <div class="tabs">
          <button :class="{ on: st.tab === 'class' }" @click="setTab('class')">课堂</button>
          <button :class="{ on: st.tab === 'workshop' }" @click="setTab('workshop')">地图工坊</button>
          <button :class="{ on: st.tab === 'review' }" @click="setTab('review')">复盘</button>
          <button v-if="st.mode !== 'workshop'" class="tabs-tool" @click="toggleRight"
                  :title="st.rightHidden ? '把控制、排行榜等卡片拿回来' : '把右边的卡片收到右侧，让出宽度'">
            {{ st.rightHidden ? '◂ 显示右栏' : '收起右栏 ▸' }}</button>
        </div>
        <div v-show="st.tab === 'review'" class="card rv-main-card">
          <ReviewMain :v="review" :active="st.mode === 'review'"/>
        </div>
        <div v-show="st.tab === 'workshop'" class="card ws-canvas-card">
          <WorkshopCanvas :w="workshop" :active="st.mode === 'workshop'"/>
        </div>
        <div v-show="st.tab === 'class'" class="card class-card">
          <div v-if="overviewOn" class="overview-bar">
            <b>全部 {{ st.groups.length }} 组</b>
            <span class="muted small">点任意一组进入 · 按 Esc 退出</span>
            <button class="btn ghost small" @click="exitOverview()">退出总览</button>
          </div>
          <div v-else-if="multi" class="groupbar">
            <button :class="['gb', { sel: st.overview }]" @click="enterOverview">全部</button>
            <button v-for="g in st.groups" :key="g.group" :class="['gb', g.status, { sel: !st.overview && st.sel === g.group }]"
                    @click="exitOverview(g.group)">
              {{ g.group_name }} <small>{{ STATUS_SHORT[g.status] }} {{ g.view.at_goal.filter(x => x).length }}/{{ g.num_agents }}</small>
            </button>
          </div>
          <div v-if="overviewOn" ref="thumbsBox" :class="['thumbs', { shown: st.thumbsShown }]"
               :style="{ gridTemplateColumns: 'repeat(' + thumbFit.cols + ', ' + (thumbFit.w + THUMB_PAD) + 'px)' }">
            <div v-for="g in st.groups" :key="g.group" class="thumb" @click="exitOverview(g.group)">
              <div class="thumb-head"><b>{{ g.group_name }}</b>
                <span :class="['badge', g.status]">{{ STATUS_SHORT[g.status] }}</span>
                <span class="muted">第 {{ g.view.t }} 步 · 到达 {{ g.view.at_goal.filter(x => x).length }}/{{ g.num_agents }}</span></div>
              <GridView :scene="thumbScene(g)" :style="{ width: thumbFit.w + 'px' }"/>
            </div>
          </div>
          <!-- 总览底部的控制栏：两侧卡片收起后，开始 / 暂停 / 结算 / 结束仍然够得着（作用于所有组） -->
          <div v-if="overviewOn" :class="['ov-controls', { shown: st.thumbsShown }]">
            <span :class="['badge', status]">{{ STATUS[status] }}</span>
            <button class="btn ok" :disabled="status !== 'lobby'" @click="cmd('start')">▶ 开始</button>
            <button v-if="status !== 'paused'" class="btn ghost" :disabled="status !== 'playing'" @click="cmd('pause')">⏸ 暂停</button>
            <button v-else class="btn" @click="cmd('resume')">▶ 继续</button>
            <button class="btn ghost" :disabled="status !== 'playing'" @click="cmd('step_now')" title="所有组都不等倒计时，马上结算这一步">
              ⏭ 全部立即结算</button>
            <button class="btn danger" :disabled="status === 'finished'" @click="cmd('end')">■ 结束</button>
          </div>
          <template v-else>
          <div v-if="r.ai_hint" class="ai-hint">🤖 {{ r.ai_hint }}</div>
          <div class="toggles">
            <label><input type="checkbox" v-model="st.show.expert"> 专家箭头</label>
            <label><input type="checkbox" v-model="st.show.ai"> AI 箭头</label>
            <label><input type="checkbox" v-model="st.show.boxes"> 学生视野</label>
            <label><input type="checkbox" v-model="st.show.edges"> AI 通信</label>
            <label><input type="checkbox" v-model="st.show.links"> 学生通信</label>
            <label><input type="checkbox" v-model="st.show.range"> 通信范围</label>
          </div>
          <div class="zoombar">
            <button class="zb" title="缩小" @click="zoomStep(-1)">−</button>
            <button :class="['zb', 'wide', { on: st.zoom === 'fit' }]" title="整张地图缩进窗口" @click="setZoom('fit')">适应窗口</button>
            <button class="zb" title="放大" @click="zoomStep(1)">＋</button>
            <span v-if="st.zoom !== 'fit'" class="muted">{{ zoomLabel }}</span>
          </div>
          <div ref="mapWrap" :class="['map-wrap', { fit: st.zoom === 'fit' }]" :style="wrapStyle"
               @dragover.prevent @drop.prevent="onDropMap">
            <GridView v-if="scene" :scene="scene" :style="mapStyle"/>
          </div>
          <div class="legend">
            <span><i style="background:#0ea08c"></i>专家</span>
            <span><i style="background:#e0a030"></i>AI</span>
            <span><i style="background:#e0a030"></i>学生消息</span>
            <span><i style="background:#00a3b4"></i>交换视野</span>
            <span><i style="background:var(--c3)"></i>AI 通信</span>
            <span>带光圈 = 在线学生控制</span>
            <span v-if="simpleMode">编号旁的动物 = 学生页上看到的头像</span>
          </div>
          </template>
        </div>
      </div>

      <div v-show="st.mode !== 'workshop' && !st.rightHidden" class="col side right">
        <div class="stack">
        <div ref="ctrlCard" class="card stack-top">
          <h2 class="with-tool">控制
            <button class="stack-btn" @click="toggleStack" :title="st.stepStacked ? '展开下面的卡片' : '把本步、通信、结果卡片叠到控制卡片后面'">
              {{ st.stepStacked ? '展开 ▾' : '叠起 ▴' }}</button>
          </h2>
          <div class="controls">
            <button class="btn ok" :disabled="status !== 'lobby'" @click="cmd('start')">▶ 开始</button>
            <button v-if="status !== 'paused'" class="btn ghost" :disabled="status !== 'playing'" @click="cmd('pause')">⏸ 暂停</button>
            <button v-else class="btn" @click="cmd('resume')">▶ 继续</button>
            <button class="btn ghost" :disabled="r.status !== 'playing'" @click="multi ? cmd('step_now', { group: st.sel }) : cmd('step_now')"
                    :title="multi ? '只结算正在看的' + r.group_name : '不等倒计时，马上结算这一步'">
              ⏭ {{ multi ? '结算本组' : '立即结算' }}</button>
            <button class="btn danger" :disabled="status === 'finished'" @click="cmd('end')">■ 结束</button>
          </div>
        </div>
        <div :class="['card', 'stack-under', { stacked: st.stepStacked }]" :style="underStyle('step')"
             @click="st.stepStacked && toggleStack()">
          <div class="stack-body" :ref="(el) => setBody('step', el)">
          <h2>本步{{ multi ? ' · ' + r.group_name : '' }}</h2>
          <div class="stat"><span>步数</span><b>{{ r.view.t + (r.status === 'finished' ? 0 : 1) }} / {{ r.settings.max_steps }}</b></div>
          <div class="stat"><span>倒计时</span><b>{{ remaining == null ? '—' : Math.ceil(remaining / 1000) + ' 秒' }}</b></div>
          <div class="stat"><span>已提交</span><b>{{ r.progress.submitted }} / {{ r.progress.total }}</b></div>
          <div class="stat"><span>到达目标</span><b>{{ r.view.at_goal.filter(x => x).length }} / {{ r.num_agents }}</b></div>
          <div class="stat" v-if="r.view.expert_source"><span>专家</span><b>{{ r.view.expert_source === 'lacam' ? 'LaCAM' : '简易专家' }}</b></div>
          <div class="chips" style="margin-top:8px">
            <span v-for="(pid, a) in r.seats" :key="a" :class="['chip', { done: r.choices[a] !== undefined }]">
              <span class="sw" :style="{ background: agentColor(Number(a)) }"></span>
              {{ Number(a) + 1 }} 号{{ ava(Number(a)) }} {{ (playerOfAgent[a] || {}).nickname }} {{ r.choices[a] !== undefined ? icon(r.choices[a]) : '…' }}
            </span>
          </div>
          </div>
          <!-- 叠起时露在上一张卡片下沿的那一条 -->
          <div class="peek">本步：第 {{ r.view.t + (r.status === 'finished' ? 0 : 1) }} 步 · 已提交 {{ r.progress.submitted }}/{{ r.progress.total }} · 到达 {{ r.view.at_goal.filter(x => x).length }}/{{ r.num_agents }} ▾</div>
        </div>
        <div v-if="stackKeys.includes('comm')" :class="['card', 'stack-under', { stacked: st.stepStacked }]" :style="underStyle('comm')"
             @click="st.stepStacked && toggleStack()">
          <div class="stack-body" :ref="(el) => setBody('comm', el)">
          <h2>本步通信（{{ (r.comm_events || []).length }}）</h2>
          <div v-if="!(r.comm_events || []).length" class="muted" style="font-size:14px">还没有人通信</div>
          <div class="commlog">
            <div v-for="(e, i) in (r.comm_events || [])" :key="i">{{ eventText(e) }}</div>
          </div>
          </div>
          <div class="peek">本步通信：{{ (r.comm_events || []).length ? (r.comm_events || []).length + ' 条' : '还没有' }} ▾</div>
        </div>
        <div v-if="stackKeys.includes('result')" :class="['card', 'stack-under', { stacked: st.stepStacked }]" :style="underStyle('result')"
             @click="st.stepStacked && toggleStack()">
          <div class="stack-body" :ref="(el) => setBody('result', el)">
          <h2>本局结果{{ multi ? ' · ' + r.group_name : '' }}</h2>
          <div class="stat"><span>全员到达 CSR</span><b>{{ r.metrics.CSR === 1 ? '是' : '否' }}</b></div>
          <div class="stat"><span>到达比例 ISR</span><b>{{ Math.round(r.metrics.ISR * 100) }}%</b></div>
          <div class="stat"><span>总步数 SoC</span><b>{{ r.metrics.SoC }}</b></div>
          <div class="stat"><span>用时 makespan</span><b>{{ r.metrics.makespan }}</b></div>
          </div>
          <div class="peek">本局结果：{{ r.metrics.CSR === 1 ? '全员到达' : '到达 ' + Math.round(r.metrics.ISR * 100) + '%' }} · SoC {{ r.metrics.SoC }} · 用时 {{ r.metrics.makespan }} ▾</div>
        </div>
        </div>
        <div class="card">
          <h2>排行榜</h2>
          <div class="board">
            <div v-for="(e, i) in st.leaderboard" :key="i" :class="['brow', e.kind]">
              <span class="rank">{{ e.rank ?? '·' }}</span>
              <span class="bname">{{ e.name }}</span>
              <span v-if="e.kind === 'group'" :class="['badge', e.status]">{{ STATUS_SHORT[e.status] }}</span>
              <span v-else class="muted small">{{ e.status === 'computing' ? '计算中…' : e.status === 'failed' ? '没算出来' : '参照' }}</span>
              <span class="bm">{{ e.metrics ? fmtMetric(e.metrics) : '' }}<i v-if="e.provisional && e.status !== 'lobby'">（暂定）</i></span>
            </div>
          </div>
          <label class="small" style="display:block;margin-top:10px">
            <input type="checkbox" :checked="st.screenLive" @change="setScreenLive($event.target.checked)">
            比赛中投屏也显示各组全图（迷雾课不要开）</label>
          <a class="small" href="/screen" target="_blank">打开投屏页 ↗</a>
        </div>
      </div>
    </div>
    <div v-if="st.toast" class="toast">{{ st.toast }}</div>
  </div>`,
}).mount("#app");
