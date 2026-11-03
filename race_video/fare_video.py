# -*- coding: utf-8 -*-
"""
ネタ②「競合路線、運賃が安いのはどっち?」動画。

※ 現在の標準は鳥瞰図(3D)版(race_video/fare3d_video.py)。このファイルの平面版は
  config に "view": "2d" と書いたときだけ使われる(同じconfigのまま切り替えられる)。

同じ発着駅を結ぶ2〜4本の競合ルートについて、
  1) 地図フェーズ : 車両が同じペース(km基準)で進み、運賃を入れた途中駅を
                    通過すると駅の近くに「¥xxx」がポップアップ。下の棒グラフも
                    その駅までの運賃に伸びていく。
  2) 距離グラフ   : 横軸=距離(km)・縦軸=運賃 の階段グラフを描き、
                    「約10km / 20km / 30km …」の同じ距離で運賃を並べて比べる。
                    (config の "show_distance_chart": false で省略可)
  3) 最終運賃     : 最終目的地までの運賃を ¥0 からカウントアップで表示し、
                    差額と 1kmあたりの運賃を出す。

config 例は configs/fare_shinjuku_fujisawa.json を参照。
  "video_type": "fare"
  "fare_label": "IC運賃"
  "view": "2d"                     # 省略時は鳥瞰図版(race_video/fare3d_video.py)。"2d" で以前の平面版
  "compare_km": [10, 20, 30]       # 省略時は自動
  "routes": [ {"key","name","short_name","color",
               "stations": [{"line": "...", "name": "新宿", "fare": 0},
                            {"line": "...", "name": "登戸", "fare": 270, "popup": true}, ...]} ]
"fare" を書いた駅だけがポップアップ・グラフの対象になる(始発駅は0扱い)。
駅に "km"(始発駅からの距離。営業キロなど)を書くと、距離グラフ・同距離比較・
1kmあたり運賃にその値を使う(省略時は線路データ上の距離)。
終着駅の "fare" は必須。

■ 距離運賃表から運賃を計算する書き方(駅ごとの運賃を調べなくてよい)
  ルートに "fare_tables" を書くと、各駅の "km"(始発駅からの営業キロ)から運賃を自動計算する。
    "fare_tables": [{"name": "JR東日本", "table": [[3, 150], [6, 190], [10, 210], ...]}]
    よく使う表は data/fare_tables.json にプリセットとして入っていて、{"preset": "jre_kansen_ic"} で呼べる
      (jre_kansen_ic / jre_kansen_ticket = JR東日本 幹線 IC/きっぷ運賃、2026年3月改定後)。
      table の各行は [何kmまで, 運賃]。km の端数は切り上げて表に当てる("km_round": "none" で切り上げなし)。
    会社をまたぐルート(乗り継ぎ)は表を複数書き、乗り換え駅に "company": "東急" のように書くと
    その駅から先はその会社の表で計算し、会社ごとの運賃を足す(乗継割引などは考えない)。
    1社だけなら "fare_table": [[3, 150], ...] でもよい。
  駅に "fare" を書いた駅はその値を優先する(加算運賃などの例外用)。
  距離運賃表を使うときは全駅が運賃駅になる(ラベルが出るのは "popup": true の駅と終着駅)。
  "km" を書かない駅は線路データの距離から補うので、営業キロを入れておくのがおすすめ。
"""
import math

from PIL import ImageDraw

from .common import (
    CANVAS_W, CANVAS_H, font, build_base_map, build_geometry, PolyPath, draw_trail,
    draw_train, draw_chip, pop_scale, ease_out_cubic, ease_in_out, FrameWriter,
    write_text_script, load_icon,
)
from .geo import project
from .route_builder import load_line_geoms

MAP_BOX = (60, 470, 1020, 1090)
PALETTE = [(0, 110, 235), (232, 70, 40), (31, 158, 107), (139, 92, 246)]


def _parse_table(rows):
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            km, fare = row.get("km"), row.get("fare")
        else:
            km, fare = row[0], row[1]
        out.append((float(km), float(fare)))
    out.sort()
    if not out:
        raise ValueError("距離運賃表が空です")
    return out


_PRESETS = None


