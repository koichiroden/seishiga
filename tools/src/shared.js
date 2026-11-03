// ===== 3つのビルダー共通: 駅データ・区間エディタ・時刻/保存ユーティリティ =====
const STATION_DATA = JSON.parse(document.getElementById("station-data").textContent);
const LINE_NAMES = Object.keys(STATION_DATA).sort((a, b) => a.localeCompare(b, "ja"));
/*MANUAL_PRESETS*/

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined || c === false) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}
function svgEl(tag, attrs, text) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---- 路線・区間 ----
function isScrambled(line) { return !!(STATION_DATA[line] && STATION_DATA[line].scrambled); }
function lineStationList(line, presetIdx) {
  const entry = STATION_DATA[line];
  if (!entry) return [];
  const byName = new Map(entry.stations.map(s => [s.name, s]));
  if (entry.scrambled && MANUAL_PRESETS[line]) {
    const p = MANUAL_PRESETS[line][presetIdx || 0] || MANUAL_PRESETS[line][0];
    return p.stations.filter(n => byName.has(n)).map(n => ({ name: n, cum_km: byName.get(n).cum_km, approx: true }));
  }
  return entry.stations.map(s => ({ name: s.name, cum_km: s.cum_km }));
}
function resolveSegment(seg) {
  const list = lineStationList(seg.line, seg.preset);
  const a = list.findIndex(s => s.name === seg.from);
  const b = list.findIndex(s => s.name === seg.to);
  if (a < 0 || b < 0) return [];
  const slice = a <= b ? list.slice(a, b + 1) : list.slice(b, a + 1).reverse();
  return slice.map(s => ({ ...s, line: seg.line }));
}
// 区間の連結 → [{line, name, km(始発からの累積), approx}]
function buildStationList(segments) {
  const out = [];
  let base = 0;
  segments.forEach((seg, si) => {
    const st = resolveSegment(seg);
    if (!st.length) return;
    const k0 = st[0].cum_km;
    st.forEach((s, i) => {
      const km = base + Math.abs(s.cum_km - k0);
      if (si > 0 && i === 0 && out.length && out[out.length - 1].name === s.name) return; // 乗継駅は1回だけ
      out.push({ line: s.line, name: s.name, km, approx: !!s.approx });
    });
    base = out.length ? out[out.length - 1].km : base;
  });
  // 駅データの都合で同じ駅が2回出てくる路線(横須賀線の横浜付近など)は、
  // 1回目と2回目の間の寄り道を取り除く(ループを畳む)
  for (let i = 0; i < out.length; i++) {
    const j = out.findIndex((s, k) => k > i && s.name === out[i].name);
    if (j > 0) {
      const loopKm = out[j].km - out[i].km;
      for (let k = j + 1; k < out.length; k++) out[k].km -= loopKm;
      out.splice(i + 1, j - i);
    }
  }
  return out;
}

// ---- 途中駅の削除(外す) ----
// holder.removed に外した駅名を持たせる。始発駅・終着駅は外せない。
function filterRemoved(st, removed) {
  const set = new Set(removed || []);
  return st.filter((s, i) => i === 0 || i === st.length - 1 || !set.has(s.name));
}
function removeStationButton(holder, name, onChange) {
  return el("button", { type: "button", class: "ghost small del-st", title: `${name} を外す`, "aria-label": `${name} を外す`,
    onclick: () => { holder.removed = (holder.removed || []).concat([name]); onChange(); } }, ["✕"]);
}
// 外した駅の一覧(「戻す」ボタン付き)。区間に含まれない駅名は自動で掃除する
function removedBar(holder, fullSt, onChange) {
  const inRange = new Set(fullSt.map(s => s.name));
  holder.removed = (holder.removed || []).filter(n => inRange.has(n));
  if (!holder.removed.length) return el("span");
  const order = new Map(fullSt.map((s, i) => [s.name, i]));
  const names = [...holder.removed].sort((a, b) => order.get(a) - order.get(b));
  return el("div", { class: "removed-bar" }, [
    el("span", { class: "k" }, [`外した駅(${names.length})`]),
    ...names.map(n => el("button", { type: "button", class: "chip", title: `${n} を戻す`, onclick: () => { holder.removed = holder.removed.filter(x => x !== n); onChange(); } }, [n, el("span", { "aria-hidden": "true" }, [" ↺"])])),
    el("button", { type: "button", class: "ghost small", onclick: () => { holder.removed = []; onChange(); } }, ["すべて戻す"]),
  ]);
}
// 区間から作った駅一覧と config の駅一覧の差 = 外した駅(JSON読み込み時に復元する)
function removedFrom(segments, cfgStations) {
  const keep = new Set(cfgStations.map(s => s.name));
  return buildStationList(segments).map(s => s.name).filter(n => !keep.has(n));
}

