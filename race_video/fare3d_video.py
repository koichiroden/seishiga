# -*- coding: utf-8 -*-
"""
運賃比較の「鳥瞰図(3D)」版。"video_type": "fare" の標準の見せ方
(以前の平面版と同じconfigで動く。平面版にしたいときだけ "view": "2d")。
最後のグラフの横軸は標準で「全行程の何%まで乗ったか」(0〜100%)。"chart_x": "km" で距離(km)にできる。
目印線: %表示では "compare_pct"(例 [50])、km表示では "compare_km"(例 [10, 20, 30])の位置に引く。

  1) タイトル   : 画面中央に大きくタイトル → 上部の見出しへ
  2) 鳥瞰図     : 地図を斜め上から見下ろし、各ルートの線路の上に「高さ=運賃」の壁を立てる。
                  車両が進むと後ろに壁が伸び、運賃が上がる駅で段が上がる。カメラはゆっくり回り込む。
  3) たたむ     : 壁が「ほどけて」(先に距離方向、少し遅れて運賃方向)重ね合わせの階段グラフに倒れ込む
  4) グラフ     : 横軸=距離 / 縦軸=運賃 の重ね合わせグラフ(主な駅名・終点の運賃)
  5) 最終運賃   : 平面版と同じカウントアップ

文字(駅名・金額)は「先に置いた文字と重ならない位置」を探して置き、どこにも置けないときは省略する。
既定は 60fps(config の "fps" で変更可、--fast は 20fps)。
"""
import math

import numpy as np
from PIL import ImageDraw

from .common import (
    CANVAS_W, CANVAS_H, SAFE_TOP_Y, font, build_geometry, draw_train, FrameWriter,
    ease_in_out, ease_out_cubic, pop_scale, write_text_script,
)
from .fare_video import FareRoute, PALETTE, yen, _draw_final
from .render_base import load_all_lines, load_land_polygons, vertical_gradient
from .route_builder import load_line_geoms
from pathlib import Path

STROKE = (5, 8, 18, 255)


def _nice(span, target=4):
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag)


class LabelPlacer:
    """文字を重ならない位置に置く。候補位置を順に試し、既に置いた矩形と重ならない最初の位置を使う。
    前のフレームで使った候補を優先するので、カメラが動いても文字がパタパタ動きにくい。"""

    def __init__(self, d, memory=None, pad=6):
        self.d = d
        self.boxes = []
        self.memory = memory if memory is not None else {}
        self.pad = pad

    def block(self, box):
        self.boxes.append(tuple(box))

    def _hits(self, b):
        p = self.pad
        for o in self.boxes:
            if b[0] - p < o[2] and b[2] + p > o[0] and b[1] - p < o[3] and b[3] + p > o[1]:
                return True
        return b[0] < 8 or b[2] > CANVAS_W - 8

    def place(self, key, x, y, text, fnt, offsets, anchor="mb", stroke=4):
        order = list(range(len(offsets)))
        if key in self.memory and self.memory[key] < len(offsets):
            order.remove(self.memory[key])
            order.insert(0, self.memory[key])
        for idx in order:
            dx, dy = offsets[idx]
            b = self.d.textbbox((x + dx, y + dy), text, font=fnt, anchor=anchor, stroke_width=stroke)
            if not self._hits(b):
                self.boxes.append(b)
                self.memory[key] = idx
                return (x + dx, y + dy), (dx, dy)
        return None, None


SCENE_OFFSETS = [(0, -16), (0, -50), (-80, -30), (80, -30), (0, -84), (-130, -6), (130, -6),
                 (-90, -70), (90, -70), (0, 44), (0, -118)]
CHART_OFFSETS = [(0, -14), (0, 36), (-60, -14), (60, -14), (0, -46), (-60, 36), (60, 36), (0, 68)]


