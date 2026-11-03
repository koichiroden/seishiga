# -*- coding: utf-8 -*-
"""
路線くらべカルーセル投稿(Instagram / X / Threads 向け 1080x1350 の画像セット)。

動画用に作った configs/*.json(race / fare / rush / overtake)をそのまま読み込んで
「駅の並び・路線色・ルート形状(表紙の地図)」を使い回し、運賃・所要時間・本数・始発の
4項目を1枚ずつ比べる画像と、まとめ画像・投稿文(キャプション)を書き出す。

    python3 -m race_video.cli configs/carousel_shinjuku_fujisawa.json
    → output/<slug>/01_cover.png ... 06_summary.png, output/<slug>/caption.txt

config の例は configs/carousel_shinjuku_fujisawa.json と README の「カルーセル投稿」を参照。

ルートごとの値(書かなければ元configから拾う):
    "base": "configs/shinjuku_fujisawa.json#route_a"   … 元configと route の key
             (rush の複数路線は "#東海道線" のように lines[].name、
              overtake / 1路線の rush は "#" なしでファイル名だけ)
    "fare":  {"ic": 607, "ticket": 610}  … 省略時は参照先の運賃動画configから(下記)
    "fare_from": "configs/fare_shinjuku_fujisawa.json#route_a"   … 運賃を拾う運賃動画のconfig
    "time_min": 56, "time_note": "快速急行"   … 省略時は参照先の電車最速バトルconfigから(下記)
    "time_from": "configs/shinjuku_fujisawa.json#route_a"        … 所要時間を拾う最速バトル/rushのconfig

トップレベルに "fare_config" / "time_config"(ファイル名だけ)を書くと、各ルートに当たるルートを
key(base の '#route_a')→ 名前 → 並び順 で自動で選ぶ。距離運賃表(fare_tables)の運賃も動画と同じ計算で出す。
    "per_hour": 3, "per_hour_note": "快速急行(直通)"
    "origin": "all" | "many" | "few" | "none", "origin_note": "全列車が新宿始発"
"""
import json
import math
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .common import font, compute_projection_box, PolyPath
from .geo import project
from .render_base import (
    load_all_lines, load_land_polygons, draw_land_and_sea, vertical_gradient,
    draw_glow_polyline,
)
from .route_builder import load_line_geoms, build_route, apply_clock_times

W, H = 1080, 1350
PAD = 64
BG_TOP, BG_BOTTOM = (10, 15, 34), (3, 5, 14)
INK = (255, 255, 255)
INK2 = (200, 208, 228)
MUTED = (135, 145, 172)
GOLD = (255, 210, 40)
CARD = (22, 28, 52)
CARD_LINE = (52, 62, 98)

# 始発の度合い(状態色は アイコン+ラベル とセットで使う)
ORIGIN_LEVELS = {
    "all":  {"rank": 3, "mark": "◎", "label": "全列車が始発", "color": (60, 200, 120)},
    "many": {"rank": 2, "mark": "○", "label": "始発あり", "color": (120, 200, 90)},
    "few":  {"rank": 1, "mark": "△", "label": "ごく一部", "color": (240, 170, 40)},
    "none": {"rank": 0, "mark": "×", "label": "始発なし", "color": (235, 80, 80)},
}

METRICS = ["fare", "time", "freq", "origin"]
METRIC_TITLE = {"fare": "運賃", "time": "所要時間", "freq": "本数", "origin": "始発"}


# ---------------------------------------------------------------- config の読み込み

def _load_json(path, cache):
    p = str(path)
    if p not in cache:
        with open(p, encoding="utf-8") as f:
            cache[p] = json.load(f)
    return cache[p]


def _base_route(ref, cache):
    """'configs/x.json#route_a' から元configのルート(stations 等)を取り出す。"""
    path, _, key = ref.partition("#")
    cfg = _load_json(path, cache)
    if cfg.get("routes"):
        for r in cfg["routes"]:
            if not key or r.get("key") == key or r.get("name") == key:
                return cfg, r
        raise KeyError(f"{path} に route '{key}' がありません")
    if cfg.get("lines"):          # rush の複数路線
        for ln in cfg["lines"]:
            if not key or ln.get("name") == key:
                return cfg, {"name": ln["name"], "color": ln.get("color"), "stations": ln["stations"],
                             "_runs": ln.get("runs", [])}
        raise KeyError(f"{path} に line '{key}' がありません")
    if cfg.get("stations"):       # overtake / 1路線の rush
        r = {"name": cfg.get("line_name") or cfg.get("title_line1", path), "stations": cfg["stations"],
             "_runs": cfg.get("runs", [])}
        if cfg.get("local"):
            r["color"] = cfg["local"].get("color")
        return cfg, r
    raise KeyError(f"{path} からルートを読み取れません")


