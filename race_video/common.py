# -*- coding: utf-8 -*-
"""
新ネタ動画(朝ラッシュ比較 / 運賃比較 / 普通列車抜かれ)で共通に使う描画部品。

既存の render_base.py / animate.py の見た目(暗い背景・陸地・発光ライン)を
そのまま流用しつつ、
  - 地図を描く領域(box)を動画ごとに変えられるベースマップ
  - 線路の左右にずらして描ける(=同じ線路上の2列車を並べて見せる)位置計算
  - 種別一文字バッジ付きの車両アイコン
  - ffmpegに生フレームをパイプで直接流し込む書き出し(連番PNGを作らないので速い)
をまとめてある。
"""
import bisect
import math
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .geo import CANVAS_W, CANVAS_H, SAFE_TOP_Y, SAFE_BOTTOM_Y, project
from .render_base import (
    load_all_lines, load_land_polygons, draw_land_and_sea, vertical_gradient,
    draw_glow_polyline,
)
from . import fonts as _fonts

FONT_BOLD, FONT_REGULAR, FONT_BLACK = _fonts.resolve()

_font_cache = {}


def font(size, weight="bold"):
    path = {"bold": FONT_BOLD, "black": FONT_BLACK, "regular": FONT_REGULAR}[weight]
    size = max(1, int(size))
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, int(size), index=0)
    return _font_cache[key]


# ---------------------------------------------------------------- 時刻ユーティリティ

