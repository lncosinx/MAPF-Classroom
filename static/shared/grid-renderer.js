// 地图 SVG 渲染组件。老师端、学生端、投屏页统一走这里，保证三处外观一致（CLAUDE.md 前端约定）。
//
// scene 结构（各页面自己从服务端消息拼出来）：
//   H, W                 地图尺寸
//   cells[r]             每行一个字符串：'.' 可见空地  '#' 可见障碍  'm' 记忆中的空地  'M' 记忆中的障碍
//                        's' 队友共享来的空地  'S' 共享来的障碍  '?' 未知（迷雾）
//   agents[]             {id, r, c, color, label, self, contact, emoji, badge, shake}
//                        contact=true：在通信范围内，画高亮光圈、可点击；emoji：简洁模式的动物头像（代替编号）；
//                        badge：编号旁的小动物（老师页简洁模式）；shake=true：这一步被挡住了，晃一下（随 stepKey 重播）
//   goals[]              {id, r, c, color, self, proj, label, emoji, arrived}   arrived=true：刚到达，旗子弹一下       proj=true：目标在视野外，这是它在视野框边上的投影（同 pogema ch2，可能落在墙上或别人身上）
//   arrows[]             {r, c, action, color, dashed}     动作箭头（专家 / AI / 幽灵预览）
//   box                  [r0, c0, r1, c1] | null            自己的视野框（虚线）
//   boxes[]              {box, color}                       老师端：各学生的视野框
//   edges[]              {a: [r, c], b: [r, c]}            AI 通信边
//   links[]              {a: [r, c], b: [r, c], color, dashed}   学生通信连线（老师端）
//   circles[]            {r, c, radius, color}             通信范围圆（L2 半径，单位：格）
//   sharedBoxes[]        {box, color}                      队友共享来的视野框（细实线）
//   bubbles[]            {r, c, text}                      消息气泡（显示在发送者头上）
//   clickable            点击格子时发出 cell 事件；点击带 contact 的智能体发出 agent 事件
//   stepKey              当前是第几步。智能体换位置有 250 ms 补间，叠加层（箭头 / 连线 / 气泡）却是按新位置直接画的，
//                        会比智能体先到。给了 stepKey 时，叠加层随步号重建，并等智能体走到位后再淡入（.after-move）
const { computed } = Vue;

const S = 40; // 每格的用户坐标单位

const FILL = { ".": "var(--free)", "#": "var(--obstacle)", "m": "var(--memory)", "M": "var(--memory-wall)",
               "s": "var(--shared)", "S": "var(--shared-wall)", "?": "var(--fog)" };

function arrowPath(r, c, action, len = 0.42) {
  const d = [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]][action] || [0, 0];
  const cx = (c + 0.5) * S, cy = (r + 0.5) * S;
  if (action === 0) return null;
  const x2 = cx + d[1] * len * S, y2 = cy + d[0] * len * S;
  // 箭头三角
  const hx = d[1], hy = d[0], px = -hy, py = hx, h = 0.16 * S;
  const tip = `${x2 + hx * h * 0.6},${y2 + hy * h * 0.6}`;
  const l = `${x2 + px * h * 0.6},${y2 + py * h * 0.6}`;
  const rr = `${x2 - px * h * 0.6},${y2 - py * h * 0.6}`;
  return { line: { x1: cx, y1: cy, x2, y2 }, head: `${tip} ${l} ${rr}` };
}