// 区間エディタ: segments 配列を直接書き換え、変更のたびに onChange()
function renderSegmentEditor(container, segments, onChange) {
  container.innerHTML = "";
  const list = el("div", { class: "seg-list" });
  segments.forEach((seg, idx) => {
    const box = el("div", { class: "seg" });
    const lineSel = el("select", { id: `${container.id}-line-${idx}`, "aria-label": "路線" });
    LINE_NAMES.forEach(n => lineSel.appendChild(el("option", { value: n }, [n + (isScrambled(n) ? "(系統を選択)" : "")])));
    lineSel.value = seg.line;
    const stations = lineStationList(seg.line, seg.preset);
    const mkSel = (key, label) => {
      const s = el("select", { id: `${container.id}-${key}-${idx}`, "aria-label": label });
      stations.forEach(st => s.appendChild(el("option", { value: st.name }, [st.name])));
      if (!stations.some(x => x.name === seg[key])) seg[key] = key === "from" ? stations[0]?.name : stations[stations.length - 1]?.name;
      s.value = seg[key];
      s.addEventListener("change", () => { seg[key] = s.value; onChange(); });
      return el("div", { class: "field" }, [el("label", { for: s.id }, [label]), s]);
    };
    lineSel.addEventListener("change", () => {
      seg.line = lineSel.value; seg.preset = 0;
      const st = lineStationList(seg.line, 0);
      seg.from = st[0]?.name; seg.to = st[st.length - 1]?.name;
      if (idx > 0) { const prev = resolveSegment(segments[idx - 1]); const last = prev[prev.length - 1]; if (last && st.some(x => x.name === last.name)) seg.from = last.name; }
      renderSegmentEditor(container, segments, onChange); onChange();
    });
    // 区間が1つだけのときは消せないので、押せない「削除」ボタンは出さない(路線ごとの削除と紛らわしいため)
    const del = segments.length > 1
      ? el("button", { class: "ghost small", type: "button", onclick: () => { segments.splice(idx, 1); renderSegmentEditor(container, segments, onChange); onChange(); } }, ["区間を削除"])
      : el("span");
    box.appendChild(el("div", { class: "field line-f" }, [el("label", { for: lineSel.id }, [idx === 0 ? "路線" : `乗継・直通 ${idx}`]), lineSel]));
    box.appendChild(mkSel("from", "から"));
    box.appendChild(mkSel("to", "まで"));
    box.appendChild(del);
    if (isScrambled(seg.line) && MANUAL_PRESETS[seg.line]) {
      const ps = el("select", { id: `${container.id}-preset-${idx}`, "aria-label": "系統" });
      MANUAL_PRESETS[seg.line].forEach((p, i) => ps.appendChild(el("option", { value: String(i) }, [p.label])));
      ps.value = String(seg.preset || 0);
      ps.addEventListener("change", () => { seg.preset = Number(ps.value); renderSegmentEditor(container, segments, onChange); onChange(); });
      box.appendChild(el("div", { class: "field preset" }, [el("label", { for: ps.id }, ["系統(この路線は複数系統が混ざっているため、停車順を系統で決めます)"]), ps]));
    }
    list.appendChild(box);
  });
  container.appendChild(list);
  container.appendChild(el("div", { class: "btn-row" }, [
    el("button", { class: "ghost small", type: "button", onclick: () => {
      const prev = segments[segments.length - 1];
      const prevSt = resolveSegment(prev);
      const last = prevSt[prevSt.length - 1]?.name;
      const line = LINE_NAMES.find(n => n !== prev.line && lineStationList(n).some(s => s.name === last)) || prev.line;
      const st = lineStationList(line);
      segments.push({ line, preset: 0, from: last && st.some(s => s.name === last) ? last : st[0].name, to: st[st.length - 1].name });
      renderSegmentEditor(container, segments, onChange); onChange();
    } }, ["+ 乗継・直通の区間を追加"]),
  ]));
}

