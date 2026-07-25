extends Node
## rig-service が出力したリグ済みGLBを Godot 4 で検証する (計画書 §7 R1-4)。
##
## 検証内容:
##   1. Godot のネイティブ glTF インポータで Skeleton3D 付きで読めるか
##   2. ボーン名が `SkeletonProfileHumanoid` に一致し、BoneMap を自動生成できるか
##      (= インポートドックの Retarget でボーンマップが自動で埋まる状態)
##   3. 親子関係がプロファイルと一致するか(リターゲットの前提)
##   4. 実際にボーンを回してメッシュが変形するか(=「Godotで手足が動く」)
##   5. 向き・スケール(Y-up・原点=足元・メートル系)がGodot側で正しく見えるか
##
## 結果は JSON で `--out` (既定 res://verify_result.json) に書き出し、
## ディスプレイがあれば Tポーズ/ポーズ後のスクリーンショットも保存する。

const DEFAULT_MODEL := "res://model.glb"

## Tポーズから腕を下ろし、肘・膝・首を曲げるテストポーズ(度)。
## 「掴んでいるボーンが正しい部位か」はウェイト率では分からないため実際に回す。
const TEST_POSE := {
	"LeftUpperArm": Vector3(0, 0, -65),
	"RightUpperArm": Vector3(0, 0, 65),
	"LeftLowerArm": Vector3(-20, 0, -20),
	"RightLowerArm": Vector3(-20, 0, 20),
	"LeftUpperLeg": Vector3(25, 0, 0),
	"RightUpperLeg": Vector3(25, 0, 0),
	"LeftLowerLeg": Vector3(-45, 0, 0),
	"RightLowerLeg": Vector3(-45, 0, 0),
	"Head": Vector3(-10, 0, 0),
	"Spine": Vector3(5, 0, 0),
}

var _result := {}
var _skeleton: Skeleton3D
var _out_path := "res://verify_result.json"
var _shot_dir := "res://shots"


func _ready() -> void:
	var model_path := DEFAULT_MODEL
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--model="):
			model_path = arg.substr("--model=".length())
		elif arg.begins_with("--out="):
			_out_path = arg.substr("--out=".length())
		elif arg.begins_with("--shots="):
			_shot_dir = arg.substr("--shots=".length())

	_result = {"ok": false, "model": model_path, "errors": [], "warnings": []}

	if not ResourceLoader.exists(model_path):
		_fail("GLB が見つかりません(インポート済みか確認する): %s" % model_path)
		return

	var scene: PackedScene = load(model_path)
	if scene == null:
		_fail("GLB を PackedScene として読み込めませんでした: %s" % model_path)
		return

	var model: Node3D = scene.instantiate()
	add_child(model)
	await get_tree().process_frame

	_skeleton = _find_skeleton(model)
	if _skeleton == null:
		_fail("Skeleton3D が見つかりません(スキン情報が失われている)")
		return

	_check_bones()
	_check_bone_map()
	_check_transform(model)
	_check_facing()
	await _check_animations(model)
	_setup_camera_and_light(model)

	await _shot("00_rest.png")
	var moved := _apply_pose()
	await _shot("01_posed.png")
	_result["pose"] = moved

	_result["ok"] = _result["errors"].is_empty()
	_write_result()
	get_tree().quit(0 if _result["ok"] else 1)


func _fail(message: String) -> void:
	push_error(message)
	_result["errors"].append(message)
	_write_result()
	get_tree().quit(1)


func _write_result() -> void:
	var path := ProjectSettings.globalize_path(_out_path)
	FileAccess.open(path, FileAccess.WRITE).store_string(
		JSON.stringify(_result, "  ")
	)
	print(JSON.stringify(_result, "  "))


func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _check_bones() -> void:
	var names: Array[String] = []
	for i in _skeleton.get_bone_count():
		names.append(_skeleton.get_bone_name(i))
	_result["bone_count"] = _skeleton.get_bone_count()
	_result["bones"] = names

	var skins := 0
	for child in _skeleton.get_children():
		if child is MeshInstance3D and child.skin != null:
			skins += 1
	_result["skinned_meshes"] = skins
	if skins == 0:
		_result["errors"].append("Skeleton3D にスキン付きメッシュがぶら下がっていません")


