#!/usr/bin/env bash
# リグ済みGLBを Godot 4 にインポートして検証する (計画書 §7 Phase R1-4)。
#
#   ./run_check.sh <rigged.glb>
#
# 同梱クリップを焼き込んだGLB(`?motion=` 付きでダウンロードしたもの)を渡すと、
# Godot でアニメーションが再生できるかも併せて検証する。
#
# Godot の実行ファイルは $GODOT_BIN で指定する(既定は PATH 上の `godot`)。
# スクリーンショットの保存にはディスプレイが必要(ヘッドレスでも判定自体は動く)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GODOT_BIN="${GODOT_BIN:-godot}"
if ! command -v "$GODOT_BIN" >/dev/null 2>&1 && [ ! -x "$GODOT_BIN" ]; then
  echo "Godot が見つかりません。GODOT_BIN で実行ファイルを指定してください。" >&2
  echo "  例: GODOT_BIN=~/3D-world/bin/godot $0 rigged.glb" >&2
  exit 1
fi

if [ $# -lt 1 ]; then
  echo "使い方: $0 <rigged.glb>" >&2
  exit 1
fi

INPUT="$1"
if [ ! -f "$INPUT" ]; then
  echo "GLB が見つかりません: $INPUT" >&2
  exit 1
fi

cp "$INPUT" model.glb

# アセット追加後はインポートが必要(これを飛ばすと load() が null を返す)
"$GODOT_BIN" --headless --path . --import >/dev/null

# 検証本体。ディスプレイがあればスクリーンショットも撮る。
"$GODOT_BIN" --path . || true

echo
echo "--- 結果: $SCRIPT_DIR/verify_result.json ---"
python3 - <<'PY'
import json
import sys

r = json.load(open("verify_result.json"))
print("ok:", r["ok"])
print("ボーン数:", r.get("bone_count"), "/ スキン付きメッシュ:", r.get("skinned_meshes"))
print(
    "BoneMap:", len(r.get("mapped_bones", [])), "/", r.get("profile_bone_count"),
    "必須ボーン欠け:", r.get("missing_required_bones"),
)
print("親子関係の不一致:", r.get("parent_mismatch"))
print("向き:", r.get("facing"))
print("AABB:", r.get("aabb"))
pose = r.get("pose", {})
print("ポーズ適用:", len(pose.get("applied", [])), "左手の移動量:", pose.get("left_hand_moved_m"), "m")
animations = r.get("animations") or []
if animations:
    print(
        "アニメーション:", ", ".join(f"{a['name']} ({a['length']}s)" for a in animations),
        "/ 再生時の移動量:", r.get("animation_moved_m"), "m",
    )
for e in r["errors"]:
    print("ERROR:", e)
for w in r["warnings"]:
    print("WARN:", w)
sys.exit(0 if r["ok"] else 1)
PY
