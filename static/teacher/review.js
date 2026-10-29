// 复盘：选课（本节课或以前的课）→ 选局 → 拖进度条 / 自动播放；每一步"学生 / 专家 / AI"三列对比、
// 教学指标汇总、本步通信；导出 CSV / JSONL / SVG。数据都来自课堂记录（服务端只读打开）。
// 复盘有自己的布局：左栏 ReviewSide（本步每个智能体 / 本步通信 / 教学指标 / 导出），中间 ReviewMain（选课选局、地图、播放条）。
// 两边共用 createReview() 建的同一份状态，整页只建一次；每次切到复盘标签由 enter() 重新读课堂记录。
import { ACTIONS, MESSAGES, agentColor } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { reactive, computed, ref, nextTick, watch, onMounted, onBeforeUnmount } = Vue;
const CTRL = { student: "学生", model: "AI", expert: "专家", idle: "不动" };
const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);

export function createReview({ rpc, toast, keyParam }) {
  const rv = reactive({ sessions: [], session: "", rounds: [], roundId: null, data: null, t: 0, playing: false,
                        sel: null, show: { student: true, expert: true, ai: false, comm: true }, loading: false });

  async function loadSessions() {
    try {
      rv.sessions = (await rpc("review.sessions", {})).sessions;
      const prev = rv.session;
      if (!rv.sessions.some((s) => s.name === rv.session)) {
        rv.session = (rv.sessions.find((s) => s.current) || rv.sessions[0] || {}).name || "";
      }
      // 选中的课变了由 watch 去加载；没变（例如点"刷新"、重新切到复盘）就在这里加载
      if (rv.session === prev) await loadRounds();
    } catch (e) { toast(e.message); }
  }
  async function loadRounds() {
    if (!rv.session) { rv.rounds = []; return; }
    try {
      rv.rounds = (await rpc("review.rounds", { session: rv.session })).rounds;
      const prev = rv.roundId;
      if (!rv.rounds.some((r) => r.id === rv.roundId)) rv.roundId = rv.rounds.length ? rv.rounds[0].id : null;
      // 同一局已经读过就不重读，保留进度条位置
      if (rv.roundId === prev && !rv.data) await loadRound();
    } catch (e) { toast(e.message); }
  }
  async function loadRound() {
    stop();
    rv.data = null;
    if (rv.roundId == null) return;
    rv.loading = true;
    try {
      rv.data = await rpc("review.load", { session: rv.session, round: rv.roundId });
      rv.t = 0; rv.sel = null;
    } catch (e) { toast(e.message); } finally { rv.loading = false; }
  }
  // 切到复盘标签时调用：读到最新的课堂记录；离开时停止播放
  const enter = () => loadSessions();
  const leave = () => stop();
  function refresh() { rv.data = null; return loadSessions(); }

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
  const pick = (a) => { rv.sel = rv.sel === a ? null : a; };

  // ---- 导出（数据含学生作答，下载前提醒）
  function download(target) {
    if (!confirm("导出的文件含学生的作答记录（只有昵称和座位号）。请妥善保管，不要随意转发。继续下载？")) return;
    const q = keyParam ? `?key=${encodeURIComponent(keyParam)}` : "";
    const a = document.createElement("a");
    a.href = `/api/export/${encodeURIComponent(rv.session)}/${target}${q}`;
    a.click();
  }
  const roundLabel = (r) => `${r.group} · ${r.title} · ${r.N} 人 · ${r.T} 步` + (r.metrics ? (r.metrics.CSR === 1 ? " · 全员到达" : ` · 到达 ${Math.round(r.metrics.ISR * 100)}%`) : "");

  return { rv, T, play, stop, step, scene, stepRows, commNow, commText, pick, download, roundLabel, refresh,
           enter, leave, icon, pct, CTRL, agentColor };
}

