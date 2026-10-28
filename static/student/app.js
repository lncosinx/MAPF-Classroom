// 学生端：加入 → 等待老师开始 → 每步选一个方向（倒计时内可以改）→ 终局看结果。
// 服务器是唯一权威：这里只画服务端下发的视野，不做任何本地推演。
import { Connection, store, tabStore, ACTIONS, MESSAGES } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { createApp, reactive, computed, onMounted, onBeforeUnmount } = Vue;

const KEYMAP = { ArrowUp: 1, ArrowDown: 2, ArrowLeft: 3, ArrowRight: 4, " ": 0, w: 1, s: 2, a: 3, d: 4 };
const PAD = [null, 1, null, 3, 0, 4, null, 2, null];   // 3×3 方向键布局，中间是"停"

createApp({
  components: { GridView },
  setup() {
    const st = reactive({
      joined: false, nickname: store.get("mapf.nickname") || "", seat: store.get("mapf.seat") || "",
      player: null, online: false, kicked: false,
      status: "lobby", seated: true, uiMode: "advanced", view: null, myChoice: null,
      progress: { submitted: 0, total: 0 }, countdownMs: null, countdownTotal: 20000, receivedAt: 0,
      now: performance.now(), reveal: null, metrics: null, atGoal: false, maxSteps: 128, toast: "",
      commTarget: null,   // 正在和谁通信（打开了操作卡）
    });
    let toastTimer = null;
    const showToast = (m) => { st.toast = m; clearTimeout(toastTimer); toastTimer = setTimeout(() => (st.toast = ""), 2200); };

    let conn = null;
    function sendJoin() {
      conn.send({ type: "join", nickname: st.nickname, seat: st.seat, token: tabStore.get("mapf.token") || undefined });
    }
    function onMessage(m) {
      if (m.type === "welcome") {
        st.joined = true; st.player = m.player; tabStore.set("mapf.token", m.token);
      } else if (m.type === "state") {
        st.status = m.status; st.uiMode = m.ui_mode || st.uiMode;
        st.seated = m.seated !== false;
        if (m.view) {
          if (!st.view || st.view.t !== m.view.t) { st.myChoice = null; st.commTarget = null; }
          st.view = m.view;
          // 操作卡对象不在范围内了就关掉
          if (st.commTarget != null && !m.view.contacts.some((c) => c.agent === st.commTarget)) st.commTarget = null;
        }
        if ("my_choice" in m) st.myChoice = m.my_choice;
        if (m.progress) st.progress = m.progress;
        st.countdownMs = m.countdown_ms ?? null; st.receivedAt = performance.now();
        if (m.countdown_total_ms) st.countdownTotal = m.countdown_total_ms;
        if (m.max_steps) st.maxSteps = m.max_steps;
        if (m.status === "finished") { st.metrics = m.metrics; st.atGoal = !!m.at_goal; }
        if (m.status === "lobby") { st.metrics = null; st.reveal = null; st.view = null; }
      } else if (m.type === "progress") {
        st.progress = { submitted: m.submitted, total: m.total };
        if (st.view && m.t === st.view.t) st.myChoice = m.my_choice;
      } else if (m.type === "reveal") {
        st.reveal = m;
      } else if (m.type === "kicked") {
        // 被老师移除：token 作废，回到加入页，可以重新加入
        tabStore.set("mapf.token", "");
        st.joined = false; st.player = null; st.view = null; st.metrics = null; st.reveal = null;
        showToast(m.message);
      } else if (m.type === "error") {
        showToast(m.message);
      }
    }
    function connect() {
      conn = new Connection("/ws/student", {
        onMessage, onOpen: sendJoin,
        onStatus: (ok, code) => {
          st.online = ok;
          if (code === 4000) st.kicked = true;
          if (code === 4001) conn = null;   // 被移除：下次点"加入"时重新建连接
        },
      });
    }
    function join() {
      if (!st.nickname.trim()) { showToast("先写上你的昵称"); return; }
      store.set("mapf.nickname", st.nickname.trim()); store.set("mapf.seat", st.seat.trim());
      if (!conn) connect(); else sendJoin();
    }
    // 之前加入过（有 token）就直接重连
    if (tabStore.get("mapf.token")) connect();

    const phase = computed(() => {
      if (!st.joined) return "join";
      if (st.status === "finished") return "ended";
      if ((st.status === "playing" || st.status === "paused") && st.view && st.seated) return "play";
      return "wait";
    });
    const canAct = computed(() => st.status === "playing" && !!st.view);

    function choose(a) {
      if (!canAct.value || !st.view.legal[a]) return;
      if (conn.send({ type: "choose", t: st.view.t, action: a })) st.myChoice = a;
    }
    function onKey(e) {
      if (phase.value !== "play" || e.target.tagName === "INPUT") return;
      const a = KEYMAP[e.key];
      if (a !== undefined) { e.preventDefault(); choose(a); }
    }
    // 倒计时刷新
    let raf = null;
    const tick = () => { st.now = performance.now(); raf = requestAnimationFrame(tick); };
    onMounted(() => { window.addEventListener("keydown", onKey); raf = requestAnimationFrame(tick); });
    onBeforeUnmount(() => { window.removeEventListener("keydown", onKey); cancelAnimationFrame(raf); });

    const remainingMs = computed(() => {
      if (st.countdownMs == null) return null;
      if (st.status !== "playing") return st.countdownMs;
      return Math.max(0, st.countdownMs - (st.now - st.receivedAt));
    });
    const ring = computed(() => {
      const C = 2 * Math.PI * 30;
      const frac = remainingMs.value == null ? 1 : remainingMs.value / st.countdownTotal;
      return { C, offset: C * (1 - Math.max(0, Math.min(1, frac))), low: frac < 0.25 };
    });

    // ---- 学生通信 ----
    const contacts = computed(() => (st.view && st.view.contacts) || []);
    const comm = computed(() => st.view && st.view.comm);
    const contactOf = (j) => contacts.value.find((c) => c.agent === j);
    function openComm(j) { if (contactOf(j)) st.commTarget = st.commTarget === j ? null : j; }
    function requestView(j) { conn.send({ type: "view_request", t: st.view.t, to: j }); }
    function answerView(j, accept) { conn.send({ type: "view_answer", t: st.view.t, from: j, accept }); }
    function sendMsg(j, kind) { if (conn.send({ type: "message", t: st.view.t, to: j, kind })) st.commTarget = null; }
    const budgetDots = computed(() => {
      const c = comm.value;
      if (!c || c.budget_left == null) return null;
      return c.budget_left;
    });
    const sharedWith = computed(() => (st.view ? st.view.shared.map((s) => s.agent) : []));
    const msgLine = (m) => `${MESSAGES[m.kind].icon} ${MESSAGES[m.kind].text}`;

    // 把服务端视野拼成 GridView 的 scene
    const scene = computed(() => {
      const v = st.view;
      if (!v) return null;
      const cells = Array.from({ length: v.H }, () => Array(v.W).fill("?"));
      for (const [r, c, wall] of v.memory) cells[r][c] = wall ? "M" : "m";
      const [r0, c0] = v.box;
      v.rows.forEach((row, i) => { for (let j = 0; j < row.length; j++) cells[r0 + i][c0 + j] = row[j]; });
      // 队友共享来的视野：只填自己看不到的格子
      const others = new Map();
      for (const sh of v.shared) {
        const [sr0, sc0] = sh.box;
        sh.rows.forEach((row, i) => {
          for (let j = 0; j < row.length; j++) {
            const r = sr0 + i, c = sc0 + j;
            if (cells[r][c] === "?" || cells[r][c] === "m" || cells[r][c] === "M") cells[r][c] = row[j] === "#" ? "S" : "s";
          }
        });
        for (const a of sh.agents) others.set(a.agent, a.pos);
      }
      for (const a of v.agents) others.set(a.agent, a.pos);
      const [pr, pc] = v.pos;
      const contactSet = new Set(contacts.value.map((c) => c.agent));
      const agents = [...others].map(([id, p]) => ({ id, r: p[0], c: p[1], color: "var(--other)", label: String(id + 1),
                                                     contact: contactSet.has(id) && st.status === "playing" }));
      agents.push({ id: v.agent, r: pr, c: pc, color: "var(--self)", self: true });
      // 消息气泡：本步收到的、发送者看得见的，每人只显示最新一条
      const latest = new Map();
      for (const m of (v.comm ? v.comm.inbox : [])) latest.set(m.from, m);
      const bubbles = [];
      for (const [from, m] of latest) {
        const p = others.get(from);
        if (p) bubbles.push({ r: p[0], c: p[1], text: MESSAGES[m.kind].icon });
      }
      const circles = v.comm && st.status === "playing" ? [{ r: pr, c: pc, radius: v.comm.radius, color: "var(--contact)" }] : [];
      const sharedBoxes = v.shared.map((s) => ({ box: s.box, color: "var(--contact)" }));
      const goals = [];
      if (v.goal) goals.push({ id: v.agent, r: v.goal[0], c: v.goal[1], color: "var(--self)", self: true });
      else if (v.goal_proj) goals.push({ id: v.agent, r: v.goal_proj[0], c: v.goal_proj[1], color: "var(--self)", self: true, proj: true });
      const arrows = [];
      if (st.myChoice != null && st.myChoice !== 0) arrows.push({ r: pr, c: pc, action: st.myChoice, color: "var(--self)", dashed: true, opacity: 0.55, len: 0.9 });
      const clickCells = [];
      if (st.status === "playing") {
        // 站着可联系队友的格子留给"点队友通信"
        const occ = new Set(agents.filter((a) => a.contact).map((a) => a.r + "," + a.c));
        ACTIONS.forEach((a) => {
          const r = pr + a.dr, c = pc + a.dc;
          if (v.legal[a.id] && !occ.has(r + "," + c)) clickCells.push({ r, c });
        });
      }
      return { H: v.H, W: v.W, cells: cells.map((r) => r.join("")), agents, goals, arrows, bubbles, circles, sharedBoxes,
               box: v.radius == null ? null : v.box, clickable: st.status === "playing", clickCells };
    });
    function onCell(r, c) {
      const [pr, pc] = st.view.pos;
      const a = ACTIONS.find((x) => x.dr === r - pr && x.dc === c - pc);
      if (a) choose(a.id);
    }
    const icon = (a) => (a == null ? "—" : ACTIONS[a].icon);

    return { st, phase, choose, scene, onCell, remainingMs, ring, PAD, ACTIONS, icon, join,
             contacts, comm, openComm, requestView, answerView, sendMsg, budgetDots, sharedWith, msgLine, MESSAGES };
  },
  template: `
  <div :class="['wrap', st.uiMode]">
    <div v-if="st.joined && !st.online && !st.kicked" class="conn-lost">连接断开，正在重连……</div>
    <div v-if="st.kicked" class="conn-lost">你在另一个页面登录了，这个页面已停用</div>

    <div v-if="phase === 'join'" class="center">
      <div class="card join">
        <div class="big">🧭</div>
        <h1>MAPF 课堂</h1>
        <p class="muted">多人一起找路：每人控制地图上的一个小圆点，走到自己的旗子那里</p>
        <input v-model="st.nickname" maxlength="12" placeholder="你的昵称（不要写真名）" @keyup.enter="join">
        <input v-model="st.seat" maxlength="8" placeholder="座位号" @keyup.enter="join">
        <button class="btn" @click="join">加入课堂</button>
      </div>
    </div>

    <div v-else-if="phase === 'wait'" class="center">
      <div class="card join">
        <div class="big">⏳</div>
        <h1>你好，{{ st.player && st.player.nickname }}</h1>
        <p v-if="st.status === 'lobby'" class="muted">等待老师开始……</p>
        <p v-else-if="!st.seated" class="muted">这一局人满了，你先看大屏幕，下一局再上场</p>
        <p v-else class="muted">正在准备地图……</p>
      </div>
    </div>

    <div v-else-if="phase === 'play'">
      <div class="top">
        <div class="me"><span class="dot"></span>{{ st.player.nickname }}</div>
        <span :class="['badge', st.status]">{{ st.status === 'paused' ? '暂停中' : '进行中' }}</span>
        <span v-if="st.uiMode === 'advanced'" class="muted">第 {{ st.view.t + 1 }} 步 / 最多 {{ st.maxSteps }} 步</span>
        <span v-if="st.view.at_goal" class="badge playing">🏁 你在终点上</span>
      </div>
      <div class="play">
        <div class="card map-card">
          <GridView :scene="scene" @cell="onCell" @agent="openComm"/>
          <p v-if="st.view.goal_proj" class="hint muted">灰色空心小旗只是方向标记：你的终点在这个方向、视野外面（标记可能画在墙上或别人身上）</p>
        </div>
        <div class="side">
          <div v-if="st.status === 'paused'" class="paused-banner">老师暂停了，先想一想</div>
          <div class="card timer">
            <svg viewBox="0 0 76 76">
              <circle cx="38" cy="38" r="30" fill="none" stroke="var(--line)" stroke-width="7"/>
              <circle cx="38" cy="38" r="30" fill="none" :stroke="ring.low ? 'var(--danger)' : 'var(--accent)'" stroke-width="7"
                      stroke-linecap="round" :stroke-dasharray="ring.C" :stroke-dashoffset="ring.offset" transform="rotate(-90 38 38)"/>
              <text x="38" y="45" text-anchor="middle" font-size="20" font-weight="700" fill="var(--text)">
                {{ remainingMs == null ? '' : Math.ceil(remainingMs / 1000) }}</text>
            </svg>
            <div>
              <div class="num">{{ st.myChoice == null ? '请选择方向' : '已选 ' + ACTIONS[st.myChoice].icon }}</div>
              <div class="muted">等待队友 {{ st.progress.submitted }}/{{ st.progress.total }}</div>
            </div>
          </div>
          <div class="card">
            <div class="pad">
              <template v-for="(a, i) in PAD" :key="i">
                <button v-if="a !== null" :class="{ sel: st.myChoice === a, stop: a === 0 }"
                        :disabled="st.status !== 'playing' || !st.view.legal[a]" @click="choose(a)"
                        :aria-label="ACTIONS[a].name">{{ a === 0 ? '停' : ACTIONS[a].icon }}</button>
                <span v-else></span>
              </template>
            </div>
            <p class="hint muted" v-if="st.uiMode === 'advanced'">也可以用键盘方向键，空格 = 停；或者直接点旁边的格子</p>
          </div>
          <!-- 收到的交换视野请求 -->
          <div v-for="j in (comm ? comm.requests_in : [])" :key="'rq' + j" class="card request">
            <div>👀 <b>{{ j + 1 }} 号</b>想和你交换视野</div>
            <div class="row">
              <button class="btn ok" @click="answerView(j, true)">同意</button>
              <button class="btn ghost" @click="answerView(j, false)">拒绝</button>
            </div>
          </div>
          <!-- 通信 -->
          <div v-if="comm" class="card comm">
            <div class="comm-head">
              <b>通信</b>
              <span v-if="budgetDots != null" class="dots" :title="'这一步还能通信 ' + budgetDots + ' 次'">
                <i v-for="k in budgetDots" :key="k"></i><span v-if="budgetDots === 0" class="muted">本步次数用完</span>
              </span>
            </div>
            <div v-if="!contacts.length" class="muted small">通信范围内没有队友</div>
            <div class="chips">
              <button v-for="c in contacts" :key="c.agent" :class="['chip', { on: st.commTarget === c.agent }]"
                      :disabled="st.status !== 'playing'" @click="openComm(c.agent)">
                {{ c.agent + 1 }} 号
                <span v-if="!c.visible" class="tag">看不见</span>
                <span v-if="c.ai_controlled" class="tag">AI</span>
                <span v-if="sharedWith.includes(c.agent)" class="tag ok">已共享</span>
                <span v-else-if="comm.requests_out.includes(c.agent)" class="tag">等同意</span>
                <span v-else-if="comm.rejected_by.includes(c.agent)" class="tag no">拒绝了</span>
              </button>
            </div>
            <div v-if="contacts.some(c => !c.visible) && st.uiMode === 'advanced'" class="muted small">
              看不见的同伴也能联系</div>
            <div v-if="st.commTarget != null" class="panel">
              <div class="small"><b>和 {{ st.commTarget + 1 }} 号</b>
                <span v-if="(contacts.find(c => c.agent === st.commTarget) || {}).ai_controlled" class="muted">
                  （AI 队友：会和你交换视野，但不读消息）</span></div>
              <button v-if="comm.view_exchange" class="btn full"
                      :disabled="sharedWith.includes(st.commTarget) || comm.requests_out.includes(st.commTarget) || budgetDots === 0"
                      @click="requestView(st.commTarget)">👀 交换视野</button>
              <div class="msgs">
                <button v-for="k in comm.kinds" :key="k" class="msg" :disabled="budgetDots === 0"
                        :title="MESSAGES[k].text" @click="sendMsg(st.commTarget, k)">
                  <span class="ic">{{ MESSAGES[k].icon }}</span>
                  <span v-if="st.uiMode === 'advanced'">{{ MESSAGES[k].text }}</span>
                </button>
              </div>
            </div>
            <div v-if="comm.inbox.length" class="log">
              <div v-for="(m, i) in comm.inbox" :key="'in' + i">
                <b>{{ m.from + 1 }} 号</b>：{{ st.uiMode === 'advanced' ? msgLine(m) : MESSAGES[m.kind].icon }}
                <span v-if="m.t !== st.view.t" class="muted small">（上一步发的）</span>
              </div>
            </div>
          </div>
          <div v-if="st.reveal" class="card">
            <div class="muted" style="margin-bottom:6px">第 {{ st.reveal.t + 1 }} 步对比</div>
            <div class="reveal">
              <div class="col"><div class="lbl">你{{ st.reveal.timed_out ? '（超时）' : '' }}</div><div class="act">{{ icon(st.reveal.you) }}</div></div>
              <div class="col"><div class="lbl">专家</div><div :class="['act', { same: st.reveal.expert === st.reveal.you }]">{{ icon(st.reveal.expert) }}</div></div>
              <div class="col"><div class="lbl">AI</div><div :class="['act', { same: st.reveal.ai === st.reveal.you }]">{{ icon(st.reveal.ai) }}</div></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-else-if="phase === 'ended'" class="center">
      <div class="card join">
        <div class="big">{{ st.atGoal ? '🎉' : '🏁' }}</div>
        <h1>{{ st.atGoal ? '你到达了终点！' : '本局结束' }}</h1>
        <p class="muted">{{ st.metrics && st.metrics.CSR === 1 ? '全队都到了，太棒了！' : '还有队友没到终点，下次再努力' }}</p>
        <div v-if="st.metrics" class="metrics">
          <div><b>{{ Math.round(st.metrics.ISR * 100) }}%</b>到达比例</div>
          <div><b>{{ st.metrics.makespan }}</b>全队用时（步）</div>
          <div><b>{{ st.metrics.SoC }}</b>总步数</div>
          <div><b>{{ st.metrics.CSR === 1 ? '是' : '否' }}</b>全员到达</div>
        </div>
      </div>
    </div>

    <div v-if="st.toast" class="toast">{{ st.toast }}</div>
  </div>`,
}).mount("#app");
