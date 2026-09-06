# kaiun-map

日本および世界の AIS（船舶自動識別装置）位置を、海の地図上にほぼリアルタイムで表示する Web アプリです。船名 / MMSI 検索と、LLM による自然言語検索・船の解説を第1版の範囲とします。

公式の運航情報・貨物情報ではありません。AIS は船舶の自主送信であり、欠測・遅延・船名未着があります。

デモ GIF / スクリーンショットは `docs/` に後から追加できます。

## できること

- プロジェクトを作って船名・呼出符号・MMSI をまとめ、選択したプロジェクトの船だけを地図上で管理
- いま見えている地図範囲の船をマーカー表示（ズームアウト時は範囲をクリップ）
- 船名 / MMSI / 呼出符号の検索（サーバが保持しているキャッシュ内）
- 「大阪湾のタンカー」などの自然言語検索（地名はサーバ側カタログのみ。座標の捏造はしない）
- 選択した船のブリーフィング（**事実** と **推測** を分離）

## 必要なキー

| 変数 | 用途 |
| --- | --- |
| `AISSTREAM_API_KEY` | [AISStream](https://aisstream.io/) の WebSocket。未設定だと地図は開きますが船は流れません |
| `OPENAI_API_KEY` | 自然言語検索と解説。未設定時は地名キーワード照合と事実の列挙にフォールバック |

`.env.example` を `.env` にコピーして値を入れてください。ブラウザにはキーを出しません。

## 起動

```bash
cd ~/Desktop/kaiun-map
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # キーを記入
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

ブラウザで http://127.0.0.1:8000 を開きます。

コードは https://github.com/Oliver19996/kaiun-map にあります。`.env` はコミットしません。

## 外部公開（インターネットから触れるようにする）

AIS は常時 WebSocket、プロジェクト名簿はサーバ上の `data/projects.json` です。無料の PaaS なら **Render** か **Railway** が扱いやすいです。手順の概要は次のとおりです。

1. [AISStream](https://aisstream.io/) と [OpenAI](https://platform.openai.com/) のキーを用意する（GitHub やチャットに貼らない）。
2. リポジトリ https://github.com/Oliver19996/kaiun-map を Render / Railway / Fly.io に接続する。
3. 起動コマンドは `uvicorn app.main:app --host 0.0.0.0 --port $PORT`（`Procfile` 済み）。
4. 環境変数に `AISSTREAM_API_KEY` と `OPENAI_API_KEY` を入れる。任意で `OPENAI_MODEL`。
5. ディスクを永続化できるなら `data/` をマウントする。しないと再起動でプロジェクトが消えます。
6. HTTPS の URL が付いたら、ブラウザで開き、左のプロジェクトから船団を選べます。

注意: 公式の航海援助ではありません。AISStream・地図タイル・OpenAI の利用規約を守ってください。AI はレート制限付きです。キーをフロントに出さない構成のままにしてください。

## アーキテクチャ

ブラウザ（Leaflet） → FastAPI（REST + WebSocket） → AISStream / OpenAI。船の最新状態は MMSI 単位のメモリキャッシュです。全世界の常時購読はしません。

## 公開時の注意

- AI エンドポイントは IP あたりの分次・日次レート制限あり
- 解説 API はユーザー自由文を船データに連結しません（MMSI だけ受け取り、サーバがキャッシュから事実を埋める）
- 地図タイル（Esri Ocean / OpenStreetMap）と AISStream のクレジットを UI に表示済み

## 第1版でやらないこと / 次のアイディア

- 貨物・スケジュール・有料 AIS・ログイン・3D 地球儀は対象外
- フックとして残している構想: ルールベースの異常検知を LLM で文章化のみする、港湾規制の出典付き Q&A、解説の多言語化
