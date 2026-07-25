// リグ済みGLB/VRM の3Dプレビュー (計画書 §7 Phase R3-1)。
//
// three-vrm は使わない。rig-service の出力は**標準スキン付きの正当な glTF**
// なので、素の three.js の GLTFLoader だけで読み込みもポーズ適用もできる
// (three-vrm が足すのは表情・スプリングボーン・MToon 等、現状出力していない
// VRM固有機能のみ)。ビルド工程も持たない(importmap + vendor 配置)。
//
// ポーズはサーバに投げず**ブラウザ内でボーンに直接当てる**。数十MBのGLBを
// 毎操作で往復させないため。ポーズ済みファイルが欲しいときだけ
// POST /api/rig/jobs/{id}/pose を使う。
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

// レスト姿勢からの相対回転(度, XYZ順)。サーバ側 server/pose.py と同じ意味づけ。
export const POSE_PRESETS = {
  tpose: {},
  arms_down: {
    LeftUpperArm: [0, 0, -70],
    RightUpperArm: [0, 0, 70],
    LeftLowerArm: [0, 0, -25],
    RightLowerArm: [0, 0, 25],
  },
  relaxed: {
    LeftUpperArm: [0, 0, -65],
    RightUpperArm: [0, 0, 65],
    LeftLowerArm: [-20, 0, -20],
    RightLowerArm: [-20, 0, 20],
    LeftUpperLeg: [25, 0, 0],
    RightUpperLeg: [25, 0, 0],
    LeftLowerLeg: [-45, 0, 0],
    RightLowerLeg: [-45, 0, 0],
    Head: [-10, 0, 0],
    Spine: [5, 0, 0],
  },
  wave: {
    LeftUpperArm: [0, 0, 25],
    LeftLowerArm: [0, -40, 20],
    RightUpperArm: [0, 0, 70],
    RightLowerArm: [0, 0, 25],
    Head: [0, 15, 0],
  },
};