func _check_bone_map() -> void:
	## `SkeletonProfileHumanoid` の各ボーンに、同名のボーンが骨格側にあるか。
	## 同名なら Godot のインポートドックの Retarget で BoneMap が自動で埋まる。
	var profile := SkeletonProfileHumanoid.new()
	var bone_map := BoneMap.new()
	bone_map.profile = profile

	var mapped: Array[String] = []
	var unmapped: Array[String] = []
	var parent_mismatch: Array[String] = []

	for i in profile.bone_size:
		var profile_bone := profile.get_bone_name(i)
		var idx := _skeleton.find_bone(profile_bone)
		if idx < 0:
			unmapped.append(profile_bone)
			continue
		bone_map.set_skeleton_bone_name(profile_bone, profile_bone)
		mapped.append(profile_bone)

		# 親子関係もプロファイルと一致していないとリターゲットが破綻する。
		# プロファイル側の親を遡り、骨格に存在する最も近い祖先と比べる。
		var expected := _nearest_existing_ancestor(profile, i)
		var parent_idx := _skeleton.get_bone_parent(idx)
		var actual := "" if parent_idx < 0 else _skeleton.get_bone_name(parent_idx)
		if expected != "" and expected != actual:
			parent_mismatch.append("%s: profile=%s actual=%s" % [profile_bone, expected, actual])

	_result["profile_bone_count"] = profile.bone_size
	_result["mapped_bones"] = mapped
	_result["unmapped_profile_bones"] = unmapped
	_result["parent_mismatch"] = parent_mismatch

	# VRM 1.0 / SkeletonProfileHumanoid の必須ボーン
	var required := [
		"Hips", "Spine", "Head",
		"LeftUpperArm", "LeftLowerArm", "LeftHand",
		"RightUpperArm", "RightLowerArm", "RightHand",
		"LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
		"RightUpperLeg", "RightLowerLeg", "RightFoot",
	]
	var missing := []
	for bone in required:
		if not mapped.has(bone):
			missing.append(bone)
	_result["missing_required_bones"] = missing
	if not missing.is_empty():
		_result["errors"].append("必須ボーンが BoneMap に載りませんでした: %s" % str(missing))
	if not parent_mismatch.is_empty():
		_result["warnings"].append("プロファイルと親子関係が異なるボーン: %s" % str(parent_mismatch))


func _nearest_existing_ancestor(profile: SkeletonProfileHumanoid, bone_idx: int) -> String:
	## プロファイル上の親を遡り、骨格に実在する最も近い祖先の名前を返す。
	## 任意ボーン(UpperChest 等)を省いていても正しく比較できるようにする。
	##
	## 注意: `SkeletonProfile.get_bone_parent()` はインデックスではなく
	## **親ボーン名(StringName)** を返す(`Skeleton3D` 側と逆なので紛らわしい)。
	var parent_name := String(profile.get_bone_parent(bone_idx))
	while parent_name != "":
		if _skeleton.find_bone(parent_name) >= 0:
			return parent_name
		var parent_idx := profile.find_bone(parent_name)
		if parent_idx < 0:
			return ""
		parent_name = String(profile.get_bone_parent(parent_idx))
	return ""


func _check_transform(model: Node3D) -> void:
	var aabb := _world_aabb(model)
	_result["aabb"] = {
		"min": [aabb.position.x, aabb.position.y, aabb.position.z],
		"size": [aabb.size.x, aabb.size.y, aabb.size.z],
	}
	# Y-up・原点=足元・メートル系で読めているか
	if aabb.size.y < aabb.size.z:
		_result["errors"].append("Y方向より前後方向が大きい(横倒しで読まれている)")
	if absf(aabb.position.y) > 0.05:
		_result["warnings"].append("足元がY=0にありません: y_min=%.3f" % aabb.position.y)
	if aabb.size.y < 0.3 or aabb.size.y > 5.0:
		_result["warnings"].append("身長が人型として不自然です: %.2fm" % aabb.size.y)


func _check_animations(model: Node3D) -> void:
	## 同梱クリップを焼き込んだGLB(`?motion=`)が Godot で再生できるか。
	## アニメが入っていないGLBでは何も検査しない(通常の出力なので正常)。
	var players := _find_animation_players(model)
	if players.is_empty():
		_result["animations"] = []
		return

	var player: AnimationPlayer = players[0]
	var names := player.get_animation_list()
	var listed: Array = []
	for name in names:
		listed.append({"name": name, "length": player.get_animation(name).length})
	_result["animations"] = listed
	if names.is_empty():
		_result["errors"].append("AnimationPlayer にクリップが入っていません")
		return

	# クリップの途中まで進めて、ボーンが実際に動くかを確かめる
	var hand := _skeleton.find_bone("LeftHand")
	var before := _skeleton.get_bone_global_pose(hand).origin if hand >= 0 else Vector3.ZERO
	var animation := player.get_animation(names[0])
	player.play(names[0])
	player.seek(animation.length * 0.5, true)
	await get_tree().process_frame
	var after := _skeleton.get_bone_global_pose(hand).origin if hand >= 0 else Vector3.ZERO
	var moved := before.distance_to(after)
	_result["animation_moved_m"] = snappedf(moved, 0.0001)
	if moved < 0.01:
		_result["errors"].append(
			"アニメーションを再生してもボーンが動きません(%.4fm)" % moved
		)
	player.stop()


func _find_animation_players(node: Node) -> Array[AnimationPlayer]:
	var out: Array[AnimationPlayer] = []
	if node is AnimationPlayer:
		out.append(node)
	for child in node.get_children():
		out.append_array(_find_animation_players(child))
	return out


