# -*- coding: utf-8 -*-
"""
ネタ③「普通列車は、目的地に着くまでに何本に抜かれる?」動画。

1本の普通列車(各駅で停車=止まる動作あり)と、後ろから来る優等列車
(快特・特急・急行…)を同じ線路の上で走らせる。
  - 車両アイコンの隣に種別一文字(普/快/特/急 …)のバッジ
  - 駅では減速→停車→加速(台形の速度パターン)
  - 抜かれた瞬間に「抜かれた!」+ カウンターが増える
  - 待避で長く止まる駅には「◯◯で待避」
  - 画面下に時間-距離のダイヤグラム(スジ)をリアルタイムで描く
  - 最後に「N本に抜かれた!」のRESULT

config 例は configs/overtake_keikyu_shinagawa_yokohama.json を参照。
  "video_type": "overtake"
  "stations": [{"line": "京急本線", "name": "品川", "popup": true}, ...]   # 普通列車が走る駅の並び
  "local":   {"name": "普通", "badge": "普", "color": [...], "default_dwell_sec": 30,
              "times": {"品川": "10:00", "北品川": "10:02", "平和島": {"arr": "10:09", "dep": "10:13"}, ...}}
  "passers": [{"name": "快特", "badge": "快", "color": [...],
               "times": {"品川": "10:04", "京急川崎": {"arr": "10:12", "dep": "10:13"}, "横浜": "10:21"}}, ...]
時刻の書き方:
  普通列車  : "10:02" = その駅の発車時刻(到着は default_dwell_sec 秒前とみなす)。始発は発車、終着は到着。
              {"arr": "...", "dep": "..."} で到着・発車を個別に指定(待避で長く止まる駅など)。
  優等列車  : "10:05" = 通過時刻(始発駅なら発車、終着駅なら到着)。
              {"arr": "...", "dep": "..."} または ["到着", "発車"] で停車。
  時刻の無い駅は前後の時刻から距離比例で補完されます(停車扱いにはなりません)。
"""
from PIL import ImageDraw

from .common import (
    CANVAS_W, CANVAS_H, font, parse_hhmm, fmt_hhmm, build_base_map, build_geometry, PolyPath,
    draw_trail, draw_train, draw_chip, pop_scale, ease_out_cubic, FrameWriter, write_text_script, load_icon,
)
from .geo import project
from .route_builder import load_line_geoms

MAP_BOX = (60, 560, 1020, 1120)
DIA_BOX = (150, 1180, 1030, 1630)
LOCAL_OFFSET = -13
PASSER_OFFSET = 13
ICON_SIDE = 34
PASSER_PALETTE = [(230, 40, 40), (255, 150, 30), (40, 170, 90), (150, 90, 240), (240, 80, 170)]


def _trap(f, ease_in, ease_out, a=0.25):
    """台形速度パターンでの 0..1 の進み具合(駅に止まる側だけ加減速する)。"""
    ai = a if ease_in else 0.0
    ao = a if ease_out else 0.0
    total = 1 - ai / 2 - ao / 2
    if ai and f < ai:
        s = f * f / (2 * ai)
    elif f <= 1 - ao:
        s = ai / 2 + (f - ai)
    else:
        g = 1 - f
        s = total - g * g / (2 * ao)
    return s / total