/** 左栏：本步每个智能体的三列对比、本步通信、教学指标、导出。 */
export const ReviewSide = {
  name: "ReviewSide",
  props: { v: { type: Object, required: true } },
  setup(props) { return { ...props.v }; },
  template: `
  <div class="rv-side">
    <div class="card">
      <h2>{{ rv.data ? (rv.t < T ? '第 ' + (rv.t + 1) + ' 步' : '终局') + ' · 每个智能体' : '每个智能体' }}</h2>
      <p v-if="!rv.data" class="muted small">{{ rv.loading ? '正在读取……' : '先在中间选一局。' }}</p>
      <template v-else>
        <div class="rtable-wrap rv-scroll-steps">
          <table v-if="stepRows.length" class="rtable">
            <tr><th>智能体</th><th>谁在控制</th><th>实际</th><th>专家</th><th>AI</th><th>AI 的把握</th><th>思考</th></tr>
            <tr v-for="s in stepRows" :key="s.agent" :class="{ picked: rv.sel === s.agent }" @click="pick(s.agent)">
              <td><span class="sw" :style="{ background: agentColor(s.agent) }"></span>{{ s.agent + 1 }} 号</td>
              <td>{{ s.controller === 'student' ? (s.nickname || '学生') : CTRL[s.controller] }}</td>
              <td class="act">{{ icon(s.action) }}<small v-if="s.timed_out" class="muted">超时</small></td>
              <td :class="['act', { same: s.expert === s.action }]">{{ icon(s.expert) }}</td>
              <td :class="['act', { same: s.ai === s.action }]">{{ icon(s.ai) }}</td>
              <td><span class="probs"><i v-for="(p, k) in s.ai_probs" :key="k" :style="{ height: Math.round(p * 100) + '%' }" :title="'停上下左右'[k] + ' ' + Math.round(p * 100) + '%'"></i></span></td>
              <td>{{ s.think_ms == null ? '—' : (s.think_ms / 1000).toFixed(1) + ' 秒' }}</td>
            </tr>
          </table>
          <p v-else class="muted small">终局：拖回前面某一步看每个智能体的选择。</p>
        </div>
        <p class="muted small" style="margin:6px 0 0">点一行只看那个智能体<button v-if="rv.sel != null" class="btn ghost small" style="margin-left:8px" @click="rv.sel = null">看全部</button></p>
      </template>
    </div>
    <div v-if="rv.data && commNow.length" class="card">
      <h2>本步通信（{{ commNow.length }}）</h2>
      <div class="commlog small"><div v-for="(e, i) in commNow" :key="i">💬 {{ commText(e) }}</div></div>
    </div>
    <div v-if="rv.data" class="card">
      <h2>教学指标 <span class="muted small">只统计学生控制的步</span></h2>
      <div v-if="rv.data.teaching.length" class="rtable-wrap rv-scroll-teach">
        <table class="rtable">
          <tr><th>智能体</th><th>同学</th><th>步数</th><th>和专家一致</th><th>和 AI 一致</th><th>平均思考</th><th>等待</th><th>被挡</th><th>超时</th><th>通信</th></tr>
          <tr v-for="m in rv.data.teaching" :key="m.agent" :class="{ picked: rv.sel === m.agent }" @click="pick(m.agent)">
            <td><span class="sw" :style="{ background: agentColor(m.agent) }"></span>{{ m.agent + 1 }} 号</td>
            <td>{{ m.nickname }}</td><td>{{ m.steps }}</td><td>{{ pct(m.expert_rate) }}</td><td>{{ pct(m.ai_rate) }}</td>
            <td>{{ m.avg_think_ms == null ? '—' : (m.avg_think_ms / 1000).toFixed(1) + ' 秒' }}</td>
            <td>{{ m.waits }}</td><td>{{ m.blocked }}</td><td>{{ m.timeouts }}</td><td>{{ m.comm }}</td>
          </tr>
        </table>
      </div>
      <p v-else class="muted small">这一局没有学生参与（全部由 AI / 专家补位）。</p>
    </div>
    <div v-if="rv.session" class="card">
      <h2>导出</h2>
      <div class="ws-btns" v-if="rv.data">
        <b class="small">本局</b>
        <button class="btn ghost" @click="download(rv.roundId + '.csv')">CSV（Excel）</button>
        <button class="btn ghost" @click="download(rv.roundId + '.jsonl')">JSONL（研究用）</button>
        <button class="btn ghost" @click="download(rv.roundId + '.svg')">SVG 动画</button>
      </div>
      <div class="ws-btns" style="margin-top:8px">
        <b class="small">整节课</b>
        <button class="btn ghost" @click="download('all.csv')">CSV</button>
        <button class="btn ghost" @click="download('all.jsonl')">JSONL</button>
      </div>
    </div>
  </div>`,
};

/** 中间：选课选局、显示开关、地图（按可用宽高放大）、播放条。 */
export const ReviewMain = {
  name: "ReviewMain",
  components: { GridView },
  props: { v: { type: Object, required: true }, active: { type: Boolean, default: false } },
  setup(props) {
    const v = props.v;
    const box = ref(null);
    const fitH = ref(520);
    const BOTTOM = 90;      // 地图下面的播放条 + 卡片底边距
    function measure() {
      const el = box.value;
      if (!el) return;
      fitH.value = Math.max(260, window.innerHeight - (el.getBoundingClientRect().top + window.scrollY) - BOTTOM);
    }
    onMounted(() => { window.addEventListener("resize", measure); nextTick(measure); });
    onBeforeUnmount(() => { window.removeEventListener("resize", measure); v.stop(); });
    watch(() => props.active, (a) => { if (a) nextTick(measure); });
    watch(() => v.rv.data, () => nextTick(measure));
    const mapStyle = computed(() => {
      const s = v.scene.value;
      return s ? { width: `min(100%, ${Math.floor(fitH.value * s.W / s.H)}px)`, margin: "0 auto" } : {};
    });
    return { ...v, box, mapStyle };
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
      <button class="btn ghost" @click="refresh">刷新</button>
    </div>
    <p v-if="!rv.rounds.length" class="muted small">这节课还没有结束的局。一局结束后就能在这里复盘。</p>
    <template v-if="rv.data">
      <div class="ws-row">
        <label><input type="checkbox" v-model="rv.show.student"> 实际走法</label>
        <label><input type="checkbox" v-model="rv.show.expert"> 专家</label>
        <label><input type="checkbox" v-model="rv.show.ai"> AI</label>
        <label><input type="checkbox" v-model="rv.show.comm"> 通信</label>
        <span v-if="rv.sel != null" class="muted small">只看 {{ rv.sel + 1 }} 号
          <button class="btn ghost small" @click="rv.sel = null">看全部</button></span>
      </div>
      <div ref="box"><GridView :scene="scene" :style="mapStyle"/></div>
      <div class="player">
        <button class="zb" @click="step(-1)" title="上一步">◀</button>
        <button class="zb wide" @click="rv.playing ? stop() : play()">{{ rv.playing ? '⏸ 暂停' : '▶ 播放' }}</button>
        <button class="zb" @click="step(1)" title="下一步">▶</button>
        <input type="range" min="0" :max="T" v-model.number="rv.t" @input="stop()">
        <b>{{ rv.t < T ? '第 ' + (rv.t + 1) + ' 步' : '终局' }} / {{ T }}</b>
      </div>
    </template>
    <p v-else-if="rv.loading" class="muted">正在读取……</p>
  </div>`,
};
