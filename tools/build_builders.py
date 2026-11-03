# -*- coding: utf-8 -*-
"""
tools/src/*.html (ビルダーのテンプレート) に、共通CSS/JS と駅データ
(data/stations_shp のSHP + tools/src/alias_lines.json)・系統プリセット
(tools/config_builder.html から抽出)を埋め込んで、1ファイルで動く
tools/<name>_builder.html を生成する。

    python3 tools/build_builders.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"


# 駅の並び順が複数系統で混線していて、範囲選択ではなく系統プリセットで選ばせる路線
SCRAMBLED_LINES = {"上野東京ライン", "湘南新宿ライン"}


def station_data_from_shp():
    """動画生成(Python)と同じ駅データSHP(data/stations_shp/tokyo_stations)から、
    ビルダー用の駅一覧JSONを作る。以前は config_builder.html に埋め込まれた古い
    駅一覧をコピーしていたため、路線が足りない・埼京線の駅順が違うなどの不整合が
    あった。SHPを唯一の正にすることで、Python側と必ず一致させる。"""
    import sys
    sys.path.insert(0, str(ROOT.parent))
    from race_video.station_db import load_station_db
    db = load_station_db(str(ROOT.parent / "data" / "stations_shp" / "tokyo_stations"))
    data = {}
    for line, rows in db.items():
        data[line] = {
            "scrambled": line in SCRAMBLED_LINES,
            "stations": [{"name": r["name"], "seq": r["seq"], "cum_km": round(r["cum_km"], 3)} for r in rows],
        }
    # SHPに無い愛称路線(東海道本線・宇都宮線・高崎線)は手作業で用意した停車順を足す
    alias = json.loads((SRC / "alias_lines.json").read_text(encoding="utf-8"))
    for k, v in alias.items():
        if not k.startswith("_"):
            data[k] = v
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def update_config_builder_station_data(station_json):
    """既存の駅レースビルダー(config_builder.html)の埋め込み駅データも同じものに更新する。"""
    p = ROOT / "config_builder.html"
    h = p.read_text(encoding="utf-8")
    new = re.sub(r'(<script id="station-data" type="application/json">)(.*?)(</script>)',
                 lambda m: m.group(1) + station_json + m.group(3), h, count=1, flags=re.S)
    if new != h:
        p.write_text(new, encoding="utf-8")
        print("updated station data in", p)


def extract_from_config_builder():
    h = (ROOT / "config_builder.html").read_text(encoding="utf-8")
    station_json = station_data_from_shp()
    i = h.index("const MANUAL_PRESETS = {")
    depth, j = 0, h.index("{", i)
    for k in range(j, len(h)):
        if h[k] == "{":
            depth += 1
        elif h[k] == "}":
            depth -= 1
            if depth == 0:
                break
    presets = h[i:k + 1] + ";"
    return station_json, presets


def overtake_sample_state():
    """configs/overtake_keikyu_shinagawa_yokohama.json をビルダーの初期状態に変換する。"""
    import json
    c = json.loads((ROOT.parent / "configs" / "overtake_keikyu_shinagawa_yokohama.json").read_text(encoding="utf-8"))

    def fmt_p(v):
        if v is None:
            return ""
        if isinstance(v, dict):
            return f"{v['arr']}/{v['dep']}"
        if isinstance(v, list):
            return "/".join(v)
        return str(v)

    rows = {}
    n = len(c["stations"])
    for i, s in enumerate(c["stations"]):
        v = c["local"]["times"].get(s["name"])
        arr = dep = ""
        if isinstance(v, dict):
            arr, dep = v["arr"], v["dep"]
        elif v:
            if i == n - 1:
                arr = v
            else:
                dep = v
        rows[s["name"]] = {"popup": bool(s.get("popup")), "arr": arr, "dep": dep,
                           "p": [fmt_p(p["times"].get(s["name"])) for p in c["passers"]]}
    hexc = lambda rgb: "#" + "".join(f"{v:02x}" for v in rgb)
    st = {
        "slug": c["slug"], "title_line1": c["title_line1"], "title_line2": c["title_line2"],
        "video_duration_sec": c.get("video_duration_sec", 40),
        "default_dwell_sec": c["local"].get("default_dwell_sec", 30),
        "segments": [{"line": c["stations"][0]["line"], "preset": 0, "from": c["stations"][0]["name"],
                      "to": c["stations"][-1]["name"]}],
        "local": {"name": c["local"]["name"], "badge": c["local"]["badge"], "color": hexc(c["local"]["color"])},
        "passers": [{"name": p["name"], "badge": p["badge"], "color": hexc(p["color"])} for p in c["passers"]],
        "rows": rows,
    }
    return json.dumps(st, ensure_ascii=False)


def repo_configs_json():
    """configs/*.json(カルーセル以外)を {"configs/x.json": {...}} にまとめる。
    カルーセルビルダーが「過去に作った動画のconfig」を参照するのに使う。"""
    out = {}
    for p in sorted((ROOT.parent / "configs").glob("*.json")):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(c, dict) and c.get("video_type") != "carousel":
            out[f"configs/{p.name}"] = c
    return json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def main():
    station_json, presets = extract_from_config_builder()
    update_config_builder_station_data(station_json)
    css = (SRC / "shared.css").read_text(encoding="utf-8")
    js = (SRC / "shared.js").read_text(encoding="utf-8").replace("/*MANUAL_PRESETS*/", presets)
    for name in ("rush", "fare", "overtake", "carousel"):
        p = SRC / f"{name}.html"
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        t = t.replace("/*SHARED_CSS*/", css).replace("/*SHARED_JS*/", js).replace("/*STATION_DATA*/", station_json)
        if "/*FARE_PRESETS*/" in t:
            fp = json.loads((ROOT.parent / "data" / "fare_tables.json").read_text(encoding="utf-8"))
            t = t.replace("/*FARE_PRESETS*/", json.dumps({k: v for k, v in fp.items() if not k.startswith("_")}, ensure_ascii=False, separators=(",", ":")))
        if "/*REPO_CONFIGS*/" in t:
            t = t.replace("/*REPO_CONFIGS*/", repo_configs_json())
        if "/*SAMPLE_STATE*/" in t:
            t = t.replace("/*SAMPLE_STATE*/", overtake_sample_state())
        out = ROOT / f"{name}_builder.html"
        out.write_text(t, encoding="utf-8")
        print("wrote", out, f"{len(t) // 1024}KB")


if __name__ == "__main__":
    main()
