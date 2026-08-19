# ChatGPT vs Claude

ログイン済みの ChatGPT と、このコンソールの Claude Code を**自動で議論させる** CLI ツールです。[chatgpt-cleaner](https://github.com/takano32/chatgpt-cleaner) / [chatgpt-memory-distiller](https://github.com/takano32/chatgpt-memory-distiller) と同じく、Playwright で**専用の Chromium プロファイル**を起動して操作するため、普段使いのブラウザには拡張機能もスクリプトも一切入れません。

テーマを渡すと、ChatGPT と Claude が交互に発言し、進行はすべてこのツールが中継します。発言はコンソールに逐次表示され、トランスクリプト(JSON + Markdown)も毎発言ごとに保存されます。

## 仕組み

どちらの側も **API キーは不要**です。手元にあるログインをそのまま使います。

- **ChatGPT 側** … 会話の送信 API(`POST /backend-api/conversation`)は proof-of-work で保護されており、直接叩くのは脆いため、専用ブラウザの ChatGPT 画面を実際に操作してプロンプトを送ります。返信は conversation API(GET は保護されていません)をポーリングして読み取ります。姉妹ツールと違い議論は**1つの会話の中で続く**ので、2発言目以降は「直前に読んだ返信の時刻」を透かしにして、それより新しい完了済みの返信だけを答えとして採用します。
- **Claude 側** … ローカルの Claude Code CLI(`claude -p`)を呼びます。初回の返信が返す session id を `--resume` に渡し続けることで、Claude 側も文脈を保ったまま議論します。Claude Code は本来コーディング用アシスタントなので、そのままだと非技術系のテーマを「職務範囲外」と断ることがあります(実測: haiku で発生)。そのため毎回 `--append-system-prompt` で「この討論への参加があなたのタスク」と明示しています。
- **進行** … 相手の発言は原文のまま「◯◯ の発言: ...」という枠に入れて中継します。両者に同じルール(発言回数・目安の文字数)を伝え、各自の最終発言では総括を求めます。

## インストール

```console
$ python3 -m venv .venv
$ source .venv/bin/activate
(.venv) $ pip install -e .
(.venv) $ playwright install chromium   # 省略時はシステムの chromium を自動使用
```

Claude 側には [Claude Code](https://claude.com/claude-code) がインストール済みで、ログインしている必要があります(`claude` コマンドが動けば OK)。

## 使い方

```console
$ chatgpt-vs-claude login                    # 専用ブラウザが開くのでログイン(初回のみ)
$ chatgpt-vs-claude discuss "AIに創造性はあるか" --turns 3
```

[chatgpt-cleaner](https://github.com/takano32/chatgpt-cleaner) や [chatgpt-memory-distiller](https://github.com/takano32/chatgpt-memory-distiller) を使ったことがあれば、そのログインを**コピーして引き継げます**(再ログイン不要):

```console
$ chatgpt-vs-claude login --copy-from ~/.local/share/chatgpt-memory-distiller/profile
```

ログイン状態の確認は `chatgpt-vs-claude status` です。

### discuss のオプション

```console
$ chatgpt-vs-claude discuss "テーマ" \
    --turns 3                 # 1人あたりの発言回数(既定: 3)
    --first claude            # 先手(既定: chatgpt)
    --max-chars 400           # 1発言の目安文字数(既定: 400)
    --instructions "英語で議論する"   # 両者に追加するルール
    --chatgpt-model gpt-5-thinking   # ChatGPT 側のモデル(既定: アカウントの既定)
    --claude-model sonnet     # Claude 側のモデル(既定: CLI の既定)
    --headless                # ブラウザの画面を出さない(後述の注意を参照)
```

Claude 側は Claude Code の既定モデルで動きます。コストを抑えたい場合は `--claude-model sonnet` などを指定してください。

返信待ちの上限は ChatGPT 側が `--timeout`、Claude 側が `--claude-timeout`(いずれも既定 600 秒)です。思考の長いモデル同士で議論させる場合は伸ばしてください。

### 実行中と実行後

各発言はコンソールに色付きで表示されます。中断(Ctrl-C)しても、**そこまでの発言はトランスクリプトに残っています**。ChatGPT 側の会話はブラウザの履歴にも残るので、`discuss` の最後に表示される会話 URL から続きを読めます。

## ファイルの置き場所

| 場所 | 中身 | 変更 |
|---|---|---|
| `~/.local/share/chatgpt-vs-claude/profile/` | 専用 Chromium プロファイル(ログイン状態) | `--profile-dir` |
| `~/.local/share/chatgpt-vs-claude/claude/` | Claude CLI の作業ディレクトリ(セッション継続用) | — |
| `./chatgpt-vs-claude-transcripts/` | 議論のトランスクリプト(JSON / Markdown) | `--transcript-dir` |

トランスクリプトは**実行したディレクトリ配下**に書かれます。ファイルは `0600`、ディレクトリは `0700` で作られます。`.gitignore` しているのはこのリポジトリだけなので、別のリポジトリの中で実行すると会話の内容がそのリポジトリに書かれます。無視設定が無い場所へ書こうとした場合は警告を出しますが、`--transcript-dir` で明示する運用が安全です。

Claude CLI の作業ディレクトリを固定しているのは、Claude Code がセッションを作業ディレクトリ単位で保存するためです(別の場所から `--resume` しても見つからない)。また、プロジェクトのディレクトリで実行すると、そこにある CLAUDE.md などの文脈を議論が拾ってしまうのを避ける意味もあります。

## 注意

- `--headless` は ChatGPT 側の Cloudflare ボット判定に**ほぼ確実に阻まれます**(実測: 「Just a moment...」画面で停止し、その旨のエラーになります)。基本はウィンドウ表示のまま使ってください。ログイン済みセッションの確認(`status`)だけはヘッドレスでも動きます。
- ChatGPT 側の発言は**あなたのアカウントの会話として**行われます。メモリー機能が有効なら、議論の内容がメモリーに保存されることもあります(気になる場合は ChatGPT の設定で一時チャットやメモリーを調整してください)。
- Claude 側の呼び出しは Claude Code の利用枠を消費します。
- ChatGPT の UI・API は予告なく変わります。送信ボタンのセレクタが変わった場合は Enter 送信にフォールバックしますが、動かなくなったら issue を立ててください。

## 開発

```console
(.venv) $ pip install -e '.[dev]'
(.venv) $ pytest
```

テストはブラウザも `claude` CLI も起動しません(どちらも偽物に差し替えて検証します)。
