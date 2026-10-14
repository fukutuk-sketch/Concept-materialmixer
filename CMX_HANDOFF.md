# Concept Material Mixer — 引き継ぎ・開発仕様

Blender 用アドオン `concept_material_mixer.py`（v1.0.0、単一ファイル・約4300行）の引き継ぎ資料です。
次の作業（下の「バックログ」）を GitHub 上で Claude Code に依頼するために書いています。

- 利用者：小坂さん（コンセプトアーティスト。日本語。プログラマーではない）
- 目的：Quixel Mixer のように、マテリアルをレイヤーで重ね、自動マスク＋手塗りで塗り分け、最後にテクスチャへ焼き込む
- 対象環境：Windows、Blender 4.5.4 LTS（EEVEE Next）、RTX 3060（OptiX）、メモリ 32GB
- UI：3Dビューのサイドバー、既存タブ「Concept」に「マテリアルミキサー」パネル

---

## 1. 守ること（必須ルール）

1. **UIは日本語**。ラベル・ツールチップ（`description`）は、プログラマーでない人が読んでわかる言葉で書く。
2. **アイコン名は必ず `_icon(name)` を通す**。存在しないアイコン名を渡すとパネル全体が描画されなくなる（`bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items` で存在確認済みの名前だけを返す）。
3. **名前の衝突を避ける**。新しいデータ（マテリアル・画像・ノードグループ・モディファイア・オブジェクト）の名前は `CMX_` で始める。プロパティは `Object.cmx`、オペレーターは `cmx.*`、クラスは `CMX_*`。
4. **ほかのアドオンのデータは変更しない**：地面ペイント（`concept_ground_paint`, `Object.cgp`, `CGP_`）、接地ブレンド（`CCB_`）、水たまり（`concept_puddle`, `CPD_`）。読むのはよい。
5. **ユーザーの状態を勝手に変えない**：アクティブシーン、モード、選択、アクティブオブジェクト、ビューポートのシェーディング、`scene.camera`、レンダーエンジン。どうしても変える処理（編集モード・ベイク）は `_SoloActive` / `_CyclesBakeEnv` の中で行い、終わったら必ず元に戻す。
   - 注意：`context.temp_override(active_object=…)` のまま `bpy.ops.object.mode_set` を呼ぶと、**本当のアクティブオブジェクト**のモードが変わる（実際に起きた不具合）。編集モードが必要なときは `_SoloActive` を使う。
6. **共有ノードグループの入出力を作り直さない**。`CMX_Layer::…` などのグループは複数のマテリアルで共有される。`interface.clear()` するとほかのマテリアル側のリンクが切れて、白く抜ける（実際に起きた不具合）。入出力は `_ensure_interface(g, spec)` でそろえ、中身（`g.nodes.clear()`）だけ作り直す。新しい入出力を足すときは、spec に追加すれば一度だけ作り直される。
7. **パネルの `draw()` は必ずモック検証する**（「5. テスト」を参照）。存在しないプロパティ・オペレーター・アイコンがあると、パネルが黙って壊れる。
8. 新しいノードグループやGNを変えたときは、`*_VER` 定数（`HL_VER`, `PN_VER`, `SM_VER`）を上げる（古いファイルを開いたときに作り直されるように）。

---

## 2. ファイル構成（`concept_material_mixer.py`）

