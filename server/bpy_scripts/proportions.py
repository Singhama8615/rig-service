"""Tポーズメッシュの計測とヒューマノイド骨格レイアウト(純numpy・bpy非依存)。

計画書 §4「Tポーズ前提による単純化」の実体。関節位置の推定に学習モデルは使わず、
バウンディングボックス・断面プロファイル・左右対称性から決定的に配置する。

bpy に依存しないので、Blender 抜きで単体テストできる(`tests/test_proportions.py`)。

## 座標系

本モジュールが扱う「正規化座標」は **Blenderワールド座標**であり、以下を満たす:

- **+Z が上**、足元が `z = 0`
- **+X がキャラクターから見て左**
- **-Y がキャラクターの正面**(Blender のフロントビュー方向)
  → glTF エクスポート時の Y-up 変換 `(x, y, z)_blender -> (x, z, -y)_gltf` により
     -Y は glTF の **+Z** になる。VRM 1.0 が要求する「アバターは +Z を向く」と一致する。
- 単位はメートル(`height_m` にスケール済み)

入力GLBをこの座標系へ持ち込む変換は `normalize_matrix()` が返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# --- 人体プロポーション既定値(全高比)。計測に失敗した項目のフォールバック --------
# 出典系の標準値をアニメ調キャラでも破綻しにくいよう丸めたもの。
DEFAULT_SHOULDER_Z = 0.82
DEFAULT_CROTCH_Z = 0.50
DEFAULT_NECK_Z = 0.87
DEFAULT_ANKLE_Z = 0.055
DEFAULT_LEG_CENTER_X = 0.075
DEFAULT_ARM_HALF_SPAN = 0.50
DEFAULT_TORSO_HALF = 0.13

# 肩関節→指先を 1.0 としたときの肘・手首・手先の位置(実測比)
ELBOW_RATIO = 0.42
WRIST_RATIO = 0.78
HAND_END_RATIO = 0.98

# 断面プロファイルのスラブ数
SLABS = 120

# --- 実測値の妥当範囲(全高比) ------------------------------------------------
# ここは「人体標準からのズレ」ではなく「明らかな計測ミス」だけを弾く。
# 対象はデフォルメされたキャラクター(頭身が低い・動物型・着ぐるみ)も含むため、
# 人体標準比よりかなり広く取る。範囲外なら DEFAULT_* へ倒して警告する。
VALID_SHOULDER_Z = (0.40, 0.92)
VALID_CROTCH_Z = (0.10, 0.65)
# 股下を実測できなかったときの代用値。全高比の固定値だと頭身の低いキャラで
# 大きくズレるため、**実測できた肩の高さ**から比例で出す(実測比の実績:
# 人体標準 0.61 / デフォルメ体型 0.41〜0.49 → 中間を採る)。
CROTCH_TO_SHOULDER_RATIO = 0.55
# 首(頭の付け根)を探す範囲: 肩より上のうち下半分。上半分まで見ると
# **頭頂・帽子の先端のほうが細い**ため、そちらを首と誤認する。
NECK_SEARCH_FRACTION = 0.5
VALID_HEAD_BASE_Z = 0.95  # 上限のみ(下限は肩の少し上)
VALID_LEG_CENTER_X = (0.02, 0.25)
VALID_TORSO_HALF = (0.05, 0.30)
# Tポーズ判定: 腕帯の中心高さの下限と、腕の張り出し量の下限
MIN_ARM_BAND_CENTER = 0.42
MIN_ARM_HALF_SPAN = 0.33
# 腕は水平なので、腕帯は高さ方向に薄いはず。スカート等の縦長の幅広部と区別する
# 最も効く条件がこれ(実測: 腕を下ろしたキャラでは帯が 0.8*h に及ぶ)。
MAX_ARM_BAND_EXTENT = 0.22


@dataclass
class BoneSpec:
    """1ボーンの配置指定(正規化座標)。"""

    name: str
    head: tuple[float, float, float]
    tail: tuple[float, float, float]
    parent: str | None
    connect: bool = False


@dataclass
class Measurements:
    """メッシュから実測(または既定値へフォールバック)した寸法。単位はメートル。"""

    height: float
    arm_half_span: float
    shoulder_z: float
    torso_half: float
    crotch_z: float
    leg_center_x: float
    knee_z: float
    ankle_z: float
    neck_z: float
    head_base_z: float
    foot_tip_y: float
    toe_break_y: float
    toe_z: float
    # 入力がTポーズと判定できたか(腕が水平に大きく張り出しているか)
    t_pose: bool = True
    # 実測できず既定値へフォールバックした項目名(ジョブの警告として返す)
    fallbacks: list[str] = field(default_factory=list)

    def scaled(self, factor: float) -> "Measurements":
        """全長を factor 倍したときの計測値を返す(比率は不変)。

        リグ付けは作業スケールで行い最後に縮めるため、報告値も同じ倍率で
        揃える必要がある(autorig の `_RIG_WORKING_HEIGHT` 参照)。
        """
        lengths = {
            name: value * factor
            for name, value in self.__dict__.items()
            if name not in ("t_pose", "fallbacks")
        }
        return Measurements(t_pose=self.t_pose, fallbacks=list(self.fallbacks), **lengths)

    def to_dict(self) -> dict:
        skip = {"fallbacks", "t_pose"}
        d = {k: v for k, v in self.__dict__.items() if k not in skip}
        return {
            **{k: round(float(v), 4) for k, v in d.items()},
            "t_pose": self.t_pose,
            "fallbacks": list(self.fallbacks),
        }


# --- 座標系の判定・正規化 ---------------------------------------------------


def detect_source_up_axis(verts_blender: np.ndarray) -> str:
    """Blenderへインポート済みの頂点から、**元GLBの**上方向軸を判定する。

    Blender の glTF インポータは仕様通り Y-up -> Z-up 変換
    `(x, y, z)_gltf -> (x, -z, y)_blender` を掛ける。したがって:

    - 仕様準拠(Y-up)のGLB     -> モデルは Blender の **+Z** 方向に立つ
    - image-3d の Z-up GLB      -> モデルは Blender の **-Y** 方向に倒れる

    どちらの場合も左右軸は Blender X のままなので、
    **Y と Z の広がりを比べるだけ**で判別できる。Tポーズでは腕を広げた幅が
    身長に匹敵するため「最長軸=上」は使えない(左右軸と拮抗する)ことに注意。

    Returns:
        "y"(仕様準拠) または "z"(image-3d 慣習)
    """
    size = verts_blender.max(axis=0) - verts_blender.min(axis=0)
    return "y" if size[2] >= size[1] else "z"


def _rotation_x(deg: float) -> np.ndarray:
    r = np.radians(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array(
        [[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]], dtype=float
    )


def _rotation_z(deg: float) -> np.ndarray:
    r = np.radians(deg)
    c, s = np.cos(r), np.sin(r)
    return np.array(
        [[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float
    )


def _apply(matrix: np.ndarray, verts: np.ndarray) -> np.ndarray:
    return verts @ matrix[:3, :3].T + matrix[:3, 3]


def detect_facing(verts: np.ndarray) -> tuple[int, bool]:
    """上方向を +Z に揃えた頂点から、キャラクターの正面が ±Y のどちらかを判定する。

    独立な2つの手がかりを突き合わせる:

    1. **足はつま先方向へ突き出す**。足元スラブ(下位8%)が bbox 中心から
       より遠くまで伸びている側が正面。
    2. **後頭部(髪・フード)は背面側に膨らむ**。頭部スラブ(上位10%)の重心が
       胴体重心より寄っている側が背面。

    image-3d の既存生成物 12 体で両者は一致し、いずれも正面 = -Y だった
    (glTF出力時に +Z = VRM 1.0 の要求と一致する向き)。

    Returns:
        (符号, 2つの手がかりが一致したか)。符号は +1(正面が +Y) / -1(正面が -Y)。
    """
    z = verts[:, 2]
    lo_z, hi_z = float(z.min()), float(z.max())
    height = hi_z - lo_z
    if height <= 0:
        return -1, False

    lo_y, hi_y = float(verts[:, 1].min()), float(verts[:, 1].max())
    center_y = (lo_y + hi_y) / 2

    foot = verts[z <= lo_z + height * 0.08]
    if len(foot) < 16:
        return -1, False
    feet_sign = 1 if (foot[:, 1].max() - center_y) > (center_y - foot[:, 1].min()) else -1

    head = verts[z >= lo_z + height * 0.90]
    body = verts[(z > lo_z + height * 0.2) & (z < lo_z + height * 0.8)]
    if len(head) < 16 or len(body) < 16:
        return feet_sign, False
    lean = float(head[:, 1].mean() - body[:, 1].mean())
    if abs(lean) < 0.01 * height:
        # 前後対称で頭部からは判断できない(素体など)。足の手がかりだけを採る。
        return feet_sign, True
    head_sign = -1 if lean > 0 else 1

    # 食い違ったときは**頭部の手がかりを優先する**。image-3d の生成物77体で
    # 突き合わせた結果(正解は設計上すべて -Y):
    #   足の突出   hunyuan3d 57/62 ・ pixal3d 3/8  = 86%
    #   後頭部     hunyuan3d 56/62 ・ pixal3d 8/8  = 91%
    # 特に pixal3d のぬいぐるみ体型では足の手がかりがほぼ機能しない
    # (大きな前足が前後に張り出すため)。
    return head_sign, feet_sign == head_sign


def normalize_matrix(
    verts_blender: np.ndarray,
    height_m: float,
    up_axis: str = "auto",
    facing: str = "auto",
) -> tuple[np.ndarray, dict]:
    """Blenderインポート直後の頂点を、本モジュールの正規化座標へ移す4x4行列を作る。

    「上を +Z に回す」「正面を -Y に回す」「足元を原点へ」「身長を height_m へ」を
    この順で合成する。

    Args:
        verts_blender: インポート+トランスフォーム適用後のワールド頂点 (N, 3)。
        height_m: 出力の全高(メートル)。VRM/Godot はメートル系。
        up_axis: "auto" | "y" | "z" — 元GLBの上方向軸。
        facing: "auto" | "+y" | "-y" — 上を +Z に揃えた段階でキャラが向く軸。

    Returns:
        (4x4行列, 判定内容のdict)
    """
    if up_axis == "auto":
        up_axis = detect_source_up_axis(verts_blender)
    if up_axis not in ("y", "z"):
        raise ValueError(f"up_axis は 'auto'/'y'/'z' のいずれか (got: {up_axis!r})")

    # 元が Z-up(=Blenderで -Y 方向に倒れている)なら X 軸まわり -90° で起こす。
    matrix = _rotation_x(-90.0) if up_axis == "z" else np.eye(4)
    verts = _apply(matrix, verts_blender)

    facing_confident = True
    if facing == "auto":
        facing_sign, facing_confident = detect_facing(verts)
    elif facing in ("+y", "-y"):
        facing_sign = 1 if facing == "+y" else -1
    else:
        raise ValueError(f"facing は 'auto'/'+y'/'-y' のいずれか (got: {facing!r})")

    # 正面は -Y に揃える(エクスポート時に glTF +Z になる = VRM 1.0 の要求)
    if facing_sign > 0:
        matrix = _rotation_z(180.0) @ matrix
        verts = _apply(_rotation_z(180.0), verts)

    lo, hi = verts.min(axis=0), verts.max(axis=0)
    height = float(hi[2] - lo[2])
    if height <= 0:
        raise ValueError("メッシュの高さが 0 です。上方向の判定に失敗しています。")
    scale = height_m / height

    # 足元(min Z)を原点に、左右・前後は bbox 中心に寄せる
    center = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    translate = np.eye(4)
    translate[:3, 3] = -center
    scale_m = np.diag([scale, scale, scale, 1.0])

    info = {
        "up_axis": up_axis,
        "facing": "+y" if facing_sign > 0 else "-y",
        "facing_confident": facing_confident,
        "source_height": round(height, 4),
        "scale": round(scale, 6),
    }
    return scale_m @ translate @ matrix, info


# --- 計測 -------------------------------------------------------------------


def _slab_profile(verts: np.ndarray, height: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """高さ方向 SLABS 分割の断面プロファイルを返す。

    Returns:
        (各スラブ中心z, 各スラブの左右半幅(max|x|), 各スラブの頂点数)
    """
    edges = np.linspace(0.0, height, SLABS + 1)
    idx = np.clip(np.searchsorted(edges, verts[:, 2], side="right") - 1, 0, SLABS - 1)
    centers = (edges[:-1] + edges[1:]) / 2
    half = np.zeros(SLABS)
    counts = np.bincount(idx, minlength=SLABS)
    for i in np.flatnonzero(counts):
        xs = verts[idx == i, 0]
        half[i] = max(float(xs.max()), float(-xs.min()))
    return centers, half, counts


def measure(verts: np.ndarray) -> Measurements:
    """正規化座標の頂点からヒューマノイド関節の高さ・幅を実測する。

    Tポーズを前提とし、実測できない項目は人体標準比(全高比)へフォールバックし
    `fallbacks` に記録する(呼び出し側でジョブ警告にする)。
    """
    height = float(verts[:, 2].max())
    fallbacks: list[str] = []
    centers, half, counts = _slab_profile(verts, height)
    arm_half_span = float(np.max(np.abs(verts[:, 0])))

    # --- 肩の高さ: 腕が張り出しているスラブ帯の中心 -------------------------
    # Tポーズなら腕のあるスラブの半幅は全体最大に近づき、胴体だけのスラブ
    # (半幅 ~0.13*h)とは明確に差がつく。閾値 0.75 はその中間。
    # ただしスカートのように**縦に長い幅広部**も同じ条件を満たしてしまうため、
    # 「帯が水平に薄い」ことを併せて確認する(腕は水平なので帯は薄いはず)。
    arm_band = np.flatnonzero(half >= 0.75 * arm_half_span)
    t_pose = False
    shoulder_z = DEFAULT_SHOULDER_Z * height
    band_bottom = shoulder_z
    if len(arm_band):
        band_center = float(centers[arm_band].mean())
        band_extent = float(centers[arm_band.max()] - centers[arm_band.min()])
        t_pose = (
            arm_half_span >= MIN_ARM_HALF_SPAN * height
            and band_extent <= MAX_ARM_BAND_EXTENT * height
            and band_center >= MIN_ARM_BAND_CENTER * height
        )
        if t_pose:
            shoulder_z = band_center
            band_bottom = float(centers[arm_band.min()])
    if not (VALID_SHOULDER_Z[0] * height <= shoulder_z <= VALID_SHOULDER_Z[1] * height):
        t_pose = False
    if not t_pose:
        fallbacks.append("shoulder_z")
        shoulder_z = DEFAULT_SHOULDER_Z * height
        band_bottom = shoulder_z
    if arm_half_span < MIN_ARM_HALF_SPAN * height:
        fallbacks.append("arm_half_span")
        arm_half_span = DEFAULT_ARM_HALF_SPAN * height

    # --- 胴体の半幅: 腕帯のすぐ下のスラブ(腕が写り込まない高さ) -----------
    below = np.flatnonzero((centers < band_bottom - 0.02 * height) & (counts > 0))
    torso_half = float(half[below[-1]]) if len(below) else 0.0
    if not (VALID_TORSO_HALF[0] * height <= torso_half <= VALID_TORSO_HALF[1] * height):
        fallbacks.append("torso_half")
        torso_half = DEFAULT_TORSO_HALF * height

    # --- 股下: 下から上へ走査し、左右に分かれなくなる高さ -------------------
    crotch_z = _measure_crotch(verts, centers, height)
    if (
        crotch_z is None
        or not (VALID_CROTCH_Z[0] * height <= crotch_z <= VALID_CROTCH_Z[1] * height)
        or crotch_z >= shoulder_z
    ):
        fallbacks.append("crotch_z")
        # 肩を実測できているならそこから比例で出す。全高比の固定値だと
        # デフォルメ体型で股下が倍近くズレる(実測: 肩0.45のキャラで 0.22 vs 0.50)。
        crotch_z = (
            DEFAULT_CROTCH_Z * height
            if "shoulder_z" in fallbacks
            else CROTCH_TO_SHOULDER_RATIO * shoulder_z
        )

    # --- 脚の中心X: 太もも中央の高さでの +X 側クラスタの中心 ----------------
    thigh_z = crotch_z * 0.72
    thigh = verts[(np.abs(verts[:, 2] - thigh_z) < 0.03 * height) & (verts[:, 0] > 0)]
    leg_center_x = (
        float((thigh[:, 0].min() + thigh[:, 0].max()) / 2) if len(thigh) >= 16 else 0.0
    )
    if not (VALID_LEG_CENTER_X[0] * height <= leg_center_x <= VALID_LEG_CENTER_X[1] * height):
        fallbacks.append("leg_center_x")
        leg_center_x = DEFAULT_LEG_CENTER_X * height

    # --- 首: 肩の上〜頭の中ほどで最も細いスラブ ------------------------------
    # 上限を切らないと、頭頂や帽子の先端(こちらのほうが細い)を首と誤認する。
    neck_search_top = shoulder_z + NECK_SEARCH_FRACTION * (height - shoulder_z)
    above = np.flatnonzero(
        (centers > shoulder_z + 0.02 * height) & (centers < neck_search_top) & (counts > 0)
    )
    head_base_z = (
        float(centers[above[np.argmin(half[above])]]) if len(above) else 0.0
    )
    # 首が肩の直上〜頭頂手前の妥当な範囲に無ければ標準比へ倒す
    if not (shoulder_z + 0.03 * height <= head_base_z <= VALID_HEAD_BASE_Z * height):
        fallbacks.append("head_base_z")
        head_base_z = max(DEFAULT_NECK_Z * height, shoulder_z + 0.04 * height)
    neck_z = shoulder_z + 0.02 * height

    # --- 足首・つま先 --------------------------------------------------------
    ankle_z = DEFAULT_ANKLE_Z * height
    foot = verts[verts[:, 2] <= ankle_z * 1.6]
    if len(foot) >= 16:
        foot_tip_y = float(foot[:, 1].min())  # 正面は -Y
    else:
        fallbacks.append("foot_tip_y")
        foot_tip_y = -0.10 * height
    toe_break_y = foot_tip_y * 0.55
    toe_z = ankle_z * 0.35

    return Measurements(
        height=height,
        arm_half_span=arm_half_span,
        shoulder_z=shoulder_z,
        torso_half=torso_half,
        crotch_z=crotch_z,
        leg_center_x=leg_center_x,
        knee_z=crotch_z * 0.55,
        ankle_z=ankle_z,
        neck_z=neck_z,
        head_base_z=head_base_z,
        foot_tip_y=foot_tip_y,
        toe_break_y=toe_break_y,
        toe_z=toe_z,
        t_pose=t_pose,
        fallbacks=fallbacks,
    )


# 股下検出のパラメータ
# 走査する高さ範囲(全高比)。下限は VALID_CROTCH_Z に合わせて低く取る
# (デフォルメ体型では股下が全高の 15% 程度まで下がる)。
CROTCH_SCAN_RANGE = (0.10, 0.68)
CROTCH_BINS = 25  # 左右方向のヒストグラム分割数(奇数=中央ビンが存在する)
CROTCH_MIN_GAP_BINS = 2  # 脚の間と認めるのに必要な空ビン数
CROTCH_MIN_POINTS = 24  # 判定に必要な最小点数(少ないとビンが穴だらけになる)
CROTCH_WINDOW_SLABS = 2.5  # 点数を稼ぐために前後何スラブ分をまとめて見るか
CROTCH_RUN_RATIO = 0.8  # 「そこより下がほぼ全部分離している」とみなす割合


def _has_leg_gap(xs: np.ndarray) -> bool:
    """左右方向の断面に、中央をまたぐ隙間(=脚の間)があるか。

    「中心付近に頂点が無い」を絶対距離で判定すると、脚がぴったり閉じたキャラで
    すぐ破綻する。ヒストグラムの**中央ビンから連続する空ビンの幅**で見ることで、
    隙間が細くても検出できるようにする。

    点数が足りない・端まで空(中央に胴が無い)といった判定不能なケースは
    False を返す。呼び出し側は「そこより下の大半が分離している」形で
    単発のノイズを吸収する。
    """
    if len(xs) < CROTCH_MIN_POINTS:
        return False
    half = float(np.max(np.abs(xs)))
    if half <= 0:
        return False
    hist, _ = np.histogram(xs, bins=CROTCH_BINS, range=(-half, half))
    center = CROTCH_BINS // 2
    if hist[center] > 0:
        return False
    gap = 1
    i = center - 1
    while i >= 0 and hist[i] == 0:
        gap += 1
        i -= 1
    left_edge_empty = i < 0
    i = center + 1
    while i < CROTCH_BINS and hist[i] == 0:
        gap += 1
        i += 1
    if left_edge_empty or i >= CROTCH_BINS:
        return False
    return gap >= CROTCH_MIN_GAP_BINS


def _measure_crotch(verts: np.ndarray, centers: np.ndarray, height: float) -> float | None:
    """左右の脚が分離している最上部の高さ(=股下)を返す。見つからなければ None。

    スカート等で脚が覆われていると分離が検出できず None になる(既定値へ倒す)。
    低ポリなメッシュでは1スラブが空になることがあるため、前後数スラブを
    まとめた窓で判定し、さらに「そこより下の大半が分離している」最も高い
    スラブを採ることで単発のノイズを吸収する。
    """
    lo_i = int(SLABS * CROTCH_SCAN_RANGE[0])
    hi_i = int(SLABS * CROTCH_SCAN_RANGE[1])
    window_h = height / SLABS * CROTCH_WINDOW_SLABS
    z = verts[:, 2]

    flags = np.array(
        [
            _has_leg_gap(verts[(z >= centers[i] - window_h) & (z < centers[i] + window_h), 0])
            for i in range(lo_i, hi_i)
        ],
        dtype=bool,
    )
    if not flags.any():
        return None
    cumulative = np.cumsum(flags)
    for k in range(len(flags) - 1, -1, -1):
        if flags[k] and cumulative[k] >= CROTCH_RUN_RATIO * (k + 1):
            return float(centers[lo_i + k])
    return None


# --- 骨格レイアウト ---------------------------------------------------------


def bone_layout(m: Measurements) -> list[BoneSpec]:
    """計測結果から 21 ボーンのヒューマノイド骨格を組む。

    ボーン名は Godot の `SkeletonProfileHumanoid` に合わせる(R1-4 の
    BoneMap 自動割り当てが効く)。VRM 1.0 の humanoid 必須ボーン
    (hips/spine/head/左右 upperArm・lowerArm・hand・upperLeg・lowerLeg・foot)を
    すべて含み、chest/neck/shoulder/toes は任意ボーンとして追加している。
    """
    h = m.height
    hip_z = min(m.crotch_z + 0.03 * h, m.neck_z - 0.05 * h)
    torso = m.neck_z - hip_z
    spine_z = hip_z + 0.35 * torso
    chest_z = hip_z + 0.70 * torso

    span = max(m.arm_half_span - m.torso_half, 1e-6)
    shoulder_x = 0.85 * m.torso_half
    elbow_x = shoulder_x + ELBOW_RATIO * span
    wrist_x = shoulder_x + WRIST_RATIO * span
    hand_end_x = shoulder_x + HAND_END_RATIO * span

    bones: list[BoneSpec] = [
        BoneSpec("Hips", (0, 0, hip_z), (0, 0, spine_z), None),
        BoneSpec("Spine", (0, 0, spine_z), (0, 0, chest_z), "Hips", connect=True),
        BoneSpec("Chest", (0, 0, chest_z), (0, 0, m.neck_z), "Spine", connect=True),
        BoneSpec("Neck", (0, 0, m.neck_z), (0, 0, m.head_base_z), "Chest", connect=True),
        BoneSpec("Head", (0, 0, m.head_base_z), (0, 0, h), "Neck", connect=True),
    ]

    for side, sx in (("Left", 1.0), ("Right", -1.0)):
        z = m.shoulder_z
        bones += [
            BoneSpec(
                f"{side}Shoulder",
                (sx * 0.15 * shoulder_x, 0, z),
                (sx * shoulder_x, 0, z),
                "Chest",
            ),
            BoneSpec(
                f"{side}UpperArm",
                (sx * shoulder_x, 0, z),
                (sx * elbow_x, 0, z),
                f"{side}Shoulder",
                connect=True,
            ),
            BoneSpec(
                f"{side}LowerArm",
                (sx * elbow_x, 0, z),
                (sx * wrist_x, 0, z),
                f"{side}UpperArm",
                connect=True,
            ),
            BoneSpec(
                f"{side}Hand",
                (sx * wrist_x, 0, z),
                (sx * hand_end_x, 0, z),
                f"{side}LowerArm",
                connect=True,
            ),
            BoneSpec(
                f"{side}UpperLeg",
                (sx * m.leg_center_x, 0, hip_z),
                (sx * m.leg_center_x, 0, m.knee_z),
                "Hips",
            ),
            BoneSpec(
                f"{side}LowerLeg",
                (sx * m.leg_center_x, 0, m.knee_z),
                (sx * m.leg_center_x, 0, m.ankle_z),
                f"{side}UpperLeg",
                connect=True,
            ),
            BoneSpec(
                f"{side}Foot",
                (sx * m.leg_center_x, 0, m.ankle_z),
                (sx * m.leg_center_x, m.toe_break_y, m.toe_z),
                f"{side}LowerLeg",
                connect=True,
            ),
            BoneSpec(
                f"{side}Toes",
                (sx * m.leg_center_x, m.toe_break_y, m.toe_z),
                (sx * m.leg_center_x, m.foot_tip_y, m.toe_z),
                f"{side}Foot",
                connect=True,
            ),
        ]
    return bones