export const GridView = {
  name: "GridView",
  props: { scene: { type: Object, required: true } },
  emits: ["cell", "agent"],
  setup(props) {
    const S_ = S;
    const cellList = computed(() => {
      const out = [];
      const sc = props.scene;
      for (let r = 0; r < sc.H; r++) {
        const row = (sc.cells && sc.cells[r]) || "";
        for (let c = 0; c < sc.W; c++) {
          const ch = row[c] || "?";
          if (ch !== ".") out.push({ r, c, fill: FILL[ch] || FILL["?"], key: `${r}-${c}` });
        }
      }
      return out;
    });
    // 目标标记：记下格子上是否站着智能体，有人时旗子缩小到右上角
    const goalMarks = computed(() => {
      const occ = new Set((props.scene.agents || []).map((a) => a.r + "," + a.c));
      return (props.scene.goals || []).map((g) => ({ ...g, small: occ.has(g.r + "," + g.c) }));
    });
    const arrows = computed(() => (props.scene.arrows || []).map((a, i) => {
      const p = arrowPath(a.r, a.c, a.action, a.len || 0.42);
      return { ...a, p, key: i };
    }));
    const rectOf = (b) => ({ x: b[1] * S + 1.5, y: b[0] * S + 1.5, w: (b[3] - b[1] + 1) * S - 3, h: (b[2] - b[0] + 1) * S - 3 });
    const sk = computed(() => (props.scene.stepKey == null ? "" : String(props.scene.stepKey)));
    return { S: S_, cellList, arrows, rectOf, goalMarks, sk };
  },
  template: `
  <svg class="grid-svg" :viewBox="'0 0 ' + scene.W * S + ' ' + scene.H * S" xmlns="http://www.w3.org/2000/svg"
       role="img" aria-label="地图">
    <rect x="0" y="0" :width="scene.W * S" :height="scene.H * S" fill="var(--free)"/>
    <rect v-for="cl in cellList" :key="cl.key" :x="cl.c * S" :y="cl.r * S" :width="S" :height="S" :fill="cl.fill"/>
    <!-- 网格细线 -->
    <g stroke="var(--line)" stroke-width="1">
      <line v-for="r in scene.H - 1" :key="'h' + r" x1="0" :x2="scene.W * S" :y1="r * S" :y2="r * S"/>
      <line v-for="c in scene.W - 1" :key="'v' + c" y1="0" :y2="scene.H * S" :x1="c * S" :x2="c * S"/>
    </g>
    <rect v-for="(b, i) in (scene.boxes || [])" :key="'bx' + i" :x="rectOf(b.box).x" :y="rectOf(b.box).y"
          :width="rectOf(b.box).w" :height="rectOf(b.box).h" fill="none" :stroke="b.color" stroke-width="2"
          stroke-dasharray="6 5" opacity="0.6"/>
    <rect v-if="scene.box" :x="rectOf(scene.box).x" :y="rectOf(scene.box).y" :width="rectOf(scene.box).w"
          :height="rectOf(scene.box).h" fill="none" stroke="var(--self)" stroke-width="2.5" stroke-dasharray="7 5"/>
    <!-- 目标底色（在智能体下层） -->
    <rect v-for="g in goalMarks" :key="'gb' + g.id" v-show="!g.proj" :x="g.c * S + 4" :y="g.r * S + 4"
          :width="S - 8" :height="S - 8" rx="6" :fill="g.color" opacity="0.14"/>
    <!-- 队友共享来的视野框 -->
    <rect v-for="(b, i) in (scene.sharedBoxes || [])" :key="'sb' + i" :x="rectOf(b.box).x" :y="rectOf(b.box).y"
          :width="rectOf(b.box).w" :height="rectOf(b.box).h" fill="none" :stroke="b.color" stroke-width="2" opacity="0.8"/>
    <!-- 通信范围圆 -->
    <circle v-for="(c, i) in (scene.circles || [])" :key="'ci' + i" :cx="(c.c + 0.5) * S" :cy="(c.r + 0.5) * S"
            :r="c.radius * S" :fill="c.color" fill-opacity="0.05" :stroke="c.color" stroke-width="1.5"
            stroke-dasharray="3 5" opacity="0.7"/>
    <!-- 学生通信连线 -->
    <line v-for="(e, i) in (scene.links || [])" :key="'l' + sk + '-' + i" :class="{ 'after-move': sk !== '' }" :x1="(e.a[1] + 0.5) * S" :y1="(e.a[0] + 0.5) * S"
          :x2="(e.b[1] + 0.5) * S" :y2="(e.b[0] + 0.5) * S" :stroke="e.color" stroke-width="3" opacity="0.75"
          stroke-linecap="round" :stroke-dasharray="e.dashed ? '6 5' : null"/>
    <!-- AI 通信边 -->
    <line v-for="(e, i) in (scene.edges || [])" :key="'e' + sk + '-' + i" :class="{ 'after-move': sk !== '' }" :x1="(e.a[1] + 0.5) * S" :y1="(e.a[0] + 0.5) * S"
          :x2="(e.b[1] + 0.5) * S" :y2="(e.b[0] + 0.5) * S" stroke="var(--c3)" stroke-width="2" opacity="0.55"/>
    <!-- 智能体 -->
    <g v-for="a in (scene.agents || [])" :key="'a' + a.id" class="agent"
       :style="{ transform: 'translate(' + (a.c * S) + 'px,' + (a.r * S) + 'px)' }">
      <circle v-if="a.self" class="pulse" :cx="S / 2" :cy="S / 2" :r="S * 0.42" fill="none" :stroke="a.color" stroke-width="3"/>
      <circle v-if="a.contact" :cx="S / 2" :cy="S / 2" :r="S * 0.46" fill="none" stroke="var(--contact)"
              stroke-width="3" stroke-dasharray="5 3"/>
      <g :key="'in' + sk + (a.shake ? 's' : '')" :class="{ shake: a.shake }">
        <template v-if="a.emoji">
          <circle :cx="S / 2" :cy="S / 2" :r="S * 0.4" fill="#fff" :stroke="a.color" stroke-width="3.5"/>
          <text :x="S / 2" :y="S / 2 + S * 0.17" text-anchor="middle" :font-size="S * 0.5" class="emoji">{{ a.emoji }}</text>
        </template>
        <template v-else>
          <circle :cx="S / 2" :cy="S / 2" :r="S * 0.34" :fill="a.color" stroke="#fff" stroke-width="2.5"/>
          <text v-if="a.label" :x="S / 2" :y="S / 2 + 5" text-anchor="middle" font-size="14" font-weight="700" fill="#fff">{{ a.label }}</text>
        </template>
        <text v-if="a.badge" :x="S * 0.86" :y="S * 0.3" text-anchor="middle" :font-size="S * 0.32" class="emoji">{{ a.badge }}</text>
      </g>
      <rect v-if="a.contact && scene.clickable" :width="S" :height="S" fill="transparent" class="cell-click"
            @click.stop="$emit('agent', a.id)"/>
    </g>
    <!-- 目标旗帜：画在智能体**上层**，否则投影 / 目标格上站着人时会被圆点盖住。
         投影（目标在视野外，pogema ch2 语义，可能落在墙上或别人身上）：灰色空心旗 + 虚线框，不抢眼但墙上也看得清。
         格子上有智能体时，旗子缩小画在右上角。 -->
    <g v-for="g in goalMarks" :key="'g' + g.id"
       :transform="'translate(' + (g.c * S + (g.small ? S * 0.5 : 0)) + ',' + (g.r * S + (g.small ? -S * 0.04 : 0)) + ') scale(' + (g.small ? 0.5 : 1) + ')'">
      <g :key="'gi' + sk + (g.arrived ? 'a' : '')" :class="{ pop: g.arrived }">
      <!-- 投影：灰色、空心、虚线，刻意做得不起眼，避免被当成真目标；真目标是实心彩色旗 -->
      <template v-if="g.proj">
        <rect x="5" y="5" :width="S - 10" :height="S - 10" rx="7" fill="#fff" fill-opacity="0.6"
              stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="4 3" stroke-opacity="0.8"/>
        <line :x1="S * 0.34" :y1="S * 0.24" :x2="S * 0.34" :y2="S * 0.78" stroke="var(--muted)" stroke-width="2"
              stroke-linecap="round" opacity="0.8"/>
        <path :d="'M' + S * 0.36 + ',' + S * 0.26 + ' L' + S * 0.74 + ',' + S * 0.37 + ' L' + S * 0.36 + ',' + S * 0.48 + ' z'"
              fill="none" stroke="var(--muted)" stroke-width="1.8" stroke-linejoin="round" opacity="0.8"/>
      </template>
      <template v-else>
        <rect v-if="g.small" x="4" y="4" :width="S - 8" :height="S - 8" rx="7" fill="#fff" fill-opacity="0.9"/>
        <line :x1="S * 0.32" :y1="S * 0.18" :x2="S * 0.32" :y2="S * 0.84" :stroke="g.color" stroke-width="3" stroke-linecap="round"/>
        <path :d="'M' + S * 0.34 + ',' + S * 0.2 + ' L' + S * 0.8 + ',' + S * 0.34 + ' L' + S * 0.34 + ',' + S * 0.5 + ' z'" :fill="g.color"/>
        <!-- 编号（老师端）：色板只有 7 种颜色会重复，靠编号对应到同号智能体。白色描边保证压在墙上也能读 -->
        <text v-if="g.label" :x="S * 0.66" :y="S * 0.86" text-anchor="middle" :font-size="g.small ? 22 : 15"
              font-weight="700" :fill="g.color" stroke="#fff" stroke-width="3.5" paint-order="stroke">{{ g.label }}</text>
        <text v-if="g.emoji" :x="S * 0.7" :y="S * 0.92" text-anchor="middle" :font-size="g.small ? 26 : 16" class="emoji">{{ g.emoji }}</text>
      </template>
      </g>
    </g>
    <!-- 动作箭头 -->
    <g v-for="a in arrows" :key="'ar' + sk + '-' + a.key" :opacity="a.opacity || 0.9" :class="{ 'after-move': sk !== '' }">
      <template v-if="a.p">
        <line :x1="a.p.line.x1" :y1="a.p.line.y1" :x2="a.p.line.x2" :y2="a.p.line.y2" :stroke="a.color"
              stroke-width="4" stroke-linecap="round" :stroke-dasharray="a.dashed ? '4 4' : null"/>
        <polygon :points="a.p.head" :fill="a.color"/>
      </template>
      <rect v-else :x="(a.c + 0.5) * S - 6" :y="(a.r + 0.5) * S - 6" width="12" height="12" rx="2" :fill="a.color"/>
    </g>
    <!-- 消息气泡 -->
    <g v-for="(b, i) in (scene.bubbles || [])" :key="'bu' + sk + '-' + i" :class="{ 'after-move': sk !== '' }"
       :transform="'translate(' + ((b.c + 0.5) * S) + ',' + (b.r * S - 4) + ')'">
      <rect :x="-S * 0.5" :y="-S * 0.62" :width="S" :height="S * 0.58" rx="9" fill="#fff" stroke="var(--contact)" stroke-width="1.5"/>
      <path :d="'M-5,' + (-S * 0.05) + ' L0,4 L5,' + (-S * 0.05) + ' z'" fill="#fff" stroke="var(--contact)" stroke-width="1.5"/>
      <text x="0" :y="-S * 0.22" text-anchor="middle" font-size="18">{{ b.text }}</text>
    </g>
    <!-- 点击层 -->
    <template v-if="scene.clickable">
      <rect v-for="cl in (scene.clickCells || [])" :key="'k' + cl.r + '-' + cl.c" class="cell-click"
            :x="cl.c * S" :y="cl.r * S" :width="S" :height="S" fill="transparent" @click="$emit('cell', cl.r, cl.c)"/>
    </template>
  </svg>`,
};