func _check_facing() -> void:
	## 向きはスクショの目視では判断を誤りやすい(逆光だと顔の凹凸が読めない)。
	## ボーンのレスト位置で機械的に判定する。
	##   - つま先が足首より前 = キャラクターは +Z を向いている
	##   - 左手が +X 側 = 右手系(上=+Y, 正面=+Z)で左右が正しい
	var foot := _skeleton.find_bone("LeftFoot")
	var toes := _skeleton.find_bone("LeftToes")
	var hand := _skeleton.find_bone("LeftHand")
	if foot < 0 or toes < 0 or hand < 0:
		_result["warnings"].append("向き判定に必要なボーンが足りません")
		return

	var toe_forward := (
		_skeleton.get_bone_global_rest(toes).origin.z
		- _skeleton.get_bone_global_rest(foot).origin.z
	)
	var hand_x := _skeleton.get_bone_global_rest(hand).origin.x
	_result["facing"] = {
		"toe_forward_z": snappedf(toe_forward, 0.0001),
		"left_hand_x": snappedf(hand_x, 0.0001),
	}
	if toe_forward <= 0.0:
		_result["errors"].append(
			"つま先が背面側を向いています(キャラクターが +Z を向いていない): %.3f" % toe_forward
		)
	if hand_x <= 0.0:
		_result["errors"].append("左手が -X 側にあります(左右が反転している): %.3f" % hand_x)


func _world_aabb(node: Node) -> AABB:
	var aabb := AABB()
	var first := true
	for mesh in _all_mesh_instances(node):
		var box: AABB = mesh.global_transform * mesh.get_aabb()
		if first:
			aabb = box
			first = false
		else:
			aabb = aabb.merge(box)
	return aabb


func _all_mesh_instances(node: Node) -> Array[MeshInstance3D]:
	var out: Array[MeshInstance3D] = []
	if node is MeshInstance3D:
		out.append(node)
	for child in node.get_children():
		out.append_array(_all_mesh_instances(child))
	return out


func _apply_pose() -> Dictionary:
	## ボーンを回してメッシュが実際に変形するかを確かめる。
	var applied: Array[String] = []
	var missing: Array[String] = []
	for bone_name in TEST_POSE:
		var idx := _skeleton.find_bone(bone_name)
		if idx < 0:
			missing.append(bone_name)
			continue
		var degrees: Vector3 = TEST_POSE[bone_name]
		_skeleton.set_bone_pose_rotation(
			idx, Quaternion.from_euler(Vector3(
				deg_to_rad(degrees.x), deg_to_rad(degrees.y), deg_to_rad(degrees.z)
			))
		)
		applied.append(bone_name)

	# ポーズによってボーンのワールド位置が実際に動いたかを数値で確認する
	var hand := _skeleton.find_bone("LeftHand")
	var moved := 0.0
	if hand >= 0:
		var rest_pos := (_skeleton.get_bone_global_rest(hand)).origin
		var posed_pos := (_skeleton.get_bone_global_pose(hand)).origin
		moved = rest_pos.distance_to(posed_pos)
	if moved < 0.05:
		_result["errors"].append(
			"ポーズを適用しても LeftHand がほとんど動きません(%.4fm)" % moved
		)
	return {
		"applied": applied,
		"missing": missing,
		"left_hand_moved_m": snappedf(moved, 0.0001),
	}


func _setup_camera_and_light(model: Node3D) -> void:
	var aabb := _world_aabb(model)
	var height: float = maxf(aabb.size.y, 0.1)
	var center := aabb.position + aabb.size * 0.5

	var camera := Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = height * 1.3
	# 正面(glTFの+Z)側から見る
	camera.position = Vector3(center.x, center.y, aabb.position.z + aabb.size.z + height * 2.0)
	add_child(camera)
	camera.look_at(center, Vector3.UP)
	camera.current = true

	# ライトは必ずカメラ側に置く。背後から当てると顔の凹凸が潰れ、
	# 正面と背面をスクショで見分けられなくなる。
	var light := DirectionalLight3D.new()
	light.light_energy = 2.0
	add_child(light)
	light.position = camera.position + Vector3(0, height * 0.8, 0)
	light.look_at(center, Vector3.UP)

	var env := WorldEnvironment.new()
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color(0.16, 0.17, 0.20)
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color(0.6, 0.6, 0.65)
	environment.ambient_light_energy = 0.6
	env.environment = environment
	add_child(env)


func _shot(fname: String) -> void:
	if DisplayServer.get_name() == "headless":
		return
	for i in 6:
		await get_tree().process_frame
	await RenderingServer.frame_post_draw
	var dir := ProjectSettings.globalize_path(_shot_dir)
	DirAccess.make_dir_recursive_absolute(dir)
	get_viewport().get_texture().get_image().save_png(dir + "/" + fname)
	print("saved ", fname)