def fare_table_presets():
    """data/fare_tables.json の距離運賃表プリセット(JR東日本 幹線など)"""
    global _PRESETS
    if _PRESETS is None:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "data" / "fare_tables.json"
        _PRESETS = {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if not k.startswith("_")} if p.exists() else {}
    return _PRESETS


def _resolve_table(t, i):
    """{"preset": "jre_kansen_ic"} ならプリセットの表を使う(name を書けば会社名を上書き)"""
    if t.get("preset") and not t.get("table"):
        pr = fare_table_presets().get(t["preset"])
        if pr is None:
            raise ValueError(f"距離運賃表のプリセット「{t['preset']}」が data/fare_tables.json にありません")
        return t.get("name") or pr["name"], _parse_table(pr["table"])
    return t.get("name") or f"会社{i + 1}", _parse_table(t.get("table"))


def fare_tables_of(cfg):
    """ルートconfigの距離運賃表を [(会社名, [(上限km, 運賃), ...]), ...] で返す(無ければ空)"""
    if cfg.get("fare_tables"):
        return [_resolve_table(t, i) for i, t in enumerate(cfg["fare_tables"])]
    if cfg.get("fare_table"):
        return [(cfg.get("company") or cfg.get("name") or "会社1", _parse_table(cfg["fare_table"]))]
    return []


def lookup_fare(table, km, round_up=True, who=""):
    if km <= 1e-9:
        return 0.0
    k = math.ceil(round(km, 3) - 1e-9) if round_up else km
    for upper, fare in table:
        if k <= upper + 1e-9:
            return fare
    print(f"[warn] {who}: {km:.1f}km が距離運賃表の範囲外です。表の最後の運賃({int(table[-1][1])}円)を使います")
    return table[-1][1]


def _edge_score(u):
    """運賃表の境目らしさ(大きいほど境目に使われやすい km)"""
    if u > 100:
        return 4 if u % 20 == 0 else (2 if u % 10 == 0 else 0)
    if u >= 50:
        return 4 if u % 10 == 0 else (2 if u % 5 == 0 else 0)
    if u > 10:
        return 4 if u % 5 == 0 else 0
    return 4 if u in (3, 6, 10) else (2 if u in (1, 2, 4, 5, 7, 8) else 1)


def estimate_edge(da, db):
    """駅ごとの運賃しか無いとき、da〜db km の2駅のあいだで運賃が上がる距離を推定する。
    運賃は「1km未満切り上げの距離」で決まるので、境目は ceil(da)〜ceil(db)-1 の整数km のどれか。
    その中から運賃表の境目らしい km(5・10km刻み、3・6・10km)を優先し、同点なら2駅の中ほどを選ぶ"""
    lo, hi = math.ceil(da - 1e-9), math.ceil(db - 1e-9) - 1
    if hi < lo or db - da < 1e-6:
        return (da + db) / 2
    mid = (da + db) / 2
    best = max(range(lo, hi + 1), key=lambda u: (_edge_score(u), -abs(u - mid)))
    return float(max(best, da))


def yen(v):
    return f"¥{int(round(v)):,}"


