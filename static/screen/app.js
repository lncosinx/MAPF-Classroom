// 投屏页：大字号排行榜 + 各组卡片。只显示服务端下发的内容：
// 比赛中（老师没打开"显示全图"）服务端根本不发地图，这里只画进度；某组结束后才画它的全图。
import { Connection, agentColor } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { createApp, reactive, computed } = Vue;
const STATUS = { lobby: "未开始", playing: "进行中", paused: "暂停", finished: "已结束" };

createApp({
  components: { GridView },
  setup() {
    const st = reactive({ data: null, online: false, joinUrl: "" });
    new Connection("/ws/screen", {
      onMessage: (m) => { if (m.type === "screen") { st.data = m; if (m.join_url) st.joinUrl = m.join_url; } },
      onStatus: (ok) => { st.online = ok; },
    });
    fetch("/api/join-info").then((r) => r.json()).then((j) => (st.joinUrl = j.url)).catch(() => {});

    const simple = computed(() => st.data && st.data.ui_mode === "simple");
    const groupOf = (g) => st.data.groups.find((x) => x.group === g);
    // 简洁模式：按到达比例给星星
    const stars = (m) => (!m ? "" : "★".repeat(m.CSR === 1 ? 3 : m.ISR >= 0.66 ? 2 : m.ISR > 0 ? 1 : 0).padEnd(3, "☆"));
    function scene(g) {
      const v = g.map;
      return { H: v.H, W: v.W, cells: v.map,
               agents: v.positions.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) })),
               goals: v.targets.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) })) };
    }
    const pct = (e) => (e.N ? Math.round(100 * (e.at_goal || 0) / e.N) : 0);
    return { st, STATUS, simple, groupOf, stars, scene, pct };
  },
  template: `
  <div class="wrap">
    <div v-if="!st.online" class="conn-lost">连接断开，正在重连……</div>
    <div class="head">
      <h1>🏆 排行榜</h1>
      <span v-if="st.data" class="sub">{{ st.data.title }} · {{ STATUS[st.data.status] }}</span>
      <div class="join"><span>加入：<b>{{ st.joinUrl }}</b></span><img src="/api/join-qr.svg" alt="加入二维码"></div>
    </div>
    <template v-if="st.data">
      <div class="board">
        <div v-for="(e, i) in st.data.leaderboard" :key="i"
             :class="['row', { ref: e.kind !== 'group', first: e.rank === 1 && e.status === 'finished' }]">
          <span class="rank">{{ e.kind === 'group' ? (e.rank === 1 && e.status === 'finished' ? '🏆' : e.rank) : '' }}</span>
          <span class="name">{{ e.name }}</span>
          <span v-if="e.kind === 'group'">
            <div class="bar"><i :style="{ width: pct(e) + '%' }"></i></div>
          </span>
          <span v-else class="num">{{ e.status === 'computing' ? '计算中…' : '参照' }}</span>
          <span class="num">
            <template v-if="e.kind === 'group'">到达 <b>{{ e.at_goal }}/{{ e.N }}</b> · 第 {{ e.t }} 步</template>
            <template v-if="e.metrics && (e.kind !== 'group' || e.status === 'finished')">
              <span v-if="simple" class="stars">{{ stars(e.metrics) }}</span>
              <span v-else> · 用时 <b>{{ e.metrics.makespan }}</b> · 总步数 <b>{{ e.metrics.SoC }}</b></span>
            </template>
          </span>
        </div>
      </div>
      <div class="groups">
        <div v-for="g in st.data.groups" :key="g.group" class="gcard">
          <h3>{{ g.name }} <span :class="['badge', g.status]">{{ STATUS[g.status] }}</span></h3>
          <GridView v-if="g.map" :scene="scene(g)"/>
          <div v-else class="hidden-map">比赛中，地图结束后公布<br>到达 {{ g.at_goal }}/{{ g.N }} · 第 {{ g.t }} 步</div>
        </div>
      </div>
    </template>
  </div>`,
}).mount("#app");
