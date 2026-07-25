"""RigEngine 抽象基底と共通のサブプロセス実行(計画書 §4/§5)。

計画書では bpy エンジンを「モジュール直接呼び出し」としていたが、実装では
**同一venvの Python を使ったサブプロセス実行**に変えている。理由:

- bpy は Blender のグローバル状態をそのまま抱えるシングルトンで、Python API は
  スレッドセーフではない。FastAPI のワーカースレッドから `bpy.ops` を叩くのは
  公式に非サポートで、クラッシュするとサーバごと落ちる。
- 実際に `BLENDER_WORKBENCH` でのレンダリングは GL コンテキストが無いと
  `epoxy_get_proc_address` でプロセスを abort させる(=捕捉不能)。
- サブプロセスならジョブ単位でメモリが解放され、`bpy` モジュール方式と
  Blender CLI 方式が**まったく同じスクリプト**を共有できる。
- image-3d が Pixal3D を「別venv+外部プロセス」で統合しているのと同じパターン。

追加コストは bpy の import 時間(実測 1〜2秒)のみで、リグ処理自体(5〜8秒)に対して
十分小さい。
"""
from __future__ import annotations

import json
import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class RigEngineError(RuntimeError):
    """リグエンジンの実行失敗。ジョブを failed にする。"""


@dataclass
class RigParams:
    """`POST /api/rig` の params (計画書 §6)。

    `vrm` / `vrm_meta` はリグ処理そのものには使わない(リグ後に純Pythonで
    GLB→VRM 変換する `server/vrm.py` が読む)。ジョブの永続化を単純に保つため
    同じデータクラスに載せている。
    """

    height_m: float = 1.6
    up_axis: str = "auto"
    facing: str = "auto"
    bone_set: str = "standard"
    preview: bool = True
    vrm: bool = True
    vrm_meta: dict = field(default_factory=dict)


@dataclass
class RigResult:
    output_path: Path
    preview_path: Path | None
    summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class RigEngine(ABC):
    """リグエンジンの抽象基底。UniRig 等への差し替えを想定してプラガブルにする。"""

    name: str = "base"

    @abstractmethod
    def command(self, args: list[str]) -> list[str]:
        """`autorig.py` に `args` を渡して実行するコマンドラインを組み立てる。"""
        raise NotImplementedError

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """このエンジンが実行可能かと、その根拠(バージョン文字列等)を返す。"""
        raise NotImplementedError

    def rig(
        self,
        input_glb: Path,
        output_glb: Path,
        params: RigParams,
        work_dir: Path,
        timeout_sec: int,
    ) -> RigResult:
        """リグ処理を実行する(同期・ワーカースレッドから呼ばれる)。"""
        result_path = work_dir / "rig_result.json"
        preview_path = work_dir / "preview.png" if params.preview else None

        args = [
            str(input_glb),
            # `--facing` の値は "-y" のようにハイフンで始まりうる。
            # 値を別トークンで渡すと argparse がオプション名と誤認して
            # 「expected one argument」で落ちるため、`=` 形式で繋ぐ。
            f"--output={output_glb}",
            f"--result={result_path}",
            f"--height-m={params.height_m}",
            f"--up-axis={params.up_axis}",
            f"--facing={params.facing}",
        ]
        if preview_path is not None:
            args.append(f"--preview={preview_path}")

        cmd = self.command(args)
        logger.info("running rig engine %s: %s", self.name, " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RigEngineError(
                f"リグ処理が {timeout_sec} 秒でタイムアウトしました。"
            ) from exc
        except FileNotFoundError as exc:
            raise RigEngineError(
                f"リグエンジン({self.name})の実行ファイルが見つかりません: {exc}"
            ) from exc

        # スクリプトが結果JSONを書けていれば、それがもっとも詳しい失敗理由になる。
        summary: dict = {}
        if result_path.exists():
            try:
                summary = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("結果JSONを解析できませんでした: %s", result_path)

        if proc.returncode != 0 or not summary.get("ok"):
            detail = summary.get("error") or _tail(proc.stderr) or _tail(proc.stdout)
            raise RigEngineError(f"リグ処理に失敗しました: {detail}")
        if not output_glb.exists():
            raise RigEngineError("リグ処理は成功しましたが出力GLBが見つかりません。")

        if preview_path is not None and not preview_path.exists():
            preview_path = None

        summary.pop("ok", None)
        return RigResult(
            output_path=output_glb,
            preview_path=preview_path,
            summary=summary,
            warnings=list(summary.pop("warnings", [])),
        )


def _tail(text: str | None, lines: int = 8) -> str:
    if not text:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])