def _fallback_time(base):
    sts = [dict(s) for s in base.get("stations", [])]
    if sts and any(s.get("time") for s in sts):
        apply_clock_times({"name": base.get("name", ""), "stations": sts})
    last = sts[-1] if sts else {}
    if last.get("t_min") is not None:
        return float(last["t_min"])
    # rush の runs(日中を優先)
    runs = base.get("_runs") or []
    run = next((r for r in runs if r.get("key") == "daytime"), runs[0] if runs else None)
    if run and sts:
        from .route_builder import norm_clock
        a, b = norm_clock(run["times"].get(sts[0]["name"])), norm_clock(run["times"].get(sts[-1]["name"]))
        if a is not None and b is not None:
            return (b - a) % (24 * 60)
    return None


def _pick_route(path, rc, i, cache):
    """トップレベルの fare_config / time_config から、このルートに当たるものを選ぶ。
    ① ルートの key(base の '#route_a' など)② 名前 ③ 並び順 の順で探す。"""
    cfg = _load_json(path, cache)
    cands = cfg.get("routes") or [{"name": ln.get("name"), **ln} for ln in cfg.get("lines", [])]
    key = rc.get("key") or (rc.get("base") or "").partition("#")[2]
    for r in cands:
        if key and (r.get("key") == key or r.get("name") == key):
            return f"{path}#{r.get('key') or r.get('name')}"
    for r in cands:
        if rc.get("name") and r.get("name") == rc.get("name"):
            return f"{path}#{r.get('key') or r.get('name')}"
    if i < len(cands):
        r = cands[i]
        return f"{path}#{r.get('key') or r.get('name')}"
    return path if cfg.get("stations") else None


def _fare_of(route_cfg, line_geoms, station_db, idx=0):
    """運賃動画(fare)のconfigのルートから終着駅までの運賃を出す。
    駅ごとの fare があればそれ、距離運賃表(fare_tables / プリセット)なら動画と同じ計算をする。"""
    sts = route_cfg.get("stations") or []
    if not sts:
        return None
    if sts[-1].get("fare") not in (None, ""):
        return float(sts[-1]["fare"])
    if route_cfg.get("fare_tables") or route_cfg.get("fare_table"):
        if line_geoms is None or station_db is None:
            return None
        from .common import build_geometry
        from .fare_video import FareRoute
        route, _w = build_geometry([dict(s) for s in sts], line_geoms, station_db,
                                   key=route_cfg.get("key", "r"), name=route_cfg.get("name", ""))
        cfg = {k: v for k, v in route_cfg.items() if k != "icon_path"}
        return float(FareRoute(cfg, route, idx).final_fare)
    return None


def resolve_routes(config, line_geoms=None, station_db=None):
    """carousel config のルートを、参照先の動画config から値を補って組み立てる。

    値の優先順位(上ほど優先):
      運賃     … ルートの "fare" > ルートの "fare_from" > トップレベルの "fare_config" > "base"
      所要時間 … ルートの "time_min" > ルートの "time_from" > トップレベルの "time_config" > "base"
    """
    cache = {}
    routes = []
    for i, rc in enumerate(config["routes"]):
        base = {}
        if rc.get("base"):
            _cfg, base = _base_route(rc["base"], cache)
        r = {**{k: v for k, v in base.items() if not k.startswith("_")}, **rc}
        r["key"] = rc.get("key") or base.get("key") or f"route_{i}"
        r["name"] = rc.get("name") or base.get("name") or r["key"]
        r["short_name"] = rc.get("short_name") or base.get("short_name") or r["name"]
        r["color"] = tuple(rc.get("color") or base.get("color") or (220, 220, 220))
        r["_src"] = {}

        # 運賃
        fare = rc.get("fare")
        if isinstance(fare, (int, float)):
            fare = {"ic": fare}
        if fare:
            r["_src"]["fare"] = "手入力"
        else:
            refs = [rc.get("fare_from")]
            if config.get("fare_config"):
                refs.append(_pick_route(config["fare_config"], rc, i, cache))
            refs.append(rc.get("base"))
            for ref in [x for x in refs if x]:
                try:
                    fcfg, fr = _base_route(ref, cache)
                except (KeyError, OSError) as e:
                    print(f"[warn] {r['name']}: 運賃の参照先 {ref} を読めません ({e})")
                    continue
                v = _fare_of(fr, line_geoms, station_db, i)
                if v is not None:
                    fare = {"ic": v}
                    r["_src"]["fare"] = ref
                    if not config.get("fare_label") and fcfg.get("fare_label"):
                        config["fare_label"] = f"{fcfg['fare_label']}(大人・片道)"
                    break
        r["fare"] = fare

        # 所要時間
        if r.get("time_min") is not None:
            r["_src"]["time"] = "手入力"
        else:
            refs = [rc.get("time_from")]
            if config.get("time_config"):
                refs.append(_pick_route(config["time_config"], rc, i, cache))
            refs.append(rc.get("base"))
            for ref in [x for x in refs if x]:
                try:
                    _tc, tr = _base_route(ref, cache)
                except (KeyError, OSError) as e:
                    print(f"[warn] {r['name']}: 所要時間の参照先 {ref} を読めません ({e})")
                    continue
                v = _fallback_time(tr)
                if v is not None:
                    r["time_min"] = v
                    r["_src"]["time"] = ref
                    break
        r["origin"] = rc.get("origin")
        r["_base"] = base
        routes.append(r)
    for r in routes:
        f = (r.get("fare") or {}).get("ic")
        t = r.get("time_min")
        print(f"  {r['name']}: 運賃 {yen(f) if f is not None else '-'} ({r['_src'].get('fare', 'なし')}) / "
              f"所要時間 {fmt_min(t) if t is not None else '-'} ({r['_src'].get('time', 'なし')})")
    return routes


