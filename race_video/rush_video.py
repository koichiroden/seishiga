# -*- coding: utf-8 -*-
"""
ネタ①「朝ラッシュと日中で、同じ区間の所要時間はどれだけ変わる?」動画。

同じ路線・同じ駅の並びを、朝ラッシュの列車と日中の列車が「同時にスタート」
したとして走らせる。線路の左右に1本ずつずらして並走させ、
  - 上: 地図(主要駅を通過するとポップアップ。遅い方が通過した時に「+N分」)
  - 下: 2つの時計(実際の時刻が進む)・経過分・進捗バー・差の分数
  - 最後: 所要時間・差・何%増しのRESULT
を描く。

config 例は configs/rush_chuo_tokyo_takao.json を参照。
  "video_type": "rush"
  "stations": [{"line": "中央線快速", "name": "東京", "popup": true}, ...]   # 走る駅の並び
  "runs": [
     {"key": "morning", "label": "朝ラッシュ", "badge": "朝", "train_type": "快速", "color": [235,70,60],
      "times": {"東京": "07:30", "新宿": "07:46", "高尾": "08:41"}},   # 始発駅と終着駅は必須
     {"key": "daytime", "label": "日中", "badge": "昼", "train_type": "中央特快", "color": [70,160,255],
      "times": {"東京": "12:00", "高尾": "12:57"}}
  ]
途中駅の時刻は省略可(距離に比例して自動補完)。

複数路線(最大3路線)を並べて比べるときは、stations / runs を路線ごとに "lines" に入れる
(例: configs/rush_tokaido_yokosuka.json):
  "lines": [
     {"name": "東海道線", "color": [245,130,30], "stations": [...], "runs": [{朝}, {日中}]},
     {"name": "横須賀線", "color": [40,110,220], "stations": [...], "runs": [{朝}, {日中}]}
  ]
runs の color を省略すると、路線色(朝)とそれを明るくした色(日中)になる。
"train_type"(種別: 快速 / 中央特快 / 通勤快速 …)を書くと、車両アイコンの隣に
種別の一文字バッジ(省略時は種別の先頭1文字、"type_badge" で変更可)が出て、
パネル・凡例・RESULTにも種別名が入る。書かなければ従来どおり "badge"(朝/昼)を表示。
"""
from .common import (
    CANVAS_W, CANVAS_H, SAFE_BOTTOM_Y, font, parse_hhmm, fmt_hhmm, monotonic_times,
    build_base_map, build_geometry, PolyPath, draw_trail, draw_train, draw_chip,
    pop_scale, ease_out_cubic, shadow_text, FrameWriter, write_text_script, load_icon,
)
from .geo import project
from .route_builder import load_line_geoms

from PIL import ImageDraw

MAP_BOX = (60, 470, 1020, 1170)
LANE_OFFSET = 15          # 2本のコースを平行移動でずらす量(px)
ICON_OFFSET = 34          # 車両アイコンを平行移動でずらす量(px)
DEFAULT_COLORS = [(235, 70, 60), (70, 160, 255)]
# 複数路線モードで color 省略時に使う路線色(朝=この色、日中=明るくした色)
LINE_PALETTE = [(245, 130, 30), (40, 110, 220), (30, 170, 100)]