def parse_hhmm(value):
    """'08:02' / '8:02:30' / 482.5(分の数値) を「0時からの分」に変換する。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().replace("：", ":").split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    s = float(parts[2]) if len(parts) > 2 else 0.0
    return h * 60 + m + s / 60.0


def fmt_hhmm(minutes, with_sec=False):
    minutes = minutes % (24 * 60)
    h = int(minutes // 60)
    m = int(minutes % 60)
    if with_sec:
        s = int(round((minutes * 60) % 60))
        if s == 60:
            s = 0
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}"


def monotonic_times(times):
    """日付またぎ(23:58→00:03)を考慮して、時刻列が単調増加になるよう+24hする。"""
    out = []
    add = 0.0
    prev = None
    for t in times:
        if t is None:
            out.append(None)
            continue
        if prev is not None and t + add < prev - 12 * 60:
            add += 24 * 60
        out.append(t + add)
        prev = t + add
    return out


# ---------------------------------------------------------------- 投影 / 地図

def compute_projection_box(all_coords, box, pad_ratio=0.12):
    """geo.compute_projection と同じ計算だが、地図を描く矩形 box=(l,t,r,b) を
    動画ごとに指定できる版。"""
    left, top, right, bottom = box
    lons = [c[0] for c in all_coords]
    lats = [c[1] for c in all_coords]
    lon_min, lat_max = min(lons), max(lats)
    lat0 = (min(lats) + lat_max) / 2
    cos0 = math.cos(math.radians(lat0))
    xs = [(lon - lon_min) * cos0 for lon in lons]
    ys = [(lat_max - lat) for lat in lats]
    x_span = max(max(xs) - min(xs), 1e-4)
    y_span = max(max(ys) - min(ys), 1e-4)
    pad_x, pad_y = x_span * pad_ratio, y_span * pad_ratio
    xsp, ysp = x_span + 2 * pad_x, y_span + 2 * pad_y
    aw, ah = right - left, bottom - top
    scale = min(aw / xsp, ah / ysp)
    off_x = left + (aw - xsp * scale) / 2
    off_y = top + (ah - ysp * scale) / 2
    return {
        "lon_min": lon_min, "lat_max": lat_max, "cos0": cos0,
        "x_min": min(xs) - pad_x, "y_min": min(ys) - pad_y,
        "scale": scale, "off_x": off_x, "off_y": off_y,
    }


def build_base_map(paths, box, title1="", title2="", legend=None,
                   geojson_path="data/routes.geojson", start_label=None, end_label=None,
                   dot_station_names=None, offsets=None, title_y=None, parallel=False):
    """背景グラデーション + 陸地 + 全路線(うす暗く) + 対象路線(発光) + 駅ドット
    + タイトル/凡例 を描いたベース画像と投影パラメータを返す。

    paths   : route_builder.build_route() の戻り値のリスト
    offsets : paths と同じ長さの、線路を左右にずらすピクセル量(省略時0)
    parallel: True ならずらし方を「コース全体の平行移動」にする(PolyPath参照)
    """
    focus = []
    for r in paths:
        focus.extend(r["polyline"])
    proj = compute_projection_box(focus, box)

    canvas = vertical_gradient(CANVAS_W, CANVAS_H, (8, 12, 28), (2, 4, 12)).convert("RGBA")
    land = load_land_polygons(str(Path(geojson_path).parent / "coastline.geojson"))
    draw_land_and_sea(canvas, proj, land)

    faint = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    fd = ImageDraw.Draw(faint)
    for _name, segs in load_all_lines(geojson_path):
        for seg in segs:
            pts = [project(proj, lon, lat) for lon, lat in seg]
            pts = [p for p in pts if -50 <= p[0] <= CANVAS_W + 50 and -50 <= p[1] <= CANVAS_H + 50]
            if len(pts) >= 2:
                fd.line(pts, fill=(150, 170, 210, 70), width=2, joint="curve")
    canvas.alpha_composite(faint)

    offsets = offsets or [0] * len(paths)
    for r, off in reversed(list(zip(paths, offsets))):
        pp = PolyPath(r, parallel=parallel)
        pts = pp.screen_polyline(proj, off)
        draw_glow_polyline(canvas, pts, tuple(r["color"]), width=9, glow_width=30, glow_alpha=85)

    d = ImageDraw.Draw(canvas, "RGBA")
    dot_station_names = set(dot_station_names or [])
    for r in paths:
        for i, st in enumerate(r["stations"]):
            is_term = i in (0, len(r["stations"]) - 1)
            if not (st.get("popup") or is_term or st["name"] in dot_station_names):
                continue
            x, y = project(proj, st["lon"], st["lat"])
            rad = 12 if is_term else 7
            d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 255, 255, 255),
                      outline=tuple(r["color"]) + (255,), width=4)

    first = paths[0]["stations"][0]
    x, y = project(proj, first["lon"], first["lat"])
    if start_label:
        d.text((x, y - 34), "START", font=font(24, "black"), fill=(255, 215, 0, 255), anchor="mm")
        d.text((x, y - 62), start_label, font=font(26), fill=(255, 255, 255, 255), anchor="mm",
               stroke_width=3, stroke_fill=(5, 8, 18, 255))
    if end_label:
        lasts = [project(proj, r["stations"][-1]["lon"], r["stations"][-1]["lat"]) for r in paths]
        fx = sum(p[0] for p in lasts) / len(lasts)
        fy = max(p[1] for p in lasts)
        d.text((fx, fy + 36), "FINISH", font=font(24, "black"), fill=(255, 215, 0, 255), anchor="mm")
        d.text((fx, fy + 64), end_label, font=font(26), fill=(255, 255, 255, 255), anchor="mm",
               stroke_width=3, stroke_fill=(5, 8, 18, 255))

    ty1 = title_y if title_y is not None else SAFE_TOP_Y + 50
    d.text((CANVAS_W / 2, ty1), title1, font=font(60, "black"), fill=(255, 255, 255, 255), anchor="mm")
    d.text((CANVAS_W / 2, ty1 + 58), title2, font=font(30), fill=(255, 215, 0, 255), anchor="mm")

    if legend:
        f_leg = font(28)
        widths = [d.textlength(name, font=f_leg) + 46 for name, _c in legend]
        total = sum(widths) + 40 * (len(widths) - 1)
        cx = CANVAS_W / 2 - total / 2
        ly = ty1 + 118
        for (name, color), w in zip(legend, widths):
            d.ellipse([cx, ly - 12, cx + 24, ly + 12], fill=tuple(color) + (255,))
            d.text((cx + 34, ly), name, font=f_leg, fill=(255, 255, 255, 255), anchor="lm")
            cx += w + 40

    # 毎フレームはRGBで描く(半透明の塗りがちゃんと下地と合成されるように)
    return canvas.convert("RGB"), proj


# ---------------------------------------------------------------- 線路上の位置

class PolyPath:
    """build_route() の結果から「起点からkm地点」の座標を引く。"""

    def __init__(self, route, parallel=False):
        """parallel=True のとき、offset_px は「コース全体を平行移動する量」になる。
        法線方向に1点ずつずらす方式(parallel=False)だとカーブの内側と外側で
        線の長さが変わり(内輪差)、並走する2本が奇妙にずれて見えるので、
        同じ線路を2本並べて見せる動画では平行移動を使う。"""
        self.polyline = route["polyline"]
        self.cum = route["cum_dist"]
        self.total_km = self.cum[-1]
        self.stations = route["stations"]
        self.parallel = parallel
        self._shift_dir = None

    def shift_dir(self, proj):
        """平行移動の向き: 始点→終点を結ぶ直線に垂直な単位ベクトル(画面座標)。"""
        if self._shift_dir is None:
            ax, ay = project(proj, *self.polyline[0])
            bx, by = project(proj, *self.polyline[-1])
            dx, dy = bx - ax, by - ay
            n = math.hypot(dx, dy) or 1.0
            self._shift_dir = (-dy / n, dx / n)
        return self._shift_dir

    def lonlat_at_km(self, km):
        cd = self.cum
        if km <= cd[0]:
            return tuple(self.polyline[0])
        if km >= cd[-1]:
            return tuple(self.polyline[-1])
        i = max(0, min(bisect.bisect_right(cd, km) - 1, len(cd) - 2))
        d0, d1 = cd[i], cd[i + 1]
        p0, p1 = self.polyline[i], self.polyline[i + 1]
        f = 0.0 if d1 == d0 else (km - d0) / (d1 - d0)
        return (p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f)

    def screen_at_km(self, proj, km, offset_px=0.0):
        x, y = project(proj, *self.lonlat_at_km(km))
        if not offset_px:
            return x, y
        if self.parallel:
            ux, uy = self.shift_dir(proj)
            return x + ux * offset_px, y + uy * offset_px
        # 前後約0.4kmの点から進行方向を求め、その法線方向にずらす
        span = max(0.15, min(0.4, self.total_km * 0.02))
        ax, ay = project(proj, *self.lonlat_at_km(max(0.0, km - span)))
        bx, by = project(proj, *self.lonlat_at_km(min(self.total_km, km + span)))
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        return x + (-dy / n) * offset_px, y + (dx / n) * offset_px

    def screen_polyline(self, proj, offset_px=0.0, km_from=0.0, km_to=None):
        km_to = self.total_km if km_to is None else km_to
        pts = []
        if not offset_px or self.parallel:
            pts.append(project(proj, *self.lonlat_at_km(km_from)))
            for (lon, lat), d in zip(self.polyline, self.cum):
                if km_from < d < km_to:
                    pts.append(project(proj, lon, lat))
            pts.append(project(proj, *self.lonlat_at_km(km_to)))
            if offset_px:
                ux, uy = self.shift_dir(proj)
                pts = [(x + ux * offset_px, y + uy * offset_px) for x, y in pts]
            return pts
        # オフセットありは等間隔サンプリング(法線が滑らかになる)
        n = max(2, int((km_to - km_from) / 0.12) + 1)
        for i in range(n + 1):
            pts.append(self.screen_at_km(proj, km_from + (km_to - km_from) * i / n, offset_px))
        return pts


def composite(canvas, img, xy=(0, 0)):
    """RGBA画像を重ねる。フレームはRGB(=ImageDraw の半透明塗りが正しく合成される)
    で扱うので paste のマスク合成を使い、RGBAキャンバスなら alpha_composite。"""
    if canvas.mode == "RGB":
        canvas.paste(img, (int(xy[0]), int(xy[1])), img)
    else:
        canvas.alpha_composite(img, (int(xy[0]), int(xy[1])))


def draw_trail(canvas, pts, color, width=6, glow=18):
    """進行済み区間のハイライト(外接矩形だけぼかすので軽い)。"""
    if len(pts) < 2:
        return
    cw, ch = canvas.size
    pad = 50
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bx0, by0 = max(0, int(min(xs) - pad)), max(0, int(min(ys) - pad))
    bx1, by1 = min(cw, int(max(xs) + pad)), min(ch, int(max(ys) + pad))
    if bx1 <= bx0 or by1 <= by0:
        return
    local = [(x - bx0, y - by0) for x, y in pts]
    crop = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
    ImageDraw.Draw(crop).line(local, fill=tuple(color) + (150,), width=glow, joint="curve")
    crop = crop.filter(ImageFilter.GaussianBlur(6))
    ImageDraw.Draw(crop).line(local, fill=(255, 255, 255, 235), width=width, joint="curve")
    composite(canvas, crop, (bx0, by0))


# ---------------------------------------------------------------- 車両アイコン

def load_icon(path, width=86):
    """assets/ などに置いた透過PNG(進行方向=右向き推奨)を幅 width px に縮小して返す。
    回転はしない。パスが無い/見つからない場合は None(=標準の簡易アイコン)。"""
    if path and not os.path.exists(path):
        print(f"[warn] 車両アイコン {path} が見つからないため、標準アイコンで描きます")
    if path and os.path.exists(path):
        img = Image.open(path).convert("RGBA")
        h = int(width * img.height / img.width)
        return img.resize((width, h))
    return None


def draw_train(canvas, cx, cy, color, badge=None, icon_img=None, size=56,
               badge_side="right", badge_color=None, scale=1.0, alpha=255):
    """非回転の車両アイコン + その隣に種別一文字バッジ(各/快/特 など)。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    size = size * scale
    color = tuple(color)
    if icon_img is not None:
        w, h = icon_img.size
        composite(canvas, icon_img, (cx - w / 2, cy - h / 2))
        half_w = w / 2
    else:
        w, h = size, size * 0.62
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        d.rounded_rectangle([x0 + 3, y0 + 5, x1 + 3, y1 + 5], radius=h / 2, fill=(0, 0, 0, 90))
        d.rounded_rectangle([x0, y0, x1, y1], radius=h / 2, fill=color + (alpha,),
                            outline=(255, 255, 255, int(230 * alpha / 255)), width=3)
        ww, gap, start = w * 0.16, w * 0.06, x0 + w * 0.16
        for i in range(3):
            wx = start + i * (ww + gap)
            d.rounded_rectangle([wx, y0 + h * 0.22, wx + ww, y0 + h * 0.6], radius=3,
                                fill=(235, 245, 255, alpha))
        d.ellipse([x1 - h * 0.22, y0 + h * 0.62, x1 - h * 0.02, y0 + h * 0.9], fill=(255, 240, 150, alpha))
        half_w = w / 2
    if badge:
        r = 21 * scale
        bx = cx + (half_w + r + 2) * (1 if badge_side == "right" else -1)
        bc = tuple(badge_color or color)
        d.ellipse([bx - r - 2, cy - r - 2, bx + r + 2, cy + r + 2], fill=(255, 255, 255, alpha))
        d.ellipse([bx - r, cy - r, bx + r, cy + r], fill=bc + (alpha,))
        d.text((bx, cy + 1), badge[:1], font=font(int(26 * scale), "black"),
               fill=(255, 255, 255, alpha), anchor="mm")


