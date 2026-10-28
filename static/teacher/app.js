// 老师端：规则（含课例模板）→ 座位（下拉 / 拖拽）→ 开始 / 暂停 / 立即结算 / 结束，上帝视角看全图和建议；
// "地图工坊"标签页里生成、编辑、保存、导入导出地图（static/teacher/workshop.js）。
import { Connection, ACTIONS, MESSAGES, agentColor, store, cellFromEvent } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";
import { MapWorkshop } from "/static/teacher/workshop.js";

const { createApp, reactive, computed, ref, nextTick, onMounted, onBeforeUnmount } = Vue;

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
  components: { GridView, MapWorkshop },
  setup() {
    const st = reactive({
      online: false, denied: false, players: [], maps: [], templates: [], defaults: null, round: null, joinUrl: "",
      tab: "class", templateName: "",
      form: null, dirty: false, pendingApply: false, applyError: "", toast: "", now: performance.now(), receivedAt: 0,
      show: { expert: true, ai: false, boxes: true, edges: false, range: false, links: true },
      // 地图缩放："fit" = 整张图缩进窗口；数字 = 相对地图框宽度的倍数（放大后在框内滚动）
      zoom: store.get("mapf.teacher.zoom") === null || store.get("mapf.teacher.zoom") === "fit"
        ? "fit" : Number(store.get("mapf.teacher.zoom")) || "fit",
      lastResolved: null,
    });
    let toastTimer = null;
    const toast = (m) => { st.toast = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (st.toast = ""), 3000); };

    function setRound(r) {
      nextTick(measure);   // 地图框第一次出现、或上方内容高度变化后重新测量
      st.round = r; st.receivedAt = performance.now();
      if (!st.dirty) st.form = JSON.parse(JSON.stringify(r.settings));
    }
    function onMessage(m) {
      if (m.type === "classroom") {
        // 应用规则成功后服务端会推一份新的 classroom 快照；这时才把表单换成服务端的设置
        if (st.pendingApply) { st.pendingApply = false; st.dirty = false; st.applyError = ""; }
        st.players = m.players; st.maps = m.maps; st.templates = m.templates; st.defaults = m.defaults; setRound(m.round);
        if (m.join_url) st.joinUrl = m.join_url;
      } else if (m.type === "teacher_state") {
        setRound(m);
      } else if (m.type === "progress" && st.round) {
        st.round.progress = { submitted: m.submitted, total: m.total };
        st.round.choices = m.choices;
      } else if (m.type === "comm_event" && st.round) {
        if (st.round.view && m.event.t === st.round.view.t) st.round.comm_events.push(m.event);
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
      st.form.custom_map = spec; st.dirty = true; st.tab = "class";
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
      send({ type: "assign", player_id: pid, agent });
    }
    const cmd = (type) => send({ type });
    const assign = (pid, v) => send({ type: "assign", player_id: pid, agent: v === "" ? null : Number(v) });
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

    const r = computed(() => st.round);
    const status = computed(() => (r.value ? r.value.status : "lobby"));
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
    const remaining = computed(() => {
      const ms = r.value && r.value.countdown_ms;
      if (ms == null) return null;
      return status.value === "playing" ? Math.max(0, ms - (st.now - st.receivedAt)) : ms;
    });

    const scene = computed(() => {
      const v = r.value && r.value.view;
      if (!v) return null;
      const agents = v.positions.map((p, i) => {
        const pl = playerOfAgent.value[i];
        return { id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1), self: !!pl && pl.online };
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
      return { H: v.H, W: v.W, cells: v.map, agents, goals, arrows, boxes, edges, circles, links };
    });
    const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);

    // ---- 地图缩放 ----
    const ZOOMS = [0.4, 0.5, 0.65, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];
    function setZoom(z) { st.zoom = z; store.set("mapf.teacher.zoom", String(z)); }
    const mapWrap = ref(null);
    const fitH = ref(480);       // 地图框可用高度（px）：窗口底部到地图框顶部，再留出图例的位置
    const LEGEND_SPACE = 60;
    function measure() {
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

    return { toast, rpc, pickPreset, useWorkshopMap, currentTemplate, loadTemplate, saveTemplate, deleteTemplate,
             onDragStart, onDropMap, eventText, zoomStep, setZoom, mapStyle, wrapStyle, zoomLabel, mapWrap, st, r, status, scene, remaining, OPTIONS, STATUS, applySettings, cmd, assign, agentOfPlayer,
             removePlayer, removeOffline, sortedPlayers, offlineCount, MESSAGES,
             playerOfAgent, agentColor, icon };
  },
  template: `
  <div>
    <div v-if="st.denied" class="conn-lost">老师页面只能在本机打开，或者使用启动时打印的带 key 的地址</div>
    <div v-else-if="!st.online" class="conn-lost">连接断开，正在重连……</div>
    <div class="topbar">
      <h1>🧭 MAPF 课堂 · 老师</h1>
      <span :class="['badge', status]">{{ STATUS[status] }}</span>
      <span v-if="r" class="muted">{{ r.preset_title }} · {{ r.num_agents }} 个智能体</span>
      <div class="join">
        <div style="text-align:right"><div class="muted">学生加入地址</div><div class="url">{{ st.joinUrl }}</div></div>
        <img src="/api/join-qr.svg" alt="加入二维码">
      </div>
    </div>

    <div class="layout" v-if="r && st.form">
      <div class="col">
        <div class="card">
          <h2>规则</h2>
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
        <div class="card">
          <h2>同学（{{ st.players.filter(p => p.online).length }} 人在线）</h2>
          <table class="players">
            <tr><th>昵称</th><th>座位</th><th>智能体</th><th></th></tr>
            <tr v-for="p in sortedPlayers" :key="p.id" :class="{ offline: !p.online }" draggable="true"
                @dragstart="onDragStart($event, p)" title="可以把这一行拖到地图上的圆点上分配座位">
              <td><span :class="['online-dot', { yes: p.online }]"></span>{{ p.nickname }}<span v-if="!p.online" class="muted">（已离开）</span></td>
              <td>{{ p.seat }}</td>
              <td>
                <select :value="agentOfPlayer[p.id] ?? ''" @change="assign(p.id, $event.target.value)" :disabled="status === 'finished'">
                  <option value="">观战</option>
                  <option v-for="i in r.num_agents" :value="i - 1">{{ i }} 号</option>
                </select>
              </td>
              <td><button class="x" title="移出课堂" @click="removePlayer(p)">✕</button></td>
            </tr>
          </table>
          <button v-if="offlineCount" class="btn ghost" style="margin-top:10px;width:100%" @click="removeOffline">
            清除已离开的同学（{{ offlineCount }} 人）</button>
          <button class="btn ghost" style="margin-top:10px;width:100%" :disabled="status !== 'lobby'" @click="cmd('shuffle')">随机分配座位</button>
        </div>
      </div>

      <div class="col">
        <div class="tabs">
          <button :class="{ on: st.tab === 'class' }" @click="st.tab = 'class'">课堂</button>
          <button :class="{ on: st.tab === 'workshop' }" @click="st.tab = 'workshop'">地图工坊</button>
        </div>
        <div v-show="st.tab === 'workshop'" class="card">
          <MapWorkshop :rpc="rpc" :maps="st.maps" :can-use="status !== 'playing' && status !== 'paused'"
                       @use="useWorkshopMap" @toast="toast"/>
        </div>
        <div v-show="st.tab === 'class'" class="card">
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
          </div>
        </div>
      </div>

      <div class="col">
        <div class="card">
          <h2>控制</h2>
          <div class="controls">
            <button class="btn ok" :disabled="status !== 'lobby'" @click="cmd('start')">▶ 开始</button>
            <button v-if="status !== 'paused'" class="btn ghost" :disabled="status !== 'playing'" @click="cmd('pause')">⏸ 暂停</button>
            <button v-else class="btn" @click="cmd('resume')">▶ 继续</button>
            <button class="btn ghost" :disabled="status !== 'playing'" @click="cmd('step_now')">⏭ 立即结算</button>
            <button class="btn danger" :disabled="status === 'finished'" @click="cmd('end')">■ 结束</button>
          </div>
        </div>
        <div class="card">
          <h2>本步</h2>
          <div class="stat"><span>步数</span><b>{{ r.view.t + (status === 'finished' ? 0 : 1) }} / {{ r.settings.max_steps }}</b></div>
          <div class="stat"><span>倒计时</span><b>{{ remaining == null ? '—' : Math.ceil(remaining / 1000) + ' 秒' }}</b></div>
          <div class="stat"><span>已提交</span><b>{{ r.progress.submitted }} / {{ r.progress.total }}</b></div>
          <div class="stat"><span>到达目标</span><b>{{ r.view.at_goal.filter(x => x).length }} / {{ r.num_agents }}</b></div>
          <div class="stat" v-if="r.view.expert_source"><span>专家</span><b>{{ r.view.expert_source === 'lacam' ? 'LaCAM' : '简易专家' }}</b></div>
          <div class="chips" style="margin-top:8px">
            <span v-for="(pid, a) in r.seats" :key="a" :class="['chip', { done: r.choices[a] !== undefined }]">
              <span class="sw" :style="{ background: agentColor(Number(a)) }"></span>
              {{ Number(a) + 1 }} 号 {{ (playerOfAgent[a] || {}).nickname }} {{ r.choices[a] !== undefined ? icon(r.choices[a]) : '…' }}
            </span>
          </div>
        </div>
        <div class="card" v-if="r.settings.comm_enabled && status !== 'lobby'">
          <h2>本步通信（{{ (r.comm_events || []).length }}）</h2>
          <div v-if="!(r.comm_events || []).length" class="muted" style="font-size:14px">还没有人通信</div>
          <div class="commlog">
            <div v-for="(e, i) in (r.comm_events || [])" :key="i">{{ eventText(e) }}</div>
          </div>
        </div>
        <div class="card" v-if="r.metrics">
          <h2>本局结果</h2>
          <div class="stat"><span>全员到达 CSR</span><b>{{ r.metrics.CSR === 1 ? '是' : '否' }}</b></div>
          <div class="stat"><span>到达比例 ISR</span><b>{{ Math.round(r.metrics.ISR * 100) }}%</b></div>
          <div class="stat"><span>总步数 SoC</span><b>{{ r.metrics.SoC }}</b></div>
          <div class="stat"><span>用时 makespan</span><b>{{ r.metrics.makespan }}</b></div>
        </div>
      </div>
    </div>
    <div v-if="st.toast" class="toast">{{ st.toast }}</div>
  </div>`,
}).mount("#app");