class TimedRun:
    """駅ごとの経過分 t(始発=0)と cum_km から、任意の経過分の km を返す。"""

    def __init__(self, cfg, route, idx):
        self.cfg = cfg
        self.key = cfg.get("key", f"run{idx}")
        self.label = cfg.get("label", self.key)
        self.badge = cfg.get("badge", self.label[:1])
        self.train_type = (cfg.get("train_type") or "").strip()
        self.type_badge = (cfg.get("type_badge") or self.train_type[:1] or self.badge)[:1]
        # 「朝ラッシュ(快速)」のような表示名
        self.full_label = f"{self.label}({self.train_type})" if self.train_type else self.label
        self.color = tuple(cfg.get("color") or DEFAULT_COLORS[idx % 2])
        self.icon = load_icon(cfg.get("icon_path"), int(cfg.get("icon_width", 86)))
        times_cfg = cfg.get("times", {})
        names = [s["name"] for s in route["stations"]]
        raw = monotonic_times([parse_hhmm(times_cfg.get(n)) for n in names])
        if raw[0] is None or raw[-1] is None:
            raise ValueError(f"[{self.label}] 始発駅({names[0]})と終着駅({names[-1]})の時刻は必須です")
        self.dep_abs = raw[0]
        kms = [s["cum_km"] for s in route["stations"]]
        # 時刻が無い途中駅は、前後の時刻が分かっている駅の間で距離に比例して補完
        t = [None if v is None else v - self.dep_abs for v in raw]
        known = [i for i, v in enumerate(t) if v is not None]
        for a, b in zip(known, known[1:]):
            for i in range(a + 1, b):
                span = kms[b] - kms[a]
                f = 0.5 if span <= 0 else (kms[i] - kms[a]) / span
                t[i] = t[a] + (t[b] - t[a]) * f
        self.t = t
        self.kms = kms
        self.total_min = t[-1]
        self.total_km = kms[-1] - kms[0]

    def km_at(self, e):
        t, k = self.t, self.kms
        if e <= 0:
            return k[0]
        if e >= t[-1]:
            return k[-1]
        for i in range(len(t) - 1):
            if t[i] <= e <= t[i + 1]:
                if t[i + 1] == t[i]:
                    return k[i]
                return k[i] + (k[i + 1] - k[i]) * (e - t[i]) / (t[i + 1] - t[i])
        return k[-1]

    def speed_kmh(self):
        return self.total_km / (self.total_min / 60) if self.total_min > 0 else 0


def _draw_panel(canvas, runs, e_by_run, t_video, finish_video_t):
    d = ImageDraw.Draw(canvas, "RGBA")
    top = 1195
    row_h = 205
    d.rounded_rectangle([30, top - 10, CANVAS_W - 30, top + row_h * 2 + 5], radius=26, fill=(8, 10, 22, 205),
                        outline=(255, 255, 255, 40), width=2)
    fastest = min(r.total_min for r in runs)
    for i, r in enumerate(runs):
        y = top + i * row_h
        e = e_by_run[r.key]
        done = e >= r.total_min
        # 種別バッジ+ラベル
        d.ellipse([60, y + 20, 112, y + 72], fill=r.color + (255,))
        d.text((86, y + 47), r.badge[:1], font=font(30, "black"), fill=(255, 255, 255, 255), anchor="mm")
        d.text((128, y + 46), r.label, font=font(34, "black"), fill=(255, 255, 255, 255), anchor="lm")
        if r.train_type:
            px = 128 + d.textlength(r.label, font=font(34, "black")) + 16
            f_t = font(26, "black")
            tw = d.textlength(r.train_type, font=f_t)
            d.rounded_rectangle([px, y + 28, px + tw + 24, y + 64], radius=10, fill=r.color + (255,))
            d.text((px + 12 + tw / 2, y + 46), r.train_type, font=f_t, fill=(255, 255, 255, 255), anchor="mm")
        d.text((128, y + 92), f"発 {fmt_hhmm(r.dep_abs)}", font=font(26), fill=(190, 200, 220, 255), anchor="lm")
        # 時計(実際の時刻)
        now_abs = r.dep_abs + min(e, r.total_min)
        clock = fmt_hhmm(now_abs)
        d.text((CANVAS_W - 70, y + 58), clock, font=font(78, "black"),
               fill=(255, 255, 255, 255) if not done else r.color + (255,), anchor="rm")
        # 経過分
        d.text((CANVAS_W - 70, y + 122), f"{min(e, r.total_min):.0f}分経過" + ("  GOAL!" if done else ""),
               font=font(28), fill=(255, 215, 0, 255) if done else (200, 210, 230, 255), anchor="rm")
        # 進捗バー
        bx0, bx1, by = 60, CANVAS_W - 70, y + 168
        d.rounded_rectangle([bx0, by - 11, bx1, by + 11], radius=11, fill=(255, 255, 255, 38))
        frac = min(1.0, e / r.total_min) if r.total_min else 1
        if frac > 0:
            d.rounded_rectangle([bx0, by - 11, bx0 + (bx1 - bx0) * frac, by + 11], radius=11, fill=r.color + (255,))
        # 速い方がゴールした後、遅い方の「+N分」を増やしていく
        if r.total_min > fastest + 1e-6 and e > fastest:
            lag = min(e, r.total_min) - fastest
            d.rounded_rectangle([470, y + 98, 650, y + 146], radius=14, fill=(235, 50, 50, 255))
            d.text((560, y + 122), f"+{lag:.0f}分", font=font(32, "black"), fill=(255, 255, 255, 255), anchor="mm")