# ---------------------------------------------------------------- 描画ヘルパー

def new_canvas():
    # RGB のキャンバスに ImageDraw.Draw(img, "RGBA") で描くと半透明の塗りが下地と混ざる
    return vertical_gradient(W, H, BG_TOP, BG_BOTTOM).convert("RGB")


def rrect(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def text_w(d, s, f):
    return d.textlength(s, font=f)


def wrap_jp(d, s, f, max_w):
    """日本語を幅で折り返す(英数字の単語は分割しない程度の簡易版)。"""
    lines, cur = [], ""
    for ch in s:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if text_w(d, cur + ch, f) > max_w and cur:
            # 行頭禁則(句読点・閉じ括弧を前の行へ)
            if ch in "、。,.)）」』!?！？ー":
                cur += ch
                continue
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def fit_font(d, s, max_w, size, weight="black", min_size=20):
    while size > min_size and text_w(d, s, font(size, weight)) > max_w:
        size -= 2
    return font(size, weight)


def yen(v):
    return f"¥{int(round(v)):,}"


def fmt_min(m):
    m = int(round(m))
    return f"{m // 60}時間{m % 60:02d}分" if m >= 60 else f"{m}分"


def chrome(img, config, page, total, kicker):
    """全スライド共通: 上部キッカー・ページ番号、下部ハンドル・データ時点・スワイプ表示"""
    d = ImageDraw.Draw(img, "RGBA")
    f = font(26, "bold")
    tw = text_w(d, kicker, f)
    rrect(d, (PAD, 52, PAD + tw + 36, 98), 23, fill=GOLD + (255,))
    d.text((PAD + 18, 75), kicker, font=f, fill=(15, 18, 30), anchor="lm")
    d.text((W - PAD, 75), f"{page}/{total}", font=font(28, "bold"), fill=MUTED, anchor="rm")

    handle = config.get("handle", "@geogram_jp")
    d.line([(PAD, H - 96), (W - PAD, H - 96)], fill=CARD_LINE + (255,), width=2)
    d.text((PAD, H - 58), handle, font=font(26, "bold"), fill=INK2, anchor="lm")
    if page < total:
        d.text((W - PAD, H - 58), "スワイプ →", font=font(26, "bold"), fill=GOLD, anchor="rm")
    else:
        d.text((W - PAD, H - 58), "保存して乗り換えの参考に", font=font(24, "bold"), fill=GOLD, anchor="rm")
    note = config.get("as_of", "")
    if note:
        d.text((W / 2, H - 58), note, font=font(20, "regular"), fill=MUTED, anchor="mm")


def header(img, title, sub, num=None):
    d = ImageDraw.Draw(img, "RGBA")
    y = 150
    x = PAD
    if num is not None:
        f_num = font(46, "black")
        d.ellipse((x, y - 6, x + 72, y + 66), fill=GOLD + (255,))
        d.text((x + 36, y + 30), str(num), font=f_num, fill=(15, 18, 30), anchor="mm")
        x += 92
    d.text((x, y + 30), title, font=font(84, "black"), fill=INK, anchor="lm")
    if sub:
        d.text((PAD, y + 112), sub, font=font(30, "regular"), fill=INK2, anchor="lm")


def route_tag(d, x, y, r, size=36, max_w=None):
    """色の丸 + 路線名(色だけで区別しないよう必ず名前を添える)"""
    rad = size * 0.36
    d.ellipse((x, y - rad, x + 2 * rad, y + rad), fill=tuple(r["color"]) + (255,))
    name = r["name"]
    f = font(size, "black")
    if max_w:
        f = fit_font(d, name, max_w - 2 * rad - 16, size)
    d.text((x + 2 * rad + 14, y), name, font=f, fill=INK, anchor="lm")


def badge(d, x, y, text, fill=GOLD, ink=(15, 18, 30), size=28, anchor="r"):
    f = font(size, "black")
    tw = text_w(d, text, f)
    h = size + 18
    x0 = x - tw - 32 if anchor == "r" else x
    rrect(d, (x0, y - h / 2, x0 + tw + 32, y + h / 2), h / 2, fill=fill + (255,))
    d.text((x0 + 16 + tw / 2, y), text, font=f, fill=ink, anchor="mm")


def callout(img, y0, y1, big, small=None):
    d = ImageDraw.Draw(img, "RGBA")
    rrect(d, (PAD, y0, W - PAD, y1), 28, fill=(255, 210, 40, 26), outline=GOLD + (200,), width=3)
    cy = (y0 + y1) / 2
    fb = fit_font(d, big, W - 2 * PAD - 60, 52)
    if small:
        d.text((W / 2, cy - 26), big, font=fb, fill=INK, anchor="mm")
        d.text((W / 2, cy + 36), small, font=font(30, "bold"), fill=GOLD, anchor="mm")
    else:
        d.text((W / 2, cy), big, font=fb, fill=INK, anchor="mm")


# ---------------------------------------------------------------- 勝ち負け判定

def metric_value(r, m):
    if m == "fare":
        return (r.get("fare") or {}).get("ic")
    if m == "time":
        return r.get("time_min")
    if m == "freq":
        return r.get("per_hour")
    if m == "origin":
        o = r.get("origin")
        return ORIGIN_LEVELS[o]["rank"] if o in ORIGIN_LEVELS else None
    return None


def winners(routes, m):
    vals = [(r, metric_value(r, m)) for r in routes]
    vals = [(r, v) for r, v in vals if v is not None]
    if len(vals) < 2:
        return set()
    lower_better = m in ("fare", "time")
    best = (min if lower_better else max)(v for _r, v in vals)
    if all(v == best for _r, v in vals):
        return set()        # 全部同じなら引き分け
    return {r["key"] for r, v in vals if v == best}


def best_vs_next(routes, m):
    """(勝ったルート名, 次点ルート名, 差) — 差の説明文に使う"""
    vals = [(r, metric_value(r, m)) for r in routes if metric_value(r, m) is not None]
    if len(vals) < 2:
        return None
    lower_better = m in ("fare", "time")
    vals.sort(key=lambda t: t[1], reverse=not lower_better)
    best_v = vals[0][1]
    best = [r for r, v in vals if v == best_v]
    rest = [(r, v) for r, v in vals if v != best_v]
    if not rest:
        return None
    nxt_v = rest[0][1]
    nxt = [r for r, v in rest if v == nxt_v]
    return best, nxt, abs(best_v - nxt_v)


def names(rs):
    return "・".join(r["short_name"] for r in rs)


# ---------------------------------------------------------------- 各スライド

def slide_cover(config, routes, paths, geojson_path):
    img = new_canvas().convert("RGBA")

    # 地図
    box = (PAD, 470, W - PAD, 1080)
    focus = []
    for p in paths:
        focus.extend(p["polyline"])
    proj = compute_projection_box(focus, box, pad_ratio=0.10)
    land = load_land_polygons(str(Path(geojson_path).parent / "coastline.geojson"))
    draw_land_and_sea(img, proj, land)
    faint = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(faint)
    for _name, segs in load_all_lines(geojson_path):
        for seg in segs:
            pts = [project(proj, lon, lat) for lon, lat in seg]
            pts = [p for p in pts if -50 <= p[0] <= W + 50 and -50 <= p[1] <= H + 50]
            if len(pts) >= 2:
                fd.line(pts, fill=(150, 170, 210, 60), width=2, joint="curve")
    img.alpha_composite(faint)

    n = len(paths)
    gap = 14
    for i, p in enumerate(paths):
        off = (i - (n - 1) / 2) * gap
        pts = PolyPath(p, parallel=True).screen_polyline(proj, off)
        draw_glow_polyline(img, pts, tuple(p["color"]), width=9, glow_width=28, glow_alpha=90)

    # 上下を背景色でフェードさせて、文字が地図に埋もれないようにする
    fade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fdd = ImageDraw.Draw(fade)
    for k in range(120):
        a = int(255 * (1 - k / 120) ** 1.6)
        fdd.line([(0, 470 + k), (W, 470 + k)], fill=BG_TOP + (a,))
        fdd.line([(0, 1080 - k), (W, 1080 - k)], fill=BG_BOTTOM + (a,))
    fdd.rectangle((0, 0, W, 470), fill=BG_TOP + (255,))
    fdd.rectangle((0, 1080, W, H), fill=BG_BOTTOM + (255,))
    img.alpha_composite(fade)
    img = img.convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")

    # 起点・終点ラベル
    s0 = paths[0]["stations"][0]
    x0, y0 = project(proj, s0["lon"], s0["lat"])
    ends = [project(proj, p["stations"][-1]["lon"], p["stations"][-1]["lat"]) for p in paths]
    x1 = sum(e[0] for e in ends) / len(ends)
    y1 = sum(e[1] for e in ends) / len(ends)
    for (x, y), label in (((x0, y0), config.get("start_name", s0["name"])),
                          ((x1, y1), config.get("end_label", paths[0]["stations"][-1]["name"]))):
        d.ellipse((x - 14, y - 14, x + 14, y + 14), fill=INK + (255,), outline=GOLD + (255,), width=5)
        f = font(40, "black")
        tw = text_w(d, label, f)
        lx = min(max(x, PAD + tw / 2 + 20), W - PAD - tw / 2 - 20)
        ly = y - 52 if y > 560 else y + 52
        rrect(d, (lx - tw / 2 - 18, ly - 30, lx + tw / 2 + 18, ly + 30), 14, fill=(5, 8, 18, 220))
        d.text((lx, ly), label, font=f, fill=INK, anchor="mm")

    # タイトル
    d.text((PAD, 75), config.get("cover_kicker", "4項目でガチ比較"), font=font(32, "black"), fill=GOLD, anchor="lm")
    t1 = config.get("title_line1", "")
    d.text((PAD, 205), t1, font=fit_font(d, t1, W - 2 * PAD, 128), fill=INK, anchor="lm")
    t2 = config.get("title_line2", "")
    d.text((PAD, 320), t2, font=fit_font(d, t2, W - 2 * PAD, 52), fill=INK, anchor="lm")
    # 比べる項目のチップ
    x = PAD
    for m in METRICS:
        lab = METRIC_TITLE[m]
        f = font(30, "bold")
        tw = text_w(d, lab, f)
        rrect(d, (x, 380, x + tw + 40, 432), 26, outline=GOLD + (255,), width=3)
        d.text((x + 20 + tw / 2, 406), lab, font=f, fill=GOLD, anchor="mm")
        x += tw + 56

    # 凡例(路線名)
    y = 1130
    col_w = (W - 2 * PAD) / min(len(routes), 3)
    for i, r in enumerate(routes):
        route_tag(d, PAD + col_w * (i % 3), y + 60 * (i // 3), r, size=34, max_w=col_w - 20)

    handle = config.get("handle", "@geogram_jp")
    d.line([(PAD, H - 96), (W - PAD, H - 96)], fill=CARD_LINE + (255,), width=2)
    d.text((PAD, H - 58), handle, font=font(26, "bold"), fill=INK2, anchor="lm")
    d.text((W - PAD, H - 58), "スワイプして比較 →", font=font(28, "black"), fill=GOLD, anchor="rm")
    return img


def _bar_rows(img, routes, metric, value_text, note_of, y0, row_h, win_label, lower_better):
    d = ImageDraw.Draw(img, "RGBA")
    vals = [metric_value(r, metric) for r in routes]
    vmax = max(v for v in vals if v is not None) or 1
    wins = winners(routes, metric)
    bar_x0, bar_x1 = PAD, W - PAD
    for i, r in enumerate(routes):
        v = metric_value(r, metric)
        y = y0 + i * row_h
        route_tag(d, PAD, y + 26, r, size=40, max_w=W - 2 * PAD - 220)
        if r["key"] in wins:
            badge(d, W - PAD, y + 26, win_label)
        by = y + 78
        bh = 84 if row_h >= 280 else 64
        rrect(d, (bar_x0, by, bar_x1, by + bh), 14, fill=(255, 255, 255, 14))
        if v is None:
            d.text((bar_x0 + 20, by + bh / 2), "データなし", font=font(30, "bold"), fill=MUTED, anchor="lm")
            continue
        frac = max(0.08, v / vmax)
        bx1 = bar_x0 + (bar_x1 - bar_x0) * frac
        col = tuple(r["color"])
        rrect(d, (bar_x0, by, bx1, by + bh), 14, fill=col + (255,))
        txt = value_text(r)
        f = font(60 if bh > 70 else 50, "black")
        tw = text_w(d, txt, f)
        if bx1 - bar_x0 > tw + 40:
            d.text((bx1 - 20, by + bh / 2), txt, font=f, fill=INK, anchor="rm",
                   stroke_width=3, stroke_fill=(0, 0, 0, 120))
        else:
            d.text((bx1 + 16, by + bh / 2), txt, font=f, fill=INK, anchor="lm")
        note = note_of(r)
        if note:
            d.text((PAD, by + bh + 32), note, font=font(28, "regular"), fill=INK2, anchor="lm")


def _layout(n):
    """ルート数に応じたバーの行の高さ"""
    return {2: 300, 3: 214}.get(n, max(150, 640 // max(n, 1)))


def slide_fare(config, routes):
    img = new_canvas()
    header(img, "運賃", config.get("fare_label", "IC運賃(大人・片道)"), num=1)
    row_h = _layout(len(routes))
    y0 = 340

    def note(r):
        f = r.get("fare") or {}
        bits = []
        if f.get("ticket") is not None and f.get("ticket") != f.get("ic"):
            bits.append(f"きっぷ {yen(f['ticket'])}")
        if r.get("fare_note"):
            bits.append(r["fare_note"])
        return " / ".join(bits)

    _bar_rows(img, routes, "fare", lambda r: yen(r["fare"]["ic"]), note, y0, row_h, "最安", True)
    bvn = best_vs_next(routes, "fare")
    if bvn:
        best, nxt, diff = bvn
        callout(img, 1010, 1200, f"{names(best)}が {yen(diff)} 安い",
                f"往復で {yen(diff * 2)}・月20往復なら {yen(diff * 40)} の差")
    else:
        callout(img, 1010, 1200, "運賃はどれも同じ")
    return img


def slide_time(config, routes):
    img = new_canvas()
    header(img, "所要時間", config.get("time_label", "日中の直通列車(速い種別)"), num=2)
    row_h = _layout(len(routes))
    _bar_rows(img, routes, "time", lambda r: fmt_min(r["time_min"]),
              lambda r: r.get("time_note", ""), 340, row_h, "最速", True)
    bvn = best_vs_next(routes, "time")
    if bvn:
        best, nxt, diff = bvn
        callout(img, 1010, 1200, f"{names(best)}が {fmt_min(diff)} 速い",
                config.get("time_callout_sub") or f"{names(nxt)}より {fmt_min(diff)} 早く着く")
    else:
        callout(img, 1010, 1200, "所要時間はほぼ同じ")
    return img


def _train_icon(d, x, y, w, h, color):
    rrect(d, (x, y, x + w, y + h), min(10, h / 3, w / 3), fill=color + (255,))
    # 窓
    for k in range(3):
        wx = x + 8 + k * (w - 16) / 3
        d.rectangle((wx + 2, y + 8, wx + (w - 16) / 3 - 2, y + h * 0.45), fill=(10, 15, 34, 230))
    d.ellipse((x + 6, y + h - 6, x + 16, y + h + 4), fill=(200, 208, 228, 255))
    d.ellipse((x + w - 16, y + h - 6, x + w - 6, y + h + 4), fill=(200, 208, 228, 255))


_ICON_CACHE = {}


def load_route_icon(r):
    """ルートの icon_path(assets/xxx.png)を読み込む。無い・読めないときは None(標準の簡易アイコン)。
    元configの icon_path(電車最速バトル用の車両アイコン)もそのまま引き継ぐ。"""
    path = r.get("icon_path")
    if not path:
        return None
    if path not in _ICON_CACHE:
        p = Path(path)
        try:
            im = Image.open(p).convert("RGBA")
            bbox = im.getbbox()          # 透明な余白を切り落とす
            _ICON_CACHE[path] = im.crop(bbox) if bbox else im
        except (OSError, ValueError) as e:
            print(f"[warn] {r['name']} のアイコン {path} を読めないので標準アイコンで描きます ({e})")
            _ICON_CACHE[path] = None
    return _ICON_CACHE[path]


def _icon_row(img, d, x0, x1, y_top, max_h, cnt, icon, color):
    """x0〜x1 の幅に cnt 個のアイコンを1行で並べる(入りきらなければ縮め、それでも小さすぎれば2行)。"""
    if cnt <= 0:
        return
    gap = 10
    aspect = (icon.width / icon.height) if icon is not None else 1.3     # 幅/高さ
    avail = x1 - x0
    h = min(max_h, (avail - gap * (cnt - 1)) / cnt / aspect)
    rows = 1
    if h < 24 and cnt > 1:                                              # 2行に折り返す
        rows = 2
        per = math.ceil(cnt / 2)
        h = min(max_h / 2 - 4, (avail - gap * (per - 1)) / per / aspect)
    per_row = math.ceil(cnt / rows)
    w = h * aspect
    row_gap = 8
    total_h = rows * h + (rows - 1) * row_gap
    yy0 = y_top + (max_h - total_h) / 2
    scaled = icon.resize((max(1, round(w)), max(1, round(h))), Image.LANCZOS) if icon is not None else None
    for k in range(cnt):
        rr, cc = divmod(k, per_row)
        x = x0 + cc * (w + gap)
        y = yy0 + rr * (h + row_gap)
        if scaled is not None:
            img.paste(scaled, (round(x), round(y)), scaled)
        else:
            _train_icon(d, x, y, w, h, color)


def slide_freq(config, routes):
    img = new_canvas()
    header(img, "本数", config.get("per_hour_label", "日中・1時間あたり"), num=3)
    d = ImageDraw.Draw(img, "RGBA")
    wins = winners(routes, "freq")
    row_h = _layout(len(routes))
    for i, r in enumerate(routes):
        y = 340 + i * row_h
        route_tag(d, PAD, y + 26, r, size=38, max_w=W - 2 * PAD - 220)
        if r["key"] in wins:
            badge(d, W - PAD, y + 26, "多い")
        n = r.get("per_hour")
        if n is None:
            d.text((PAD, y + 110), "データなし", font=font(30, "bold"), fill=MUTED, anchor="lm")
            continue
        # 大きい数字
        d.text((PAD, y + 112), f"{n:g}", font=font(92, "black"), fill=INK, anchor="lm")
        nw = text_w(d, f"{n:g}", font(92, "black"))
        d.text((PAD + nw + 8, y + 128), "本/時", font=font(34, "bold"), fill=INK2, anchor="lm")
        # 列車アイコンを本数ぶん並べる(icon_path のPNGがあればそれを使う)
        _icon_row(img, d, PAD + 300, W - PAD, y + 76, 70, int(round(n)),
                  load_route_icon(r), tuple(r["color"]))
        interval = 60 / n if n else None
        sub = r.get("per_hour_note", "")
        if interval:
            sub = (f"約{interval:.0f}分に1本" + (f" / {sub}" if sub else ""))
        d.text((PAD, y + 182), sub, font=font(26, "regular"), fill=INK2, anchor="lm")
    bvn = best_vs_next(routes, "freq")
    if bvn:
        best, nxt, diff = bvn
        bv = metric_value(best[0], "freq")
        nv = metric_value(nxt[0], "freq")
        callout(img, 1010, 1200, f"{names(best)}は1時間に{bv:g}本",
                f"{names(nxt)}の {bv / nv:.1f}倍 来る" if nv else None)
    else:
        callout(img, 1010, 1200, "本数は同じ")
    return img


def slide_origin(config, routes):
    img = new_canvas()
    header(img, "始発", config.get("origin_label", "出発駅が始発の列車(座りやすさ)"), num=4)
    d = ImageDraw.Draw(img, "RGBA")
    n = len(routes)
    top, bottom = 330, 980
    gap = 24
    ch = (bottom - top - gap * (n - 1)) / n
    for i, r in enumerate(routes):
        y = top + i * (ch + gap)
        rrect(d, (PAD, y, W - PAD, y + ch), 24, fill=CARD + (255,), outline=CARD_LINE + (255,), width=2)
        d.rectangle((PAD, y + 18, PAD + 10, y + ch - 18), fill=tuple(r["color"]) + (255,))
        lv = ORIGIN_LEVELS.get(r.get("origin"))
        cx, cy = PAD + 110, y + ch / 2
        rad = min(64, ch / 2 - 22)
        if lv:
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=lv["color"] + (40,),
                      outline=lv["color"] + (255,), width=6)
            d.text((cx, cy + 2), lv["mark"], font=font(int(rad * 1.25), "black"), fill=lv["color"], anchor="mm")
        tx = cx + rad + 40
        route_tag(d, tx, y + 44, r, size=34, max_w=W - PAD - tx - 20)
        if lv:
            d.text((tx, y + 100), r.get("origin_label") or lv["label"], font=font(40, "black"), fill=lv["color"], anchor="lm")
        note = r.get("origin_note", "")
        if note:
            f = font(26, "regular")
            for k, line in enumerate(wrap_jp(d, note, f, W - PAD - tx - 30)[:2]):
                d.text((tx, y + 150 + k * 36), line, font=f, fill=INK2, anchor="lm")
    best = [r for r in routes if r["key"] in winners(routes, "origin")]
    if best:
        callout(img, 1010, 1200, f"座って行くなら {names(best)}",
                config.get("origin_callout_sub") or "始発駅なら並べば座れるチャンス大")
    else:
        callout(img, 1010, 1200, "始発の条件はどれも同じ")
    return img


def slide_summary(config, routes):
    img = new_canvas()
    header(img, "まとめ", config.get("summary_label", "黄色の枠 = その項目の勝ち"))
    d = ImageDraw.Draw(img, "RGBA")
    n = len(routes)
    label_w = 210
    x0 = PAD + label_w
    col_w = (W - PAD - x0) / n
    top = 320
    head_h = 96
    row_h = 118
    # 列見出し
    for j, r in enumerate(routes):
        cx = x0 + col_w * j
        rrect(d, (cx + 6, top, cx + col_w - 6, top + head_h), 18, fill=tuple(r["color"]) + (255,))
        f = fit_font(d, r["short_name"], col_w - 32, 36)
        d.text((cx + col_w / 2, top + head_h / 2), r["short_name"], font=f, fill=INK, anchor="mm",
               stroke_width=2, stroke_fill=(0, 0, 0, 90))
    score = {r["key"]: 0 for r in routes}
    for i, m in enumerate(METRICS):
        y = top + head_h + 14 + i * row_h
        d.line([(PAD, y + row_h), (W - PAD, y + row_h)], fill=CARD_LINE + (255,), width=2)
        d.text((PAD, y + row_h / 2), METRIC_TITLE[m], font=font(40, "black"), fill=INK, anchor="lm")
        wins = winners(routes, m)
        for j, r in enumerate(routes):
            cx = x0 + col_w * j + col_w / 2
            cy = y + row_h / 2
            if m == "fare":
                txt = yen(r["fare"]["ic"]) if r.get("fare") else "-"
            elif m == "time":
                txt = fmt_min(r["time_min"]) if r.get("time_min") is not None else "-"
            elif m == "freq":
                txt = f"{r['per_hour']:g}本/時" if r.get("per_hour") is not None else "-"
            else:
                lv = ORIGIN_LEVELS.get(r.get("origin"))
                txt = f"{lv['mark']} {r.get('origin_label') or lv['label']}" if lv else "-"
            won = r["key"] in wins
            if won:
                score[r["key"]] += 1
                rrect(d, (cx - col_w / 2 + 10, y + 10, cx + col_w / 2 - 10, y + row_h - 10), 16,
                      fill=(255, 210, 40, 34), outline=GOLD + (255,), width=3)
            f = fit_font(d, txt, col_w - 40, 38 if m != "origin" else 30, "black")
            d.text((cx, cy), txt, font=f, fill=INK if won else INK2, anchor="mm")
    # 結論
    verdict = config.get("verdict")
    if not verdict:
        top_score = max(score.values())
        champs = [r for r in routes if score[r["key"]] == top_score]
        verdict = [f"総合: {names(champs)}が {top_score}勝"]
    if isinstance(verdict, str):
        verdict = [verdict]
    vy0 = top + head_h + 14 + 4 * row_h + 34
    vy1 = H - 130
    rrect(d, (PAD, vy0, W - PAD, vy1), 28, fill=(255, 210, 40, 26), outline=GOLD + (200,), width=3)
    lines = verdict[:3]
    lh = (vy1 - vy0) / (len(lines) + 1)
    for k, line in enumerate(lines):
        f = fit_font(d, line, W - 2 * PAD - 60, 44 if k == 0 else 36, "black" if k == 0 else "bold")
        d.text((W / 2, vy0 + lh * (k + 1)), line, font=f, fill=INK if k == 0 else GOLD, anchor="mm")
    return img


# ---------------------------------------------------------------- キャプション

def build_caption(config, routes):
    t1 = config.get("title_line1", "")
    lines = [f"【{t1}】{config.get('title_line2', '')}", ""]
    for m in METRICS:
        parts = []
        for r in routes:
            if m == "fare" and r.get("fare"):
                parts.append(f"{r['short_name']} {yen(r['fare']['ic'])}")
            elif m == "time" and r.get("time_min") is not None:
                parts.append(f"{r['short_name']} {fmt_min(r['time_min'])}")
            elif m == "freq" and r.get("per_hour") is not None:
                parts.append(f"{r['short_name']} {r['per_hour']:g}本/時")
            elif m == "origin" and r.get("origin") in ORIGIN_LEVELS:
                parts.append(f"{r['short_name']} {r.get('origin_label') or ORIGIN_LEVELS[r['origin']]['label']}")
        if parts:
            lines.append(f"▶{METRIC_TITLE[m]}: " + " / ".join(parts))
    v = config.get("verdict")
    if v:
        lines += ["", "→ " + (v if isinstance(v, str) else " / ".join(v))]
    lines += ["", "あなたはどっち派？コメントで教えてください",
              f"※{config.get('as_of', '')} {config.get('caption_note', '運賃はIC・大人片道、本数は日中の直通列車')}".strip()]
    tags = config.get("hashtags") or ["#路線くらべ", "#鉄道", "#電車", "#地図", "#乗り換え", "#Geogram_JP"]
    lines += ["", " ".join(tags)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- エントリーポイント

def render_carousel(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    slug = out_name or config.get("slug", "carousel")
    out = Path(out_dir) / slug
    out.mkdir(parents=True, exist_ok=True)
    line_geoms = load_line_geoms(geojson_path)
    routes = resolve_routes(config, line_geoms, station_db)

    # 表紙の地図用にルート形状を作る(元configの stations をそのまま使う)
    paths = []
    for r in routes:
        sts = r.get("stations") or r["_base"].get("stations")
        st = []
        for i, s in enumerate(sts):
            e = {"line": s["line"], "name": s["name"], "popup": bool(s.get("popup"))}
            e["t_min"] = 0 if i == 0 else (1 if i == len(sts) - 1 else None)
            st.append(e)
        p, warns = build_route({"key": r["key"], "name": r["name"], "color": list(r["color"]),
                                "stations": st}, line_geoms, station_db)
        paths.append(p)
        for w in warns:
            if "自動補完" not in w and "畳み込み" not in w and "間引き" not in w:
                print("[warn]", w)

    makers = {
        "cover": lambda: slide_cover(config, routes, paths, geojson_path),
        "fare": lambda: slide_fare(config, routes),
        "time": lambda: slide_time(config, routes),
        "freq": lambda: slide_freq(config, routes),
        "origin": lambda: slide_origin(config, routes),
        "summary": lambda: slide_summary(config, routes),
    }
    order = config.get("slides") or ["cover", "fare", "time", "freq", "origin", "summary"]
    total = len(order)
    written = []
    for page, name in enumerate(order, 1):
        img = makers[name]()
        if name != "cover":
            chrome(img, config, page, total, config.get("kicker", config.get("title_line1", "")))
        else:
            d = ImageDraw.Draw(img)
            d.text((W - PAD, 75), f"{page}/{total}", font=font(28, "bold"), fill=MUTED, anchor="rm")
        path = out / f"{page:02d}_{name}.png"
        img.convert("RGB").save(path, optimize=True)
        written.append(path)
        print("  wrote", path)

    cap = out / "caption.txt"
    cap.write_text(build_caption(config, routes), encoding="utf-8")
    written.append(cap)
    print("  wrote", cap)
    return str(out)