class FareRoute:
    def __init__(self, cfg, route, idx):
        self.cfg = cfg
        self.key = cfg.get("key", f"route{idx}")
        self.name = cfg.get("name", self.key)
        self.short = cfg.get("short_name", self.name)
        self.color = tuple(cfg.get("color") or PALETTE[idx % len(PALETTE)])
        self.badge = cfg.get("badge")
        self.icon = load_icon(cfg.get("icon_path"), int(cfg.get("icon_width", 86)))
        self.route = route
        self.path = PolyPath(route)
        sts = route["stations"]
        k0 = sts[0]["cum_km"]
        # 表示用の距離(グラフ・同距離比較・1kmあたり運賃に使う)。
        # 駅に "km"(始発駅からの距離。営業キロなど)が書いてあればそれを使い、
        # 無い駅は前後の手入力値の間を線路上の距離の比で補う(手入力が無ければ線路上の距離)。
        geo = [s["cum_km"] - k0 for s in sts]
        man = [None if s.get("km") in (None, "") else float(s["km"]) for s in sts]
        man[0] = 0.0 if man[0] is None else man[0]
        known = [i for i, v in enumerate(man) if v is not None]
        disp = []
        for i, g in enumerate(geo):
            if man[i] is not None:
                disp.append(man[i])
                continue
            a = max([k for k in known if k < i], default=None)
            b = min([k for k in known if k > i], default=None)
            if a is not None and b is not None and geo[b] > geo[a]:
                disp.append(man[a] + (man[b] - man[a]) * (g - geo[a]) / (geo[b] - geo[a]))
            elif a is not None:
                ratio = (man[a] / geo[a]) if geo[a] > 0 and man[a] > 0 else 1.0
                disp.append(man[a] + (g - geo[a]) * ratio)
            else:
                disp.append(g)
        # 距離運賃表があれば、運賃の書いていない駅の運賃を営業キロ(表示km)から計算する
        tables = fare_tables_of(cfg)
        self.sections = None   # 距離運賃表のとき: [(区間の始まりの表示km, 表, それまでの運賃)]
        if tables:
            tmap = dict(tables)
            round_up = cfg.get("km_round", "ceil") != "none"
            cur, start, done = tables[0][0], 0.0, 0.0
            secs = []
            if sts[0].get("company") in tmap:
                cur = sts[0]["company"]
            for i, s in enumerate(sts):
                c = s.get("company")
                if i > 0 and c and c != cur:
                    if c not in tmap:
                        raise ValueError(f"[{self.name}] {s['name']} の company「{c}」の距離運賃表がありません")
                    secs.append((start, tmap[cur], done))
                    done += lookup_fare(tmap[cur], disp[i] - start, round_up, f"{self.name} {s['name']}")
                    calc = done
                    cur, start = c, disp[i]
                else:
                    calc = done + lookup_fare(tmap[cur], disp[i] - start, round_up, f"{self.name} {s['name']}")
                if i == 0:
                    s["fare"] = 0
                elif s.get("fare") in (None, ""):
                    s["fare"] = calc
            secs.append((start, tmap[cur], done))
            self.sections, self.round_up = secs, round_up
            if len(known) < 2:
                print(f"[warn] {self.name}: 駅の km(営業キロ)が入っていないので、線路データの距離で運賃を計算しています")
        self.points = []   # (km_from_start, fare, station)
        for i, s in enumerate(sts):
            fare = s.get("fare")
            if i == 0 and fare is None:
                fare = 0
            if fare is None or fare == "":
                continue
            self.points.append((s["cum_km"] - k0, float(fare), s))
        if sts[-1].get("fare") in (None, ""):
            raise ValueError(f"[{self.name}] 終着駅 {sts[-1]['name']} の fare(運賃)は必須です")
        self.k0 = k0
        self.total_km = sts[-1]["cum_km"] - k0
        self.final_fare = self.points[-1][1]

        by_station = {id(s): d for s, d in zip(sts, disp)}
        self.dpoints = [(by_station[id(st)], fare, st) for _k, fare, st in self.points]   # 表示km版
        self.disp_total_km = disp[-1]
        self.manual_km = len(known) > 1

    def fare_edges(self):
        """運賃が変わる表示kmの一覧(描画で境目をきっちり出すために使う)"""
        end = self.disp_total_km
        if self.sections:
            out = [start + u for start, table, _ in self.sections for u, _f in table]
            return sorted(e for e in out if 0 < e < end)
        return [e for e in self._station_edges() if 0 < e < end]

    def _station_edges(self):
        """駅ごとの運賃のルート: 隣り合う運賃駅のあいだで運賃が上がる位置(推定)"""
        if getattr(self, "_edges", None) is None:
            dp = self.dpoints
            self._edges = []
            for i in range(len(dp) - 1):
                (da, fa, _), (db, fb, _) = dp[i], dp[i + 1]
                if i == 0:
                    e = da                       # 出発直後から初乗り運賃
                elif fb > fa:
                    e = estimate_edge(da, db)
                else:
                    e = db                       # 値下がり(特定運賃など)は駅に着いたところで
                self._edges.append(e)
        return self._edges

    def fare_at_dkm(self, d):
        """表示km d 地点まで乗ったときの運賃。距離運賃表のルートは表から(距離の境目で上がる)、
        駅ごとの運賃のルートは隣の運賃駅とのあいだの推定した境目で上がる"""
        if not self.sections:
            dp, edges = self.dpoints, self._station_edges()
            if len(dp) < 2:
                return dp[-1][1]
            for i in range(len(dp) - 1):
                if d <= dp[i + 1][0] + 1e-9:
                    return dp[i][1] if (d <= edges[i] + 1e-9 and i > 0) else dp[i + 1][1]
            return dp[-1][1]
        sec = self.sections[0]
        for x in self.sections:
            if d >= x[0] - 1e-9:
                sec = x
        start, table, done = sec
        return done + lookup_fare(table, max(0.0, d - start), self.round_up)

    def fare_at_progress(self, km):
        """km 地点までに通過した最後の運賃駅の (運賃, 駅, 通過km)"""
        cur = self.points[0]
        for p in self.points:
            if p[0] <= km + 1e-6:
                cur = p
        return cur

    def nearest_point(self, km):
        """同距離比較用: 表示km(手入力があればそれ)で km に一番近い運賃駅"""
        cands = [p for p in self.dpoints if p[0] > 0.5]
        return min(cands, key=lambda p: abs(p[0] - km)) if cands else self.dpoints[-1]