// ---- 時刻 ----
// "730" "0730" "7:30" "7.30" "7時30分" "7:30:15" → "07:30" / "07:30:15"
function normTime(raw) {
  if (raw === undefined || raw === null) return "";
  let s = String(raw).trim().replace(/[：．.]/g, ":").replace(/時/g, ":").replace(/分/g, ":").replace(/秒/g, "").replace(/[０-９]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
  s = s.replace(/:+$/, "");
  if (!s) return "";
  let h, m, sec = 0;
  if (/^\d{3,4}$/.test(s)) { h = Number(s.slice(0, -2)); m = Number(s.slice(-2)); }
  else if (/^\d{5,6}$/.test(s)) { h = Number(s.slice(0, -4)); m = Number(s.slice(-4, -2)); sec = Number(s.slice(-2)); }
  else {
    const p = s.split(":");
    if (p.length < 2 || p.length > 3 || p.some(x => !/^\d{1,2}$/.test(x))) return null;
    h = Number(p[0]); m = Number(p[1]); sec = p[2] ? Number(p[2]) : 0;
  }
  if (h > 29 || m > 59 || sec > 59) return null;
  const pad = n => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(m)}` + (sec ? `:${pad(sec)}` : "");
}
function toMin(s) { const n = normTime(s); if (!n) return null; const p = n.split(":").map(Number); return p[0] * 60 + p[1] + (p[2] || 0) / 60; }
function fmtMin(m) { m = ((m % 1440) + 1440) % 1440; const h = Math.floor(m / 60), mm = Math.floor(m % 60), s = Math.round((m * 60) % 60); return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}` + (s && s !== 60 ? `:${String(s).padStart(2, "0")}` : ""); }
// 日付またぎを考慮して単調増加に
function monotonic(arr) { let add = 0, prev = null; return arr.map(v => { if (v === null) return null; if (prev !== null && v + add < prev - 720) add += 1440; prev = v + add; return prev; }); }
// 時刻入力欄: フォーカスが外れたら正規化して色で知らせる
function timeInput(id, value, onSet, cls) {
  const inp = el("input", { type: "text", id, value: value || "", inputmode: "numeric", placeholder: "--:--", class: cls, autocomplete: "off" });
  inp.addEventListener("change", () => {
    const n = normTime(inp.value);
    if (n === null) { inp.classList.add("bad"); onSet(inp.value.trim(), false); return; }
    inp.classList.remove("bad"); inp.value = n; onSet(n, true);
  });
  if (value && normTime(value) === null) inp.classList.add("bad");
  return inp;
}

// ---- 色 ----
function hexToRgb(hex) { const h = hex.replace("#", ""); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); }
function rgbToHex(rgb) { return "#" + rgb.map(v => Math.max(0, Math.min(255, v | 0)).toString(16).padStart(2, "0")).join(""); }

// ---- 保存・コピー・下書き ----
function draftLoad(key) { try { const r = localStorage.getItem(key); return r ? JSON.parse(r) : null; } catch (e) { return null; } }
function draftSave(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* 保存できない環境では何もしない */ } }
function draftClear(key) { try { localStorage.removeItem(key); } catch (e) {} }
let _downloadsNs;
async function getDownloads() {
  if (_downloadsNs !== undefined) return _downloadsNs;
  try { _downloadsNs = (window.claude && typeof window.claude.use === "function") ? await window.claude.use("downloads") : null; }
  catch (e) { _downloadsNs = null; }
  return _downloadsNs;
}
async function copyText(text, statusEl, textarea) {
  try { await navigator.clipboard.writeText(text); setStatus(statusEl, "クリップボードにコピーしました。動画生成ページの「貼り付け」に使えます", "ok"); }
  catch (e) { if (textarea) { textarea.focus(); textarea.select(); } setStatus(statusEl, "自動コピーできなかったので、JSON全体を選択しました。Ctrl+C / ⌘C でコピーしてください", "err"); }
}
async function saveJsonFile(filename, text, statusEl, textarea) {
  const dl = await getDownloads();
  if (!dl) return copyText(text, statusEl, textarea);
  try { await dl.save({ filename, data: text }); setStatus(statusEl, `${filename} を保存しました。configs/ に置いて使えます`, "ok"); }
  catch (e) {
    if (e && e.code === "declined") setStatus(statusEl, "保存をキャンセルしました", "");
    else return copyText(text, statusEl, textarea);
  }
}
function setStatus(elm, msg, cls) { if (!elm) return; elm.textContent = msg; elm.className = "status " + (cls || ""); }
function slugOk(s) { return /^[A-Za-z0-9_-]{1,64}$/.test(s || ""); }

