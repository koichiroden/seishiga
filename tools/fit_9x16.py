#!/usr/bin/env python3
"""カルーセル画像を、文字や図を伸ばさずに 9:16 (既定 1080x1920) へ変換する。

画像内の「何も描かれていない横帯」を見つけ、その余白へ均等に高さを足す。
背景の行を複製して広げるので、文字・アイコン・枠線は歪まない。

使い方:
    python fit_9x16.py 入力フォルダ 出力フォルダ
    python fit_9x16.py slide4.png 出力フォルダ
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def find_gaps(arr, tolerance, min_gap):
    """内容のない横帯を (開始行, 終了行) のリストで返す。"""
    rgb = arr[:, :, :3].astype(np.int16)
    # 左右端の色を各行の背景とみなし、そこから外れる画素がない行を空行とする
    bg = rgb[:, :4].mean(axis=1, keepdims=True)
    blank = (np.abs(rgb - bg).max(axis=2) <= tolerance).all(axis=1)
    gaps, start = [], None
    for y, is_blank in enumerate(blank):
        if is_blank and start is None:
            start = y
        elif not is_blank and start is not None:
            if y - start >= min_gap:
                gaps.append((start, y))
            start = None
    if start is not None and len(blank) - start >= min_gap:
        gaps.append((start, len(blank)))
    return gaps


def fit(img, width, height, tolerance=6, min_gap=16):
    img = img.convert("RGBA")
    if img.width != width:
        new_h = round(img.height * width / img.width)
        img = img.resize((width, new_h), Image.LANCZOS)
    if img.height > height:
        raise ValueError(f"高さ {img.height}px が目標 {height}px を超えています")

    arr = np.asarray(img)
    extra = height - img.height
    if extra == 0:
        return img

    gaps = find_gaps(arr, tolerance, min_gap)
    if not gaps:
        # 空行がなければ上下へ均等に足す
        gaps = [(0, 1), (img.height - 1, img.height)]

    share, rest = divmod(extra, len(gaps))
    parts, cursor = [], 0
    for i, (start, end) in enumerate(gaps):
        mid = (start + end) // 2
        parts.append(arr[cursor:mid])
        n = share + (1 if i < rest else 0)
        parts.append(np.repeat(arr[mid:mid + 1], n, axis=0))
        cursor = mid
    parts.append(arr[cursor:])
    return Image.fromarray(np.concatenate(parts, axis=0), "RGBA")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input", type=Path, help="画像ファイルまたはフォルダ")
    p.add_argument("output", type=Path, help="出力フォルダ")
    p.add_argument("--width", type=int, default=1080)
    p.add_argument("--height", type=int, default=1920)
    p.add_argument("--tolerance", type=int, default=6, help="背景とみなす色の許容差")
    p.add_argument("--min-gap", type=int, default=16, help="余白とみなす最小の高さ(px)")
    a = p.parse_args()

    if a.input.is_dir():
        files = sorted(f for f in a.input.iterdir() if f.suffix.lower() in EXTS)
    else:
        files = [a.input]
    if not files:
        sys.exit(f"画像が見つかりません: {a.input}")

    a.output.mkdir(parents=True, exist_ok=True)
    if a.input.is_dir() and a.input.resolve() == a.output.resolve():
        sys.exit("入力と出力は別のフォルダにしてください")

    for f in files:
        with Image.open(f) as im:
            out = fit(im, a.width, a.height, a.tolerance, a.min_gap)
        dest = a.output / f"{f.stem}.png"
        out.save(dest)
        print(f"{f.name} -> {dest} ({out.width}x{out.height})")


if __name__ == "__main__":
    main()