def _draw_result(canvas, cfg, runs, alpha):
    d = ImageDraw.Draw(canvas, "RGBA")
    a = int(alpha)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, int(205 * a / 255)))
    slow = max(runs, key=lambda r: r.total_min)
    fast = min(runs, key=lambda r: r.total_min)
    diff = slow.total_min - fast.total_min
    pct = diff / fast.total_min * 100 if fast.total_min else 0
    cx = CANVAS_W / 2
    d.text((cx, 520), "RESULT", font=font(84, "black"), fill=(255, 215, 0, a), anchor="mm")
    if diff < 0.5:
        d.text((cx, 650), "ほぼ同じ所要時間!", font=font(56, "black"), fill=(255, 255, 255, a), anchor="mm")
    else:
        d.text((cx, 640), f"{slow.label}は", font=font(46), fill=(255, 255, 255, a), anchor="mm")
        d.text((cx, 730), f"+{diff:.0f}分 おそい!", font=font(96, "black"), fill=slow.color + (a,), anchor="mm")
        d.text((cx, 830), f"(約{pct:.0f}%増し)", font=font(42), fill=(255, 255, 255, a), anchor="mm")
    y = 960
    for r in runs:
        d.rounded_rectangle([90, y, CANVAS_W - 90, y + 150], radius=24, fill=(20, 24, 40, int(235 * a / 255)),
                            outline=r.color + (a,), width=4)
        d.text((130, y + 48), r.full_label, font=font(38, "black"), fill=r.color + (a,), anchor="lm")
        d.text((130, y + 106), f"{fmt_hhmm(r.dep_abs)} → {fmt_hhmm(r.dep_abs + r.total_min)}",
               font=font(32), fill=(220, 225, 240, a), anchor="lm")
        d.text((CANVAS_W - 130, y + 75), f"{r.total_min:.0f}分", font=font(64, "black"), fill=(255, 255, 255, a),
               anchor="rm")
        y += 180
    note = cfg.get("result_note")
    if note:
        d.text((cx, y + 40), note, font=font(30), fill=(255, 255, 255, a), anchor="mm")


def _lighten(color, f=0.45):
    return tuple(int(c + (255 - c) * f) for c in color)


def _normalize_lines(config):
    """旧形式(トップレベルに stations / runs)と新形式(lines: [...])のどちらも、
    [{"name", "color", "stations", "runs"}] のリストにそろえる。"""
    if config.get("lines"):
        lines = config["lines"]
    else:
        lines = [{"name": config.get("line_label", ""), "color": config.get("color"),
                  "stations": config["stations"], "runs": config["runs"]}]
    out = []
    for li, ln in enumerate(lines):
        base = tuple(ln.get("color") or LINE_PALETTE[li % len(LINE_PALETTE)])
        runs = []
        for ri, rc in enumerate(ln["runs"][:2]):
            rc = dict(rc)
            if not rc.get("color"):
                # 路線色をベースに、朝=そのまま / 日中=明るく
                rc["color"] = list(base if ri == 0 else _lighten(base))
            runs.append(rc)
        if len(runs) != 2:
            raise ValueError(f"[{ln.get('name', '路線')}] runs には2本(朝・日中など)を指定してください")
        out.append({"name": ln.get("name", ""), "color": base, "stations": ln["stations"], "runs": runs})
    return out


