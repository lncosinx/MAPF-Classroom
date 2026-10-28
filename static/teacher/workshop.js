// 地图工坊：生成器 + 编辑器 + 地图库（载入 / 保存 / 删除 / 导入 / 导出）+ 实时校验。
// 编辑的始终是一份 MapSpec：{title, map: [行], agents_xy: [[r,c]], targets_xy: [[r,c]]}（unpadded）。
// 生成、随机放置、校验、导入导出都走服务端 RPC，前端不重复实现，保证和开局时的口径一致。
//
// 布局：切到地图工坊时，左边是工具卡片（WorkshopTools），中间是放大的画布（WorkshopCanvas）。
// 两个组件共用 createWorkshop() 建的同一份编辑状态，由老师页只建一次。
import { agentColor, cellFromEvent } from "/static/shared/ws.js";
import { GridView } from "/static/shared/grid-renderer.js";

const { reactive, computed, watch, ref, onMounted, onBeforeUnmount, nextTick } = Vue;

const KINDS = [["random", "随机障碍"], ["maze", "迷宫"], ["warehouse", "仓库货架"], ["house", "房间"]];
// 橡皮兼管删除：擦到某一对的起点或终点就删掉这一对（用户建议，不再单独放"删除一对"按钮）
const TOOLS = [["wall", "🧱 画墙"], ["erase", "🧽 橡皮"], ["pair", "🚩 放起点和终点"]];

const blankMap = (H, W) => Array.from({ length: H }, () => ".".repeat(W));