def _nice_step(span, target=5):
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _auto_compare_km(routes):
    shortest = min(r.disp_total_km for r in routes)
    step = 10 if shortest >= 25 else 5
    kms = [k for k in range(step, int(shortest) + 1, step)]
    return kms[:4] or [round(shortest / 2)]


def _draw_live_bars(canvas, routes, tv, map_t0, speed, max_fare):
    d = ImageDraw.Draw(canvas, "RGBA")
    n = len(routes)
    row_h = 118 if n <= 2 else 96
    top = 1130
    d.rounded_rectangle([30, top - 20, CANVAS_W - 30, top + row_h * n + 10], radius=26, fill=(8, 10, 22, 210),
                        outline=(255, 255, 255, 40), width=2)
    km_now = max(0.0, (tv - map_t0) * speed)
    for i, r in enumerate(routes):
        y = top + i * row_h
        kpass, fare, st = r.fare_at_progress(km_now)
        # 直前の運賃からのカウントアップ(通過してから0.4秒)
        prev = [p for p in r.points if p[0] < kpass - 1e-6]
        prev_fare = prev[-1][1] if prev else 0
        t_pass = map_t0 + kpass / speed
        f = ease_out_cubic((tv - t_pass) / 0.4) if kpass > 0 else 1
        shown = prev_fare + (fare - prev_fare) * f
        d.text((60, y + 26), r.short, font=font(30, "black"), fill=r.color + (255,), anchor="lm")
        sect = f"{r.route['stations'][0]['name']}→{st['name']}" if kpass > 0 else "乗車前"
        d.text((60, y + 66), sect, font=font(24), fill=(200, 210, 230, 255), anchor="lm")
        bx0, bx1 = 330, CANVAS_W - 210
        d.rounded_rectangle([bx0, y + 30, bx1, y + 62], radius=16, fill=(255, 255, 255, 36))
        w = (bx1 - bx0) * (shown / max_fare if max_fare else 0)
        if w > 4:
            d.rounded_rectangle([bx0, y + 30, bx0 + w, y + 62], radius=16, fill=r.color + (255,))
        d.text((CANVAS_W - 60, y + 46), yen(shown), font=font(44, "black"), fill=(255, 255, 255, 255), anchor="rm")


def _chart_frame(d, box, max_km, max_fare):
    x0, y0, x1, y1 = box
    xs = _nice_step(max_km, 5)
    ys = _nice_step(max_fare, 5)
    f_ax = font(24)
    k = 0.0
    while k <= max_km + 1e-6:
        x = x0 + (x1 - x0) * k / max_km
        d.line([(x, y0), (x, y1)], fill=(255, 255, 255, 28), width=1)
        d.text((x, y1 + 26), f"{k:g}km", font=f_ax, fill=(190, 200, 220, 255), anchor="mm")
        k += xs
    v = 0.0
    while v <= max_fare + 1e-6:
        y = y1 - (y1 - y0) * v / max_fare
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, 28), width=1)
        d.text((x0 - 14, y), yen(v), font=f_ax, fill=(190, 200, 220, 255), anchor="rm")
        v += ys
    d.line([(x0, y1), (x1, y1)], fill=(255, 255, 255, 140), width=2)
    d.line([(x0, y0), (x0, y1)], fill=(255, 255, 255, 140), width=2)