class Train:
    def __init__(self, cfg, route, is_local, idx, default_dwell_sec=30):
        self.cfg = cfg
        self.is_local = is_local
        self.name = cfg.get("name", "普通" if is_local else f"列車{idx + 1}")
        self.badge = cfg.get("badge", self.name[:1])
        self.color = tuple(cfg.get("color") or ((90, 200, 255) if is_local else PASSER_PALETTE[idx % 5]))
        self.icon = load_icon(cfg.get("icon_path"), int(cfg.get("icon_width", 86)))
        times = cfg.get("times", {})
        sts = route["stations"]
        names = [s["name"] for s in sts]
        unknown = [n for n in times if n not in names]
        if unknown:
            raise ValueError(f"[{self.name}] 駅の並びに無い駅名があります: {unknown}")
        dwell = default_dwell_sec / 60.0
        kf = []   # (t, km, stop, station_index)
        given = [i for i, n in enumerate(names) if n in times]
        if not given:
            raise ValueError(f"[{self.name}] 時刻が1つもありません")
        last_i = given[-1] if not is_local else len(names) - 1
        first_i = given[0]
        if is_local and (names[0] not in times or names[-1] not in times):
            raise ValueError(f"[{self.name}] 普通列車は始発駅と終着駅の時刻が必須です")
        for i in given:
            v = times[names[i]]
            km = sts[i]["cum_km"]
            if isinstance(v, dict):
                arr, dep = parse_hhmm(v.get("arr") or v.get("dep")), parse_hhmm(v.get("dep") or v.get("arr"))
                stop = True
            elif isinstance(v, (list, tuple)):
                arr, dep = parse_hhmm(v[0]), parse_hhmm(v[-1])
                stop = True
            else:
                t = parse_hhmm(v)
                if is_local and i not in (first_i, last_i):
                    arr, dep, stop = t - dwell, t, True
                else:
                    arr = dep = t
                    stop = is_local or i in (first_i, last_i)
            if i == first_i:
                kf.append((dep, km, True, i))
            elif i == last_i:
                kf.append((arr, km, True, i))
            elif stop:
                kf.append((arr, km, True, i))
                kf.append((dep, km, True, i))
            else:
                kf.append((arr, km, False, i))
        # 日付またぎ対策
        fixed, add = [], 0.0
        for j, (t, km, s, i) in enumerate(kf):
            if fixed and t + add < fixed[-1][0] - 12 * 60:
                add += 24 * 60
            fixed.append((t + add, km, s, i))
        for a, b in zip(fixed, fixed[1:]):
            if b[0] < a[0]:
                raise ValueError(f"[{self.name}] {names[a[3]]}→{names[b[3]]} で時刻が逆転しています")
        self.kf = fixed
        self.t_start, self.t_end = fixed[0][0], fixed[-1][0]
        self.names = names
        self.route = route

    def km_at(self, t):
        kf = self.kf
        if t <= kf[0][0]:
            return kf[0][1]
        if t >= kf[-1][0]:
            return kf[-1][1]
        for a, b in zip(kf, kf[1:]):
            if a[0] <= t <= b[0]:
                if b[0] == a[0] or a[1] == b[1]:
                    return a[1] if t < b[0] else b[1]
                f = _trap((t - a[0]) / (b[0] - a[0]), a[2], b[2])
                return a[1] + (b[1] - a[1]) * f
        return kf[-1][1]

    def stopped_at(self, t):
        """停車中ならその駅のindex(待避判定用)"""
        for a, b in zip(self.kf, self.kf[1:]):
            if a[3] == b[3] and a[0] <= t <= b[0]:
                return a[3], b[0] - a[0]
        return None, 0

    def visible(self, t):
        return self.t_start <= t <= self.t_end


def find_overtakes(local, passers, step=0.02):
    events = []
    for p in passers:
        t0 = max(p.t_start, local.t_start)
        t1 = min(p.t_end, local.t_end)
        if t1 <= t0:
            continue
        prev = p.km_at(t0) - local.km_at(t0)
        t = t0 + step
        while t <= t1 + 1e-9:
            cur = p.km_at(t) - local.km_at(t)
            if prev <= 0 < cur or (prev < 0 <= cur and cur > 0):
                tc = t - step * (cur / (cur - prev)) if cur != prev else t
                si, dw = local.stopped_at(tc)
                events.append({"t": tc, "passer": p, "km": local.km_at(tc),
                               "station": local.names[si] if si is not None else None,
                               "waiting": si is not None and dw >= 1.0})
            prev = cur
            t += step
    events.sort(key=lambda e: e["t"])
    for n, e in enumerate(events, 1):
        e["n"] = n
    return events