// 出力パネル(JSON表示+保存/コピー/読込)。getConfig() は {config, problems} を返す
function renderOutput(container, filenameFn, getText, onImport) {
  const ta = el("textarea", { class: "json", id: container.id + "-json", readonly: true, spellcheck: "false", "aria-label": "config JSON" });
  const status = el("span", { class: "status", id: container.id + "-status" });
  const imp = el("textarea", { class: "json", id: container.id + "-imp", placeholder: "以前このページで作ったJSONを貼り付け", spellcheck: "false", style: "min-height:90px" });
  const impStatus = el("span", { class: "status" });
  const serverBox = el("div", { id: container.id + "-server" });
  detectServer().then(info => { if (info) renderServerPanel(serverBox, info, getText); });
  container.append(
    serverBox,
    el("div", { class: "btn-row" }, [
      el("button", { type: "button", onclick: () => saveJsonFile(filenameFn(), getText(), status, ta) }, ["JSONを保存"]),
      el("button", { type: "button", class: "ghost", onclick: () => copyText(getText(), status, ta) }, ["コピー"]),
      status,
    ]),
    ta,
    el("details", { class: "imp" }, [
      el("summary", {}, ["作成済みのJSONを読み込む"]),
      el("div", { style: "display:grid;gap:8px;margin-top:8px" }, [imp, el("div", { class: "btn-row" }, [
        el("button", { type: "button", class: "ghost small", onclick: () => {
          try { onImport(JSON.parse(imp.value)); setStatus(impStatus, "読み込みました", "ok"); }
          catch (e) { setStatus(impStatus, "読み込めませんでした: " + e.message, "err"); }
        } }, ["読み込む"]), impStatus])]),
    ]),
  );
  return ta;
}
// 車両アイコン欄: assets/ に置いたPNGのパスを config の icon_path に入れる。
// 「PNGを選ぶ」はファイル名を拾ってプレビューを出すだけ(画像そのものはJSONに入らないので、
// 同じファイルをリポジトリの assets/ に置いておく)。
const _iconPreviews = {};
function iconField(obj, id, onChange) {
  const txt = el("input", { type: "text", id, value: obj.icon_path || "", placeholder: "assets/xxx.png(空欄=標準アイコン)", class: "mono", autocomplete: "off", list: "server-icons" });
  const thumb = el("img", { alt: "", style: "height:28px;max-width:80px;object-fit:contain;display:none;border-radius:4px;background:var(--surface)" });
  const showThumb = () => { const src = _iconPreviews[obj.icon_path]; if (src) { thumb.src = src; thumb.style.display = ""; } else { thumb.removeAttribute("src"); thumb.style.display = "none"; } };
  const norm = (v) => { v = v.trim(); if (v && !v.includes("/")) v = "assets/" + v; return v; };
  txt.addEventListener("change", () => { obj.icon_path = norm(txt.value); txt.value = obj.icon_path; showThumb(); onChange(); });
  const file = el("input", { type: "file", accept: "image/png", id: id + "-file", style: "display:none" });
  file.addEventListener("change", () => {
    const f = file.files && file.files[0]; if (!f) return;
    obj.icon_path = "assets/" + f.name; txt.value = obj.icon_path;
    const rd = new FileReader(); rd.onload = () => { _iconPreviews[obj.icon_path] = rd.result; showThumb(); onChange(); }; rd.readAsDataURL(f);
    onChange();
  });
  const pick = el("button", { type: "button", class: "ghost small", onclick: () => file.click() }, ["PNGを選ぶ"]);
  const clear = el("button", { type: "button", class: "ghost small", onclick: () => { obj.icon_path = ""; txt.value = ""; showThumb(); onChange(); } }, ["標準に戻す"]);
  showThumb();
  return el("div", { class: "field", style: "flex:1 1 100%" }, [
    el("label", { for: id }, ["車両アイコン(assets/ のPNG・右向き推奨)"]),
    txt,
    el("div", { class: "btn-row" }, [pick, clear, thumb, file]),
  ]);
}

