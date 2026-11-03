# -*- coding: utf-8 -*-
"""
station_data.json (config_builder.html / artifact 用の駅データ) に、
上野東京ラインの一部区間だけを取り出した「愛称レベルの路線」を
scrambled=false(=範囲選択が使える清潔な順序)として追加するワンショットスクリプト。

実データ(station_db / routes.geojson)には「東海道本線」「高崎線」「宇都宮線」
という路線名は存在せず、これらの区間はすべて「上野東京ライン」という1つの
路線名の中に(複数の系統が混線した状態で)含まれている。
race_video.route_builder.LINE_ALIASES で、これらの愛称をrace_video側の
実路線名(上野東京ライン)に変換して座標・線形を引く。

このスクリプトは、その愛称路線の「駅の並び順」を、ユーザーが確認した
実際の停車順に基づいて、上野東京ラインの実座標から再計算して
station_data.json に書き込む。
"""
import json
import sys

sys.path.insert(0, ".")
from race_video.station_db import load_station_db, get_station_lonlat, haversine_km  # noqa: E402

STATION_DATA_PATH = "/tmp/work/artifact/station_data.json"
BASE_LINE = "上野東京ライン"

# ユーザー提供の停車順(重複区間は上野東京ラインの実データから座標を引く)
VIRTUAL_LINES = {
    "高崎線": [
        "高崎", "倉賀野", "新町", "神保原", "本庄", "岡部", "深谷", "籠原", "熊谷",
        "行田", "吹上", "北鴻巣", "鴻巣", "北本", "桶川", "北上尾", "上尾", "宮原", "大宮",
    ],
    "宇都宮線": [
        "宇都宮", "雀宮", "石橋", "自治医大", "小金井", "小山", "間々田", "野木", "古河",
        "栗橋", "東鷲宮", "久喜", "新白岡", "白岡", "蓮田", "東大宮", "土呂", "大宮",
    ],
    "東海道本線": [
        "東京", "新橋", "品川", "川崎", "横浜", "戸塚", "大船", "藤沢", "辻堂",
        "茅ヶ崎", "平塚", "大磯", "二宮", "国府津", "鴨宮", "小田原",
        "早川", "根府川", "真鶴", "湯河原", "熱海",
    ],
}


def main():
    db = load_station_db()
    with open(STATION_DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    for virtual_name, names in VIRTUAL_LINES.items():
        coords = []
        for name in names:
            lon, lat = get_station_lonlat(db, BASE_LINE, name)
            coords.append((lon, lat))
        cum = [0.0]
        for i in range(1, len(coords)):
            cum.append(cum[-1] + haversine_km(coords[i - 1], coords[i]))
        stations = [
            {"name": name, "seq": i + 1, "cum_km": round(cum[i], 3)}
            for i, name in enumerate(names)
        ]
        data[virtual_name] = {"scrambled": False, "stations": stations}
        print(f"{virtual_name}: {len(stations)} stations, {cum[-1]:.1f}km")

    with open(STATION_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("wrote", STATION_DATA_PATH)


if __name__ == "__main__":
    main()
