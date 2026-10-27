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
export const PALETTE = ["#c1433c", "#2e6f9e", "#6e81af", "#00b9c8", "#72D5C8", "#0ea08c", "#8F7B66"];
export const agentColor = (i) => PALETTE[i % PALETTE.length];
