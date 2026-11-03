# Render (Docker Runtime) 向けの最小構成。
# ffmpeg と日本語フォント(Noto Sans CJK)を含める。
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Renderなどでそのまま動かすと、ブラウザから動画生成を実行できる
# 簡易Webページ(web/app.py)が起動する。PORT環境変数はRenderが自動で
# 渡してくれる(未設定時は8080)。
# ローカルでCLIから直接1本だけ生成したい場合は、このCMDの代わりに
# 例えば `python3 -m race_video.cli configs/shinjuku_fujisawa.json` を
# 直接実行してください。
CMD ["python3", "web/app.py"]