def draw_chip(canvas, x, y, text, color, size=30, alpha=255, scale=1.0, text_color=(255, 255, 255),
              tail=True, anchor_bottom=True):
    """駅のそばに出す吹き出しチップ(料金・駅名など)。(x,y) は吹き出しの先端位置。"""
    d = ImageDraw.Draw(canvas, "RGBA")
    f = font(int(size * scale), "black")
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    px, py = 18 * scale, 10 * scale
    h = th + 2 * py
    y_box_bottom = y - (14 * scale if tail else 0)
    bx0, bx1 = x - tw / 2 - px, x + tw / 2 + px
    by0, by1 = y_box_bottom - h, y_box_bottom
    # 画面からはみ出さないよう水平方向だけクランプ
    shift = 0
    if bx0 < 12:
        shift = 12 - bx0
    elif bx1 > CANVAS_W - 12:
        shift = CANVAS_W - 12 - bx1
    bx0 += shift
    bx1 += shift
    fill = (15, 18, 30, int(235 * alpha / 255))
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=12 * scale, fill=fill,
                        outline=tuple(color) + (alpha,), width=max(2, int(3 * scale)))
    if tail:
        d.polygon([(x - 9 * scale, by1), (x + 9 * scale, by1), (x, by1 + 14 * scale)], fill=tuple(color) + (alpha,))
    d.text(((bx0 + bx1) / 2, (by0 + by1) / 2), text, font=f, fill=tuple(text_color) + (alpha,), anchor="mm")