class RushLine:
    def __init__(self, idx, ln, line_geoms, station_db):
        self.idx = idx
        self.name = ln["name"]
        self.color = ln["color"]
        self.route, warnings = build_geometry(ln["stations"], line_geoms, station_db, key=f"line{idx}",
                                              color=list(self.color))
        for w in warnings:
            print("[warn]", w)
        self.runs = [TimedRun(rc, self.route, i) for i, rc in enumerate(ln["runs"])]
        for r in self.runs:
            r.key = f"{idx}:{r.key}"
        self.path = PolyPath(self.route, parallel=True)   # 2本のコースは平行移動で並べる(内輪差を出さない)
        slow = max(self.runs, key=lambda r: r.total_min)
        fast = min(self.runs, key=lambda r: r.total_min)
        self.slow, self.fast = slow, fast
        self.diff = slow.total_min - fast.total_min
        self.pct = self.diff / fast.total_min * 100 if fast.total_min else 0


def _draw_panel_multi(canvas, lines, e_by, top):
    """複数路線用のコンパクトなパネル(路線ごとに見出し+2行)。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    row_h, head_h = 74, 46
    bottom = top + len(lines) * (head_h + 2 * row_h) + 10
    d.rounded_rectangle([30, top - 12, CANVAS_W - 30, bottom], radius=26, fill=(8, 10, 22, 215),
                        outline=(255, 255, 255, 40), width=2)
    y = top
    for ln in lines:
        d.rounded_rectangle([56, y + 10, 70, y + 36], radius=4, fill=ln.color + (255,))
        d.text((82, y + 23), ln.name, font=font(30, "black"), fill=(255, 255, 255, 255), anchor="lm")
        y += head_h
        for r in ln.runs:
            e = e_by[r.key]
            done = e >= r.total_min
            d.ellipse([60, y + 6, 100, y + 46], fill=r.color + (255,))
            d.text((80, y + 27), r.badge[:1], font=font(24, "black"), fill=(255, 255, 255, 255), anchor="mm")
            lab = r.label + (f" {r.train_type}" if r.train_type else "")
            d.text((114, y + 26), lab, font=font(26, "black"), fill=(255, 255, 255, 255), anchor="lm")
            d.text((CANVAS_W - 70, y + 26), fmt_hhmm(r.dep_abs + min(e, r.total_min)), font=font(46, "black"),
                   fill=r.color + (255,) if done else (255, 255, 255, 255), anchor="rm")
            info = f"{min(e, r.total_min):.0f}分" + (" GOAL" if done else "")
            d.text((CANVAS_W - 225, y + 28), info, font=font(24), fill=(255, 215, 0, 255) if done else
                   (200, 210, 230, 255), anchor="rm")
            if r is ln.slow and ln.diff > 1e-6 and e > ln.fast.total_min:
                lag = min(e, r.total_min) - ln.fast.total_min
                d.rounded_rectangle([CANVAS_W - 420, y + 8, CANVAS_W - 300, y + 46], radius=12, fill=(235, 50, 50, 255))
                d.text((CANVAS_W - 360, y + 27), f"+{lag:.0f}分", font=font(26, "black"), fill=(255, 255, 255, 255),
                       anchor="mm")
            bx0, bx1, by = 114, CANVAS_W - 70, y + 60
            d.rounded_rectangle([bx0, by - 6, bx1, by + 6], radius=6, fill=(255, 255, 255, 38))
            frac = min(1.0, e / r.total_min) if r.total_min else 1
            if frac > 0:
                d.rounded_rectangle([bx0, by - 6, bx0 + (bx1 - bx0) * frac, by + 6], radius=6, fill=r.color + (255,))
            y += row_h


def _draw_result_multi(canvas, cfg, lines, alpha):
    d = ImageDraw.Draw(canvas, "RGBA")
    a = int(alpha)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, int(205 * a / 255)))
    cx = CANVAS_W / 2
    d.text((cx, 470), "RESULT", font=font(84, "black"), fill=(255, 215, 0, a), anchor="mm")
    worst = max(lines, key=lambda ln: ln.diff)
    d.text((cx, 570), "ラッシュでいちばん遅くなるのは", font=font(38), fill=(255, 255, 255, a), anchor="mm")
    d.text((cx, 650), f"{worst.name}  +{worst.diff:.0f}分", font=font(72, "black"), fill=worst.color + (a,),
           anchor="mm")
    y = 740
    card_h = 210 if len(lines) <= 2 else 168
    for ln in sorted(lines, key=lambda l: -l.diff):
        d.rounded_rectangle([70, y, CANVAS_W - 70, y + card_h - 20], radius=24, fill=(20, 24, 40, int(235 * a / 255)),
                            outline=ln.color + (a,), width=4)
        d.text((105, y + 42), ln.name, font=font(38, "black"), fill=ln.color + (a,), anchor="lm")
        d.text((CANVAS_W - 105, y + 42), f"+{ln.diff:.0f}分 (約{ln.pct:.0f}%増し)", font=font(36, "black"),
               fill=(255, 255, 255, a), anchor="rm")
        yy = y + 92
        for r in ln.runs:
            lab = r.label + (f"({r.train_type})" if r.train_type else "")
            d.text((105, yy), lab, font=font(28), fill=r.color + (a,), anchor="lm")
            d.text((CANVAS_W - 105, yy), f"{fmt_hhmm(r.dep_abs)}→{fmt_hhmm(r.dep_abs + r.total_min)}  "
                   f"{r.total_min:.0f}分", font=font(26), fill=(220, 225, 240, a), anchor="rm")
            yy += 44 if card_h > 180 else 34
        y += card_h
    note = cfg.get("result_note")
    if note:
        d.text((cx, y + 30), note, font=font(30), fill=(255, 255, 255, a), anchor="mm")


def render_rush(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    slug = out_name or config.get("slug", "rush")
    line_geoms = load_line_geoms(geojson_path)
    lines = [RushLine(i, ln, line_geoms, station_db) for i, ln in enumerate(_normalize_lines(config)[:3])]
    multi = len(lines) > 1
    runs = [r for ln in lines for r in ln.runs]
    offsets = [-LANE_OFFSET, LANE_OFFSET]

    # 複数路線のときはパネルが縦に長くなるぶん、地図の領域を上に詰める
    if multi:
        panel_h = len(lines) * (46 + 2 * 74) + 10
        panel_top = SAFE_BOTTOM_Y - 30 - panel_h
        map_box = (MAP_BOX[0], MAP_BOX[1], MAP_BOX[2], panel_top - 40)
    else:
        map_box = MAP_BOX
    lanes, lane_offsets = [], []
    for ln in lines:
        for r, off in zip(ln.runs, offsets):
            lanes.append(dict(ln.route, color=list(r.color)))
            lane_offsets.append(off)
    first_st = lines[0].route["stations"]
    if multi:
        legend = [(ln.name, ln.color) for ln in lines]
    else:
        legend = [(f"{r.full_label} {fmt_hhmm(r.dep_abs)}発", r.color) for r in runs]
    base, proj = build_base_map(
        lanes, map_box, config.get("title_line1", ""), config.get("title_line2", ""), legend=legend,
        geojson_path=geojson_path, start_label=config.get("start_name", first_st[0]["name"]),
        end_label=config.get("end_label", first_st[-1]["name"]), offsets=lane_offsets, parallel=True)
    base.convert("RGB").save(f"{out_dir}/{slug}_base_map.png")

    fps = 10 if fast_preview else int(config.get("fps", 30))
    intro = float(config.get("intro_sec", 1.5))
    result_hold = float(config.get("outro_hold_sec", 4.5 if not multi else 5.5))
    longest = max(r.total_min for r in runs)
    duration = float(config.get("video_duration_sec", 30))
    ratio = max(0.05, (duration - intro - result_hold - 1.0) / longest)   # 動画秒 / 実分
    race_end = intro + longest * ratio
    result_start = race_end + 0.8
    total = result_start + result_hold

    # 駅ポップアップのイベント(路線ごとに: 先に通過した方で駅名、後から通過した方で+N分)
    pop_events = []
    for ln in lines:
        for si, st in enumerate(ln.route["stations"]):
            if not st.get("popup") or si == 0:
                continue
            ts = sorted(((r.t[si], r) for r in ln.runs), key=lambda x: x[0])
            (t_first, r_first), (t_second, r_second) = ts
            pop_events.append((intro + t_first * ratio, "name", st, r_first, None, ln))
            if t_second - t_first >= 0.5:
                pop_events.append((intro + t_second * ratio, "lag", st, r_second, t_second - t_first, ln))

    script = [(0.0, f"{config.get('title_line1', '')}。朝ラッシュと日中で、所要時間はどれだけ変わるのか?")]
    for ln in lines:
        for r in ln.runs:
            script.append((intro, f"{ln.name + 'の' if ln.name else ''}{r.label}の{r.train_type or '列車'}は"
                                  f"{fmt_hhmm(r.dep_abs)}発。"))
    for tv, kind, st, r, lag, ln in pop_events:
        if kind == "lag":
            script.append((tv, f"{st['name']}の時点で、{ln.name + 'の' if ln.name else ''}{r.label}は{lag:.0f}分の遅れ。"))
    for ln in lines:
        script.append((result_start, f"結果、{ln.name + 'は' if ln.name else ''}{ln.slow.label}が{ln.fast.label}より"
                                     f"{ln.diff:.0f}分おそい。"))
    write_text_script(out_dir, slug, sorted(script, key=lambda x: x[0]))

    writer = FrameWriter(f"{out_dir}/{slug}.mp4", fps)
    n_frames = int(total * fps)
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    for fi in range(n_frames):
        tv = fi / fps
        canvas = base.copy()
        e_real = max(0.0, (tv - intro) / ratio)
        e_by = {r.key: min(e_real, r.total_min) for r in runs}
        for ln in lines:
            for r, off in zip(ln.runs, offsets):
                km = r.km_at(e_by[r.key])
                if km > ln.path.cum[0] + 0.01:
                    draw_trail(canvas, ln.path.screen_polyline(proj, off, ln.path.cum[0], km), r.color)
        for ln in reversed(lines):
            for r, side in reversed(list(zip(ln.runs, (-1, 1)))):
                km = r.km_at(e_by[r.key])
                x, y = ln.path.screen_at_km(proj, km, side * ICON_OFFSET)
                draw_train(canvas, x, y, r.color, badge=r.type_badge, icon_img=r.icon,
                           badge_side="left" if side < 0 else "right", scale=0.85 if multi else 1.0)
        for t0, kind, st, r, lag, ln in pop_events:
            el = tv - t0
            if el < 0:
                continue
            x, y = project(proj, st["lon"], st["lat"])
            if kind == "name":
                # 駅名は従来どおりポップアップして消える
                if el <= 1.6:
                    alpha = 255 if el < 1.25 else int(255 * (1.6 - el) / 0.35)
                    draw_chip(canvas, x, y - 18, st["name"], (255, 255, 255), size=32,
                              scale=pop_scale(el), alpha=alpha)
            else:
                # 遅い方が通過した時点の差「朝+1分」は、文字だけで最後まで表示し続ける
                d = ImageDraw.Draw(canvas, "RGBA")
                sc = pop_scale(el) if el < 0.4 else 1.0
                label = f"{r.badge[:1]}+{lag:.0f}分"
                if multi:
                    label = f"{ln.name}{label}" if len(lines) > 1 and ln.name else label
                ty = y + 40 + (ln.idx * 34 if multi else 0)
                d.text((x, ty), label, font=font(max(1, int((26 if multi else 30) * sc)), "black"),
                       fill=tuple(r.color) + (255,), anchor="mm", stroke_width=4, stroke_fill=(5, 8, 18, 255))
        if multi:
            _draw_panel_multi(canvas, lines, e_by, panel_top)
        else:
            _draw_panel(canvas, runs, e_by, tv, race_end)
        if tv >= result_start:
            fade = 255 * ease_out_cubic((tv - result_start) / 0.6)
            if multi:
                _draw_result_multi(canvas, config, lines, fade)
            else:
                _draw_result(canvas, config, runs, fade)
        writer.write(canvas)
    return writer.close()
