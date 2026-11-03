# -*- coding: utf-8 -*-
"""
使い方:
    python -m race_video.cli configs/shinjuku_fujisawa.json
    python -m race_video.cli configs/shinjuku_fujisawa.json --fast   # 低fpsプレビュー

configファイルの書き方は configs/shinjuku_fujisawa.json と README を参照。

config に "video_type" を書くと別ネタの動画になる:
    "race"     … 電車最速バトル(省略時)
    "rush"     … 朝ラッシュ vs 日中の所要時間比較     (race_video/rush_video.py)
    "fare"     … 競合路線の運賃比較                   (race_video/fare_video.py)
    "overtake" … 普通列車が何本に抜かれるか           (race_video/overtake_video.py)
    "carousel" … 運賃・所要時間・本数・始発を比べるカルーセル投稿画像
                 (race_video/carousel.py。動画用configを "base" で参照して使い回す)
"""
import argparse
import json
import sys

from .station_db import load_station_db, check_line_quality
from .route_builder import build_all_routes
from .render_base import render_base_map
from .animate import render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="レース設定JSONのパス")
    ap.add_argument("--geojson", default="data/routes.geojson")
    ap.add_argument("--shp", default="data/stations_shp/tokyo_stations")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--frames-dir", default="frames")
    ap.add_argument("--fast", action="store_true", help="10fpsの低画質プレビューを高速生成")
    ap.add_argument("--out-name", default=None,
                     help="出力ファイル名(拡張子なし)。省略時はconfigの'slug'を使う")
    ap.add_argument("--check-stations", action="store_true",
                     help="駅データSHPの品質チェックだけ実行して終了する")
    args = ap.parse_args()

    station_db = load_station_db(args.shp)

    if args.check_stations:
        for line, n, bad, worst in sorted(check_line_quality(station_db), key=lambda x: -x[2]):
            flag = "NG" if bad else "ok"
            print(f"{flag:3s} {line:16s} n={n:3d} suspicious_gaps={bad:3d} worst_gap_km={worst:.1f}")
        return

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    # 動画ネタの種類で振り分け(省略時は従来の「電車最速バトル」)
    video_type = config.get("video_type", "race")
    if video_type != "race":
        import importlib
        modules = {"rush": "rush_video", "fare": "fare_video", "overtake": "overtake_video",
                   "carousel": "carousel"}
        if video_type not in modules:
            print(f"未対応の video_type です: {video_type} (race / rush / fare / overtake / carousel)")
            return 1
        print(f"=== rendering '{video_type}': {config.get('title_line1', config.get('slug'))} ===")
        renderer = getattr(importlib.import_module(f".{modules[video_type]}", __package__),
                           f"render_{video_type}")
        out_path = renderer(config, args.geojson, station_db, out_dir=args.out_dir,
                            fast_preview=args.fast, out_name=args.out_name)
        print("done ->", out_path)
        return 0

    print(f"=== building routes for '{config.get('title_line1', config.get('slug'))}' ===")
    paths, warnings = build_all_routes(config, geojson_path=args.geojson, station_db=station_db)
    for w in warnings:
        print("[warn]", w)
    for key, route in paths.items():
        print(f"  {key}: {route['name']}  {len(route['polyline'])}pts  "
              f"{route['cum_dist'][-1]:.1f}km  {route['total_min']}min")

    print("=== rendering base map ===")
    base_map, proj = render_base_map(config, paths, geojson_path=args.geojson)
    base_map.convert("RGB").save(f"{args.out_dir}/{config.get('slug','race')}_base_map.png")

    print("=== rendering video ===")
    out_path = render(config, paths, base_map, proj,
                       out_dir=args.out_dir, frames_dir=args.frames_dir,
                       fast_preview=args.fast, out_name=args.out_name)
    print("done ->", out_path)


if __name__ == "__main__":
    sys.exit(main())
