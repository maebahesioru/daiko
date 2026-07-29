# daiko — 完全匿名ツイート代行bot

Tor (onion) サイトで匿名のツイート投稿を受け付け、管理者の承認後に投稿する。

## デプロイ (Coolify)

1. Coolify で "New Service" → Docker Compose
2. このリポジトリを指定
3. 環境変数を設定:
   - `ADMIN_PASSWORD` — 管理画面パスワード
   - `SECRET_KEY` — Flask セッション鍵
   - `MIN_POST_INTERVAL` — 投稿間隔 秒 (デフォルト 1800 = 30分)

4. Deploy 後、ログで onion アドレス確認:
   ```
   docker logs daiko | grep "Hidden service ready"
   ```

## ローカル動作確認

```bash
pip install -r requirements.txt
python3 app.py
# → http://localhost:5000
# → http://localhost:5000/admin
```

## 使い方

### 投稿者 (onion)
1. onion サイトにアクセス
2. ツイート/RT/リプライ を選択
3. 内容を入力して送信
4. 発行されたIDで状態確認

### 管理者
1. `/admin` にアクセス
2. パスワードでログイン
3. 審査キューで承認/却下
4. 承認された投稿は自動で順次投稿される

## ファイル構成

```
daiko/
├── app.py              # Flaskアプリ
├── bot_worker.py       # 投稿ワーカー (バックグラウンド)
├── twitter_client.py   # Twiforkラッパー (cookie認証)
├── config.py           # 設定
├── models.py           # SQLiteモデル
├── cookies.json        # Twitterブラウザcookie
├── templates/          # HTMLテンプレート
├── static/             # CSS/JS
├── docker-compose.yml  # Coolify用
├── Dockerfile
└── entrypoint.sh       # Tor + Flask起動スクリプト
```