/** 编辑状态和操作。rpc：老师 RPC；toast：提示；getMaps：当前地图库列表；onUse：用这张图开新局。 */
export function createWorkshop({ rpc, toast, getMaps, onUse }) {
  const ws = reactive({
    spec: { title: "我的地图", map: blankMap(10, 12), agents_xy: [], targets_xy: [] },
    tool: "wall",
    pending: null,                // 放起点终点时：已点了起点、等终点
    gen: { kind: "random", width: 12, height: 10, density: 0.2, seed: 0 },
    size: { H: 10, W: 12 },
    randomN: 6,
    check: { errors: [], ai_hint: null, checking: false },
    loadName: "",
    importText: "",
    showImport: false,
    busy: false,
  });
  const dom = { canvas: null };   // 画布 DOM（不放进 reactive）
  const H = computed(() => ws.spec.map.length);
  const W = computed(() => (ws.spec.map[0] || "").length);

  // ---------------- 校验：编辑停顿 300 ms 后请求服务端
  let timer = null;
  watch(() => JSON.stringify(ws.spec), () => {
    clearTimeout(timer);
    ws.check.checking = true;
    timer = setTimeout(async () => {
      try {
        const r = await rpc("map.validate", { spec: ws.spec });
        ws.check = { ...r, checking: false };
      } catch (e) { ws.check = { errors: [String(e.message || e)], ai_hint: null, checking: false }; }
    }, 300);
  }, { immediate: true });
  const usable = computed(() => !ws.check.checking && ws.check.errors.length === 0 && ws.spec.agents_xy.length > 0);

  // ---------------- 编辑
  const same = (a, b) => a && b && a[0] === b[0] && a[1] === b[1];
  const pairAt = (rc) => {
    const i = ws.spec.agents_xy.findIndex((p) => same(p, rc));
    if (i >= 0) return { i, kind: "start" };
    const j = ws.spec.targets_xy.findIndex((p) => same(p, rc));
    return j >= 0 ? { i: j, kind: "goal" } : null;
  };
  function setCell(r, c, ch) {
    const row = ws.spec.map[r];
    if (row[c] === ch) return;
    ws.spec.map.splice(r, 1, row.slice(0, c) + ch + row.slice(c + 1));
  }
  function applyTool(rc, first) {
    const [r, c] = rc;
    if (ws.tool === "wall") {
      if (!pairAt(rc) && !same(ws.pending, rc)) setCell(r, c, "#");
    } else if (ws.tool === "erase") {
      const hit = pairAt(rc);
      if (hit) { ws.spec.agents_xy.splice(hit.i, 1); ws.spec.targets_xy.splice(hit.i, 1); }
      setCell(r, c, ".");
    } else if (first && ws.tool === "pair") {
      if (ws.spec.map[r][c] === "#") { toast("起点和终点不能放在墙上"); return; }
      if (!ws.pending) {
        if (ws.spec.agents_xy.some((p) => same(p, rc))) { toast("这里已经是别人的起点了"); return; }
        if (ws.spec.agents_xy.length >= 64) { toast("最多 64 对"); return; }
        ws.pending = rc;
      } else if (same(ws.pending, rc)) {
        ws.pending = null;                       // 再点一次起点 = 取消
      } else {
        if (ws.spec.targets_xy.some((p) => same(p, rc))) { toast("这里已经是别人的终点了"); return; }
        ws.spec.agents_xy.push(ws.pending); ws.spec.targets_xy.push(rc); ws.pending = null;
      }
    }
  }
  let painting = false;
  function onDown(ev) {
    const rc = cellFromEvent(ev, dom.canvas, H.value, W.value);
    if (!rc) return;
    painting = ws.tool === "wall" || ws.tool === "erase";
    if (painting && ev.target.setPointerCapture) { try { ev.target.setPointerCapture(ev.pointerId); } catch (e) { /* 忽略 */ } }
    applyTool(rc, true);
  }
  function onMove(ev) {
    if (!painting) return;
    const rc = cellFromEvent(ev, dom.canvas, H.value, W.value);
    if (rc) applyTool(rc, false);
  }
  const onUp = () => { painting = false; };

  // ---------------- 生成 / 随机放置 / 尺寸
  async function generate(nextSeed) {
    if (nextSeed) ws.gen.seed = Number(ws.gen.seed) + 1;
    try {
      ws.busy = true;
      const g = { ...ws.gen, width: Number(ws.gen.width), height: Number(ws.gen.height),
                  density: Number(ws.gen.density), seed: Number(ws.gen.seed) };
      const r = await rpc("map.generate", g);
      ws.spec.map = r.map; ws.spec.agents_xy = []; ws.spec.targets_xy = []; ws.pending = null;
      ws.size = { H: r.map.length, W: r.map[0].length };
    } catch (e) { toast(e.message); } finally { ws.busy = false; }
  }
  async function placeRandom() {
    try {
      const r = await rpc("map.place_random", { map: ws.spec.map, n: Number(ws.randomN), seed: Math.floor(Math.random() * 1e6) });
      ws.spec.agents_xy = r.agents_xy; ws.spec.targets_xy = r.targets_xy; ws.pending = null;
    } catch (e) { toast(e.message); }
  }
  function resize() {
    const nH = Math.max(3, Math.min(64, Number(ws.size.H) || 3)), nW = Math.max(3, Math.min(64, Number(ws.size.W) || 3));
    ws.spec.map = Array.from({ length: nH }, (_, r) => (ws.spec.map[r] || "").slice(0, nW).padEnd(nW, "."));
    const keep = ws.spec.agents_xy.map((s, i) => [s, ws.spec.targets_xy[i]])
      .filter(([s, g]) => s[0] < nH && s[1] < nW && g[0] < nH && g[1] < nW);
    ws.spec.agents_xy = keep.map((p) => p[0]); ws.spec.targets_xy = keep.map((p) => p[1]);
    ws.pending = null; ws.size = { H: nH, W: nW };
  }
  function clearAll() {
    if (!confirm("清空整张地图（墙和所有起终点）？")) return;
    ws.spec.map = blankMap(H.value, W.value); ws.spec.agents_xy = []; ws.spec.targets_xy = []; ws.pending = null;
  }

  // ---------------- 地图库
  async function load() {
    if (!ws.loadName) return;
    try {
      const r = await rpc("map.load", { name: ws.loadName });
      ws.spec = r.spec; ws.pending = null; ws.size = { H: r.spec.map.length, W: r.spec.map[0].length };
    } catch (e) { toast(e.message); }
  }
  async function save() {
    const title = prompt("给这张地图起个名字", ws.spec.title || "我的地图");
    if (!title) return;
    try {
      await rpc("map.save", { spec: ws.spec, title });
      ws.spec.title = title; toast(`已保存「${title}」，以后在地图库里能找到`);
    } catch (e) { toast(e.message); }
  }
  async function del() {
    const m = getMaps().find((x) => x.name === ws.loadName);
    if (!m || m.builtin) return;
    if (!confirm(`删除「${m.title}」？删除后找不回来。`)) return;
    try { await rpc("map.delete", { name: m.name }); ws.loadName = ""; toast("已删除"); } catch (e) { toast(e.message); }
  }
  async function exportMap() {
    try {
      const r = await rpc("map.export", { spec: ws.spec });
      const blob = new Blob([r.text], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${ws.spec.title || "地图"}.${r.format === "pogema" ? "txt" : "json"}`;
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { toast(e.message); }
  }
  async function doImport(text) {
    try {
      const r = await rpc("map.import", { text });
      ws.spec = r.spec; ws.pending = null; ws.size = { H: r.spec.map.length, W: r.spec.map[0].length };
      ws.showImport = false; ws.importText = "";
      toast(r.errors.length ? `导入了，但还有问题：${r.errors[0]}` : "导入成功");
    } catch (e) { toast(`导入失败：${e.message}`); }
  }
  function onFile(ev) {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => { doImport(String(rd.result)); ev.target.value = ""; };
    rd.readAsText(f, "utf-8");
  }

  // ---------------- 画面
  const scene = computed(() => {
    const agents = ws.spec.agents_xy.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) }));
    if (ws.pending) agents.push({ id: 999, r: ws.pending[0], c: ws.pending[1], color: agentColor(ws.spec.agents_xy.length), label: "?", self: true });
    const goals = ws.spec.targets_xy.map((p, i) => ({ id: i, r: p[0], c: p[1], color: agentColor(i), label: String(i + 1) }));
    return { H: H.value, W: W.value, cells: ws.spec.map, agents, goals };
  });
  const userMaps = computed(() => getMaps().filter((m) => !m.builtin));
  const selectedIsUser = computed(() => userMaps.value.some((m) => m.name === ws.loadName));
  const use = () => onUse(JSON.parse(JSON.stringify(ws.spec)));
  const hint = computed(() => ({
    wall: "按住拖动可以连续画墙",
    erase: "按住拖动可以连续擦除；擦到起点或终点会删掉这一对（后面的编号顺延）",
    pair: ws.pending ? `已放 ${ws.spec.agents_xy.length + 1} 号的起点，再点一下放它的终点（点起点本身取消）` : "先点起点，再点终点；自动编号配色",
  }[ws.tool]));

  // 重新校验一次（例如页面刚打开时 WS 还没连上，第一次校验失败了）
  function revalidate() { ws.spec = { ...ws.spec }; }

  return { revalidate, ws, dom, KINDS, TOOLS, scene, onDown, onMove, onUp, generate, placeRandom, resize, clearAll,
           load, save, del, exportMap, doImport, onFile, usable, use, userMaps, selectedIsUser, hint, H, W };
}

/** 左边的工具卡片（竖排）。 */
export const WorkshopTools = {
  name: "WorkshopTools",
  props: { w: { type: Object, required: true }, maps: { type: Array, default: () => [] },
           canUse: { type: Boolean, default: true } },
  setup(props) { return { ...props.w }; },
  template: `
  <div class="ws-tools">
    <div class="card">
      <h2>这张地图</h2>
      <label class="ws-field">标题 <input v-model="ws.spec.title" maxlength="40"></label>
      <div class="muted small">{{ W }}×{{ H }}，{{ ws.spec.agents_xy.length }} 对起终点</div>
      <button class="btn ok full" :disabled="!usable || !canUse" @click="use">▶ 用这张图开新局</button>
      <div v-if="!canUse" class="muted small">比赛进行中，先在课堂里点「结束」</div>
    </div>
    <div class="card">
      <h2>生成</h2>
      <div class="ws-grid">
        <label>类型</label>
        <select v-model="ws.gen.kind"><option v-for="k in KINDS" :value="k[0]">{{ k[1] }}</option></select>
        <label>宽 × 高</label>
        <span class="pair"><input type="number" min="4" max="64" v-model="ws.gen.width"> × <input type="number" min="4" max="64" v-model="ws.gen.height"></span>
        <template v-if="ws.gen.kind !== 'warehouse'">
          <label>密度</label><input type="number" min="0" max="0.6" step="0.05" v-model="ws.gen.density">
        </template>
        <label>种子</label><input type="number" v-model="ws.gen.seed">
      </div>
      <div class="ws-btns">
        <button class="btn" :disabled="ws.busy" @click="generate(false)">生成</button>
        <button class="btn ghost" :disabled="ws.busy" @click="generate(true)">换一张</button>
      </div>
    </div>
    <div class="card">
      <h2>编辑</h2>
      <div class="ws-toolbtns">
        <button v-for="t in TOOLS" :key="t[0]" :class="['tool', { on: ws.tool === t[0] }]" @click="ws.tool = t[0]; ws.pending = null">{{ t[1] }}</button>
      </div>
      <p class="muted small">{{ hint }}</p>
      <div class="ws-btns">
        <label>随机放 <input type="number" min="1" max="64" v-model="ws.randomN"> 对</label>
        <button class="btn ghost" @click="placeRandom">放置</button>
      </div>
      <div class="ws-btns">
        <label>宽 <input type="number" min="3" max="64" v-model="ws.size.W"></label>
        <label>高 <input type="number" min="3" max="64" v-model="ws.size.H"></label>
        <button class="btn ghost" @click="resize">调整尺寸</button>
      </div>
      <button class="btn ghost full" @click="clearAll">清空整张图</button>
    </div>
    <div class="card">
      <h2>地图库</h2>
      <select v-model="ws.loadName" class="full-select">
        <option value="">选一张地图……</option>
        <optgroup label="内置"><option v-for="m in maps.filter(x => x.builtin)" :value="m.name">{{ m.title }}（{{ m.W }}×{{ m.H }}）</option></optgroup>
        <optgroup v-if="userMaps.length" label="我保存的"><option v-for="m in userMaps" :value="m.name">{{ m.title }}（{{ m.W }}×{{ m.H }}，{{ m.n }} 人）</option></optgroup>
      </select>
      <div class="ws-btns">
        <button class="btn ghost" :disabled="!ws.loadName" @click="load">载入</button>
        <button class="btn ghost" :disabled="!selectedIsUser" @click="del">删除</button>
        <button class="btn ghost" :disabled="!usable" @click="save">保存当前图</button>
      </div>
      <div class="ws-btns">
        <button class="btn ghost" @click="exportMap">导出文件</button>
        <label class="btn ghost file">导入文件<input type="file" accept=".txt,.json,.map,text/plain,application/json" @change="onFile"></label>
        <button class="btn ghost" @click="ws.showImport = !ws.showImport">粘贴导入</button>
      </div>
      <div v-if="ws.showImport" class="ws-import">
        <textarea v-model="ws.importText" rows="6" placeholder="粘贴 pogema 地图字符串（. 空地 # 墙 a/A 成对的起点终点），或导出的 JSON"></textarea>
        <button class="btn" :disabled="!ws.importText.trim()" @click="doImport(ws.importText)">导入</button>
      </div>
    </div>
  </div>`,
};

/** 中间的画布：按可用宽高放大；下面是校验结果。 */
export const WorkshopCanvas = {
  name: "WorkshopCanvas",
  components: { GridView },
  props: { w: { type: Object, required: true }, active: { type: Boolean, default: false } },
  setup(props) {
    const w = props.w;
    const box = ref(null);
    const fitH = ref(560);
    const BOTTOM = 130;     // 画布下面的校验结果 + 卡片底边距
    function measure() {
      const el = box.value;
      if (!el) return;
      fitH.value = Math.max(260, window.innerHeight - (el.getBoundingClientRect().top + window.scrollY) - BOTTOM);
    }
    const setBox = (el) => { box.value = el; w.dom.canvas = el; };
    onMounted(() => { window.addEventListener("resize", measure); nextTick(measure); });
    onBeforeUnmount(() => window.removeEventListener("resize", measure));
    watch(() => props.active, (a) => { if (a) nextTick(measure); });
    const canvasStyle = computed(() => ({ width: `min(100%, ${Math.floor(fitH.value * w.W.value / w.H.value)}px)`, margin: "0 auto" }));
    return { ...w, setBox, canvasStyle };
  },
  template: `
  <div class="ws-canvas-wrap">
    <div class="ws-canvas-head"><b>{{ ws.spec.title }}</b><span class="muted small">{{ hint }}</span></div>
    <div :ref="setBox" class="ws-canvas" @pointerdown.prevent="onDown" @pointermove="onMove" @pointerup="onUp" @pointerleave="onUp">
      <GridView :scene="scene" :style="canvasStyle"/>
    </div>
    <div class="ws-check">
      <div v-if="ws.check.checking" class="muted small">正在检查……</div>
      <div v-else-if="ws.check.errors.length" class="apply-error">
        <div v-for="(e, i) in ws.check.errors.slice(0, 5)" :key="i">⚠️ {{ e }}</div>
        <div v-if="ws.check.errors.length > 5">……还有 {{ ws.check.errors.length - 5 }} 条</div>
      </div>
      <div v-else-if="!ws.spec.agents_xy.length" class="muted small">还没有放起点和终点</div>
      <div v-else class="ok-box">✅ 地图检查通过，可以开局</div>
      <div v-if="ws.check.ai_hint" class="ai-hint">🤖 {{ ws.check.ai_hint }}</div>
    </div>
  </div>`,
};