def _draw_diagram(canvas, route, local, passers, t_now, t_min, t_max, events, popup_idx):
    d = ImageDraw.Draw(canvas, "RGBA")
    x0, y0, x1, y1 = DIA_BOX
    d.rounded_rectangle([30, y0 - 40, CANVAS_W - 30, y1 + 30], radius=24, fill=(8, 10, 22, 215),
                        outline=(255, 255, 255, 40), width=2)
    d.text((50, y0 - 18), "ダイヤグラム(横=時刻 / 縦=駅)", font=font(22), fill=(255, 215, 0, 255), anchor="lm")
    sts = route["stations"]
    k0, k1 = sts[0]["cum_km"], sts[-1]["cum_km"]

    def X(t):
        return x0 + (x1 - x0) * (t - t_min) / (t_max - t_min)

    def Y(km):
        return y0 + 14 + (y1 - y0 - 28) * (km - k0) / (k1 - k0)

    for i, s in enumerate(sts):
        y = Y(s["cum_km"])
        major = i in popup_idx
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, 60 if major else 18), width=1)
        if major:
            d.text((x0 - 10, y), s["name"], font=font(20), fill=(220, 225, 240, 255), anchor="rm")
    # 時刻目盛り(5分ごと)
    tt = (int(t_min) // 5 + 1) * 5
    while tt < t_max:
        d.text((X(tt), y1 + 14), fmt_hhmm(tt), font=font(18), fill=(160, 170, 190, 255), anchor="mm")
        tt += 5 if (t_max - t_min) < 60 else 10

    def sujji(tr, width):
        pts = []
        ts = max(tr.t_start, t_min)
        te = min(tr.t_end, t_now, t_max)
        if te <= ts:
            return
        n = max(2, int((te - ts) / 0.1))
        for j in range(n + 1):
            t = ts + (te - ts) * j / n
            pts.append((X(t), Y(tr.km_at(t))))
        d.line(pts, fill=tr.color + (255,), width=width, joint="curve")

    for p in passers:
        sujji(p, 3)
    sujji(local, 5)
    for e in events:
        if e["t"] <= t_now:
            x, y = X(e["t"]), Y(e["km"])
            d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=(255, 215, 0, 255), outline=(0, 0, 0, 255), width=2)
    xc = X(min(max(t_now, t_min), t_max))
    d.line([(xc, y0), (xc, y1)], fill=(255, 255, 255, 160), width=2)