def pop_scale(elapsed, grow=0.18, settle=0.22):
    """ポップアップ用: 0→大きく→通常サイズ。"""
    if elapsed < 0:
        return 0.0
    if elapsed < grow:
        t = elapsed / grow
        c1 = 1.70158
        return 0.3 + 1.2 * (1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2)
    if elapsed < grow + settle:
        t = (elapsed - grow) / settle
        return 1.5 - 0.5 * t
    return 1.0


def ease_out_cubic(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def ease_in_out(x):
    x = max(0.0, min(1.0, x))
    return 3 * x * x - 2 * x * x * x


def shadow_text(d, xy, text, f, fill, anchor="mm", stroke=3):
    d.text(xy, text, font=f, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=(5, 8, 18, 255))


# ---------------------------------------------------------------- 書き出し

class FrameWriter:
    """PIL画像を ffmpeg に生フレームで流し込んで mp4 を作る。"""

    def __init__(self, out_path, fps):
        self.out_path = out_path
        self.fps = fps
        self.n = 0
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        self.proc = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{CANVAS_W}x{CANVAS_H}",
             "-r", str(fps), "-i", "-",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium",
             out_path],
            stdin=subprocess.PIPE,
        )

    def write(self, img):
        self.proc.stdin.write(img.convert("RGB").tobytes())
        self.n += 1
        if self.n % 60 == 0:
            print(f"  frame {self.n}")

    def close(self):
        self.proc.stdin.close()
        ret = self.proc.wait()
        if ret != 0:
            raise RuntimeError(f"ffmpeg failed with code {ret}")
        print("done:", self.out_path)
        return self.out_path


