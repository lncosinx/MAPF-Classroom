// 带自动重连的 WebSocket 客户端。断线后按 0.5s→1s→2s→…（最多 5s）重试。
export class Connection {
  constructor(path, { onMessage, onOpen, onStatus } = {}) {
    this.path = path;
    this.onMessage = onMessage || (() => {});
    this.onOpen = onOpen || (() => {});
    this.onStatus = onStatus || (() => {});
    this.delay = 500;
    this.closed = false;
    this.connect();
  }
  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}${this.path}`);
    this.ws = ws;
    ws.onopen = () => { this.delay = 500; this.onStatus(true); this.onOpen(); };
    ws.onmessage = (ev) => { try { this.onMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
    ws.onclose = (ev) => {
      this.onStatus(false);
      // 4000：同一账号在别的标签页登录；4001：被老师移出课堂；4403：老师端鉴权失败。这几种不重连。
      if (this.closed || ev.code === 4000 || ev.code === 4001 || ev.code === 4403) { this.onStatus(false, ev.code); return; }
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 5000);
    };
  }
  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) { this.ws.send(JSON.stringify(obj)); return true; }
    return false;
  }
}

// 前端代码版本：服务端在 welcome / 快照里带上 version。页面只靠 WS 重连时，内存里还是旧代码，
// 发现版本和第一次拿到的不同，就刷新一次，拿到新代码（2026-10-28：老师重启服务后学生页仍是旧界面）
let seenVersion = null;
export function checkVersion(v) {
  if (!v) return;
  if (seenVersion && seenVersion !== v) { location.reload(); return; }
  seenVersion = v;
}

// localStorage 可能被禁用（隐私模式等），读写都要兜底
export const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { /* 忽略 */ } },
};

// 按标签页隔离的存储：重连 token 放这里。同一浏览器开几个学生标签页（课前演示、验收）时，
// 用 localStorage 会共用同一个 token，后开的标签页会把先开的挤下线。
export const tabStore = {
  get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* 忽略 */ } },
};

// 动作编号与 pogema 一致：0=wait、1=up、2=down、3=left、4=right（wait 在第 0 位）
export const ACTIONS = [
  { id: 0, dr: 0, dc: 0, icon: "■", name: "停" },
  { id: 1, dr: -1, dc: 0, icon: "↑", name: "上" },
  { id: 2, dr: 1, dc: 0, icon: "↓", name: "下" },
  { id: 3, dr: 0, dc: -1, icon: "←", name: "左" },
  { id: 4, dr: 0, dc: 1, icon: "→", name: "右" },
];
// 预设消息（与 classroom/core/settings.py 的 MESSAGE_KINDS 一致）
export const MESSAGES = {
  move_up: { icon: "↑", text: "我要往上走" },
  move_down: { icon: "↓", text: "我要往下走" },
  move_left: { icon: "←", text: "我要往左走" },
  move_right: { icon: "→", text: "我要往右走" },
  yield: { icon: "✋", text: "请让一下" },
  go_first: { icon: "✅", text: "你先走" },
  arrived: { icon: "🏁", text: "我到了" },
};

// 简洁模式的动物头像：每个智能体固定一个（i 号 -> ANIMALS[i]），超过 24 个时动物后面加数字
export const ANIMALS = ["🐱", "🐶", "🐰", "🐼", "🦊", "🐸", "🐯", "🐵", "🐧", "🐨", "🐷", "🐮",
                        "🦁", "🐻", "🐹", "🐭", "🐔", "🦄", "🐙", "🐳", "🐢", "🦉", "🐝", "🦋"];
export const agentAvatar = (i) => ANIMALS[i % ANIMALS.length] + (i >= ANIMALS.length ? String(Math.floor(i / ANIMALS.length) + 1) : "");

export const PALETTE = ["#c1433c", "#2e6f9e", "#6e81af", "#00b9c8", "#72D5C8", "#0ea08c", "#8F7B66"];
export const agentColor = (i) => PALETTE[i % PALETTE.length];

// 指针 / 拖放事件 -> 地图格子。container 里放的是 GridView 渲染出的 <svg>（宽高比与地图一致）。
export function cellFromEvent(ev, container, H, W) {
  const svg = container && container.querySelector("svg");
  if (!svg) return null;
  const rect = svg.getBoundingClientRect();
  const c = Math.floor(((ev.clientX - rect.left) / rect.width) * W);
  const r = Math.floor(((ev.clientY - rect.top) / rect.height) * H);
  return r >= 0 && r < H && c >= 0 && c < W ? [r, c] : null;
}