export class RigViewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x2a2d33);

    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 100);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 2.0));
    const key = new THREE.DirectionalLight(0xffffff, 2.0);
    key.position.set(1, 2, 3);
    this.scene.add(key);

    this.model = null;
    this.skeletonHelper = null;
    // 再生中のクリップ(server/motions.py が配る形式)
    this.motion = null;
    this.motionTime = 0;
    this.motionPlaying = false;
    this._clock = new THREE.Clock();
    // ユーザーがカメラを触るまでは自動フレーミングを続ける
    this._needsFrame = false;
    this.controls.addEventListener("start", () => { this._needsFrame = false; });
    // ボーン名 -> {bone, restQuaternion}。ポーズはレストに対する相対回転で当てる。
    this.bones = new Map();

    // プレビューは初期状態が hidden なので、生成時点では clientWidth が 0 になる。
    // window の resize だけを見ていると 1x1 のまま描画され続けるため、
    // 実際の表示サイズ変化を ResizeObserver で拾う。
    this._resize();
    new ResizeObserver(() => this._resize()).observe(canvas);
    this.renderer.setAnimationLoop(() => {
      this._advanceMotion(this._clock.getDelta());
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
  }

  _resize() {
    const width = this.canvas.clientWidth || 1;
    const height = this.canvas.clientHeight || 1;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    // 初回レイアウト確定前に load() されるとアスペクト比が違う状態で
    // フレーミングしてしまうので、ユーザーが操作するまでは再計算する。
    if (this._needsFrame && this.model) this._frameCamera();
  }

  async load(url) {
    const gltf = await new GLTFLoader().loadAsync(url);
    if (this.model) this.scene.remove(this.model);
    if (this.skeletonHelper) this.scene.remove(this.skeletonHelper);

    this.model = gltf.scene;
    this.scene.add(this.model);

    this.bones.clear();
    this.model.traverse((obj) => {
      if (obj.isBone) {
        this.bones.set(obj.name, { bone: obj, rest: obj.quaternion.clone() });
      }
    });

    this.skeletonHelper = new THREE.SkeletonHelper(this.model);
    this.skeletonHelper.visible = false;
    // 骨は必ずメッシュの内側にあるので、深度テストを切らないと見えない
    this.skeletonHelper.material.depthTest = false;
    this.skeletonHelper.material.depthWrite = false;
    this.scene.add(this.skeletonHelper);

    this.motion = null;
    this.motionPlaying = false;
    this._needsFrame = true;
    this._frameCamera();
    return { boneNames: [...this.bones.keys()] };
  }

  _frameCamera() {
    // SkinnedMesh のバウンディングボックスはボーンの行列から計算されるため、
    // シーンに追加した直後(まだ描画1フレーム目が来ていない)は行列が古く、
    // Box3.setFromObject が実寸の6割程度の箱を返す。先に行列を更新しておく。
    this.model.updateMatrixWorld(true);
    this.model.traverse((obj) => {
      if (obj.isSkinnedMesh) {
        obj.skeleton.update();
        obj.computeBoundingBox();
      }
    });
    const box = new THREE.Box3().setFromObject(this.model);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());

    // 縦横それぞれが画角に収まる距離を出し、大きいほうを採る。
    // 横長のキャンバスでは縦の画角が効くので、単純な max(size) では頭が切れる。
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const fitHeight = size.y / 2 / Math.tan(fov / 2);
    const fitWidth = size.x / 2 / (Math.tan(fov / 2) * this.camera.aspect);
    const distance = Math.max(fitHeight, fitWidth) * 1.25;

    // 正面(+Z)からやや斜めを初期視点にする
    this.camera.position.set(
      center.x + distance * 0.3,
      center.y + size.y * 0.1,
      center.z + distance,
    );
    this.controls.target.copy(center);
    this.controls.update();
  }

  setBonesVisible(visible) {
    if (this.skeletonHelper) this.skeletonHelper.visible = visible;
  }

  /** 同梱クリップを再生する。`null` で停止してレストに戻す。 */
  playMotion(motion) {
    this.motion = motion;
    this.motionTime = 0;
    this.motionPlaying = motion != null;
    if (motion == null) this.applyPose({});
  }

  setMotionTime(seconds) {
    if (!this.motion) return;
    this.motionTime = seconds;
    this._applyMotionFrame();
  }

  setMotionPlaying(playing) {
    this.motionPlaying = playing && this.motion != null;
  }

  _advanceMotion(delta) {
    if (!this.motion || !this.motionPlaying) return;
    this.motionTime += delta;
    if (this.motionTime > this.motion.duration) {
      this.motionTime = this.motion.loop ? this.motionTime % this.motion.duration
                                         : this.motion.duration;
      if (!this.motion.loop) this.motionPlaying = false;
    }
    this._applyMotionFrame();
  }

  _applyMotionFrame() {
    const time = this.motionTime;
    const delta = new THREE.Quaternion();
    const a = new THREE.Quaternion();
    const b = new THREE.Quaternion();
    for (const [name, entry] of this.bones) {
      const keys = this.motion.tracks[name];
      if (!keys || keys.length === 0) {
        entry.bone.quaternion.copy(entry.rest);
        continue;
      }
      // 時刻を挟む2キーを探して球面線形補間する(glTFのLINEAR回転と同じ)
      let i = 0;
      while (i < keys.length - 1 && keys[i + 1].time <= time) i += 1;
      const k0 = keys[i];
      const k1 = keys[Math.min(i + 1, keys.length - 1)];
      const span = k1.time - k0.time;
      const t = span > 0 ? Math.min(Math.max((time - k0.time) / span, 0), 1) : 0;
      a.fromArray(k0.rotation);
      b.fromArray(k1.rotation);
      delta.copy(a).slerp(b, t);
      entry.bone.quaternion.copy(entry.rest).multiply(delta);
    }
  }

  /** ポーズ(度, XYZ順)を当てる。指定の無いボーンはレストに戻す。 */
  applyPose(pose) {
    this.motion = null;
    this.motionPlaying = false;
    const euler = new THREE.Euler();
    const delta = new THREE.Quaternion();
    for (const [name, entry] of this.bones) {
      const degrees = pose[name];
      if (!degrees) {
        entry.bone.quaternion.copy(entry.rest);
        continue;
      }
      euler.set(
        THREE.MathUtils.degToRad(degrees[0]),
        THREE.MathUtils.degToRad(degrees[1]),
        THREE.MathUtils.degToRad(degrees[2]),
        "XYZ",
      );
      delta.setFromEuler(euler);
      // レストの「後ろに」合成する = ボーンのローカル系での回転
      entry.bone.quaternion.copy(entry.rest).multiply(delta);
    }
  }
}
