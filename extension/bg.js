// Chrome Window Bridge 扩展后台 (MV3 Service Worker)
// 事件推送制: 仅在窗口/标签变化或每分钟闹铃时被唤醒, 推送一次状态后即可休眠。
// 拖动窗口本身不改变标签列表, 因此 bridge 触发转移时用最近一次状态即准确。
const PORT = 9422;
let ws = null;

function sendState() {
  if (!ws || ws.readyState !== 1) return;
  chrome.windows.getAll({ populate: true }, (wins) => {
    const windows = (wins || [])
      .filter((w) => w.type === "normal" && !w.incognito)
      .map((w) => ({
        id: w.id,
        left: w.left,
        top: w.top,
        width: w.width,
        height: w.height,
        state: w.state,
        focused: !!w.focused,
        tabs: (w.tabs || [])
          .filter((t) => t.url)
          .map((t) => ({
            url: t.url,
            title: t.title || "",
            active: !!t.active,
            pinned: !!t.pinned,
          })),
      }));
    try {
      ws.send(JSON.stringify({ type: "state", windows }));
    } catch (e) {
      /* 连接已断, 忽略 */
    }
  });
}

function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  let sock;
  try {
    sock = new WebSocket("ws://127.0.0.1:" + PORT + "/bridge");
  } catch (e) {
    return;
  }
  sock.onopen = () => {
    ws = sock;
    sendState();
  };
  sock.onmessage = (ev) => {
    let m = null;
    try {
      m = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    if (m && m.cmd === "refresh") sendState();
    if (m && m.cmd === "focus" && m.url) {
      chrome.windows.getLastFocused({}, (w) => {
        if (!w || !w.id) return;
        chrome.windows.get(w.id, { populate: true }, (ww) => {
          const tab = (ww.tabs || []).find((t) => t.url === m.url);
          if (tab && !tab.active) chrome.tabs.update(tab.id, { active: true });
        });
      });
    }
  };
  sock.onclose = () => {
    if (ws === sock) ws = null;
  };
  sock.onerror = () => {
    try {
      sock.close();
    } catch (e) {}
  };
}

connect();
chrome.alarms.create("bridge-alive", { periodInMinutes: 1 });

chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.windows.onCreated.addListener(() => { connect(); setTimeout(sendState, 200); });
chrome.windows.onRemoved.addListener(() => { connect(); setTimeout(sendState, 200); });
chrome.windows.onFocusChanged.addListener(() => { connect(); sendState(); });
chrome.tabs.onCreated.addListener(() => { connect(); sendState(); });
chrome.tabs.onRemoved.addListener(() => { connect(); sendState(); });
chrome.tabs.onAttached.addListener(() => { connect(); sendState(); });
chrome.tabs.onDetached.addListener(() => { connect(); sendState(); });
chrome.tabs.onActivated.addListener(() => { connect(); sendState(); });
chrome.tabs.onUpdated.addListener((_id, info) => {
  if (info.url) {
    connect();
    sendState();
  }
});
