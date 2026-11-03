# -*- coding: utf-8 -*-
"""共通の地図投影ロジック(簡易正距円筒図法 + 経度をcos(lat)で補正)"""
import math

CANVAS_W = 1080
CANVAS_H = 1920

# リール/ショート動画のUI(キャプション・アイコン等)が上下にかぶる
# 「セーフゾーン」。縦方向に8分割した時の一番上・一番下の1区画
# (それぞれ CANVAS_H/8 = 240px)には、タイトルとプログレスバーを
# 一切入れないようにする。地図(路線・駅)自体はこの区画にはみ出しても
# 構わないが、タイトル/凡例(render_base.py)とスコアボード(animate.py)
# はこの定数を基準に、必ず内側に収まるよう計算する。
SAFE_ZONE_PX = CANVAS_H // 8       # 240
SAFE_TOP_Y = SAFE_ZONE_PX          # 240: タイトルはこれより下に
SAFE_BOTTOM_Y = CANVAS_H - SAFE_ZONE_PX  # 1680: プログレスバーはこれより上に

# 地図を描画する領域(上部はタイトル、下部はスコアボード分を空ける)
MAP_TOP = 470
MAP_BOTTOM = 1540
MAP_LEFT = 60
MAP_RIGHT = 1020


def compute_projection(all_coords, pad_ratio=0.16):
    lons = [c[0] for c in all_coords]
    lats = [c[1] for c in all_coords]
    lon_min, lon_max = min(lons), max(lons)
    lat_min, lat_max = min(lats), max(lats)
    lat0 = (lat_min + lat_max) / 2
    cos0 = math.cos(math.radians(lat0))

    def to_plane(lon, lat):
        x = (lon - lon_min) * cos0
        y = (lat_max - lat)
        return x, y

    xs, ys = [], []
    for lon, lat in all_coords:
        x, y = to_plane(lon, lat)
        xs.append(x)
        ys.append(y)
    x_span = max(xs) - min(xs)
    y_span = max(ys) - min(ys)
    x_min, y_min = min(xs), min(ys)

    pad_x = x_span * pad_ratio
    pad_y = y_span * pad_ratio
    x_span_p = x_span + 2 * pad_x
    y_span_p = y_span + 2 * pad_y

    avail_w = MAP_RIGHT - MAP_LEFT
    avail_h = MAP_BOTTOM - MAP_TOP
    scale = min(avail_w / x_span_p, avail_h / y_span_p) if x_span_p and y_span_p else 1.0

    draw_w = x_span_p * scale
    draw_h = y_span_p * scale
    off_x = MAP_LEFT + (avail_w - draw_w) / 2
    off_y = MAP_TOP + (avail_h - draw_h) / 2

    return {
        "lon_min": lon_min, "lat_max": lat_max, "cos0": cos0,
        "x_min": x_min - pad_x, "y_min": y_min - pad_y,
        "scale": scale, "off_x": off_x, "off_y": off_y,
    }


def project(params, lon, lat):
    x = (lon - params["lon_min"]) * params["cos0"]
    y = (params["lat_max"] - lat)
    px = (x - params["x_min"]) * params["scale"] + params["off_x"]
    py = (y - params["y_min"]) * params["scale"] + params["off_y"]
    return px, py


def unproject(params, px, py):
    """project() の逆変換。画面上のある範囲(例: キャンバス+余白)が
    実際の経度緯度でどこに当たるかを知りたい時に使う
    (例: 海岸線データを表示範囲だけに間引く)。"""
    x = (px - params["off_x"]) / params["scale"] + params["x_min"]
    y = (py - params["off_y"]) / params["scale"] + params["y_min"]
    lon = x / params["cos0"] + params["lon_min"]
    lat = params["lat_max"] - y
    return lon, lat
