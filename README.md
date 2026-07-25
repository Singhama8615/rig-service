# rig-service — 3Dモデル自動リグ + VRM化サービス

[image-3d](https://github.com/animede/image-3d) などで生成した3Dポリゴンモデル(GLB)に
**ヒューマノイドボーンを自動で付与**し、ポーズデータで動かせるようにする独立サービス。

image-3d とは **HTTP だけで繋がる疎結合**で、コードの相互参照は無い
(image-3d 側に `IMAGE3D_RIGSVC_URL` を設定すると「リグ/VRM化」ボタンが出る)。

設計と実装計画は [`docs/RIG_SERVICE_PLAN.md`](docs/RIG_SERVICE_PLAN.md) を参照。

## 現在の実装状況

| フェーズ | 内容 | 状態 |
|---|---|---|
| R1-1 | bpy 技術スパイク | 完了 |
| R1-2 | Tポーズ自動リグ(`autorig.py`) | 完了(実Tポーズメッシュで検証済) |
| R1-3 | FastAPI + 直列ジョブキュー + GLBダウンロード | 完了 |
| R1-4 | Godot インポート検証 | 完了(Godot 4.4.1 で合格) |
| R2 | VRM 1.0 出力 | 完了(`VRMC_vrm` を自前生成) |
| R3 | 3Dプレビュー + モーション + ポーズ駆動 | 完了(VRMA/BVH読み込みのみ未実装) |
| R4 | image-3d 統合 | 完了(「リグ/VRM化」ボタン) |

## セットアップ

`bpy` は cp311 ホイールしか無いため **Python 3.11 専用 venv** が必要
(システムに 3.11 が無くても `uv` で sudo 不要に用意できる)。

```bash
cd rig-service
uv venv --python 3.11 .venv-rig
uv pip install --python .venv-rig/bin/python -r requirements.txt
```

> venv をディレクトリごと移動した場合、`.venv-rig/bin/` のコンソールスクリプトは
> shebang に旧パスが残って壊れる。`./run.sh` と `python -m pytest` は
> `python -m` 経由なので影響を受けないが、`bin/uvicorn` 等を直接叩くなら
> 作り直すか shebang を書き換える。

システム Blender は不要(`bpy` モジュールで完結する)。
`bpy` が導入できない環境では Blender 4.5 LTS を入れて
`RIGSVC_ENGINE=blender_cli` を指定すれば同じスクリプトで動作する。

## 起動

```bash
./run.sh                      # http://127.0.0.1:8100
```

## 使い方

ブラウザで <http://127.0.0.1:8100> を開き、**Tポーズの立ち絵から生成した GLB** を
アップロードする。curl の場合:

```bash
curl -F "model=@model.glb" -F 'params={"height_m":1.6}' \
     http://127.0.0.1:8100/api/rig
# -> {"job_id": "..."}

curl "http://127.0.0.1:8100/api/rig/jobs/<id>"
curl -O -J "http://127.0.0.1:8100/api/rig/jobs/<id>/download?format=glb"
```

### API

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/rig` | multipart: `model`(GLB必須)+ `params` JSON → `{job_id}` |
| GET | `/api/rig/jobs` | ジョブ一覧 |
| GET | `/api/rig/jobs/{id}` | 状態・ボーン数・ウェイト統計・警告 |
| GET | `/api/rig/jobs/{id}/download?format=glb\|vrm[&motion=<name>]` | リグ済み GLB / VRM 1.0(`motion` 指定でアニメ焼き込み) |
| POST | `/api/rig/jobs/{id}/pose` | ポーズを適用した GLB / VRM を返す |
| GET | `/api/motions` | 同梱モーションクリップ一覧 |
| GET | `/api/motions/{name}` | クリップのキーフレーム |
| GET | `/api/rig/jobs/{id}/preview.png` | 正面プレビュー(静止画) |
| DELETE | `/api/rig/jobs/{id}` | ジョブ削除 |
| GET | `/api/health` | エンジン可否・bpyバージョン |

### params

| キー | 既定 | 説明 |
|---|---|---|
| `height_m` | `1.6` | 出力の全高(メートル)。VRM/Godot はメートル系 |
| `up_axis` | `"auto"` | 入力GLBの上方向軸。`"z"`(image-3d 慣習)/ `"y"`(glTF仕様) |
| `facing` | `"auto"` | 上方向を揃えた段階でキャラが向く軸(`"+y"` / `"-y"`) |
| `bone_set` | `"standard"` | 21ボーンのヒューマノイド骨格 |
| `preview` | `true` | 正面プレビューPNGを描くか(Cyclesで数秒) |
| `vrm` | `true` | VRM 1.0 も書き出すか(純Python変換で1秒以下) |
| `vrm_meta` | `{}` | VRMのメタ情報(下記) |

#### vrm_meta

`name`(既定はアップロードしたファイル名)、`authors`、`version`、
`copyright_information`、`contact_information`、`references`、
`third_party_licenses`、`license_url`、`other_license_url` と、
利用許諾の `avatar_permission` / `commercial_usage` / `credit_notation` /
`modification` / `allow_redistribution` / `allow_excessively_violent_usage` /
`allow_excessively_sexual_usage` / `allow_political_or_religious_usage` /
`allow_antisocial_or_hate_usage`。

**既定は最も制限的な設定**(作者のみ利用可・非営利・改変再配布不可)。
生成物の権利者が誰かはサービス側では判断できないため、緩める場合は明示指定する。

```bash
curl -F "model=@model.glb" \
     -F 'params={"vrm_meta":{"name":"momo","authors":["animede"],"commercial_usage":"personalProfit"}}' \
     http://127.0.0.1:8100/api/rig
```

設定はすべて環境変数 `RIGSVC_*` で上書きできる(`server/config.py` 参照)。

## 3Dプレビューとポーズ

ブラウザで完了ジョブの「3Dで見る」を押すと、マウスで回せる3Dプレビューが開く。

- **モーション再生** — 同梱クリップの選択・再生/一時停止・シーク
- **ボーン表示** — スケルトンを重ねて表示する
- **ポーズプリセット** — Tポーズ / 腕を下ろす / リラックス / 手を振る
- **ボーン別 XYZ スライダー** — 21ボーンを個別に回す
- **ポーズ済みGLBを保存** — 現在のポーズを焼き込んだ GLB をダウンロード

プレビューは **three-vrm を使わず素の three.js** で動く。rig-service の出力は
標準スキン付きの正当な glTF なので `GLTFLoader` だけで読めて動かせるため
(three-vrm が足すのは表情・スプリングボーン・MToon 等、現状出力していない機能)。
three.js は `web/vendor/` に配置済みでビルド工程は無い。

ポーズはブラウザ内でボーンに直接当てる(数十MBを操作のたびに往復させない)。
ファイルが欲しいときだけ下記APIを使う。

### 同梱モーションクリップ

外部アセットは使わず、`server/motions.py` が**手続き的に生成**している
(ボーン回転のキーフレームなので、どのモデルにも使い回せる)。

| 名前 | 内容 | 長さ | ループ |
|---|---|---|---|
| `idle` | 待機(呼吸) | 4.0s | あり |
| `wave` | 手を振る | 2.4s | あり |
| `bow` | お辞儀 | 3.0s | なし |
| `walk` | その場歩き | 1.0s | あり |

`motion` を付けてダウンロードすると **glTF アニメーションとして焼き込まれる**ので、
Godot 等でそのまま再生できる(`AnimationPlayer` にクリップが入る)。

```bash
curl -O -J "http://127.0.0.1:8100/api/rig/jobs/<id>/download?format=glb&motion=walk"
```

### ポーズAPI

```bash
curl -X POST http://127.0.0.1:8100/api/rig/jobs/<id>/pose \
  -H "Content-Type: application/json" \
  -d '{"pose": {"LeftUpperArm": [0, 0, -70], "Head": [0, 0, 0, 1]}, "format": "glb"}' \
  -o posed.glb
```

- 値は **長さ3ならオイラー角(度)**、**長さ4ならクォータニオン `(x,y,z,w)`**
- キーは `LeftUpperArm` でも VRM 名 `leftUpperArm` でもよい
- 回転は**レスト姿勢からの相対**。指定しないボーンはレストのまま

> **オイラー角の順序に注意**: 合成順は three.js の `"XYZ"`(= Rx·Ry·Rz)。
> Blender の `Euler(..., "XYZ")` は逆順(Rz·Ry·Rx)なので、2軸以上を同時に
> 回すと結果が変わる。曖昧さを避けたい場合はクォータニオンで渡す。

VRMA / BVH の読み込みは未実装。

## 入力の要件

- **Tポーズ**(腕を水平に左右へ広げた姿勢)であること。
  腕の張り出しから肩の高さを実測しているため、腕を下ろしたモデルでは
  関節位置を人体標準比で代用することになり、精度が大きく落ちる
  (その場合はジョブの `warnings` に明示される)。
- 頭身の低いデフォルメ体型・動物型キャラでも、股下・肩・首・脚幅は
  メッシュから実測するため対応できる。
- 座標系は自動判定するので image-3d の Z-up 出力をそのまま渡してよい。

## 出力

**glTF 2.0 仕様準拠**の スキン付き GLB:

- **Y-up**(image-3d の Z-up 慣習は引き継がない)
- 原点 = 足元、左右中心
- メートル系(`height_m` にスケール)
- キャラクターは **+Z 方向**を向く(VRM 1.0 の要求と一致)
- 21ボーン。ボーン名は Godot の `SkeletonProfileHumanoid` に合わせてあり、
  VRM 1.0 humanoid の必須ボーンをすべて含む

および **VRM 1.0**(`format=vrm`)。中身は上記GLBに `VRMC_vrm` 拡張
(`meta` + `humanoid.humanBones` 21ボーン)を足したもの。
`extensionsRequired` には入れていないため、**VRM非対応の実装でも通常の
リグ済み glTF として読める**。MToon・表情・スプリングボーンは未対応。

## テスト

```bash
.venv-rig/bin/python -m pytest tests/ -q
```

- `test_proportions.py` — 座標正規化・計測・骨格レイアウト(bpy不要)
- `test_api.py` — ジョブのライフサイクル(リグエンジンはスタブ)
- `test_vrm.py` — VRM 1.0 変換と仕様検証(bpy不要)
- `test_pose.py` — ポーズ適用とクォータニオン規約(bpy不要)
- `test_motions.py` — 同梱クリップと glTF アニメーション焼き込み(bpy不要)
- `test_autorig_e2e.py` — bpy を実際に回した素体リグ(bpy が無ければスキップ)

素体TポーズGLBは `tests/make_tpose_glb.py` が bpy で生成する
(`--shape humanoid|chibi`)。

### リグの目視検証

ウェイト付与率だけでは「ボーンが正しい部位を掴んでいるか」は分からない。
リグ済みGLBに実際にポーズを当てて描画する:

```bash
.venv-rig/bin/python tests/pose_preview.py rigged.glb --pose relaxed --output pose.png
```

### Godot でのインポート検証

`godot_check/` は Godot 4 用の検証ハーネス(計画書 §7 R1-4)。
Skeleton3D の生成・`SkeletonProfileHumanoid` への BoneMap 割当・向き・スケール・
実際にポーズを当てたときの変形量を**機械判定**し、スクリーンショットも保存する。

```bash
GODOT_BIN=~/3D-world/bin/godot godot_check/run_check.sh rigged.glb
```

判定結果は `godot_check/verify_result.json`、スクショは `godot_check/shots/`
(`00_rest.png` / `01_posed.png`)。エラーがあれば終了コードが 1 になる。

`?motion=` 付きでダウンロードしたGLBを渡すと、Godot でアニメーションが
再生できるか(`AnimationPlayer` に載り、ボーンが動くか)も併せて検証する。

## ライセンス上の注意

`bpy`(Blender)は GPL。本サービスは Blender を**独立プロセス**として呼ぶため
image-3d 本体へのライセンス波及は無いが、rig-service 自体の配布形態には注意する
(計画書 §8)。

`web/vendor/three/`(Three.js r160)は MIT。image-3d が同梱していたものを
コピーして持ち込んでおり、本リポジトリだけで自己完結している。
