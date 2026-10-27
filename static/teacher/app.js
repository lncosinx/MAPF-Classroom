// 老师端（M1 简易版）：规则 → 座位 → 开始 / 暂停 / 立即结算 / 结束，上帝视角看全图和建议。
import { Connection, ACTIONS, agentColor } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { createApp, reactive, computed, onMounted, onBeforeUnmount } = Vue;

const OPTIONS = {
  view_radius: [[null, "全图可见"], [1, "1 格"], [2, "2 格"], [3, "3 格"], [4, "4 格"], [5, "5 格"]],
  fog_mode: [["memory", "记忆地图（走过的地方记得墙）"], ["fog", "纯迷雾"]],
  goal_display: [["projection", "视野外只显示边上的投影（同 AI）"], ["visible", "始终看得见"]],
  reveal_mode: [["teacher", "只给老师看"], ["immediate", "每步结算后告诉学生"], ["review", "赛后复盘再看"]],
  fill_policy: [["model", "AI 模型"], ["expert", "专家"], ["idle", "原地不动"]],
  timeout_policy: [["wait", "原地不动"], ["ai", "由补位方代选"]],
  ui_mode: [["advanced", "进阶（高年级）"], ["simple", "简洁（低年级）"]],
  model_variant: [["big", "主模型 7.45M"], ["small", "轻量 2.16M（老电脑）"]],
};
const STATUS = { lobby: "未开始", playing: "进行中", paused: "暂停", finished: "已结束" };

createApp({
  components: { GridView },
  setup() {
    const st = reactive({
      online: false, denied: false, players: [], presets: [], round: null, joinUrl: "",
      form: null, dirty: false, pendingApply: false, applyError: "", toast: "", now: performance.now(), receivedAt: 0,
      show: { expert: true, ai: false, boxes: true, edges: false },
      lastResolved: null,
    });
    let toastTimer = null;
    const toast = (m) => { st.toast = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (st.toast = ""), 3000); };

    function setRound(r) {
      st.round = r; st.receivedAt = performance.now();
      if (!st.dirty) st.form = JSON.parse(JSON.stringify(r.settings));
    }
    function onMessage(m) {
      if (m.type === "classroom") {
        // 应用规则成功后服务端会推一份新的 classroom 快照；这时才把表单换成服务端的设置
        if (st.pendingApply) { st.pendingApply = false; st.dirty = false; st.applyError = ""; }
        st.players = m.players; st.presets = m.presets; setRound(m.round);
        if (m.join_url) st.joinUrl = m.join_url;
      } else if (m.type === "teacher_state") {
        setRound(m);
      } else if (m.type === "progress" && st.round) {
        st.round.progress = { submitted: m.submitted, total: m.total };
        st.round.choices = m.choices;
      } else if (m.type === "resolved") {
        st.lastResolved = m;
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
    function applySettings() {
      const s = { ...st.form };
      if (s.num_agents === "" || s.num_agents === 0) s.num_agents = null;
      if (s.num_agents != null) s.num_agents = Number(s.num_agents);
      s.countdown_sec = Number(s.countdown_sec); s.max_steps = Number(s.max_steps);
      st.applyError = "";
      st.pendingApply = true;
      send({ type: "configure", settings: s });
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
      const edges = st.show.edges && v.ai_edges ? v.ai_edges.filter(([i, j]) => i < j).map(([i, j]) => ({ a: v.positions[i], b: v.positions[j] })) : [];
      return { H: v.H, W: v.W, cells: v.map, agents, goals, arrows, boxes, edges };
    });
    const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);

    return { st, r, status, scene, remaining, OPTIONS, STATUS, applySettings, cmd, assign, agentOfPlayer,
             removePlayer, removeOffline, sortedPlayers, offlineCount,
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
          <div class="form" @input="st.dirty = true" @change="st.dirty = true">
            <label>地图</label>
            <select v-model="st.form.preset"><option v-for="p in st.presets" :value="p.name">{{ p.title }}</option></select>
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
            <label>精细模式</label>
            <label><input type="checkbox" v-model="st.form.tta" style="width:auto"> TTA（更准但更慢）</label>
            <button class="btn full" :disabled="status === 'playing' || status === 'paused'" @click="applySettings">
              {{ st.pendingApply ? '正在应用……' : st.dirty ? '应用规则（新开一局）' : '新开一局' }}</button>
            <div v-if="st.applyError" class="full apply-error">没有应用成功：{{ st.applyError }}</div>
            <div v-else-if="status === 'playing' || status === 'paused'" class="full muted" style="font-size:13px">
              比赛进行中不能改规则，先点「结束」</div>
          </div>
        </div>
        <div class="card">
          <h2>同学（{{ st.players.filter(p => p.online).length }} 人在线）</h2>
          <table class="players">
            <tr><th>昵称</th><th>座位</th><th>智能体</th><th></th></tr>
            <tr v-for="p in sortedPlayers" :key="p.id" :class="{ offline: !p.online }">
              <td><span :class="['on', { yes: p.online }]"></span>{{ p.nickname }}<span v-if="!p.online" class="muted">（已离开）</span></td>
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
        <div class="card">
          <div class="toggles">
            <label><input type="checkbox" v-model="st.show.expert"> 专家箭头</label>
            <label><input type="checkbox" v-model="st.show.ai"> AI 箭头</label>
            <label><input type="checkbox" v-model="st.show.boxes"> 学生视野</label>
            <label><input type="checkbox" v-model="st.show.edges"> AI 通信</label>
          </div>
          <GridView v-if="scene" :scene="scene"/>
          <div class="legend">
            <span><i style="background:#0ea08c"></i>专家</span>
            <span><i style="background:#e0a030"></i>AI</span>
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