def render_overtake(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    slug = out_name or config.get("slug", "overtake")
    line_geoms = load_line_geoms(geojson_path)
    local_cfg = config["local"]
    route, warnings = build_geometry(config["stations"], line_geoms, station_db, key="line",
                                     color=local_cfg.get("color") or [90, 200, 255])
    for w in warnings:
        print("[warn]", w)
    path = PolyPath(route, parallel=True)   # 緩行側/追い抜き側のレーンは平行移動で並べる
    local = Train(local_cfg, route, True, 0, float(local_cfg.get("default_dwell_sec", 30)))
    passers = [Train(pc, route, False, i) for i, pc in enumerate(config.get("passers", []))]
    events = find_overtakes(local, passers)
    print(f"  抜かれた回数: {len(events)}")
    for e in events:
        print(f"   {e['n']}. {fmt_hhmm(e['t'], True)} {e['passer'].name} "
              f"{'(' + e['station'] + 'で' + ('待避中' if e['waiting'] else '停車中') + ')' if e['station'] else '(走行中)'}")

    # 凡例は種別ごとに1つ
    legend, seen = [(local.name, local.color)], set()
    for p in passers:
        if p.name not in seen:
            seen.add(p.name)
            legend.append((p.name, p.color))
    passer_lane = dict(route, color=[200, 200, 210])
    base, proj = build_base_map([dict(route, color=list(local.color)), passer_lane], MAP_BOX,
                                config.get("title_line1", ""), config.get("title_line2", ""), legend=legend[:4],
                                geojson_path=geojson_path, start_label=config.get("start_name"),
                                end_label=config.get("end_label"), offsets=[LOCAL_OFFSET, PASSER_OFFSET], parallel=True)
    base.convert("RGB").save(f"{out_dir}/{slug}_base_map.png")

    fps = 10 if fast_preview else int(config.get("fps", 30))
    intro = float(config.get("intro_sec", 1.5))
    hold = float(config.get("outro_hold_sec", 5.0))
    duration = float(config.get("video_duration_sec", 40))
    t_min, t_max = local.t_start, local.t_end
    ratio = max(0.05, (duration - intro - hold - 1.0) / (t_max - t_min))   # 動画秒 / 実分

    def vt(t_real):
        return intro + (t_real - t_min) * ratio

    race_end = vt(t_max)
    result_start = race_end + 0.9
    total = result_start + hold
    popup_idx = {i for i, s in enumerate(route["stations"]) if s.get("popup")} | {0, len(route["stations"]) - 1}

    # 待避(長めの停車)イベント
    waits = []
    for a, b in zip(local.kf, local.kf[1:]):
        if a[3] == b[3] and b[0] - a[0] >= 1.5:
            waits.append((a[0], route["stations"][a[3]], b[0] - a[0]))

    script = [(0.0, f"{config.get('title_line1', '')}。この普通列車、目的地までに何本に抜かれるのか?")]
    for t, st, dw in waits:
        script.append((vt(t), f"{st['name']}で{dw:.0f}分停車。"))
    for e in events:
        where = f"{e['station']}で" if e["station"] else "走行中に"
        script.append((vt(e["t"]), f"{where}{e['passer'].name}に抜かれた。{e['n']}本目!"))
    script.append((result_start, f"結果、{len(events)}本に抜かれました。"))
    write_text_script(out_dir, slug, sorted(script, key=lambda x: x[0]))

    writer = FrameWriter(f"{out_dir}/{slug}.mp4", fps)
    n_frames = int(total * fps)
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    for fi in range(n_frames):
        tv = fi / fps
        t_real = t_min + max(0.0, tv - intro) / ratio
        t_real = min(t_real, t_max)
        canvas = base.copy()
        d = ImageDraw.Draw(canvas, "RGBA")
        km_l = local.km_at(t_real)
        if km_l > path.cum[0] + 0.01:
            draw_trail(canvas, path.screen_polyline(proj, LOCAL_OFFSET, path.cum[0], km_l), local.color)
        for p in passers:
            if p.visible(t_real) or (tv >= race_end and False):
                fade = min(1.0, (t_real - p.t_start) * ratio / 0.3, (p.t_end - t_real) * ratio / 0.3 + 0.0001)
                x, y = path.screen_at_km(proj, p.km_at(t_real), PASSER_OFFSET + ICON_SIDE - 10)
                draw_train(canvas, x, y, p.color, badge=p.badge, icon_img=p.icon, alpha=int(255 * max(0.2, fade)),
                           badge_side="right")
        x, y = path.screen_at_km(proj, km_l, LOCAL_OFFSET - ICON_SIDE + 10)
        draw_train(canvas, x, y, local.color, badge=local.badge, icon_img=local.icon, badge_side="left")

        for t0, st, dw in waits:
            el = tv - vt(t0)
            life = max(1.6, dw * ratio)
            if 0 <= el <= life:
                sx, sy = project(proj, st["lon"], st["lat"])
                al = 255 if el < life - 0.3 else int(255 * (life - el) / 0.3)
                draw_chip(canvas, sx - 150, sy + 20, f"{st['name']}で待避 {dw:.0f}分", local.color, size=26,
                          scale=pop_scale(el), alpha=al, tail=False)
        for e in events:
            el = tv - vt(e["t"])
            if 0 <= el <= 1.8:
                lx, ly = path.screen_at_km(proj, e["km"], LOCAL_OFFSET - ICON_SIDE)
                al = 255 if el < 1.45 else int(255 * (1.8 - el) / 0.35)
                draw_chip(canvas, lx, ly - 34, f"抜かれた! {e['passer'].name}", e["passer"].color, size=30,
                          scale=pop_scale(el), alpha=al)

        # 上部: 時計 + 抜かれた回数
        n_done = sum(1 for e in events if e["t"] <= t_real)
        last_ev = max([vt(e["t"]) for e in events if e["t"] <= t_real], default=-99)
        bump = pop_scale(tv - last_ev) if tv - last_ev < 0.4 else 1.0
        d.rounded_rectangle([40, 470, CANVAS_W - 40, 545], radius=22, fill=(8, 10, 22, 215),
                            outline=(255, 255, 255, 40), width=2)
        d.text((70, 508), fmt_hhmm(t_real), font=font(46, "black"), fill=(255, 255, 255, 255), anchor="lm")
        d.text((230, 508), f"{local.name} 経過{t_real - t_min:.0f}分", font=font(26), fill=(200, 210, 230, 255),
               anchor="lm")
        d.text((CANVAS_W - 200, 508), "抜かれた", font=font(28), fill=(255, 255, 255, 255), anchor="rm")
        d.text((CANVAS_W - 70, 508), f"{n_done}回", font=font(int(50 * bump), "black"),
               fill=(255, 215, 0, 255), anchor="rm")

        _draw_diagram(canvas, route, local, passers, t_real, t_min, t_max, events, popup_idx)

        if tv >= result_start:
            a = int(255 * ease_out_cubic((tv - result_start) / 0.6))
            d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(0, 0, 0, int(205 * a / 255)))
            d.text((CANVAS_W / 2, 470), "RESULT", font=font(84, "black"), fill=(255, 215, 0, a), anchor="mm")
            d.text((CANVAS_W / 2, 580), f"{local.name} {route['stations'][0]['name']}→{route['stations'][-1]['name']}",
                   font=font(42), fill=(255, 255, 255, a), anchor="mm")
            d.text((CANVAS_W / 2, 690), f"{len(events)}本に抜かれた!", font=font(92, "black"),
                   fill=local.color + (a,), anchor="mm")
            y = 800
            for e in events[:8]:
                where = f"{e['station']}で{'待避中' if e['waiting'] else '停車中'}" if e["station"] else "走行中"
                d.rounded_rectangle([120, y, CANVAS_W - 120, y + 70], radius=18, fill=(20, 24, 40, int(0.92 * a)))
                d.ellipse([140, y + 13, 184, y + 57], fill=e["passer"].color + (a,))
                d.text((162, y + 36), e["passer"].badge[:1], font=font(26, "black"), fill=(255, 255, 255, a),
                       anchor="mm")
                d.text((205, y + 35), f"{e['n']}本目  {e['passer'].name}", font=font(32, "black"),
                       fill=(255, 255, 255, a), anchor="lm")
                d.text((CANVAS_W - 150, y + 35), f"{fmt_hhmm(e['t'])} {where}", font=font(28),
                       fill=(210, 215, 230, a), anchor="rm")
                y += 84
            full = [p for p in passers if p.kf[0][3] == 0 and p.kf[-1][3] == len(route["stations"]) - 1]
            if full:
                best = min(full, key=lambda p: p.t_end - p.t_start)
                d.text((CANVAS_W / 2, y + 50),
                       f"所要時間  {local.name} {t_max - t_min:.0f}分 / {best.name} {best.t_end - best.t_start:.0f}分",
                       font=font(32), fill=(255, 255, 255, a), anchor="mm")
        writer.write(canvas)
    return writer.close()