| 行（目安） | セクション | 中身 |
|---|---|---|
| 1– | 定数 | `PAINT_UV="CMX_UV"`, `MASK_TYPES`, `MASK_DEFAULTS`, `PROJECTIONS`, `ADJ_TYPES` など |
| 87– | small helpers | `_icon()`, `_is_mixer_material()` |
| 107– | node copy helpers | マテリアルのノードを別ツリーへコピー（`_copy_node(s)`, `_copy_ramp`, `_copy_curves`）、ノード組み立て用 `_NB` |
| 341– | shader node groups | `_ensure_interface`, `ensure_heightlerp`（ハイトブレンド）, `ensure_planar_normal`（投影用ノーマルマップ）, `_make_procedurals_periodic`, `build_layer_group` |
| 658– | textures → material | ファイル名から種類を判別してマテリアル化（ドロップ取り込み用） |
| 804– | surface masks | **他ツールでも使い回す部品**。エッジ・くぼみ・AOを頂点に計算するGN `CMX_SurfaceMasks`、`ensure_mask_modifier`、`_CyclesBakeEnv`、`_TempBakeNodes`、`bake_surface_masks` |
| 1195– | paint UV | `ensure_paint_uv`（Smart UV で `CMX_UV`）, `_SoloActive` |
| 1301– | texture bake & export | `bake_textures`, `save_baked_images`, `build_baked_material`, `export_dir` |
| 1505– | smart materials | JSON＋blend でプリセット保存・適用（`pg_to_dict` / `dict_to_pg`） |
| 1657– | tiling material | `create_tile_object`（継ぎ目なしタイル素材用の平面） |
| 1684– | ground paint import | 地面ペイントのレイヤーと塗りを取り込む |
| 1787– | paint images | 塗り画像の作成・塗りつぶし |
| 1903– | material builder | `_Shared`, `_mask_value`, `_auto_mask`, `_paint_mix`, `_material_sockets`, `rebuild_material`（**中心**） |
| 2406– | light display | 塗っている間の簡易表示・サブディビジョン一時低下・終了監視タイマー |
| 2483– | property callbacks | ノード名でライブ更新（再構築なし）か、`rebuild_material` |
| 2800– | properties | `CMX_Mask`, `CMX_Layer`, `CMX_MatRef`, `CMX_Settings` |
| 2972– | operators | `cmx.*` |
| 3718– | UI | UIList・メニュー・パネル（親 `CMX_PT_main`、子パネル群） |
| 4174– | handlers / register | save_pre でパック、load_post で監視を復帰 |

---

## 3. 仕組みの要点

### 3.1 データ
- `Object.cmx`（`CMX_Settings`）：`layers`（`CMX_Layer` のコレクション）、`material`（ミキサーのマテリアル）、`orig_mats`（元のマテリアル）、表示・焼き込み・タイル設定など。
- `CMX_Layer`：`kind`（MATERIAL / ADJUST）、`material`（空なら単色）、`projection`（TRIPLANAR / UV / TOP / ORIGINAL）、`tile_size`, `uv_repeat`, `height_contrast`, `height_offset`, `softness`, `opacity`, `masks`（`CMX_Mask`）, `image`（塗り画像）, `use_auto` など。
- `CMX_Mask`：`mtype`（EDGE / CAVITY / AO / UP / HEIGHT / SLOPE / NOISE / LAYER_H）、`blend`（MULTIPLY / ADD / MAX / SUBTRACT / MIN）、`amount`, `contrast`, `noise`, `strength`, `invert` など。
- レイヤー0がベース（常に全面）。上のレイヤーほど上に重なる。

### 3.2 マテリアルの組み立て（`rebuild_material`）
- 1つの物に1つのマテリアル `CMX_<物名>` を作り、**全スロット**に入れる（`assign_slots`）。焼き込み表示中は `baked_material` を入れる。
- 各マテリアルレイヤーは `build_layer_group(mat, mode)` でノードグループ化し、出力を Color / Roughness / Metallic / Normal / Height にそろえる。
  - mode `O`：そのまま、`U`：UV（UVマップ名ごと）、`P`：投影（トリプラナー・真上。ノーマルマップは `CMX_PlanarNormal` に置き換え）、`T`：タイル素材（UV＋ノイズ・ボロノイを4Dトーラス座標にする）。
  - トリプラナー＝同じグループを3回呼び、法線の向きで重み付けする。画像テクスチャを含まない素材は1回だけ呼ぶ。
- レイヤー同士は `CMX_HeightLerp`（重み＋各レイヤーのハイトで混ぜる。やわらかいほどハイトの影響が弱まる。金属と非金属の境目では色と金属度だけ切り替えを急にする）で重ねる。
- 最後に `OUT_Color` / `OUT_Roughness` / `OUT_Metallic` / `OUT_Normal` / `OUT_Height`（Reroute）を経由して BSDF へつなぐ。焼き込みはこの出口を読む。
- 法線は BSDF の直前で `normalize(N + 0.001·Ng)` してから入れる（長さ0の法線対策）。
- **ライブ更新**：値を変えるだけのパラメータは Value ノードに名前を付け（例 `L2_M0_amount`, `L1_opacity`, `G_hs`）、コールバックで値だけ書き換える（シェーダー再コンパイルなし）。種類・構造が変わる設定だけ `rebuild_material`。

