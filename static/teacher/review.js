// 复盘：选课（本节课或以前的课）→ 选局 → 拖进度条 / 自动播放；每一步"学生 / 专家 / AI"三列对比、
// 教学指标汇总、本步通信；导出 CSV / JSONL / SVG。数据都来自课堂记录（服务端只读打开）。
import { ACTIONS, MESSAGES, agentColor } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { reactive, computed, onMounted, onBeforeUnmount, watch } = Vue;
const CTRL = { student: "学生", model: "AI", expert: "专家", idle: "不动" };
const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);

export const ReviewPanel = {
  name: "ReviewPanel",
  components: { GridView },
  props: { rpc: { type: Function, required: true }, keyParam: { type: String, default: "" } },
  emits: ["toast"],
  setup(props, { emit }) {
    const rv = reactive({ sessions: [], session: "", rounds: [], roundId: null, data: null, t: 0, playing: false,
                          sel: null, show: { student: true, expert: true, ai: false, comm: true }, loading: false });
    const toast = (m) => emit("toast", m);

    async function loadSessions() {
      try {
        rv.sessions = (await props.rpc("review.sessions", {})).sessions;
        const prev = rv.session;
        if (!rv.sessions.some((s) => s.name === rv.session)) {
          rv.session = (rv.sessions.find((s) => s.current) || rv.sessions[0] || {}).name || "";
        }
        // 选中的课变了由 watch 去加载；没变（例如点"刷新"）就在这里加载
        if (rv.session === prev) await loadRounds();
      } catch (e) { toast(e.message); }
    }
    async function loadRounds() {
      if (!rv.session) { rv.rounds = []; return; }
      try {
        rv.rounds = (await props.rpc("review.rounds", { session: rv.session })).rounds;
        const prev = rv.roundId;
        if (!rv.rounds.some((r) => r.id === rv.roundId)) rv.roundId = rv.rounds.length ? rv.rounds[0].id : null;
        if (rv.roundId === prev) await loadRound();
      } catch (e) { toast(e.message); }
    }
    async function loadRound() {
      stop();
      rv.data = null;
      if (rv.roundId == null) return;
      rv.loading = true;
      try {
        rv.data = await props.rpc("review.load", { session: rv.session, round: rv.roundId });
        rv.t = 0; rv.sel = null;
      } catch (e) { toast(e.message); } finally { rv.loading = false; }
    }
    onMounted(loadSessions);

    // ---- 播放
    let timer = null;
    const T = computed(() => (rv.data ? rv.data.frames.length - 1 : 0));
    function stop() { rv.playing = false; clearInterval(timer); timer = null; }
    function play() {
      if (rv.t >= T.value) rv.t = 0;
      rv.playing = true;
      // 每步 0.5 s：前 250 ms 是智能体移动，之后箭头淡入，留够时间看清
      timer = setInterval(() => { if (rv.t < T.value) rv.t++; else stop(); }, 500);
    }
    const step = (d) => { stop(); rv.t = Math.max(0, Math.min(T.value, rv.t + d)); };
    onBeforeUnmount(stop);
    watch(() => rv.session, loadRounds);
    watch(() => rv.roundId, loadRound);

    // ---- 画面
    const stepRows = computed(() => (rv.data && rv.t < rv.data.steps.length ? rv.data.steps[rv.t] : []));
    const scene = computed(() => {
      const d = rv.data;
      if (!d) return null;
      const pos = d.frames[rv.t];
      const agents = pos.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1),
                                         self: rv.sel === i }));
      const goals = d.goals.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) }));
      const arrows = [];
      for (const s of stepRows.value) {
        if (!s || (rv.sel != null && s.agent !== rv.sel)) continue;
        const [r, c] = s.pos_before;
        if (rv.show.student) arrows.push({ r, c, action: s.action, color: agentColor(s.agent) });
        if (rv.show.expert && s.expert != null) arrows.push({ r, c, action: s.expert, color: "#0ea08c", len: 0.3 });
        if (rv.show.ai) arrows.push({ r, c, action: s.ai, color: "#e0a030", dashed: true, len: 0.36 });
      }
      const links = [];
      if (rv.show.comm) {
        for (const e of d.comm) {
          if (e.t !== rv.t || e.to == null) continue;
          if (e.type === "msg") links.push({ a: pos[e.from], b: pos[e.to], color: "#e0a030" });
          else if (e.type === "view_accept") links.push({ a: pos[e.from], b: pos[e.to], color: "#00a3b4", dashed: true });
        }
      }
      return { H: d.map.length, W: d.map[0].length, cells: d.map, agents, goals, arrows, links, stepKey: rv.t };
    });
    const commNow = computed(() => (rv.data ? rv.data.comm.filter((e) => e.t === rv.t) : []));
    function commText(e) {
      if (e.type === "msg") return `${e.from + 1} 号 → ${e.to + 1} 号：${MESSAGES[e.payload.kind].icon} ${MESSAGES[e.payload.kind].text}`;
      if (e.type === "view_request") return `${e.from + 1} 号请求和 ${e.to + 1} 号交换视野`;
      if (e.type === "view_accept") return `${e.from + 1} 号同意了 ${e.to + 1} 号`;
      if (e.type === "view_reject") return `${e.from + 1} 号拒绝了 ${e.to + 1} 号`;
      return e.type;
    }

    // ---- 导出（数据含学生作答，下载前提醒）
    function download(target) {
      if (!confirm("导出的文件含学生的作答记录（只有昵称和座位号）。请妥善保管，不要随意转发。继续下载？")) return;
      const q = props.keyParam ? `?key=${encodeURIComponent(props.keyParam)}` : "";
      const a = document.createElement("a");
      a.href = `/api/export/${encodeURIComponent(rv.session)}/${target}${q}`;
      a.click();
    }
    const roundLabel = (r) => `${r.group} · ${r.title} · ${r.N} 人 · ${r.T} 步` + (r.metrics ? (r.metrics.CSR === 1 ? " · 全员到达" : ` · 到达 ${Math.round(r.metrics.ISR * 100)}%`) : "");

    return { rv, T, play, stop, step, scene, stepRows, commNow, commText, download, roundLabel, loadSessions,
             icon, pct, CTRL, agentColor };
  },
  template: `
  <div class="review">
    <div class="ws-row">
      <b>课</b>
      <select v-model="rv.session">
        <option v-for="s in rv.sessions" :value="s.name">{{ s.label }}{{ s.current ? '（本节课）' : '' }} · {{ s.rounds }} 局</option>
      </select>
      <b>局</b>
      <select v-model="rv.roundId" :disabled="!rv.rounds.length">
        <option v-for="r in rv.rounds" :value="r.id">{{ roundLabel(r) }}</option>
      </select>
      <button class="btn ghost" @click="loadSessions">刷新</button>
    </div>
    <p v-if="!rv.rounds.length" class="muted small">这节课还没有结束的局。一局结束后就能在这里复盘。</p>
    <template v-if="rv.data">
      <div class="ws-row">
        <label><input type="checkbox" v-model="rv.show.student"> 实际走法</label>
        <label><input type="checkbox" v-model="rv.show.expert"> 专家</label>
        <label><input type="checkbox" v-model="rv.show.ai"> AI</label>
        <label><input type="checkbox" v-model="rv.show.comm"> 通信</label>
        <span class="sep"></span>
        <span class="muted small">点表格里的一行只看那个智能体</span>
        <button v-if="rv.sel != null" class="btn ghost small" @click="rv.sel = null">看全部</button>
      </div>
      <GridView :scene="scene" :style="{ width: 'min(100%, ' + Math.floor(520 * scene.W / scene.H) + 'px)', margin: '0 auto' }"/>
      <div class="player">
        <button class="zb" @click="step(-1)" title="上一步">◀</button>
        <button class="zb wide" @click="rv.playing ? stop() : play()">{{ rv.playing ? '⏸ 暂停' : '▶ 播放' }}</button>
        <button class="zb" @click="step(1)" title="下一步">▶</button>
        <input type="range" min="0" :max="T" v-model.number="rv.t" @input="stop()">
        <b>{{ rv.t < T ? '第 ' + (rv.t + 1) + ' 步' : '终局' }} / {{ T }}</b>
      </div>
      <table v-if="stepRows.length" class="rtable">
        <tr><th>智能体</th><th>谁在控制</th><th>实际</th><th>专家</th><th>AI</th><th>AI 的把握</th><th>思考</th></tr>
        <tr v-for="s in stepRows" :key="s.agent" :class="{ picked: rv.sel === s.agent }" @click="rv.sel = rv.sel === s.agent ? null : s.agent">
          <td><span class="sw" :style="{ background: agentColor(s.agent) }"></span>{{ s.agent + 1 }} 号</td>
          <td>{{ s.controller === 'student' ? (s.nickname || '学生') : CTRL[s.controller] }}</td>
          <td class="act">{{ icon(s.action) }}<small v-if="s.timed_out" class="muted">超时</small></td>
          <td :class="['act', { same: s.expert === s.action }]">{{ icon(s.expert) }}</td>
          <td :class="['act', { same: s.ai === s.action }]">{{ icon(s.ai) }}</td>
          <td><span class="probs"><i v-for="(p, k) in s.ai_probs" :key="k" :style="{ height: Math.round(p * 100) + '%' }" :title="'停上下左右'[k] + ' ' + Math.round(p * 100) + '%'"></i></span></td>
          <td>{{ s.think_ms == null ? '—' : (s.think_ms / 1000).toFixed(1) + ' 秒' }}</td>
        </tr>
      </table>
      <div v-if="commNow.length" class="commlog small"><div v-for="(e, i) in commNow" :key="i">💬 {{ commText(e) }}</div></div>
      <h3>教学指标（只统计学生控制的步）</h3>
      <table v-if="rv.data.teaching.length" class="rtable">
        <tr><th>智能体</th><th>同学</th><th>步数</th><th>和专家一致</th><th>和 AI 一致</th><th>平均思考</th><th>等待</th><th>被挡</th><th>超时</th><th>通信</th></tr>
        <tr v-for="m in rv.data.teaching" :key="m.agent">
          <td><span class="sw" :style="{ background: agentColor(m.agent) }"></span>{{ m.agent + 1 }} 号</td>
          <td>{{ m.nickname }}</td><td>{{ m.steps }}</td><td>{{ pct(m.expert_rate) }}</td><td>{{ pct(m.ai_rate) }}</td>
          <td>{{ m.avg_think_ms == null ? '—' : (m.avg_think_ms / 1000).toFixed(1) + ' 秒' }}</td>
          <td>{{ m.waits }}</td><td>{{ m.blocked }}</td><td>{{ m.timeouts }}</td><td>{{ m.comm }}</td>
        </tr>
      </table>
      <p v-else class="muted small">这一局没有学生参与（全部由 AI / 专家补位）。</p>
      <div class="ws-row">
        <b>导出本局</b>
        <button class="btn ghost" @click="download(rv.roundId + '.csv')">CSV（Excel）</button>
        <button class="btn ghost" @click="download(rv.roundId + '.jsonl')">JSONL（研究用）</button>
        <button class="btn ghost" @click="download(rv.roundId + '.svg')">SVG 动画</button>
        <span class="sep"></span>
        <b>整节课</b>
        <button class="btn ghost" @click="download('all.csv')">CSV</button>
        <button class="btn ghost" @click="download('all.jsonl')">JSONL</button>
      </div>
    </template>
    <p v-else-if="rv.loading" class="muted">正在读取……</p>
  </div>`,
};