def _draw_distance_chart(canvas, cfg, routes, t, compare_km, chart_sec):
    """t: このフェーズに入ってからの秒数"""
    d = ImageDraw.Draw(canvas, "RGBA")
    a_in = ease_out_cubic(t / 0.5)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(215 * a_in)))
    d.text((CANVAS_W / 2, 330), "同じ距離なら どっちが安い?", font=font(52, "black"),
           fill=(255, 255, 255, int(255 * a_in)), anchor="mm")
    d.text((CANVAS_W / 2, 395), f"横軸=乗った距離 / 縦軸={cfg.get('fare_label', '運賃')}", font=font(28),
           fill=(255, 215, 0, int(255 * a_in)), anchor="mm")
    max_km = max(r.disp_total_km for r in routes) * 1.04
    max_fare = max(r.final_fare for r in routes) * 1.12
    box = (160, 470, CANVAS_W - 60, 1180)
    x0, y0, x1, y1 = box
    _chart_frame(d, box, max_km, max_fare)

    def X(k):
        return x0 + (x1 - x0) * k / max_km

    def Y(v):
        return y1 - (y1 - y0) * v / max_fare

    draw_f = ease_in_out((t - 0.4) / 2.2)
    km_lim = max_km * draw_f
    for r in routes:
        pts = []
        prev = r.dpoints[0]
        pts.append((X(0), Y(prev[1])))
        for p in r.dpoints[1:]:
            if p[0] > km_lim:
                pts.append((X(km_lim), Y(prev[1])))
                break
            pts.append((X(p[0]), Y(prev[1])))
            pts.append((X(p[0]), Y(p[1])))
            prev = p
        if len(pts) >= 2:
            d.line(pts, fill=r.color + (255,), width=6, joint="curve")
        for p in r.dpoints[1:]:
            if p[0] <= km_lim:
                d.ellipse([X(p[0]) - 6, Y(p[1]) - 6, X(p[0]) + 6, Y(p[1]) + 6], fill=r.color + (255,))

    # 凡例
    lx = x0 + 10
    for r in routes:
        d.rounded_rectangle([lx, y0 + 6, lx + 34, y0 + 18], radius=6, fill=r.color + (255,))
        d.text((lx + 44, y0 + 12), r.short, font=font(26), fill=(255, 255, 255, 255), anchor="lm")
        lx += 74 + d.textlength(r.short, font=font(26))

    # 同距離の比較マーカー
    t_mark0 = 2.8
    per = max(0.6, (chart_sec - t_mark0 - 0.6) / max(1, len(compare_km)))
    table_y = 1250
    for j, km in enumerate(compare_km):
        el = t - (t_mark0 + j * per)
        if el < 0:
            continue
        al = int(255 * ease_out_cubic(el / 0.35))
        x = X(km)
        for yy in range(int(y0), int(y1), 18):
            d.line([(x, yy), (x, min(y1, yy + 9))], fill=(255, 215, 0, al), width=3)
        d.text((x, y0 - 22), f"約{km:g}km", font=font(28, "black"), fill=(255, 215, 0, al), anchor="mm")
        row = []
        for r in routes:
            pk, pf, st = r.nearest_point(km)
            row.append((r, pf, st, pk))
            d.ellipse([X(pk) - 13, Y(pf) - 13, X(pk) + 13, Y(pf) + 13], outline=(255, 255, 255, al), width=4)
        # 下の表に1行追加
        ty = table_y + j * 92
        d.rounded_rectangle([40, ty, CANVAS_W - 40, ty + 78], radius=18, fill=(20, 24, 40, int(0.92 * al)))
        d.text((70, ty + 39), f"約{km:g}km", font=font(30, "black"), fill=(255, 215, 0, al), anchor="lm")
        cw = (CANVAS_W - 260) / len(row)
        cheapest = min(v[1] for v in row)
        for c, (r, pf, st, pk) in enumerate(row):
            cx = 230 + cw * c
            d.text((cx, ty + 24), f"{st['name']}({pk:.0f}km)", font=font(22), fill=(200, 210, 230, al), anchor="lm")
            col = r.color if pf > cheapest or len(set(v[1] for v in row)) == 1 else (255, 215, 0)
            d.text((cx, ty + 56), yen(pf) + (" 安!" if pf == cheapest and len(set(v[1] for v in row)) > 1 else ""),
                   font=font(30, "black"), fill=tuple(col) + (al,), anchor="lm")