### 3.3 マスク
- 最終マスク：`w = (自動マスク × (1 − 消す)) + 足す × (1 − …)`、`× 不透明度`。
- 自動マスク：マスクを上から順に、重ね方（掛け算など）と「強さ」で合成。マスクがなければ 1（全面）。`use_auto=False` なら 0（塗った所だけ）。
- 各マスク：`v + 共有ノイズによる崩し` → `smoothstep(1−範囲 ± (1−くっきり)/2)` → 反転。
- エッジ・くぼみ・AO の元データ（`mask_source`）：
  - `LIVE`：GN モディファイア `CMX_Masks`（スタックの最後）が頂点属性 `CMX_Masks`（R=エッジ, G=くぼみ, B=AO）を書く。曲率は `κ = (N1−N2)·(P1−P2)/|P1−P2|²` × エッジ判定の大きさ。AO は12本のレイキャスト。
  - `BAKED`：Cycles の AO ノード（inside で凸、通常で凹）を `CMX_UV` に焼いた画像。ローポリでもきれい。焼いた後、上位98パーセンタイルが1になるよう自動でそろえる。
- 共有ノイズ `G_noise`：境界の崩し用。マテリアル内で1つだけ（Colorの3チャンネルを使い分け）。

### 3.4 塗り画像（1レイヤー1枚、8bit Non-Color、`CMX_UV`）
- **R = 0.5 で「自動のまま」、1 = 足す、0 = 消す**。G = やわらかさ×塗りの量、B = ノイズ×塗りの量（乗算済み。筆の端でも値が保たれる）。
- ブラシ色はパネルが自動で設定する：足す (1, soft, noise)、消す (0, soft, noise)、自動に戻す (0.5, 0, 0)。X キー（色の入れ替え）で足す⇔消す。
- 中央値 0.5 の周りに ±0.02 の不感帯がある。
- 塗り終了時に、変更された画像をパックする（`_end_paint`）。
- ※実際のブラシでの塗りは、まだ実機で確認していない（画像への直接書き込みでのみ確認済み）。