def render_fare3d(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    slug = out_name or config.get("slug", "fare3d")
    data_dir = Path(geojson_path).parent
    geoms = load_line_geoms(geojson_path)
    routes = []
    for i, rc in enumerate(config["routes"]):
        r, warnings = build_geometry(rc["stations"], geoms, station_db, key=rc.get("key", f"r{i}"),
                                     name=rc.get("name", ""), color=rc.get("color") or PALETTE[i % len(PALETTE)])
        for w in warnings:
            print("[warn]", w)
        routes.append(FareRoute(rc, r, i))
    fare_label = config.get("fare_label", "運賃")

    # ------------------------------------------------------------ 平面座標(km)
    lon0 = float(np.mean([p[0] for r in routes for p in r.route["polyline"]]))
    lat0 = float(np.mean([p[1] for r in routes for p in r.route["polyline"]]))
    KX, KY = 111.32 * math.cos(math.radians(lat0)), 110.57

    def to_xy(lon, lat):
        return (np.asarray(lon) - lon0) * KX, (np.asarray(lat) - lat0) * KY

    for r in routes:
        # 0.25kmごとに線路をサンプリングし、各点の運賃を持たせる
        n = max(2, int(r.total_km / 0.25))
        kms = np.linspace(0, r.total_km, n + 1)
        pts = np.array([r.path.lonlat_at_km(r.k0 + k) for k in kms])
        x, y = to_xy(pts[:, 0], pts[:, 1])
        r.s_km, r.s_x, r.s_y = kms, x, y
        # 壁の高さ = 次に着く運賃駅までの運賃(出発直後から初乗り運賃、終点の値上がりも最後の区間に出る)
        fk = np.array([p[0] for p in r.points[1:]])
        fv = np.array([p[1] for p in r.points[1:]], dtype=float)
        r.s_fare = fv[np.minimum(np.searchsorted(fk - 1e-6, kms), len(fv) - 1)]
        r.g2d = (np.array([p[0] for p in r.points]), np.array([p[0] for p in r.dpoints]))
        r.s_dkm = np.interp(r.s_km, *r.g2d)
        if True:
            # 走った距離で運賃を決める(駅の途中でも距離の境目で上がる)。距離運賃表のルートは表の境目、
            # 駅ごとの運賃のルートは隣の運賃駅とのあいだで推定した境目(fare_video.estimate_edge)。
            # 境目の位置がずれないよう、境目の前後にも点を足してサンプリングし直す
            edges = np.array(r.fare_edges(), dtype=float)
            dk = np.unique(np.concatenate([r.s_dkm, edges + 1e-3, edges - 1e-3]))
            dk = dk[(dk >= 0) & (dk <= r.s_dkm[-1])]
            kms = np.interp(dk, r.g2d[1], r.g2d[0])
            pts = np.array([r.path.lonlat_at_km(r.k0 + k) for k in kms])
            x, y = to_xy(pts[:, 0], pts[:, 1])
            r.s_km, r.s_x, r.s_y, r.s_dkm = kms, x, y, dk
            r.s_fare = np.array([r.fare_at_dkm(d) for d in dk])
        r.st_xy = [(to_xy(st["lon"], st["lat"]), k, f, st) for k, f, st in r.points]

    allx = np.concatenate([r.s_x for r in routes])
    ally = np.concatenate([r.s_y for r in routes])
    cxw, cyw = (allx.min() + allx.max()) / 2, (ally.min() + ally.max()) / 2
    extent = max(allx.max() - allx.min(), ally.max() - ally.min(), 1.0)
    max_fare = max(max(r.final_fare, float(np.max(r.s_fare))) for r in routes)
    z_scale = extent * 0.42 / max_fare

    margin = extent * 0.9
    bx0, bx1, by0, by1 = cxw - margin, cxw + margin, cyw - margin, cyw + margin
    land = []
    for ring in load_land_polygons(str(data_dir / "coastline.geojson")):
        a = np.array(ring)
        x, y = to_xy(a[:, 0], a[:, 1])
        if x.max() < bx0 or x.min() > bx1 or y.max() < by0 or y.min() > by1:
            continue
        land.append(np.stack([np.clip(x, bx0, bx1), np.clip(y, by0, by1), np.zeros_like(x)], 1))
    rails = []
    for _name, segs in load_all_lines(geojson_path):
        for seg in segs:
            a = np.array(seg)
            x, y = to_xy(a[:, 0], a[:, 1])
            m = (x > bx0) & (x < bx1) & (y > by0) & (y < by1)
            if m.sum() >= 2:
                rails.append(np.stack([x[m], y[m], np.zeros(m.sum())], 1))

    ruler_xy = (float(allx.min() + extent * 0.02), float(ally.min() + extent * 0.05))
    ruler_step = 250 if max_fare > 600 else 100
    ruler_top = math.ceil(max_fare * 1.05 / ruler_step) * ruler_step

    # ------------------------------------------------------------ カメラ
    scene = (110, 440, CANVAS_W - 40, 1560)
    az0, az1 = float(config.get("camera_az_start", -38)), float(config.get("camera_az_end", 18))
    elev = float(config.get("camera_elevation", 44))

    def camera(az_deg):
        dist = extent * 2.1
        az, el = math.radians(az_deg), math.radians(elev)
        target = np.array([cxw, cyw, extent * 0.10])
        cam = target + dist * np.array([math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])
        fwd = target - cam
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, [0, 0, 1.0])
        right /= np.linalg.norm(right)
        return cam, right, np.cross(right, fwd), fwd

    def project_raw(P, cam):
        c, right, up, fwd = cam
        d = P - c
        pz = d @ fwd
        return np.stack([d @ right / pz, d @ up / pz], 1), pz

    probe = np.concatenate([np.stack([r.s_x, r.s_y, r.s_fare * z_scale], 1) for r in routes] +
                           [np.stack([r.s_x, r.s_y, np.zeros_like(r.s_x)], 1) for r in routes] +
                           [np.array([[ruler_xy[0], ruler_xy[1], 0], [ruler_xy[0], ruler_xy[1], ruler_top * z_scale]])])
    F = 1e9
    for az in np.linspace(az0, az1, 9):
        q, _ = project_raw(probe, camera(az))
        F = min(F, (scene[2] - scene[0]) * 0.92 / (np.ptp(q[:, 0]) or 1), (scene[3] - scene[1]) * 0.92 / (np.ptp(q[:, 1]) or 1))

    def make_projector(az):
        cam = camera(az)
        q, _ = project_raw(probe, cam)
        mx, my = (q[:, 0].max() + q[:, 0].min()) / 2, (q[:, 1].max() + q[:, 1].min()) / 2
        sx, sy = (scene[0] + scene[2]) / 2, (scene[1] + scene[3]) / 2

        def proj(P):
            qq, pz = project_raw(np.atleast_2d(np.asarray(P, float)), cam)
            return np.stack([sx + (qq[:, 0] - mx) * F, sy - (qq[:, 1] - my) * F], 1), pz
        return proj

    # ------------------------------------------------------------ 2Dグラフ(射影先)
    CH = (170, 560, CANVAS_W - 200, 1300)
    # 横軸: 標準は「全行程の何%まで乗ったか」(長さの違うルートでも始点・終点がそろう)。"chart_x": "km" で距離(km)
    PCT = config.get("chart_x", "percent") != "km"
    DMAX = 103.0 if PCT else max(r.disp_total_km for r in routes) * 1.03

    def GX(r, k):
        return np.asarray(k) / max(r.disp_total_km, 1e-6) * 100.0 if PCT else np.asarray(k)
    FMAX = max_fare * 1.12

    def CX(k):
        return CH[0] + (CH[2] - CH[0]) * np.asarray(k) / DMAX

    def CY(v):
        return CH[3] - (CH[3] - CH[1]) * np.asarray(v) / FMAX

    def wall_strips(r, m=None):
        """壁を「同じ運賃が続く区間(最大約3km)」の帯に分け、3D座標と2Dグラフ座標を返す"""
        out = []
        m = len(r.s_km) - 1 if m is None else m
        i = 0
        while i < m:
            j = i
            while j < m and r.s_fare[j] == r.s_fare[i] and j - i < 12:
                j += 1
            h = r.s_fare[i]
            xs, ys, ds = r.s_x[i:j + 1], r.s_y[i:j + 1], r.s_dkm[i:j + 1]
            p3 = np.concatenate([np.stack([xs, ys, np.zeros_like(xs)], 1),
                                 np.stack([xs, ys, np.full_like(xs, h * z_scale)], 1)[::-1]])
            gx = CX(GX(r, ds))
            p2 = np.concatenate([np.stack([gx, np.full_like(ds, CY(0))], 1),
                                 np.stack([gx, np.full_like(ds, CY(h))], 1)[::-1]])
            out.append((p3, p2, h))
            i = j
        return out

    full_strips = {id(r): wall_strips(r) for r in routes}

    # ------------------------------------------------------------ 描画部品
    bg = vertical_gradient(CANVAS_W, CANVAS_H, (8, 12, 28), (2, 4, 12))
    label_memory = {}

    def lighter(c, f=0.35):
        return tuple(int(v + (255 - v) * f) for v in c)

    def draw_ground(img, proj):
        d = ImageDraw.Draw(img, "RGBA")
        for poly in land:
            q, pz = proj(poly)
            if (pz > 0).all():
                d.polygon([tuple(p) for p in q], fill=(26, 26, 24, 235))
        for line in rails:
            q, pz = proj(line)
            if (pz > 0).all():
                d.line([tuple(p) for p in q], fill=(150, 170, 210, 55), width=2)
        step = 5.0
        for gx in np.arange(math.floor(bx0 / step) * step, bx1 + 0.1, step):
            q, _ = proj([[gx, by0, 0], [gx, by1, 0]])
            d.line([tuple(q[0]), tuple(q[1])], fill=(120, 150, 200, 22), width=1)
        for gy in np.arange(math.floor(by0 / step) * step, by1 + 0.1, step):
            q, _ = proj([[bx0, gy, 0], [bx1, gy, 0]])
            d.line([tuple(q[0]), tuple(q[1])], fill=(120, 150, 200, 22), width=1)

    def draw_header(img, alpha=255):
        d = ImageDraw.Draw(img, "RGBA")
        a = int(alpha)
        ty = SAFE_TOP_Y + 50
        d.text((CANVAS_W / 2, ty), config.get("title_line1", ""), font=font(60, "black"), fill=(255, 255, 255, a),
               anchor="mm")
        d.text((CANVAS_W / 2, ty + 58), config.get("title_line2", ""), font=font(30), fill=(255, 215, 0, a),
               anchor="mm")
        f_leg = font(26)
        items = [(r.name, r.color) for r in routes] + [("高さ = " + fare_label, None)]
        widths = [d.textlength(n, font=f_leg) + 40 for n, _ in items]
        cx = CANVAS_W / 2 - (sum(widths) + 30 * (len(items) - 1)) / 2
        for (n, c), w in zip(items, widths):
            if c is None:
                d.rectangle([cx + 6, ty + 104, cx + 18, ty + 128], fill=(255, 255, 255, int(200 * a / 255)))
            else:
                d.ellipse([cx, ty + 104, cx + 24, ty + 128], fill=tuple(c) + (a,))
            d.text((cx + 32, ty + 116), n, font=f_leg, fill=(255, 255, 255, a), anchor="lm")
            cx += w + 30

    def draw_title_card(img, t, dur):
        """オープニング: 中央に大きくタイトル(0.4秒で出て、終盤で上の見出しへ移る)"""
        d = ImageDraw.Draw(img, "RGBA")
        appear = ease_out_cubic(t / 0.45)
        leave = ease_in_out(max(0.0, (t - (dur - 0.6)) / 0.6))
        d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(200 * (1 - leave))))
        a = int(255 * appear * (1 - leave))
        cy = CANVAS_H / 2 - 140 - 380 * leave
        sc = pop_scale(t * 0.9) if t < 0.5 else 1.0
        kicker = config.get("title_kicker", f"{fare_label}くらべ")
        d.text((CANVAS_W / 2, cy - 120), kicker, font=font(40, "black"), fill=(255, 215, 0, a), anchor="mm")
        d.text((CANVAS_W / 2, cy), config.get("title_line1", ""), font=font(int(104 * sc), "black"),
               fill=(255, 255, 255, a), anchor="mm", stroke_width=4, stroke_fill=STROKE)
        d.text((CANVAS_W / 2, cy + 110), config.get("title_line2", ""), font=font(46, "black"),
               fill=(255, 255, 255, a), anchor="mm")
        yy = cy + 210
        for r in routes:
            d.text((CANVAS_W / 2, yy), r.name, font=font(40, "black"), fill=tuple(r.color) + (a,), anchor="mm",
                   stroke_width=3, stroke_fill=STROKE)
            yy += 62
            if r is not routes[-1]:
                d.text((CANVAS_W / 2, yy - 4), "vs", font=font(30, "black"), fill=(255, 255, 255, a), anchor="mm")
                yy += 46

    def draw_scene(img, proj, km_now, tv, map_t0, speed):
        d = ImageDraw.Draw(img, "RGBA")
        placer = LabelPlacer(d, label_memory)
        placer.block((0, 0, CANVAS_W, SAFE_TOP_Y + 150))      # 見出し・凡例の場所には置かない
        for r in routes:
            q, _ = proj(np.stack([r.s_x, r.s_y, np.zeros_like(r.s_x)], 1))
            d.line([tuple(p) for p in q], fill=tuple(r.color) + (150,), width=4, joint="curve")
        # 運賃の物差し
        sx0, sy0 = ruler_xy
        q, _ = proj([[sx0, sy0, 0], [sx0, sy0, ruler_top * z_scale]])
        d.line([tuple(q[0]), tuple(q[1])], fill=(255, 255, 255, 120), width=2)
        for v in range(0, int(ruler_top) + 1, ruler_step):
            qq, _ = proj([[sx0, sy0, v * z_scale]])
            x, y = qq[0]
            d.line([(x - 8, y), (x + 8, y)], fill=(255, 255, 255, 150), width=2)
            d.text((x - 14, y), yen(v), font=font(20), fill=(220, 225, 240, 220), anchor="rm",
                   stroke_width=3, stroke_fill=STROKE)
            placer.block(d.textbbox((x - 14, y), yen(v), font=font(20), anchor="rm", stroke_width=3))
        # 壁(通過済み区間だけ、奥から)
        strips = []
        for r in routes:
            m = int(np.searchsorted(r.s_km, min(km_now, r.total_km)))
            if m < 1:
                continue
            for p3, _p2, _h in wall_strips(r, m):
                qq, pz = proj(p3)
                strips.append((pz.mean(), qq, r))
        strips.sort(key=lambda t: -t[0])
        for _z, qq, r in strips:
            d.polygon([tuple(p) for p in qq], fill=tuple(r.color) + (105,))
        for r in routes:
            m = int(np.searchsorted(r.s_km, min(km_now, r.total_km)))
            if m < 1:
                continue
            tp = []
            for i in range(m):
                h = r.s_fare[i] * z_scale
                tp += [[r.s_x[i], r.s_y[i], h], [r.s_x[i + 1], r.s_y[i + 1], h]]
            if km_now >= r.total_km - 1e-6:
                # 終点で運賃が上がる場合: 終点で上辺を運賃の高さまで立ち上げる
                tp.append([r.s_x[-1], r.s_y[-1], r.final_fare * z_scale])
            qq, _ = proj(np.array(tp))
            d.line([tuple(p) for p in qq], fill=lighter(r.color) + (255,), width=4)
        # 車両(文字より先に場所を確保)
        for r in routes:
            lon, lat = r.path.lonlat_at_km(r.k0 + min(km_now, r.total_km))
            x, y = to_xy(lon, lat)
            qq, _ = proj([[float(x), float(y), 0]])
            draw_train(img, qq[0][0], qq[0][1], tuple(r.color), badge=r.badge, icon_img=r.icon, scale=0.8)
            placer.block((qq[0][0] - 70, qq[0][1] - 26, qq[0][0] + 70, qq[0][1] + 26))
        # 運賃駅の柱と金額。通過した順に置く(終着駅は最優先)
        labels = []
        for r in routes:
            for (sx, sy), kst, fare, st in r.st_xy[1:]:
                if kst > km_now:
                    continue
                qq, _ = proj([[float(sx), float(sy), 0], [float(sx), float(sy), fare * z_scale]])
                d.line([tuple(qq[0]), tuple(qq[1])], fill=(255, 255, 255, 170), width=2)
                last = st is r.route["stations"][-1]
                if st.get("popup") or last:
                    labels.append((0 if last else 1, kst, r, st, fare, qq[1]))
        labels.sort(key=lambda t: (t[0], t[1]))
        for _prio, kst, r, st, fare, top in labels:
            el = tv - (map_t0 + kst / speed)
            sc = pop_scale(el) if el < 0.4 else 1.0
            fnt = font(int(26 * sc), "black")
            text = f"{st['name']} {yen(fare)}"
            pos, off = placer.place((r.key, st["name"]), top[0], top[1], text, fnt, SCENE_OFFSETS)
            if pos is None:
                continue
            if abs(off[0]) + abs(off[1] + 16) > 20:
                d.line([tuple(top), (pos[0], pos[1] + (6 if off[1] < 0 else -30))], fill=tuple(r.color) + (200,), width=2)
            d.text(pos, text, font=fnt, fill=tuple(r.color) + (255,), anchor="mb", stroke_width=4, stroke_fill=STROKE)

    def draw_chart_axes(d, alpha):
        a = int(alpha)
        x0, y0, x1, y1 = CH
        ks, fs = (25.0 if PCT else _nice(DMAX, 5)), _nice(FMAX, 4)
        k = 0.0
        while k <= DMAX:
            d.line([(CX(k), y0), (CX(k), y1)], fill=(255, 255, 255, int(28 * a / 255)), width=1)
            d.text((CX(k), y1 + 26), f"{k:g}%" if PCT else f"{k:g}km", font=font(24), fill=(190, 200, 220, a), anchor="mm")
            k += ks
        v = 0.0
        while v <= FMAX:
            d.line([(x0, CY(v)), (x1, CY(v))], fill=(255, 255, 255, int(28 * a / 255)), width=1)
            d.text((x0 - 12, CY(v)), yen(v), font=font(24), fill=(190, 200, 220, a), anchor="rm")
            v += fs
        d.line([(x0, y1), (x1, y1)], fill=(255, 255, 255, int(150 * a / 255)), width=2)
        d.line([(x0, y0), (x0, y1)], fill=(255, 255, 255, int(150 * a / 255)), width=2)
        d.text((CANVAS_W / 2, y0 - 56), config.get("chart_title", "地図の壁をたたむと…"), font=font(42, "black"),
               fill=(255, 255, 255, a), anchor="mm")
        d.text((CANVAS_W / 2, y1 + 74), (f"横軸=全行程の何%まで乗ったか / 縦軸={fare_label}" if PCT else f"横軸=乗った距離 / 縦軸={fare_label}"), font=font(26),
               fill=(255, 215, 0, a), anchor="mm")

    def draw_morph(img, proj, u, label_alpha):
        fx = ease_in_out(min(1.0, u / 0.65))                  # 先に距離方向をほどく
        fy = ease_in_out(max(0.0, (u - 0.25) / 0.75))         # 少し遅れて運賃方向をそろえる
        f = ease_in_out(u)
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(225 * f)))
        draw_chart_axes(d, 255 * f)

        def mix(q3, p2):
            return np.stack([q3[:, 0] * (1 - fx) + p2[:, 0] * fx, q3[:, 1] * (1 - fy) + p2[:, 1] * fy], 1)
        items = []
        for r in routes:
            for p3, p2, h in full_strips[id(r)]:
                q3, pz = proj(p3)
                items.append((pz.mean(), mix(q3, p2), r))
        items.sort(key=(lambda t: -t[0]) if f < 0.5 else (lambda t: -t[2].final_fare))   # グラフでは安い方を手前
        for _z, q, r in items:
            d.polygon([tuple(p) for p in q], fill=tuple(r.color) + (int(105 + 40 * f),))
        for r in routes:
            tops = []
            for p3, p2, h in full_strips[id(r)]:
                n = len(p3) // 2
                q3, _ = proj(p3[n:][::-1])
                tops += [tuple(p) for p in mix(q3, p2[n:][::-1])]
            # 終点の立ち上がり(終点で運賃が上がる場合も最後の段が見えるように)
            q3, _ = proj(np.array([[r.s_x[-1], r.s_y[-1], r.final_fare * z_scale]]))
            tops += [tuple(p) for p in mix(q3, np.array([[float(CX(GX(r, r.disp_total_km))), float(CY(r.final_fare))]]))]
            d.line(tops, fill=lighter(r.color) + (255,), width=5, joint="curve")
        if label_alpha <= 0:
            return
        a = int(label_alpha)
        placer = LabelPlacer(d)
        placer.block((CH[0] - 80, CH[3] - 4, CH[2] + 40, CH[3] + 100))   # 目盛り・説明文
        # 終点の運賃(最優先。右端に縦に並べる)
        ends = sorted([[float(CX(GX(r, r.disp_total_km))), float(CY(r.final_fare)), r] for r in routes], key=lambda e: e[1])
        for e1, e2 in zip(ends, ends[1:]):
            if e2[1] - e1[1] < 76:
                e2[1] = e1[1] + 76
        for x, y, r in ends:
            d.text((x + 14, y), r.short, font=font(28, "black"), fill=tuple(r.color) + (a,), anchor="lb",
                   stroke_width=3, stroke_fill=STROKE)
            d.text((x + 14, y + 2), yen(r.final_fare), font=font(34, "black"), fill=(255, 255, 255, a), anchor="lt",
                   stroke_width=3, stroke_fill=STROKE)
            placer.block(d.textbbox((x + 14, y), r.short, font=font(28, "black"), anchor="lb", stroke_width=3))
            placer.block(d.textbbox((x + 14, y + 2), yen(r.final_fare), font=font(34, "black"), anchor="lt", stroke_width=3))
        # 同じ距離の目印(compare_km)
        # %表示のときは "compare_pct"(例 [50])、km表示のときは "compare_km" の位置に目印
        marks_cfg = (config.get("compare_pct") or []) if PCT else (config.get("compare_km") or [])
        for km in [k for k in marks_cfg if 0 < k < DMAX]:
            x = float(CX(km))
            for yy in range(int(CH[1]), int(CH[3]), 18):
                d.line([(x, yy), (x, min(CH[3], yy + 9))], fill=(255, 215, 0, int(a * 0.8)), width=2)
            d.text((x, CH[1] - 8), (f"{km:g}%" if PCT else f"約{km:g}km"), font=font(22, "black"), fill=(255, 215, 0, a), anchor="mb",
                   stroke_width=3, stroke_fill=STROKE)
            placer.block(d.textbbox((x, CH[1] - 8), (f"{km:g}%" if PCT else f"約{km:g}km"), font=font(22, "black"), anchor="mb", stroke_width=3))
        # 主な駅(点は必ず描き、名前は重ならない所にだけ置く)
        marks = []
        for r in routes:
            for (k, fare, st) in r.dpoints[1:-1]:
                if st.get("popup"):
                    x, y = float(CX(GX(r, k))), float(CY(fare))
                    d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=tuple(r.color) + (a,), outline=(255, 255, 255, a), width=2)
                    placer.block((x - 7, y - 7, x + 7, y + 7))
                    marks.append((k, x, y, r, st))
        for k, x, y, r, st in sorted(marks, key=lambda t: t[0]):
            # 駅名は路線カラー(暗い背景で読めるよう少し明るくし、太字にする)
            fnt = font(22, "black")
            col = lighter(r.color, 0.25)
            pos, off = placer.place(None, x, y, st["name"], fnt, CHART_OFFSETS, stroke=3)
            if pos is None:
                continue
            if abs(off[0]) > 0 or abs(off[1]) > 20:
                d.line([(x, y), (pos[0], pos[1] - (8 if off[1] > 0 else -2))], fill=col + (int(a * 0.7),), width=2)
            d.text(pos, st["name"], font=fnt, fill=col + (a,), anchor="mb", stroke_width=4, stroke_fill=STROKE)

    # ------------------------------------------------------------ タイムライン
    fps = 20 if fast_preview else int(config.get("fps", 60))
    title_sec = float(config.get("title_sec", 2.2))
    map_sec = float(config.get("map_sec", 14.0))
    hold, morph_sec = 1.5, float(config.get("morph_sec", 3.0))
    chart_sec, final_sec = float(config.get("chart_sec", 3.5)), float(config.get("final_sec", 5.5))
    max_km = max(r.total_km for r in routes)
    speed = max_km / map_sec
    map_t0 = title_sec
    morph_t0 = map_t0 + map_sec + hold
    chart_t0 = morph_t0 + morph_sec
    final_t0 = chart_t0 + chart_sec
    total = final_t0 + final_sec

    script = [(0.0, f"{config.get('title_line1', '')}、{fare_label}が安いのはどっち?")]
    for r in routes:
        for k, fare, st in r.points[1:]:
            if st.get("popup"):
                script.append((map_t0 + k / speed, f"{r.short}、{st['name']}までは{int(fare)}円。"))
    script.append((morph_t0, "地図の壁をたたむと、距離と運賃のグラフになります。"))
    w = min(routes, key=lambda r: r.final_fare)
    script.append((final_t0, f"最終目的地までの運賃は、{w.short}が一番安い{int(w.final_fare)}円。"))
    write_text_script(out_dir, slug, sorted(script, key=lambda x: x[0]))

    writer = FrameWriter(f"{out_dir}/{slug}.mp4", fps)
    n_frames = int(total * fps)
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    for fi in range(n_frames):
        tv = fi / fps
        az = az0 + (az1 - az0) * ease_in_out(min(1.0, tv / morph_t0))
        proj = make_projector(az)
        img = bg.copy()
        draw_ground(img, proj)
        km_now = max(0.0, (tv - map_t0) * speed)
        if tv < morph_t0:
            draw_scene(img, proj, km_now, tv, map_t0, speed)
        else:
            u = min(1.0, (tv - morph_t0) / morph_sec)
            draw_morph(img, proj, u, 255 * ease_out_cubic((tv - chart_t0) / 0.5) if tv >= chart_t0 else 0)
        if tv < title_sec:
            draw_header(img, 255 * ease_in_out(max(0.0, (tv - (title_sec - 0.6)) / 0.6)))
            draw_title_card(img, tv, title_sec)
        else:
            draw_header(img)
        if tv >= final_t0:
            _draw_final(img, config, routes, tv - final_t0)
        writer.write(img)
    return writer.close()