def _draw_final(canvas, cfg, routes, t):
    d = ImageDraw.Draw(canvas, "RGBA")
    a = ease_out_cubic(t / 0.5)
    d.rectangle([0, 0, CANVAS_W, CANVAS_H], fill=(3, 5, 14, int(225 * a)))
    end = cfg.get("end_label", routes[0].route["stations"][-1]["name"])
    start = cfg.get("start_name", routes[0].route["stations"][0]["name"])
    d.text((CANVAS_W / 2, 360), f"{start} → {end}", font=font(60, "black"), fill=(255, 255, 255, int(255 * a)),
           anchor="mm")
    d.text((CANVAS_W / 2, 430), f"最終目的地までの{cfg.get('fare_label', '運賃')}は…", font=font(34),
           fill=(255, 215, 0, int(255 * a)), anchor="mm")
    n = len(routes)
    row_h = 200 if n <= 2 else 150
    y = 520
    count_f = ease_in_out((t - 0.6) / 1.8)
    cheapest = min(r.final_fare for r in routes)
    for i, r in enumerate(routes):
        ry = y + i * row_h
        d.rounded_rectangle([70, ry, CANVAS_W - 70, ry + row_h - 30], radius=26, fill=(20, 24, 40, 240),
                            outline=r.color + (255,), width=5)
        d.text((110, ry + 50), r.name, font=font(38, "black"), fill=r.color + (255,), anchor="lm")
        d.text((110, ry + 105), f"{r.disp_total_km:.1f}km / 1kmあたり {r.final_fare / max(r.disp_total_km, 0.1):.1f}円",
               font=font(26), fill=(200, 210, 230, 255), anchor="lm")
        v = r.final_fare * count_f
        win = count_f >= 1 and r.final_fare == cheapest and len(set(x.final_fare for x in routes)) > 1
        d.text((CANVAS_W - 110, ry + (row_h - 30) / 2), yen(v), font=font(84, "black"),
               fill=(255, 215, 0, 255) if win else (255, 255, 255, 255), anchor="rm")
    if count_f >= 1:
        el = t - 2.4
        al = int(255 * ease_out_cubic(el / 0.4))
        sc = pop_scale(el)
        fares = sorted(r.final_fare for r in routes)
        yb = y + n * row_h + 70
        if fares[0] == fares[-1]:
            msg, sub = "運賃は同じ!", ""
        else:
            win = min(routes, key=lambda r: r.final_fare)
            diff = fares[1] - fares[0]
            msg = f"{win.short}が {yen(diff)} 安い!"
            sub = f"(往復なら {yen(diff * 2)} の差)"
        d.text((CANVAS_W / 2, yb), msg, font=font(int(64 * max(sc, 0.01)), "black"), fill=(255, 215, 0, al),
               anchor="mm")
        if sub:
            d.text((CANVAS_W / 2, yb + 80), sub, font=font(32), fill=(255, 255, 255, al), anchor="mm")
        note = cfg.get("result_note")
        if note:
            d.text((CANVAS_W / 2, yb + 140), note, font=font(26), fill=(200, 210, 230, al), anchor="mm")