### 3.5 焼き込み・書き出し
- `bake_textures`：BaseColor（sRGB）/ Roughness / Metallic / Normal（接空間、焼く間だけ焼き込みUVをアクティブにする）/ Height（16bit）/ AO を Cycles で焼く。GPU（OptiX）を一時的に使い、終わったら元に戻す。
- 保存先：指定フォルダ → なければ blend の隣の `textures\物名\` → blend 未保存なら `Documents\CMX_Textures\物名\`。
- `build_baked_material`：焼いた画像だけの軽いマテリアル `CMX_<物名>_Baked`。`use_baked` で切り替え（ミキサーの設定は残る）。

### 3.6 スマートマテリアル
- 保存先：`<Blender config>/concept_material_mixer/smart_materials/<名前>.json` と `<名前>.blend`（使っているマテリアル）。
- 適用時、足りないマテリアルは blend から読み込む。元が BAKED マスクなら、適用先でも自動で焼く。塗りは引き継がない。

### 3.7 タイル素材
- 正方形1枚、`CMX_UV` を 0〜1 にぴったり合わせる。レイヤーは整数回くり返し（`tile_repeats`）。
- ノイズ・ボロノイ（素材の中のものも含む）は4Dトーラス座標で継ぎ目なしにする：`(R cos 2πu, R sin 2πu, R cos 2πv) と W = R sin 2πv`。
- 確認用の3×3表示（Array モディファイア `CMX_TilePreview_X/Y`）は、焼き込み中だけ無効にする。

### 3.8 性能の目安（RTX 3060、1024²、岩・4レイヤー・全部トリプラナー）
- ミキサー表示：1サンプル 11.9 ms、塗り中の簡易表示：3.7 ms、焼き込み後：0.4 ms。
- 焼き込み：1024² で6枚 8〜11秒（初回のみ OptiX の準備に1〜2分）。

---

## 4. バックログ（次にやること）

ユーザー確認済みの優先順：**4.1 → 4.2 → 4.3** を最初のまとまりとして実装する。それ以降は「4.4 その他」から順に。

### 4.1 タイリング感をなくす「ランダムシート」（フィードバック①）
目的：同じテクスチャの繰り返しが目立たないようにする。Quixel Mixer には無い機能。

- レイヤーごとの設定（パネル「レイヤー」内の折りたたみ「タイリング崩し」）：
  - `anti_tile`（使う/使わない）
  - `at_cell`：セルの大きさ（タイル何枚分か。既定 1.0）
  - `at_shape`：四角 / 六角形
  - `at_rot`：回転なし / 90°刻み / 自由
  - `at_flip`：左右反転も混ぜる
  - `at_offset`：位置ずらしの量（0〜1）
  - `at_blend`：セルの境目の幅（0〜1）。境目はレイヤーのハイトで混ぜる（`CMX_HeightLerp` と同じ考え方）か、ノイズで崩す
  - `at_seed`
- 実装方針：
  - セル ID はボロノイ（Cell ID を Color 出力から得る）または `floor()` で作り、セルごとの乱数で UV のずらし・回転・反転を決める。
  - 隣のセルとの境目だけ2〜3回サンプルして混ぜる（ヘックスタイリング / テクスチャボミングの考え方）。
  - **ノーマルマップの向き**：UV を回転・反転したら、ノーマルの XY も同じだけ回す・反転する。`CMX_PlanarNormal` と同様、ノーマルマップノードを置き換える方式で実装する。
  - 対応する投影：UV・真上・トリプラナー（トリプラナーは平面ごとに適用）。タイル素材モードでは、セルがタイルの端で継ぎ目なくつながるよう、セル数を整数にしてセル ID を wrap する。
  - 重さ：簡易表示（塗り中）では無効にする。
- 全体の設定「大きなムラ」：数 m 単位のノイズで、最終の明るさ・色相・ラフネスをわずかに揺らす（`macro_amount`, `macro_scale`）。軽く、効果が大きい。
- 受け入れ条件：
  - 4×4 以上の繰り返しで、格子状の繰り返しが目で追えないこと。
  - 回転したセルで、光の当たり方（ノーマル）が破綻しないこと。セル境目の左右で陰影が反転しないこと。
  - タイル素材モードで焼いたとき、端と端の差が隣接ピクセルの差と同程度であること（既存の継ぎ目テストと同じ方法）。

### 4.2 ハイトで凹凸感を出す（フィードバック②）
今のハイトはレイヤー境界の判定にしか使っておらず、見た目の凹凸は素材のノーマルだけに頼っている。

- レイヤーごと：
  - `normal_strength`（ノーマルの強さ。0〜2）
  - `height_strength_layer`（このレイヤーのハイトの強さ）
  - `height_invert`
  - `normal_flip_y`（DirectX 形式の素材用）
  - `normal_from_height`（ノーマルマップのない素材に、ハイトからノーマルを作る。Bump ノード）
  - 「細かい凹凸 / 大きい凹凸」の強さを分ける（Mixer の Height Frequency）。シェーダーでは、ハイトの低周波成分を得るのが難しい。そのため、焼き込み時に分けるか、素材の Bump / ノーマルの強さと Displacement の強さを別々に掛ける近似でよい。
- 全体：
  - `global_bump`：最終 `OUT_Height` から Bump を作り、最終ノーマルに重ねる（例：苔が岩の上に盛り上がって見える）。強さと距離。
  - 重ね方の追加：「上から積もる / 下から出る」（Mixer の Invert）、「下の形に沿わせる量」（Wrap to Underlying）、「下の凹凸をぼかす」（Blur Underlying。近似でよい）。
  - 本当に形を動かす：焼いたハイトを使い、Cycles のマテリアルディスプレイスメント（`displacement_method`）または `Displace` モディファイア（`CMX_Displace`）を付けるボタン。強さ・中心値を調整でき、外すこともできる。
- 受け入れ条件：
  - 強さ0で今と同じ見た目になること（既存のファイルが変わらない）。
  - 焼き込みの Normal / Height にも反映されること。

### 4.3 配置（Placement）
- レイヤーごとに：位置ずらし X/Y、回転（自由角度。UV では 90° 刻みも選べる）、X/Y 別々の繰り返し（またはタイルサイズ）。
- トリプラナー・真上・UV・タイル素材の全モードで効くこと。回転したときはノーマルの向きも直すこと（4.1 と同じ処理を共通化する）。
- ライブ更新（Value ノード）にして、スライダー操作で再コンパイルしないこと。

### 4.4 その他（Quixel Mixer にあって、まだ無いもの）
優先度・高
1. **素材ID・頂点カラーのマスク**：元のマテリアルスロット／頂点カラー／面セットを自動マスクの種類として使う（今は全スロットを1つのマテリアルにまとめるため、部品ごとの塗り分けができない）。
2. **画像をマスクに使う**（グランジ画像など）。投影は、レイヤーと同じ選択肢から選ぶ。
3. **マスクの加工**：ぼかし、カーブ（グラデーション再割り当て）、クランプ、段階化（ポスタライズ）、自動レベル補正。
4. **チャンネルごとの調整**：レイヤーの色・ラフネス・メタルの個別コントラスト・反転・不透明度。「このレイヤーはラフネスだけ変える」などのチャンネル選択。

優先度・中

5. **デカール（アトラス）レイヤー**：アルファ付き画像（葉・ひび・染み）をスタンプ配置、マスク用ランダム散布（密度・大きさ範囲・回転範囲・明るさのばらつき・シード）。
6. **液体レイヤー**：低い所に水がたまる（しきい値・境目・下の凹凸の反映）、にごり（表面・深さ）、周りの湿り（しきい値・ぼかし・暗さ・ラフネス）。今の「濡れ」調整の発展版。水たまりアドオン（`concept_puddle`）とは別物。
7. **ノイズレイヤー**：凹凸だけを足す（強さ・細かさ・重なり数・Lacunarity・Persistence・クランプ）。
8. **模様マスク**：レンガ・タイル・縞・水玉。
9. **方向マスクの拡張**：上向き以外に、任意の方向（X/Y/Z・ワールド/ローカル）。高さグラデーションも X/Y 方向に。
10. **レイヤーのグループ（フォルダ）**：表示切り替え・不透明度をグループ単位で。

優先度・低

11. **書き出しのチャンネル詰め**：ORM（AO・ラフネス・メタル）などのプリセット（UE / Unity）、ノーマルの DirectX / OpenGL 切り替え、ビット深度・形式の指定。
12. **マスクの書き出し**（レイヤーのマスクを白黒 PNG で）。
13. **スマートマテリアル一覧のサムネイル**（保存時に小さなプレビューを焼く）。
14. **チャンネル単体の表示**（ラフネスだけ・ハイトだけ等）。今の `view_mode` に追加する。

参考（Quixel Mixer 公式ドキュメント）：
- Layer Properties / Blending: https://docs.quixel.com/mixer/1/en/topic/blending.html
- Placement: https://docs.quixel.com/mixer/1/en/topic/placement
- Channel Specific Controls: https://docs.quixel.com/mixer/1/en/topic/channel-specific-controls
- Surface Layer（Height Frequency）: https://docs.quixel.com/mixer/1/en/topic/surface-layer
- Scatter: https://docs.quixel.com/mixer/1/en/topic/scatter
- Liquid Layer: https://docs.quixel.com/mixer/1/en/topic/liquid-layer
- Noise Layer: https://docs.quixel.com/mixer/1/en/topic/noise-layer

---

## 5. テスト

Blender 実機が無い環境でも、少なくとも次を通すこと。

### 5.1 必須（どこでも）
- `python -c "import ast; ast.parse(open('concept_material_mixer.py').read())"`
- 新しい `bl_idname` / プロパティ名 / アイコン名が、既存と衝突しないこと（`CMX_` / `cmx.`）。

### 5.2 Blender があれば（ヘッドレス可）
```
blender -b --factory-startup --python-expr "import sys; sys.path.insert(0, r'<repoのパス>'); import concept_material_mixer as m; m.register(); print('ok')"
```
次を確認する：
1. **登録**：`register()` → `unregister()` → `register()` がエラーなく通る（登録中は `bpy.data` が使えないため、データに触る処理はタイマーで遅らせている）。
2. **組み立て**：立方体・高密度の球などに `enable_mixer` → レイヤー／各マスク種類／各投影／調整レイヤーを追加 → `rebuild_material` が例外なく通る。
3. **共有グループ**：同じ素材を使う物を2つ作り、交互に `rebuild_material` しても、すべての `L*_group` ノードの Color 出力がリンクされたままであること。
4. **パネルのモック描画**：`UILayout` の代わりに、`prop` / `operator` / `label` / `menu` / `template_list` を記録するモッククラスを作り、全パネルの `draw()` をいろいろな状態で呼ぶ。次がないことを確認する：
   - 存在しないプロパティ
   - 存在しないオペレーター、オペレーターのプロパティ
   - 存在しないアイコン
   （v1.0 の開発で使った方式。モックの例は下。）
5. **焼き込み**：Cycles で小さく（256²）焼き、値の範囲・NaN がないことを確認する。タイル素材は、端と端の差 ≒ 隣接ピクセルの差であること。
6. **状態の復元**：処理の前後で、アクティブオブジェクト・選択・モード・レンダーエンジン・Cycles のサンプル数とデバイス・`compute_device_type` が同じであること。

```python
# パネルのモック描画（抜粋）
ICONS = {i.identifier for i in bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items}
class L:
    def __init__(self): self.scale_y = 1; self.enabled = True; self.alignment = 'EXPAND'; self.operator_context = 'EXEC_DEFAULT'
    def row(self, **k): return L()
    column = box = grid_flow = row
    def separator(self, **k): pass
    def label(self, text="", icon='NONE', **k): assert icon in ICONS, icon
    def prop(self, data, prop, icon='NONE', **k):
        assert icon in ICONS, icon; assert prop in data.bl_rna.properties, prop
    def operator(self, idname, icon='NONE', **k):
        assert icon in ICONS, icon
        cat, name = idname.split("."); getattr(getattr(bpy.ops, cat), name).get_rna_type()
        return type("P", (), {})()
    def menu(self, m, icon='NONE', **k): assert hasattr(bpy.types, m), m
    def template_list(self, *a, **k): pass