// ---- 動画生成サーバー(web/app.py)から開かれているときだけ出す機能 ----
// アーティファクトとして開いたときは /api/server_info に届かないので何も出ない。
async function detectServer() {
  if (!/^https?:$/.test(location.protocol)) return null;
  try {
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 2500);
    const r = await fetch("/api/server_info", { signal: ctl.signal }); clearTimeout(tm);
    if (!r.ok) return null;
    const j = await r.json();
    return j && j.ok ? j : null;
  } catch (e) { return null; }
}
function renderServerPanel(container, info, getText) {
  // アイコン候補(サーバーの assets/ にあるPNG)を入力欄の候補として出す
  let dl = document.getElementById("server-icons");
  if (!dl) { dl = el("datalist", { id: "server-icons" }); document.body.appendChild(dl); }
  dl.innerHTML = ""; (info.icons || []).forEach(p => dl.appendChild(el("option", { value: p })));
  const fast = el("input", { type: "checkbox", id: container.id + "-fast" });
  const status = el("div", { class: "status" });
  const post = async (url) => {
    const body = new URLSearchParams(); body.set("json_text", getText()); if (fast.checked) body.set("fast", "1");
    const r = await fetch(url, { method: "POST", body }); return r.json();
  };
  const poll = (jobId) => fetch("/status/" + jobId).then(r => r.json()).then(job => {
    if (job.status === "running") { setStatus(status, "生成中…(数分かかることがあります)", ""); setTimeout(() => poll(jobId), 3000); }
    else if (job.status === "done") {
      const fname = job.file_name || "output.mp4";
      const name = fname.replace(/\.(mp4|zip)$/, "");
      status.className = "status ok"; status.textContent = "完成しました → ";
      status.appendChild(el("a", { href: "/download/" + encodeURIComponent(name) }, [fname + " をダウンロード"]));
    } else setStatus(status, "エラー: " + (job.error || "不明なエラー"), "err");
  }).catch(() => setTimeout(() => poll(jobId), 4000));
  const saveBtn = el("button", { type: "button", class: "ghost", onclick: async () => {
    try { const j = await post("/save_config"); if (j.error) setStatus(status, j.error, "err"); else setStatus(status, `${j.path} に保存しました`, "ok"); }
    catch (e) { setStatus(status, "保存できませんでした: " + e.message, "err"); }
  } }, ["configsに保存"]);
  const genBtn = el("button", { type: "button", onclick: async () => {
    try { const j = await post("/generate_from_json"); if (j.job_id) { setStatus(status, "生成を開始しました…", ""); poll(j.job_id); } else setStatus(status, j.error || "開始できませんでした", "err"); }
    catch (e) { setStatus(status, "開始できませんでした: " + e.message, "err"); }
  } }, [document.title.includes("カルーセル") ? "保存して画像を生成" : "保存して動画を生成"]);
  container.appendChild(el("div", { class: "subcard" }, [
    el("strong", {}, ["このPCで使う(動画生成サーバーに接続中)"]),
    el("div", { class: "btn-row" }, [saveBtn, genBtn, el("label", { for: fast.id, class: "status" }, [fast, " 低画質プレビュー(高速)"])]),
    status,
    el("p", { class: "hint" }, ["保存先は configs/<ファイル名>.json。生成した動画は ", el("a", { href: "/" }, ["動画生成ページ"]), " からも再生成・ダウンロードできます。"]),
  ]));
}

function renderProblems(container, problems) {
  container.innerHTML = "";
  if (!problems.length) { container.appendChild(el("ul", { class: "problems clear" }, [el("li", {}, ["入力OK。このJSONで動画を生成できます。"])])); return; }
  container.appendChild(el("ul", { class: "problems" }, problems.map(p => el("li", {}, [p]))));
}