def write_text_script(out_dir, slug, lines):
    """ナレーション用の台本(.txt)を出力する。lines は (秒, 文) のリスト。"""
    os.makedirs(out_dir, exist_ok=True)
    path = f"{out_dir}/{slug}_script.txt"
    with open(path, "w", encoding="utf-8") as f:
        for t, text in lines:
            f.write(f"[{int(t // 60):02d}:{t % 60:05.2f}] {text}\n")
    return path


# ---------------------------------------------------------------- 経路の組み立て

def build_geometry(stations_cfg, line_geoms, station_db, key="route", name="", color=None):
    """駅の並び(line, name, popup)だけから、線形に沿ったポリラインと各駅の
    累積km(cum_km)を組み立てる。所要時間は動画ごとに別途扱うので、
    route_builder には仮の t_min(始点0・終点1)だけ渡す。"""
    from .route_builder import build_route
    st = []
    for i, s in enumerate(stations_cfg):
        e = {"line": s["line"], "name": s["name"], "popup": bool(s.get("popup", False))}
        e["t_min"] = 0 if i == 0 else (1 if i == len(stations_cfg) - 1 else None)
        st.append(e)
    cfg = {"key": key, "name": name or key, "color": list(color or [230, 230, 230]), "stations": st}
    route, warnings = build_route(cfg, line_geoms, station_db)
    # 元configの付加情報(fare など)を駅レコードに戻しておく
    for rec, src in zip(route["stations"], stations_cfg):
        for k, v in src.items():
            if k not in ("line", "name", "popup", "t_min"):
                rec[k] = v
    return route, [w for w in warnings if "自動補完" not in w]