with bpy.context.temp_override(active_object=obj, object=obj, selected_objects=[obj]):
    for P in panels:
        if not hasattr(P, "poll") or P.poll(bpy.context):
            P.draw(types.SimpleNamespace(layout=L()), bpy.context)
```

### 5.3 見た目の確認
シェーダーの見た目は、最終的にユーザーの Blender（EEVEE）で確認する。PR の説明には、次の2つを書く。
- 何をどう確認したか
- ユーザーに試してほしい手順（日本語・短く）

---

## 6. 既知の注意点・制限
- 頂点（LIVE）のエッジ・くぼみは、ローポリでは面全体ににじむ → 「焼き込み」マスクを推奨（パネルにも表示している）。
- 画像テクスチャ以外のプロシージャル（Wave・Magic など）は、タイル素材モードで継ぎ目が出ることがある（ノイズ・ボロノイのみ自動変換）。
- 塗りは `CMX_UV` を使うため、UV 島の境目をまたぐストロークは途切れることがある。
- 1つの物に1つのミキサーマテリアル（全スロット共通）。部品ごとの塗り分けは 4.4-1 で対応予定。
- スマートマテリアルのマテリアル照合は「同じ名前なら同じもの」とみなす。

## 7. 将来の利用予定（設計上の配慮）
エッジ・くぼみ・上向きなどの自動マスク部品（`ensure_surface_mask_group`, `ensure_mask_modifier`, `bake_surface_masks`）は、今後の別アドオンでも使い回す予定です（経年変化ワンクリック、隅だまり、AOツール）。
- これらは、ミキサーの状態（`Object.cmx`）に依存しない関数のまま保つ。
- 引数・戻り値は互換性を保つ。