def render_fare(config, geojson_path, station_db, out_dir="output", fast_preview=False, out_name=None):
    if config.get("view", "3d") != "2d":
        # 標準は鳥瞰図(3D)版(race_video/fare3d_video.py)。以前の平面版は "view": "2d" のときだけ
        from .fare3d_video import render_fare3d
        return render_fare3d(config, geojson_path, station_db, out_dir=out_dir, fast_preview=fast_preview,
                             out_name=out_name)
    slug = out_name or config.get("slug", "fare")
    line_geoms = load_line_geoms(geojson_path)
    routes = []
    for i, rc in enumerate(config["routes"]):
        color = rc.get("color") or PALETTE[i % len(PALETTE)]
        route, warnings = build_geometry(rc["stations"], line_geoms, station_db, key=rc.get("key", f"r{i}"),
                                         name=rc.get("name", ""), color=color)
        for w in warnings:
            print("[warn]", w)
        routes.append(FareRoute(rc, route, i))

    base, proj = build_base_map(
        [r.route for r in routes], MAP_BOX, config.get("title_line1", ""), config.get("title_line2", ""),
        legend=[(r.name, r.color) for r in routes], geojson_path=geojson_path,
        start_label=config.get("start_name"), end_label=config.get("end_label"),
        dot_station_names=[p[2]["name"] for r in routes for p in r.points])
    base.convert("RGB").save(f"{out_dir}/{slug}_base_map.png")

    fps = 10 if fast_preview else int(config.get("fps", 30))
    intro = float(config.get("intro_sec", 1.5))
    map_sec = float(config.get("map_sec", 13))
    show_chart = bool(config.get("show_distance_chart", True))
    compare_km = config.get("compare_km") or _auto_compare_km(routes)
    compare_km = compare_km[:4]
    chart_sec = float(config.get("chart_sec", 3.0 + 1.3 * len(compare_km))) if show_chart else 0
    final_sec = float(config.get("final_sec", 5.5))

    max_km = max(r.total_km for r in routes)
    speed = max_km / map_sec        # km / 動画秒
    map_t0 = intro
    map_end = intro + map_sec + 0.8
    chart_t0 = map_end
    final_t0 = chart_t0 + chart_sec
    total = final_t0 + final_sec
    max_fare = max(r.final_fare for r in routes)

    # 駅ポップアップ: 同じ駅に複数ルートの運賃が出る場合は上下にずらす
    pops = []
    for ri, r in enumerate(routes):
        for k, fare, st in r.points[1:]:
            pops.append((map_t0 + k / speed, r, st, fare, ri))

    script = [(0.0, f"{config.get('title_line1', '')}、{config.get('fare_label', '運賃')}が安いのはどっち?")]
    for t0, r, st, fare, _ in sorted(pops, key=lambda x: x[0]):
        if st.get("popup"):
            script.append((t0, f"{r.short}、{st['name']}までは{int(fare)}円。"))
    if show_chart:
        script.append((chart_t0, "同じ距離で比べてみると…"))
    w = min(routes, key=lambda r: r.final_fare)
    script.append((final_t0, f"最終目的地までの運賃は、{w.short}が一番安い{int(w.final_fare)}円。"))
    write_text_script(out_dir, slug, sorted(script, key=lambda x: x[0]))

    writer = FrameWriter(f"{out_dir}/{slug}.mp4", fps)
    n_frames = int(total * fps)
    print(f"rendering {n_frames} frames ({total:.1f}s @ {fps}fps) ...")
    for fi in range(n_frames):
        tv = fi / fps
        map_visible_until = (chart_t0 if show_chart else final_t0) + 0.6
        if tv < map_visible_until:
            canvas = base.copy()
            km_now = max(0.0, (tv - map_t0) * speed)
            for r in routes:
                k = min(km_now, r.total_km)
                if k > 0.02:
                    draw_trail(canvas, r.path.screen_polyline(proj, 0, r.k0, r.k0 + k), r.color)
            for ri, r in reversed(list(enumerate(routes))):
                k = min(km_now, r.total_km)
                x, y = r.path.screen_at_km(proj, r.k0 + k)
                draw_train(canvas, x, y, r.color, badge=r.badge, icon_img=r.icon)
            for t0, r, st, fare, ri in pops:
                el = tv - t0
                last = st is r.route["stations"][-1]
                life = 99 if last else 2.2
                if 0 <= el <= life:
                    alpha = 255 if el < life - 0.35 else int(255 * (life - el) / 0.35)
                    x, y = project(proj, st["lon"], st["lat"])
                    dy = -20 - ri * 64 if ri % 2 == 0 else 80 + (ri // 2) * 64
                    draw_chip(canvas, x, y + dy, f"{st['name']} {yen(fare)}", r.color, size=28,
                              scale=pop_scale(el), alpha=alpha, tail=(ri % 2 == 0))
            _draw_live_bars(canvas, routes, tv, map_t0, speed, max_fare)
            last_map = canvas
        else:
            canvas = last_map.copy()
        if show_chart and chart_t0 <= tv < final_t0 + 0.6:
            _draw_distance_chart(canvas, config, routes, tv - chart_t0, compare_km, chart_sec)
        if tv >= final_t0:
            _draw_final(canvas, config, routes, tv - final_t0)
        writer.write(canvas)
    return writer.close()
