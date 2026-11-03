# -*- coding: utf-8 -*-
"""
ブラウザから動画生成を実行するための、ごく簡単なWebラッパー。
自分のPC上でも(`python3 web/app.py`)、Renderなどのホスティング上でも
同じように動く。

- 駅レースビルダー(Claude Artifact)で作ったJSONを貼り付け/ファイル選択で
  読み込み、バックグラウンドで `python3 -m race_video.cli configs/<slug>.json`
  を実行する。
- configs/*.json に保存済みの設定からも生成できる。
- 生成状況はポーリングで確認し、完了したらそのままブラウザから
  mp4をダウンロードできる。
- 出力ファイル名は、config の 'slug'(=保存先ファイル名)とは別に、
  生成のたびに自由に指定できる(空欄ならslugがそのまま使われる)。

ローカルで試す場合:
    pip install -r requirements.txt
    python3 web/app.py
    -> http://127.0.0.1:8080 を開く
"""
import json
import shutil
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file, send_from_directory

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 車両アイコン画像置き場。ここに置いたファイルは、駅レースビルダー側で何かを
# アップロードしなくても、このページ上のドロップダウンから選べるようになる
# (icon_path は "assets/<ファイル名>" という相対パスとして config JSON に入る)。
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
ALLOWED_ICON_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 自分のPCならもっと速く終わるはずだが、Renderの無料プラン(0.1 CPU)などの
# 非力な環境だとフルクオリティ(30fps)の生成に数十分かかることがあるため、
# 安全マージンを広めに取っておく。
JOB_TIMEOUT_SEC = 2700

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OUT_NAME_BAD_RE = re.compile(r"[\\/\x00-\x1f]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # アイコン画像は8MBまで


@app.after_request
def _no_cache(resp):
    # このアプリは自分のPC上でapp.pyを書き換えて再起動する運用なので、
    # ブラウザがページ/JSONを古いままキャッシュしてしまうと「直したのに
    # 反映されない(機能が消えたように見える)」という混乱が起きやすい。
    # そのため常にキャッシュさせない。
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

jobs = {}
jobs_lock = threading.Lock()

# サーバーが生成の途中で再起動されてしまった場合(無料プランのメモリ/CPU制限
# など)、ジョブ情報はメモリ上の jobs dict ごと消えてしまい、ポーリング中の
# ブラウザには "not found" とだけ返ってしまって原因が分かりにくい。
# job_id -> {slug, file_name} の対応表だけは軽量なファイルに書き出しておき、
# jobsから消えていた場合でも「サーバーが再起動した可能性がある」という
# 分かりやすいメッセージと、出力ファイルが実際にできていれば復旧できるように
# しておく。
JOBS_STATE_PATH = OUTPUT_DIR / "_jobs_state.json"
_jobs_state_lock = threading.Lock()


def _load_jobs_state():
    try:
        with open(JOBS_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _remember_job(job_id, slug, file_name):
    with _jobs_state_lock:
        state = _load_jobs_state()
        state[job_id] = {"slug": slug, "file_name": file_name}
        if len(state) > 50:  # 肥大化防止に直近50件だけ残す
            for k in list(state.keys())[:-50]:
                state.pop(k, None)
        try:
            with open(JOBS_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass


def _sanitize_out_name(raw):
    """出力ファイル名(拡張子なし)として安全かどうかを確認する。
    戻り値は (name, error) のタプル。
    空欄は「指定なし(configのslugを使う)」という意味で ("", None) を返す。
    使えない文字が含まれる場合は (None, エラーメッセージ) を返す。"""
    name = (raw or "").strip()
    if not name:
        return "", None
    if len(name) > 100 or _OUT_NAME_BAD_RE.search(name) or ".." in name or name.strip(".") == "":
        return None, "出力ファイル名に使えない文字が含まれています(/ や \\、'..' は使えません)"
    return name, None


def list_configs():
    items = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            title = f"{cfg.get('title_line1', p.stem)} {cfg.get('title_line2', '')}".strip()
        except Exception:
            title = p.stem
        items.append({"slug": p.stem, "title": title})
    return items


def list_icon_assets():
    """assets/ にある画像ファイル名の一覧(ファイル名のみ、パスなし)を返す。"""
    items = []
    for p in sorted(ASSETS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in ALLOWED_ICON_EXT:
            items.append(p.name)
    return items


def run_job(job_id, slug, fast, out_name=None):
    cfg_path = CONFIGS_DIR / f"{slug}.json"
    file_stem = out_name or slug
    cmd = ["python3", "-m", "race_video.cli", str(cfg_path)]
    if fast:
        cmd.append("--fast")
    if out_name:
        cmd += ["--out-name", out_name]
    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=JOB_TIMEOUT_SEC,
        )
        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        with jobs_lock:
            jobs[job_id]["log"] = log[-4000:]
            out_dir = OUTPUT_DIR / file_stem      # carousel は output/<名前>/ に画像を書き出す
            if proc.returncode == 0 and out_dir.is_dir():
                # 画像6枚+caption.txt を1つのzipにまとめてダウンロードできるようにする
                shutil.make_archive(str(OUTPUT_DIR / file_stem), "zip", root_dir=str(out_dir))
            if proc.returncode == 0 and ((OUTPUT_DIR / f"{file_stem}.mp4").exists()
                                         or (OUTPUT_DIR / f"{file_stem}.zip").exists()):
                jobs[job_id]["status"] = "done"
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"終了コード {proc.returncode}(ログを参照)"
    except subprocess.TimeoutExpired as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["log"] = ((e.stdout or "") + "\n" + (e.stderr or ""))[-4000:]
            jobs[job_id]["error"] = (
                f"{JOB_TIMEOUT_SEC}秒以内に終わりませんでした。"
                f"CPUが非力な環境だと、フルクオリティの生成は間に合わないことがあります。"
                f"「低画質プレビュー(高速)」を試すか、より速いPC/有料プランを検討してください。"
            )
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


def _output_ext(slug):
    """carousel(画像セット)は .zip、それ以外の動画は .mp4"""
    try:
        with open(CONFIGS_DIR / f"{slug}.json", encoding="utf-8") as f:
            return ".zip" if json.load(f).get("video_type") == "carousel" else ".mp4"
    except (OSError, ValueError, AttributeError):
        return ".mp4"


def _start_job(slug, fast, out_name_raw):
    """out_nameのバリデーション込みでジョブを開始する共通処理。
    戻り値は (response_dict, http_status)。"""
    out_name, err = _sanitize_out_name(out_name_raw)
    if err:
        return {"error": err}, 400
    file_name = f"{out_name or slug}{_output_ext(slug)}"
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "running", "slug": slug, "file_name": file_name, "log": "", "error": ""}
    _remember_job(job_id, slug, file_name)
    threading.Thread(target=run_job, args=(job_id, slug, fast, out_name or None), daemon=True).start()
    return {"job_id": job_id, "slug": slug, "file_name": file_name}, 200


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>電車最速バトル 動画生成</title>
<style>
  body { font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #201c16; background: #f6f2ea; }
  h1 { font-size: 22px; }
  h2.section { font-size: 15px; margin: 28px 0 10px; }
  .note { font-size: 12.5px; color: #a3690a; background: #f8ecd6; border-radius: 7px;
          padding: 8px 10px; margin: 0 0 16px; }
  .card { background: #fff; border: 1px solid #ded2ba; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 16px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .title { font-weight: 600; }
  .slug { color: #766c5c; font-size: 12.5px; font-family: monospace; }
  button { font: inherit; padding: 8px 14px; border-radius: 7px; border: 1px solid #201c16;
           background: #201c16; color: #f6f2ea; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  label.fast { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 4px; }
  .status { font-size: 13px; margin-top: 8px; color: #766c5c; white-space: pre-wrap; }
  .status.ok { color: #21785a; }
  .status.err { color: #b23b3b; }
  a.dl { color: #1a56c9; font-weight: 600; text-decoration: none; }
  .empty { color: #9c907c; }
  textarea.jsonbox { width: 100%; min-height: 160px; font-family: monospace; font-size: 12.5px;
                      padding: 10px; border-radius: 7px; border: 1px solid #ded2ba; box-sizing: border-box;
                      background: #fbf8f2; color: #201c16; }
  .paste-actions { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
  input.outname { font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px;
                   border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; width: 200px; }
  select.config-picker { font: inherit; font-size: 12.5px; padding: 6px 8px; border-radius: 6px;
                          border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; }
  label.outname-label { font-size: 12.5px; color: #766c5c; display: flex; align-items: center; gap: 6px; }
  input[type="file"] { font-size: 12.5px; }
  .icon-picker { margin-top: 12px; border-top: 1px dashed #ded2ba; padding-top: 4px; }
  .icon-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; flex-wrap: wrap; }
  .icon-row .rname { font-size: 13px; font-weight: 600; min-width: 110px; }
  .icon-row select { font: inherit; font-size: 12.5px; padding: 5px 6px; border-radius: 6px;
                      border: 1px solid #ded2ba; background: #fbf8f2; color: #201c16; }
  .icon-row img.thumb { width: 34px; height: 34px; border-radius: 6px; object-fit: contain;
                         background: #f0ead9; border: 1px solid #ded2ba; }
  .icon-row label.upload-lbl { font-size: 11.5px; color: #1a56c9; cursor: pointer; text-decoration: underline; }
  .icon-row input[type="file"] { display: none; }
</style>
</head>
<body>
  <h1>電車最速バトル — 動画生成</h1>
  <p class="note">config を作るツール: <a href="/builders">/builders</a>(朝ラッシュ・運賃・抜かれ回数・駅レース)。スマホなど別の端末からは <span class="slug">http://このPCのIPアドレス:8080/builders</span> で開けます。</p>
  <p class="note">自分のPCで動かす場合はフルクオリティでも数分で終わります。CPUが非力な環境(無料ホスティングなど)では、まず「低画質プレビュー(高速)」をおすすめします。</p>

  <h2 class="section">① JSONを用意して生成</h2>
  <div class="card">
    <p style="margin-top:0; font-size:13px; color:#766c5c;">
      駅レースビルダーで作ったconfig JSONを、<span class="slug">configs/</span>から選ぶか、ファイルから読み込むか、貼り付けて生成できます
      (新規の場合は <span class="slug">configs/&lt;slug&gt;.json</span> として保存されます)。
    </p>
    <div class="paste-actions" style="margin-top:0; margin-bottom:8px;">
      <select class="config-picker" id="config-picker">
        <option value="">— configs/ から選んで読み込む —</option>
        {% for c in configs %}
        <option value="{{ c.slug }}">{{ c.title }} ({{ c.slug }}.json)</option>
        {% endfor %}
      </select>
      <input type="file" id="file-input" accept=".json,application/json">
      <button id="clip-btn" type="button">📋 クリップボードから貼り付け</button>
      <span class="status" id="clip-status" style="margin-top:0;"></span>
    </div>
    <textarea class="jsonbox" id="paste-json" placeholder='{"slug": "...", "routes": [...] }'></textarea>

    <div class="icon-picker" id="icon-picker">
      <p class="empty" style="margin:10px 0 0; font-size:12.5px;">JSONを読み込むと、ここでルートごとに車両アイコンを選べます(未指定ならデフォルトのアイコンを使用します)。</p>
    </div>

    <div class="paste-actions">
      <label class="fast"><input type="checkbox" id="paste-fast" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" id="paste-outname" placeholder="省略時はslugを使用">
      </label>
      <button id="paste-btn">保存して生成する</button>
    </div>
    <div class="status" id="paste-status"></div>
  </div>

  <h2 class="section">② 保存済みの設定から生成</h2>
  {% if not configs %}
    <p class="empty">configs/ にJSONファイルが見つかりません。</p>
  {% endif %}
  {% for c in configs %}
  <div class="card" data-slug="{{ c.slug }}">
    <div class="row">
      <div>
        <div class="title">{{ c.title }}</div>
        <div class="slug">{{ c.slug }}.json</div>
      </div>
    </div>
    <div class="paste-actions">
      <label class="fast"><input type="checkbox" class="fast-cb" checked> 低画質プレビュー(高速)</label>
      <label class="outname-label">出力ファイル名(任意)
        <input type="text" class="outname" placeholder="省略時は {{ c.slug }} を使用">
      </label>
      <button class="gen-btn">生成する</button>
    </div>
    <div class="status"></div>
  </div>
  {% endfor %}

<script>
let knownIcons = {{ icons|tojson }};
let iconOverrides = {};  // ルートのindex -> icon_path("" ならデフォルトアイコン)

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// アイコンを差し替えられる対象(=車両)の一覧。video_type ごとに置き場所が違う:
//   race / fare : routes[]   rush : runs[]   overtake : local + passers[]
//   rush(複数路線) : lines[].runs[]
const iconLabels = new WeakMap();  // 一覧に出す名前(configには書き込まない)
function iconTargets(cfg) {
  if (!cfg || typeof cfg !== "object") return null;
  if (Array.isArray(cfg.routes)) return cfg.routes;
  if (Array.isArray(cfg.lines)) {
    const out = [];
    cfg.lines.forEach(l => (Array.isArray(l.runs) ? l.runs : []).forEach(r => {
      iconLabels.set(r, [l.name, r.label, r.train_type ? `(${r.train_type})` : ""].filter(Boolean).join(" "));
      out.push(r);
    }));
    return out;
  }
  if (Array.isArray(cfg.runs)) return cfg.runs;
  if (cfg.local) {
    const list = [cfg.local].concat(Array.isArray(cfg.passers) ? cfg.passers : []);
    list.forEach((t, i) => iconLabels.set(t, i === 0 ? `${t.name || "普通"}(主役)` : `${t.name || "列車"} ${i}本目`));
    return list;
  }
  return null;
}

function routesFromTextarea() {
  try {
    return iconTargets(JSON.parse(document.getElementById("paste-json").value));
  } catch (e) { /* JSONが不完全な間は何もしない */ }
  return null;
}

function setThumb(imgEl, val) {
  if (val) { imgEl.src = "/" + val; imgEl.style.display = ""; }
  else { imgEl.removeAttribute("src"); imgEl.style.display = "none"; }
}

function renderIconPicker() {
  const wrap = document.getElementById("icon-picker");
  const routes = routesFromTextarea();
  if (!routes || !routes.length) {
    wrap.innerHTML = '<p class="empty" style="margin:10px 0 0; font-size:12.5px;">JSONを読み込むと、ここでルートごとに車両アイコンを選べます(未指定ならデフォルトのアイコンを使用します)。</p>';
    return;
  }
  wrap.innerHTML = "";
  routes.forEach((r, i) => {
    const current = Object.prototype.hasOwnProperty.call(iconOverrides, i) ? iconOverrides[i] : (r.icon_path || "");
    const row = document.createElement("div");
    row.className = "icon-row";

    const name = document.createElement("span");
    name.className = "rname";
    name.textContent = iconLabels.get(r) || r.name || r.label || r.short_name || ("ルート" + (i + 1));

    const thumb = document.createElement("img");
    thumb.className = "thumb";
    setThumb(thumb, current);

    const select = document.createElement("select");
    select.appendChild(new Option("デフォルトのアイコンを使用", ""));
    knownIcons.forEach(fn => select.appendChild(new Option(fn, "assets/" + fn)));
    const currentFile = current.replace(/^assets\//, "");
    if (current && !knownIcons.includes(currentFile)) {
      select.appendChild(new Option(currentFile + "(現在の設定)", current));
    }
    select.value = current;
    select.addEventListener("change", () => {
      iconOverrides[i] = select.value;
      setThumb(thumb, select.value);
    });

    const uploadLbl = document.createElement("label");
    uploadLbl.className = "upload-lbl";
    uploadLbl.textContent = "+ 新しい画像をアップロード";
    const fileInp = document.createElement("input");
    fileInp.type = "file";
    fileInp.accept = "image/*";
    uploadLbl.appendChild(fileInp);
    fileInp.addEventListener("change", async () => {
      const f = fileInp.files && fileInp.files[0];
      if (!f) return;
      uploadLbl.textContent = "アップロード中…";
      uploadLbl.appendChild(fileInp);
      const fd = new FormData();
      fd.append("icon", f);
      try {
        const res = await fetch("/upload_icon", { method: "POST", body: fd });
        const data = await res.json();
        if (data.icon_path) {
          if (!knownIcons.includes(data.filename)) knownIcons.push(data.filename);
          iconOverrides[i] = data.icon_path;
          renderIconPicker();
        } else {
          alert(data.error || "アップロードに失敗しました");
          uploadLbl.textContent = "+ 新しい画像をアップロード";
          uploadLbl.appendChild(fileInp);
        }
      } catch (e) {
        alert("アップロードに失敗しました");
        uploadLbl.textContent = "+ 新しい画像をアップロード";
        uploadLbl.appendChild(fileInp);
      }
    });

    row.append(name, thumb, select, uploadLbl);
    wrap.appendChild(row);
  });
}

function pollJob(jobId, statusEl, btn, doneLabel) {
  fetch("/status/" + jobId).then(r => r.json()).then(job => {
    if (job.status === "running") {
      statusEl.className = "status";
      statusEl.textContent = "生成中…(CPUが非力な環境だと数分~十数分かかることがあります)";
      setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 3000);
    } else if (job.status === "done") {
      const fname = job.file_name || "output.mp4";
      statusEl.className = "status ok";
      statusEl.innerHTML = "完成しました → <a class='dl' href='/download/" +
        encodeURIComponent(fname.replace(/\\.(mp4|zip)$/, "")) + "'>" + fname + " をダウンロード</a>";
      btn.disabled = false;
      btn.textContent = doneLabel;
    } else {
      statusEl.className = "status err";
      statusEl.textContent = "エラー: " + (job.error || "不明なエラー") + "\\n\\n" + (job.log || "");
      btn.disabled = false;
      btn.textContent = "再試行";
    }
  }).catch(() => setTimeout(() => pollJob(jobId, statusEl, btn, doneLabel), 4000));
}

document.querySelectorAll(".card[data-slug]").forEach(card => {
  const slug = card.dataset.slug;
  const btn = card.querySelector(".gen-btn");
  const statusEl = card.querySelector(".status");
  const fastCb = card.querySelector(".fast-cb");
  const outnameInp = card.querySelector(".outname");

  btn.addEventListener("click", () => {
    btn.disabled = true;
    btn.textContent = "生成中…";
    statusEl.className = "status";
    statusEl.textContent = "開始しています…";
    const body = new URLSearchParams();
    if (fastCb.checked) body.set("fast", "1");
    if (outnameInp.value.trim()) body.set("out_name", outnameInp.value.trim());
    fetch("/generate/" + slug, { method: "POST", body })
      .then(r => r.json())
      .then(data => {
        if (data.job_id) pollJob(data.job_id, statusEl, btn, "もう一度生成する");
        else { statusEl.className = "status err"; statusEl.textContent = data.error || "開始できませんでした"; btn.disabled = false; btn.textContent = "生成する"; }
      });
  });
});

document.getElementById("paste-json").addEventListener("input", debounce(renderIconPicker, 400));

document.getElementById("config-picker").addEventListener("change", async (e) => {
  const slug = e.target.value;
  const clipStatus = document.getElementById("clip-status");
  if (!slug) return;
  clipStatus.className = "status";
  clipStatus.textContent = "読み込んでいます…";
  try {
    const res = await fetch("/config_json/" + encodeURIComponent(slug));
    const text = await res.text();
    if (!res.ok) throw new Error(text);
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = slug + ".json を読み込みました";
    iconOverrides = {};
    renderIconPicker();
  } catch (err) {
    clipStatus.className = "status err";
    clipStatus.textContent = "読み込みに失敗しました";
  }
});

document.getElementById("file-input").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const clipStatus = document.getElementById("clip-status");
  file.text().then(text => {
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = file.name + " を読み込みました";
    iconOverrides = {};
    renderIconPicker();
  }).catch(() => {
    clipStatus.className = "status err";
    clipStatus.textContent = "ファイルの読み込みに失敗しました";
  });
});

document.getElementById("clip-btn").addEventListener("click", async () => {
  const clipStatus = document.getElementById("clip-status");
  clipStatus.className = "status";
  clipStatus.textContent = "";
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("unsupported");
    }
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) {
      clipStatus.className = "status err";
      clipStatus.textContent = "クリップボードが空でした";
      return;
    }
    document.getElementById("paste-json").value = text;
    clipStatus.className = "status ok";
    clipStatus.textContent = "貼り付けました";
    iconOverrides = {};
    renderIconPicker();
  } catch (e) {
    clipStatus.className = "status err";
    clipStatus.textContent = "自動で読み取れませんでした。テキストエリアを選んで手動で貼り付けて(Ctrl+V / Cmd+V)ください";
  }
});

document.getElementById("paste-btn").addEventListener("click", () => {
  const btn = document.getElementById("paste-btn");
  const statusEl = document.getElementById("paste-status");
  let text = document.getElementById("paste-json").value;
  try {
    const cfg = JSON.parse(text);
    const targets = iconTargets(cfg);
    if (targets) {
      targets.forEach((r, i) => {
        if (Object.prototype.hasOwnProperty.call(iconOverrides, i)) {
          if (iconOverrides[i]) r.icon_path = iconOverrides[i];
          else delete r.icon_path;
        }
      });
      text = JSON.stringify(cfg);
    }
  } catch (e) { /* JSON構文エラーはこの後のサーバー側チェックに任せる */ }
  const fast = document.getElementById("paste-fast").checked;
  const outName = document.getElementById("paste-outname").value.trim();
  btn.disabled = true;
  btn.textContent = "保存しています…";
  statusEl.className = "status";
  statusEl.textContent = "";
  const body = new URLSearchParams();
  body.set("json_text", text);
  if (fast) body.set("fast", "1");
  if (outName) body.set("out_name", outName);
  fetch("/generate_from_json", { method: "POST", body })
    .then(r => r.json())
    .then(data => {
      if (data.job_id) {
        btn.textContent = "生成中…";
        pollJob(data.job_id, statusEl, btn, "もう一度保存して生成する");
      } else {
        statusEl.className = "status err";
        statusEl.textContent = data.error || "開始できませんでした";
        btn.disabled = false;
        btn.textContent = "保存して生成する";
      }
    });
});
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, configs=list_configs(), icons=list_icon_assets())


@app.route("/assets/<path:filename>")
def serve_asset(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/upload_icon", methods=["POST"])
def upload_icon():
    f = request.files.get("icon")
    if not f or not f.filename:
        return jsonify({"error": "ファイルが選択されていません"}), 400
    orig = Path(f.filename)
    ext = orig.suffix.lower()
    if ext not in ALLOWED_ICON_EXT:
        return jsonify({"error": "対応していない形式です(png / jpg / jpeg / webp / gif のみ)"}), 400
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", orig.stem).strip("_")[:60] or "icon"
    dest = ASSETS_DIR / f"{stem}{ext}"
    n = 1
    while dest.exists():
        dest = ASSETS_DIR / f"{stem}_{n}{ext}"
        n += 1
    f.save(dest)
    return jsonify({"filename": dest.name, "icon_path": f"assets/{dest.name}"})


@app.route("/config_json/<slug>")
def config_json(slug):
    valid = {c["slug"] for c in list_configs()}
    if slug not in valid:
        return jsonify({"error": "そのconfigは見つかりません"}), 404
    path = CONFIGS_DIR / f"{slug}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return data, 200, {"Content-Type": "application/json; charset=utf-8"}


@app.route("/generate/<slug>", methods=["POST"])
def generate(slug):
    valid = {c["slug"] for c in list_configs()}
    if slug not in valid:
        return jsonify({"error": "そのconfigは見つかりません"}), 404
    fast = request.form.get("fast") == "1"
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


def _parse_and_save_config(raw):
    """JSON文字列を検証して configs/<slug>.json に保存する。
    戻り値: (slug, None) または (None, (エラー応答, ステータス))"""
    if not raw.strip():
        return None, (jsonify({"error": "JSONを貼り付けてください"}), 400)
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, (jsonify({"error": f"JSONの構文エラー: {e}"}), 400)
    vtype = config.get("video_type", "race") if isinstance(config, dict) else None
    required = {"race": ["routes"], "fare": ["routes"], "rush": ["lines", "stations"],
                "overtake": ["stations"], "carousel": ["routes"]}.get(vtype)
    if not isinstance(config, dict) or required is None:
        return None, (jsonify({"error": "configオブジェクトではないか、未対応の video_type です"}), 400)
    if not any(config.get(k) for k in required):
        need = "' または '".join(required)
        return None, (jsonify({"error": f"'{need}' を含むconfigではないようです(video_type={vtype})"}), 400)
    slug = str(config.get("slug", "")).strip()
    if not slug:
        return None, (jsonify({"error": "configに 'slug' が必要です"}), 400)
    if not _SLUG_RE.match(slug):
        return None, (jsonify({"error": "slugは英数字とハイフン・アンダースコアのみ使えます(64文字以内)"}), 400)
    try:
        with open(CONFIGS_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return None, (jsonify({"error": f"保存に失敗しました: {e}"}), 500)
    return slug, None


# ---- config作成ツール(ビルダー)をこのサーバーからも開けるようにする --------------
# スマホや別のPCから http://<このPCのIP>:8080/builders を開けば、Claudeのアーティファクト
# と同じビルダーが使え、さらに「configsに保存」「保存して動画を生成」ボタンが出る。
TOOLS_DIR = BASE_DIR / "tools"
BUILDERS = {
    "race": ("config_builder.html", "電車最速バトル(駅レースビルダー)"),
    "rush": ("rush_builder.html", "朝ラッシュ vs 日中"),
    "fare": ("fare_builder.html", "運賃くらべ"),
    "overtake": ("overtake_builder.html", "普通列車 抜かれ回数"),
    "carousel": ("carousel_builder.html", "路線くらべカルーセル投稿(画像)"),
}
_SKELETON_HEAD = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                  '</head><body>')


@app.route("/builders")
def builders_index():
    items = "".join(
        f'<li><a href="/builder/{k}">{label}</a> <span>({fn})</span></li>' for k, (fn, label) in BUILDERS.items()
        if (TOOLS_DIR / fn).exists())
    html = (_SKELETON_HEAD + "<title>config作成ツール</title><style>body{font-family:sans-serif;max-width:640px;"
            "margin:32px auto;padding:0 16px;line-height:1.8}li{margin:6px 0}span{color:#888;font-size:12px}</style>"
            "<h1>config作成ツール</h1><p>作ったconfigは、各ツールの「configsに保存」「保存して動画を生成」で"
            "このPCに直接保存・生成できます。</p><ul>" + items +
            '</ul><p><a href="/">← 動画生成ページへ</a></p></body></html>')
    return html


@app.route("/builder/<name>")
def builder_page(name):
    if name not in BUILDERS:
        return "そのツールはありません", 404
    path = TOOLS_DIR / BUILDERS[name][0]
    if not path.exists():
        return f"{path.name} が見つかりません", 404
    body = path.read_text(encoding="utf-8")
    return _SKELETON_HEAD + body + "</body></html>", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/server_info")
def server_info():
    """ビルダーが「このサーバーから開かれているか」を判定するのに使う。"""
    return jsonify({"ok": True, "icons": [f"assets/{n}" for n in list_icon_assets()],
                    "configs": [c["slug"] for c in list_configs()]})


@app.route("/save_config", methods=["POST"])
def save_config():
    slug, err = _parse_and_save_config(request.form.get("json_text", ""))
    if err:
        return err
    return jsonify({"slug": slug, "path": f"configs/{slug}.json"})


@app.route("/generate_from_json", methods=["POST"])
def generate_from_json():
    raw = request.form.get("json_text", "")
    if not raw.strip():
        return jsonify({"error": "JSONを貼り付けてください"}), 400
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSONの構文エラー: {e}"}), 400

    # video_type: race(既定) / fare は 'routes'、rush / overtake は 'stations' が必須
    vtype = config.get("video_type", "race") if isinstance(config, dict) else None
    # (rush は1路線の 'stations' 形式と、複数路線の 'lines' 形式のどちらでもよい)
    required = {"race": ["routes"], "fare": ["routes"], "rush": ["lines", "stations"],
                "overtake": ["stations"], "carousel": ["routes"]}.get(vtype)
    if not isinstance(config, dict) or required is None:
        return jsonify({"error": "configオブジェクトではないか、未対応の video_type です"}), 400
    if not any(config.get(k) for k in required):
        need = "' または '".join(required)
        return jsonify({"error": f"'{need}' を含むconfigではないようです(video_type={vtype})"}), 400

    slug = str(config.get("slug", "")).strip()
    if not slug:
        return jsonify({"error": "configに 'slug' が必要です"}), 400
    if not _SLUG_RE.match(slug):
        return jsonify({"error": "slugは英数字とハイフン・アンダースコアのみ使えます(64文字以内)"}), 400

    cfg_path = CONFIGS_DIR / f"{slug}.json"
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return jsonify({"error": f"保存に失敗しました: {e}"}), 500

    fast = request.form.get("fast") == "1"
    data, status_code = _start_job(slug, fast, request.form.get("out_name", ""))
    return jsonify(data), status_code


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job:
        return jsonify(job)

    # jobsに無い = 生成中にプロセスが再起動された可能性がある(メモリ/CPU制限
    # など)。job_id -> {slug, file_name} の対応表(ファイル)が残っていれば、
    # 出力ファイルの有無から状況を推測して分かりやすいメッセージを返す。
    rec = _load_jobs_state().get(job_id)
    if rec:
        file_name = rec.get("file_name") or f"{rec['slug']}.mp4"
        if (OUTPUT_DIR / file_name).exists():
            return jsonify({"status": "done", "slug": rec["slug"], "file_name": file_name, "log": "", "error": ""})
        return jsonify({
            "status": "error", "slug": rec["slug"], "file_name": file_name, "log": "",
            "error": (
                "生成の途中でサーバーが再起動された可能性があります"
                "(メモリ・CPU制限によるものと考えられます)。"
                "お手数ですが、もう一度「低画質プレビュー(高速)」で試してみてください。"
            ),
        })
    return jsonify({"error": "not found"}), 404


@app.route("/download/<name>")
def download(name):
    for ext in (".mp4", ".zip"):
        path = OUTPUT_DIR / f"{name}{ext}"
        if path.exists():
            return send_file(path, as_attachment=True, download_name=f"{name}{ext}")
    return "まだ生成されていません", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
