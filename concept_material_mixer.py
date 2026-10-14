bl_info = {
    "name": "Concept Material Mixer",
    "author": "Claude",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Concept",
    "description": "Quixel Mixerのように、マテリアルをレイヤーで重ね、自動マスク＋塗りで塗り分ける",
    "category": "Material",
}

import bpy
import os
import re
import math
import uuid
import json
import numpy as np
from bpy.props import (StringProperty, FloatProperty, IntProperty, BoolProperty,
                       EnumProperty, FloatVectorProperty, PointerProperty, CollectionProperty)
from bpy.app.handlers import persistent

PAINT_UV = "CMX_UV"                  # 塗り・マスク焼き込み用のUV（自動で作る）
DISP_MOD = "CMX_Displace"            # 焼いたハイトで形を動かすモディファイア
HL_NAME = "CMX_HeightLerp"
HL_VER = 2
PN_NAME = "CMX_PlanarNormal"
PN_VER = 2
SM_GROUP = "CMX_SurfaceMasks"        # エッジ・くぼみ・AO を頂点に計算するジオメトリノード
SM_VER = 2
SM_ATTR = "CMX_Masks"                # R = エッジ, G = くぼみ, B = AO（1 = 遮蔽なし）
SM_MOD = "CMX_Masks"
AO_RAYS = 12
KEYS = ("Color", "Roughness", "Metallic", "Normal", "Height")
SOCK_TYPES = {"Color": "NodeSocketColor", "Roughness": "NodeSocketFloat", "Metallic": "NodeSocketFloat",
              "Normal": "NodeSocketVector", "Height": "NodeSocketFloat"}
DEBUG_COLORS = [(0.3, 0.3, 0.3), (0.9, 0.2, 0.15), (0.2, 0.75, 0.2), (0.2, 0.4, 0.95),
                (0.95, 0.8, 0.1), (0.8, 0.3, 0.9), (0.1, 0.85, 0.85), (1.0, 0.5, 0.1)]

ADJ_TYPES = [
    ('HSV', "明るさ・彩度・色相", "マスクの所の明るさ・彩度・色相を変える"),
    ('ROUGH', "ラフネス", "マスクの所のラフネス（ツヤ）を指定の値にする"),
    ('METAL', "メタル", "マスクの所のメタル（金属っぽさ）を指定の値にする"),
    ('WET', "濡れ", "マスクの所を濡らす（暗く・ツヤあり・平らに）"),
    ('TINT', "色をのせる", "マスクの所に色を重ねる"),
]
ADJ_LABELS = {a[0]: a[1] for a in ADJ_TYPES}
ADJ_ICONS = {'HSV': 'MOD_HUE_SATURATION', 'ROUGH': 'MATSHADERBALL', 'METAL': 'SHADING_RENDERED',
             'WET': 'MOD_OCEAN', 'TINT': 'MOD_TINT'}
ADJ_PARAMS = ("adj_hue", "adj_sat", "adj_val", "adj_rough", "adj_metal",
              "wet_dark", "wet_rough", "wet_flat", "tint_color")

MASK_TYPES = [
    ('EDGE', "エッジ（出っ張り）", "角や出っ張った所。塗装の剥げ・摩耗に"),
    ('CAVITY', "くぼみ（凹み）", "へこんだ所・すき間。汚れ・ほこりのたまりに"),
    ('AO', "AO（奥まった所）", "光が届きにくい奥まった所。汚れ・苔・湿りに"),
    ('UP', "上向きの面", "上を向いた面。雪・砂・ほこり・苔が積もる所に"),
    ('HEIGHT', "高さ", "物の下から上へのグラデーション（反転で下側）。泥はね・水位に"),
    ('SLOPE', "傾斜", "急な面ほど強い。崖・壁・流れ跡に"),
    ('NOISE', "ノイズ", "まだら模様"),
    ('LAYER_H', "下のレイヤーの高さ", "下に重なったマテリアルの凹凸の高い所（反転で低い所＝目地・すき間）。平らなタイル素材でも使える"),
]
MASK_LABELS = {a[0]: a[1] for a in MASK_TYPES}
MASK_ICONS = {'EDGE': 'MOD_BEVEL', 'CAVITY': 'SPHERECURVE', 'AO': 'SHADING_SOLID', 'UP': 'NORMALS_FACE',
              'HEIGHT': 'EMPTY_SINGLE_ARROW', 'SLOPE': 'IPO_LINEAR', 'NOISE': 'MOD_NOISE',
              'LAYER_H': 'MOD_DISPLACE'}
# (範囲, くっきり) の初期値
MASK_DEFAULTS = {'EDGE': (0.5, 0.6), 'CAVITY': (0.5, 0.6), 'AO': (0.35, 0.5), 'UP': (0.35, 0.6),
                 'HEIGHT': (0.3, 0.4), 'SLOPE': (0.4, 0.6), 'NOISE': (0.5, 0.4), 'LAYER_H': (0.4, 0.6)}
MASK_NEEDS_DATA = {'EDGE', 'CAVITY', 'AO'}
MASK_BLENDS = [
    ('MULTIPLY', "掛け算（重なる所だけ）", "前のマスクと両方ある所だけ残す"),
    ('ADD', "足し算（広げる）", "前のマスクに足して広げる"),
    ('MAX', "最大（どちらか）", "どちらか強い方"),
    ('SUBTRACT', "引き算（削る）", "前のマスクからこのマスクの所を削る"),
    ('MIN', "最小", "どちらか弱い方"),
]
PROJECTIONS = [
    ('TRIPLANAR', "トリプラナー", "3方向から投影。UVがなくても使え、継ぎ目が出にくい"),
    ('UV', "UV", "物のUVマップを使う"),
    ('TOP', "真上", "真上から投影（地面向け）"),
    ('ORIGINAL', "そのまま", "マテリアル本来の座標をそのまま使う（プロシージャルや元の見た目を保つとき）"),
]

_SUSPEND = [0]
_ENUM_KEEP = {}


# ------------------------------------------------------------------ small helpers
_ICONS = None


def _icon(name):
    """存在しないアイコン名はパネルを壊すので、必ずここを通す。"""
    global _ICONS
    if _ICONS is None:
        try:
            _ICONS = {i.identifier for i in
                      bpy.types.UILayout.bl_rna.functions["prop"].parameters["icon"].enum_items}
        except Exception:
            _ICONS = set()
    return name if name in _ICONS else 'NONE'


def _is_mixer_material(m):
    return m is not None and bool(m.get("cmx_mixer", False))


# ------------------------------------------------------------------ node copy helpers
_SKIP = {"rna_type", "name", "type", "inputs", "outputs", "internal_links", "parent", "select",
         "dimensions", "bl_idname", "bl_label", "bl_description", "bl_icon", "bl_static_type",
         "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default", "bl_height_min",
         "bl_height_max", "location_absolute", "is_active_output", "warning_propagation"}


def _sock_by_id(coll, ident):
    for s in coll:
        if s.identifier == ident:
            return s
    return None


def _src(sock):
    if sock is not None and sock.is_linked:
        lk = sock.links
        if lk:
            return lk[0].from_socket
    return None


def _copy_ramp(src, dst):
    for a in ("color_mode", "hue_interpolation", "interpolation"):
        try:
            setattr(dst, a, getattr(src, a))
        except Exception:
            pass
    els = dst.elements
    while len(els) > 1:
        els.remove(els[-1])
    for i, e in enumerate(src.elements):
        el = els[0] if i == 0 else els.new(e.position)
        el.position = e.position
        el.color = tuple(e.color)


def _copy_curves(src, dst):
    for a in ("black_level", "white_level", "clip_min_x", "clip_min_y", "clip_max_x",
              "clip_max_y", "use_clip", "extend", "tone"):
        try:
            setattr(dst, a, getattr(src, a))
        except Exception:
            pass
    for cs, cd in zip(src.curves, dst.curves):
        pts = cd.points
        while len(pts) > 2:
            pts.remove(pts[-1])
        for i, p in enumerate(cs.points):
            if i < 2:
                pts[i].location = tuple(p.location)
            else:
                pts.new(p.location[0], p.location[1])
        for p, q in zip(cs.points, pts):
            try:
                q.handle_type = p.handle_type
            except Exception:
                pass
    try:
        dst.update()
    except Exception:
        pass


def _copy_node(src, tree):
    dst = tree.nodes.new(src.bl_idname)
    props = [p for p in src.bl_rna.properties if p.identifier not in _SKIP and not p.is_readonly]
    props.sort(key=lambda p: 0 if p.type == 'ENUM' else 1)
    for p in props:
        try:
            setattr(dst, p.identifier, getattr(src, p.identifier))
        except Exception:
            pass
    if getattr(src, "color_ramp", None) is not None:
        _copy_ramp(src.color_ramp, dst.color_ramp)
    m = getattr(src, "mapping", None)
    if m is not None and hasattr(m, "curves"):
        _copy_curves(m, dst.mapping)
    for cs, cd in ((src.inputs, dst.inputs), (src.outputs, dst.outputs)):
        for s in cs:
            d = _sock_by_id(cd, s.identifier)
            if d is None:
                continue
            d.hide = s.hide
            if hasattr(s, "default_value") and hasattr(d, "default_value"):
                v = s.default_value
                try:
                    d.default_value = tuple(v) if hasattr(v, "__len__") and not isinstance(v, str) else v
                except Exception:
                    try:
                        d.default_value = v
                    except Exception:
                        pass
    return dst


def _copy_nodes(src_tree, dst_tree):
    m = {}
    for n in src_tree.nodes:
        if n.type in ('OUTPUT_MATERIAL', 'OUTPUT_AOV', 'OUTPUT_LIGHT', 'OUTPUT_WORLD'):
            continue
        m[n.name] = _copy_node(n, dst_tree)
    for n in src_tree.nodes:
        if n.name in m and n.parent is not None and n.parent.name in m:
            m[n.name].parent = m[n.parent.name]
    for l in src_tree.links:
        a = m.get(l.from_node.name)
        b = m.get(l.to_node.name)
        if a is None or b is None:
            continue
        fs = _sock_by_id(a.outputs, l.from_socket.identifier)
        ts = _sock_by_id(b.inputs, l.to_socket.identifier)
        if fs is not None and ts is not None:
            nl = dst_tree.links.new(fs, ts)
            nl.is_muted = l.is_muted
    return m


def _find_output(tree):
    outs = [n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL']
    for n in outs:
        if n.is_active_output:
            return n
    return outs[0] if outs else None


def _find_bsdf(tree):
    out = _find_output(tree)
    if out is not None:
        s = _src(out.inputs[0])
        if s is not None and s.node.type == 'BSDF_PRINCIPLED':
            return s.node
    return next((n for n in tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)


class _NB:
    """Small helper for building node trees from code."""

    def __init__(self, tree):
        self.N = tree.nodes
        self.L = tree.links
        self.x = 0
        self.y = 0

    def _place(self, n):
        n.location = (self.x, self.y)
        self.y -= 45

    def link(self, v, sock):
        if isinstance(v, bpy.types.NodeSocket):
            self.L.new(v, sock)
        elif v is not None:
            sock.default_value = v

    def math(self, op, a, b=None, clamp=False, name=None, c=None):
        n = self.N.new("ShaderNodeMath")
        n.operation = op
        n.use_clamp = clamp
        n.hide = True
        self._place(n)
        if name:
            n.name = n.label = name
        self.link(a, n.inputs[0])
        if b is not None:
            self.link(b, n.inputs[1])
        if c is not None:
            self.link(c, n.inputs[2])
        return n.outputs[0]

    def vmath(self, op, a, b=None, scale=None):
        n = self.N.new("ShaderNodeVectorMath")
        n.operation = op
        n.hide = True
        self._place(n)
        self.link(a, n.inputs[0])
        if b is not None:
            self.link(b, n.inputs[1])
        if scale is not None:
            self.link(scale, n.inputs["Scale"])
        return n.outputs["Value"] if op in ('DOT_PRODUCT', 'LENGTH', 'DISTANCE') else n.outputs[0]

    def mix(self, dt, fac, a, b, blend='MIX'):
        n = self.N.new("ShaderNodeMix")
        n.data_type = dt
        if dt == 'RGBA':
            n.blend_type = blend
        n.hide = True
        self._place(n)
        ia, ib, o = {'RGBA': (6, 7, 2), 'FLOAT': (2, 3, 0), 'VECTOR': (4, 5, 1)}[dt]
        self.link(fac, n.inputs[0])
        self.link(a, n.inputs[ia])
        self.link(b, n.inputs[ib])
        return n.outputs[o]

    def value(self, name, v):
        n = self.N.new("ShaderNodeValue")
        n.name = n.label = name
        n.outputs[0].default_value = v
        self._place(n)
        return n.outputs[0]

    def node(self, idname, name=None):
        n = self.N.new(idname)
        if name:
            n.name = n.label = name
        self._place(n)
        return n

    def sep_xyz(self, v):
        n = self.node("ShaderNodeSeparateXYZ")
        n.hide = True
        self.link(v, n.inputs[0])
        return n.outputs

    def comb_xyz(self, x, y, z):
        n = self.node("ShaderNodeCombineXYZ")
        n.hide = True
        for i, v in enumerate((x, y, z)):
            self.link(v, n.inputs[i])
        return n.outputs[0]

    def smooth(self, v, lo, hi):
        mr = self.node("ShaderNodeMapRange")
        mr.interpolation_type = 'SMOOTHSTEP'
        mr.clamp = True
        mr.hide = True
        self.link(v, mr.inputs[0])
        self.link(lo, mr.inputs[1])
        self.link(hi, mr.inputs[2])
        mr.inputs[3].default_value = 0.0
        mr.inputs[4].default_value = 1.0
        return mr.outputs[0]


# ------------------------------------------------------------------ shader node groups
def _ensure_interface(g, spec, defaults=None):
    """グループの入出力を spec [(名前, 'INPUT'/'OUTPUT', 型)] にそろえる。
    すでに同じなら何もしない。今の入出力が spec の「先頭部分」なら、足りない分を後ろに足すだけ
    （既存のつながりは切れない）。それ以外のときだけ作り直す。作り直すと、そのグループを使う
    「ほかの」マテリアルのつながりが切れてしまうため、入出力を増やすときは spec の最後に足すこと。
    defaults = {名前: 初期値}（新しく作った入力に設定する）"""
    cur = [(it.name, it.in_out, it.socket_type) for it in g.interface.items_tree if it.item_type == 'SOCKET']
    want = [tuple(x) for x in spec]
    # 並び順は入力・出力それぞれの中だけ比べる（Blender は出力を先に並べるため）
    same = True
    prefix = True
    for io in ('INPUT', 'OUTPUT'):
        c = [x for x in cur if x[1] == io]
        w = [x for x in want if x[1] == io]
        if c != w:
            same = False
        if c != w[:len(c)]:
            prefix = False
    if same:
        return False
    if not prefix:
        g.interface.clear()
        cur = []
    have = {(x[0], x[1]) for x in cur}
    for name, io, st in want:
        if (name, io) in have:
            continue
        it = g.interface.new_socket(name=name, in_out=io, socket_type=st)
        if defaults and name in defaults and io == 'INPUT':
            try:
                it.default_value = defaults[name]
            except Exception:
                pass
    return True


def _iface_socket(g, name, io='INPUT'):
    for it in g.interface.items_tree:
        if it.item_type == 'SOCKET' and it.name == name and it.in_out == io:
            return it
    return None


def ensure_heightlerp():
    """2つのマテリアルを重みと凹凸（ハイト）で混ぜる。やわらかいほど凹凸の影響が弱まる。"""
    g = bpy.data.node_groups.get(HL_NAME)
    if g is not None and g.get("cmx_ver") == HL_VER:
        return g
    if g is None:
        g = bpy.data.node_groups.new(HL_NAME, "ShaderNodeTree")
    g.nodes.clear()
    g["cmx_ver"] = HL_VER
    spec = [(p + k, 'INPUT', SOCK_TYPES[k]) for p in ("A ", "B ") for k in KEYS]
    spec += [(nm, 'INPUT', "NodeSocketFloat") for nm in ("Weight", "Softness", "Height Strength")]
    spec += [(k, 'OUTPUT', SOCK_TYPES[k]) for k in KEYS] + [("Factor", 'OUTPUT', "NodeSocketFloat")]
    _ensure_interface(g, spec)
    N, L = g.nodes, g.links
    gi = N.new("NodeGroupInput"); gi.location = (-1400, 0)
    go = N.new("NodeGroupOutput"); go.location = (600, 0)
    I, O = gi.outputs, go.inputs

    def math(op, a, b=None, clamp=False):
        n = N.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        for idx, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, bpy.types.NodeSocket):
                L.new(v, n.inputs[idx])
            else:
                n.inputs[idx].default_value = v
        return n.outputs[0]

    w = I["Weight"]
    soft = math('MAXIMUM', math('MINIMUM', I["Softness"], 1.0), 0.001)
    hs = math('MULTIPLY', math('MINIMUM', I["Height Strength"], 0.999), math('SUBTRACT', 1.0, soft))
    a = math('ADD', math('MULTIPLY', I["A Height"], hs), math('SUBTRACT', 1.0, w))
    b = math('ADD', math('MULTIPLY', I["B Height"], hs), w)
    mx = math('SUBTRACT', math('MAXIMUM', a, b), soft)
    wa = math('MAXIMUM', math('SUBTRACT', a, mx), 0.0)
    wb = math('MAXIMUM', math('SUBTRACT', b, mx), 0.0)
    f = math('DIVIDE', wb, math('ADD', wa, wb), clamp=True)
    # 金属と非金属の境目は、半端に混ぜると白っぽい縁が出るので、金属度の差に応じて色と金属度だけ切り替えを急にする
    mr = N.new("ShaderNodeMapRange"); mr.interpolation_type = 'SMOOTHSTEP'; mr.clamp = True
    L.new(f, mr.inputs[0]); mr.inputs[1].default_value = 0.4; mr.inputs[2].default_value = 0.6
    dm = math('ABSOLUTE', math('SUBTRACT', I["A Metallic"], I["B Metallic"]))
    fm = N.new("ShaderNodeMix"); fm.data_type = 'FLOAT'
    L.new(dm, fm.inputs[0]); L.new(f, fm.inputs[2]); L.new(mr.outputs[0], fm.inputs[3])
    fc = fm.outputs[0]

    def mix(dt, A, B, fac=None):
        n = N.new("ShaderNodeMix"); n.data_type = dt
        ia, ib, o = {'RGBA': (6, 7, 2), 'FLOAT': (2, 3, 0), 'VECTOR': (4, 5, 1)}[dt]
        L.new(f if fac is None else fac, n.inputs[0]); L.new(A, n.inputs[ia]); L.new(B, n.inputs[ib])
        return n.outputs[o]

    L.new(mix('RGBA', I["A Color"], I["B Color"], fc), O["Color"])
    L.new(mix('FLOAT', I["A Roughness"], I["B Roughness"]), O["Roughness"])
    L.new(mix('FLOAT', I["A Metallic"], I["B Metallic"], fc), O["Metallic"])
    nrm = N.new("ShaderNodeVectorMath"); nrm.operation = 'NORMALIZE'
    L.new(mix('VECTOR', I["A Normal"], I["B Normal"]), nrm.inputs[0])
    L.new(nrm.outputs[0], O["Normal"])
    L.new(mix('FLOAT', I["A Height"], I["B Height"]), O["Height"])
    L.new(f, O["Factor"])
    return g


def _xform_tangent_vec(nb, v, ang, flip, fy):
    """接空間ノーマル（-1〜1 のベクトル）の XY を、R(ang)·diag(flip, fy) で回す・反転する（Z はそのまま）。
    配置（回転・左右反転・DirectX 形式の上下反転）でテクスチャの向きを変えたとき、凹凸の向きを合わせるために使う。
    ang = 回転角（ラジアン）、flip = 左右反転（±1）、fy = 上下反転（±1）。
    ベクトルのノードだけで組む（スカラーのノードを並べると Cycles のスタックを食うため）。"""
    m = nb.vmath('MULTIPLY', v, nb.comb_xyz(flip, fy, 1.0))
    rot = nb.node("ShaderNodeVectorRotate")
    rot.hide = True
    rot.rotation_type = 'Z_AXIS'
    nb.link(m, rot.inputs["Vector"])
    nb.link(ang, rot.inputs["Angle"])
    return rot.outputs["Vector"]


def ensure_planar_normal():
    """投影座標（トリプラナー・真上）用のノーマルマップ。
    Plane 0 = X面 (u=Y, v=Z), 1 = Y面 (u=X, v=Z), 2 = Z面 (u=X, v=Y)。
    接線 = 投影のu/v方向なので、裏側の面（鏡像）でも正しい向きになる。
    NAngle / NFlip / NFlipY = 接空間ノーマルの XY の回転角・左右反転・上下反転（配置用。初期値は何もしない）。"""
    g = bpy.data.node_groups.get(PN_NAME)
    if g is not None and g.get("cmx_ver") == PN_VER:
        return g
    if g is None:
        g = bpy.data.node_groups.new(PN_NAME, "ShaderNodeTree")
    g.nodes.clear()
    g["cmx_ver"] = PN_VER
    _ensure_interface(g, [("Strength", 'INPUT', "NodeSocketFloat"), ("Color", 'INPUT', "NodeSocketColor"),
                          ("Plane", 'INPUT', "NodeSocketFloat"), ("Normal", 'OUTPUT', "NodeSocketVector"),
                          ("NAngle", 'INPUT', "NodeSocketFloat"), ("NFlip", 'INPUT', "NodeSocketFloat"),
                          ("NFlipY", 'INPUT', "NodeSocketFloat")],
                      defaults={"NAngle": 0.0, "NFlip": 1.0, "NFlipY": 1.0})
    _iface_socket(g, "Strength").default_value = 1.0
    _iface_socket(g, "Color").default_value = (0.5, 0.5, 1.0, 1.0)
    nb = _NB(g)
    gi = g.nodes.new("NodeGroupInput"); gi.location = (-900, 0)
    go = g.nodes.new("NodeGroupOutput"); go.location = (600, 0)
    nb.x, nb.y = -600, 200
    t = nb.vmath('MULTIPLY_ADD', gi.outputs["Color"], (2.0, 2.0, 2.0))
    t.node.inputs[2].default_value = (-1.0, -1.0, -1.0)
    sx = nb.sep_xyz(t)
    tx = nb.math('MULTIPLY', sx[0], gi.outputs["Strength"])
    ty = nb.math('MULTIPLY', sx[1], gi.outputs["Strength"])
    tv = _xform_tangent_vec(nb, nb.comb_xyz(tx, ty, 0.0), gi.outputs["NAngle"], gi.outputs["NFlip"], gi.outputs["NFlipY"])
    tx, ty, _tz = nb.sep_xyz(tv)
    is1 = nb.math('GREATER_THAN', gi.outputs["Plane"], 0.5)
    is2 = nb.math('GREATER_THAN', gi.outputs["Plane"], 1.5)
    ox = nb.math('MULTIPLY', tx, is1)
    oy = nb.math('ADD', nb.math('MULTIPLY', tx, nb.math('SUBTRACT', 1.0, is1)), nb.math('MULTIPLY', ty, is2))
    oz = nb.math('MULTIPLY', ty, nb.math('SUBTRACT', 1.0, is2))
    off = nb.comb_xyz(ox, oy, oz)
    vt = nb.node("ShaderNodeVectorTransform")
    vt.vector_type = 'VECTOR'
    vt.convert_from = 'OBJECT'
    vt.convert_to = 'WORLD'
    nb.link(off, vt.inputs[0])
    geo = nb.node("ShaderNodeNewGeometry")
    n = nb.vmath('NORMALIZE', nb.vmath('ADD', geo.outputs["Normal"], vt.outputs[0]))
    nb.link(n, go.inputs["Normal"])
    return g


def _replace_normal_maps(N, L, plane_sock, xf):
    """接空間ノーマルマップを、投影座標で正しく働く CMX_PlanarNormal に置き換える。"""
    pn = ensure_planar_normal()
    repl = {}
    for n in list(N):
        if n.type != 'NORMAL_MAP' or n.space != 'TANGENT':
            continue
        g = N.new("ShaderNodeGroup")
        g.node_tree = pn
        g.location = n.location
        g.parent = n.parent
        for nm in ("Color", "Strength"):
            si = n.inputs[nm]
            fs = _src(si)
            if fs is not None:
                L.new(fs, g.inputs[nm])
            else:
                v = si.default_value
                g.inputs[nm].default_value = tuple(v) if nm == "Color" else v
        L.new(plane_sock, g.inputs["Plane"])
        for nm, sk in zip(("NAngle", "NFlip", "NFlipY"), xf):
            L.new(sk, g.inputs[nm])
        for lk in list(n.outputs["Normal"].links):
            L.new(g.outputs["Normal"], lk.to_socket)
        repl[n.name] = g
        N.remove(n)
    return repl


def _xform_normal_maps(g, xf):
    """UV・タイル素材モード用：ノーマルマップの色（接空間）の XY に行列を掛けてから入れる。"""
    N, L = g.nodes, g.links
    nb = _NB(g)
    for n in list(N):
        if n.type != 'NORMAL_MAP' or n.space != 'TANGENT':
            continue
        nb.x, nb.y = n.location.x - 700, n.location.y
        si = n.inputs["Color"]
        fs = _src(si)
        if fs is None:
            rgb = N.new("ShaderNodeRGB")
            rgb.outputs[0].default_value = tuple(si.default_value)
            fs = rgb.outputs[0]
        t = nb.vmath('MULTIPLY_ADD', fs, (2.0, 2.0, 2.0))
        t.node.inputs[2].default_value = (-1.0, -1.0, -1.0)
        enc = nb.vmath('MULTIPLY_ADD', _xform_tangent_vec(nb, t, *xf), (0.5, 0.5, 0.5))
        enc.node.inputs[2].default_value = (0.5, 0.5, 0.5)
        L.new(enc, si)


def _has_images(mat):
    return bool(mat and mat.use_nodes and mat.node_tree and
                any(n.type == 'TEX_IMAGE' for n in mat.node_tree.nodes))


def _make_procedurals_periodic(N, L, gi):
    """タイル素材用：ノイズ・ボロノイを4Dにし、ドーナツ座標（TorusXYZ, TorusW）を入れて継ぎ目なしにする。
    座標が途中で Mapping を通るときは、その拡大率（平均）だけ引き継ぐ。"""
    for n in list(N):
        if n.type not in ('TEX_NOISE', 'TEX_VORONOI'):
            continue
        vi = n.inputs.get("Vector")
        scale = 1.0
        fs = _src(vi) if vi is not None else None
        if fs is not None and fs.node != gi:
            if fs.node.type == 'MAPPING' and _src(fs.node.inputs.get("Vector")) is not None and \
                    _src(fs.node.inputs["Vector"]).node == gi:
                si = fs.node.inputs.get("Scale")
                if si is not None and not si.is_linked:
                    scale = sum(si.default_value) / 3.0
            else:
                continue      # ほかの座標につながっているものはそのまま
        if n.type == 'TEX_NOISE':
            n.noise_dimensions = '4D'
        else:
            n.voronoi_dimensions = '4D'
        xyz, w = gi.outputs["TorusXYZ"], gi.outputs["TorusW"]
        if abs(scale - 1.0) > 1e-6:
            sv = N.new("ShaderNodeVectorMath"); sv.operation = 'SCALE'; sv.inputs["Scale"].default_value = scale
            sv.location = (n.location.x - 200, n.location.y)
            L.new(xyz, sv.inputs[0]); xyz = sv.outputs[0]
            sw = N.new("ShaderNodeMath"); sw.operation = 'MULTIPLY'; sw.inputs[1].default_value = scale
            sw.location = (n.location.x - 200, n.location.y - 60)
            L.new(w, sw.inputs[0]); w = sw.outputs[0]
        L.new(xyz, n.inputs["Vector"])
        L.new(w, n.inputs["W"])


def build_layer_group(mat, mode, uv_name="", bump=False):
    """マテリアルをノードグループに写す。
    mode: 'O' そのまま / 'U' UV（uv_name のUVで、ベクトル入力に差し替え） / 'P' 投影（トリプラナー・真上）
          'T' タイル素材（UV＋ノイズ・ボロノイを4Dドーナツ座標にして継ぎ目なし）
    bump: True なら、素材のハイトから Bump ノードで凹凸（法線）を作って足す（ノーマルマップのない素材用）
    入力 NAngle / NFlip / NFlipY = ノーマルマップの XY の回転角・左右反転・上下反転（配置用。初期値は何もしない）
    入力 BumpDist = bump のときの凹凸の高さ (m)"""
    key = mode if mode != 'U' else "U:" + uv_name
    if bump:
        key += "+B"
    gname = "CMX_Layer::%s::%s" % (mat.name, key)
    g = bpy.data.node_groups.get(gname)
    if g is None:
        g = bpy.data.node_groups.new(gname, "ShaderNodeTree")
    g.nodes.clear()
    # 入出力を増やすときは、必ず最後に足す（_ensure_interface は足りない分だけ足すので、既存のつながりが切れない）
    _ensure_interface(g, [("Vector", 'INPUT', 'NodeSocketVector'), ("Plane", 'INPUT', 'NodeSocketFloat'),
                          ("TorusXYZ", 'INPUT', 'NodeSocketVector'), ("TorusW", 'INPUT', 'NodeSocketFloat')] +
                      [(k, 'OUTPUT', SOCK_TYPES[k]) for k in KEYS] +
                      [("NAngle", 'INPUT', 'NodeSocketFloat'), ("NFlip", 'INPUT', 'NodeSocketFloat'),
                       ("NFlipY", 'INPUT', 'NodeSocketFloat'), ("BumpDist", 'INPUT', 'NodeSocketFloat')],
                      defaults={"NAngle": 0.0, "NFlip": 1.0, "NFlipY": 1.0, "BumpDist": 0.02})
    N, L = g.nodes, g.links
    gi = N.new("NodeGroupInput"); gi.location = (-2400, 0)
    go = N.new("NodeGroupOutput"); go.location = (900, 0)
    vals = {"Color": tuple(mat.diffuse_color), "Roughness": mat.roughness, "Metallic": mat.metallic,
            "Normal": None, "Height": None}
    src_tree = mat.node_tree if (mat.use_nodes and mat.node_tree) else None
    if src_tree is not None:
        m = _copy_nodes(src_tree, g)
        names = {k: v.name for k, v in m.items()}
        bsdf = _find_bsdf(src_tree)
        out = _find_output(src_tree)
        bsdf_dst = names.get(bsdf.name) if bsdf is not None else None
        # 座標の差し替え
        if mode in ('U', 'P', 'T'):
            vin = gi.outputs["Vector"]
            for n in list(N):
                if n.type == 'TEX_COORD':
                    for o in n.outputs:
                        if o.name in ("UV", "Generated", "Object"):
                            for lk in list(o.links):
                                L.new(vin, lk.to_socket)
                elif n.type == 'UVMAP':
                    for o in n.outputs:
                        for lk in list(o.links):
                            L.new(vin, lk.to_socket)
                elif n.type.startswith('TEX_') and n.type not in ('TEX_COORD', 'TEX_ENVIRONMENT', 'TEX_SKY',
                                                                    'TEX_IES', 'TEX_POINTDENSITY'):
                    vi = n.inputs.get("Vector")
                    if vi is not None and not vi.is_linked:
                        L.new(vin, vi)
        repl = {}
        if mode in ('U', 'T'):
            for n in N:
                if n.type == 'NORMAL_MAP':
                    n.uv_map = uv_name
            _xform_normal_maps(g, (gi.outputs["NAngle"], gi.outputs["NFlip"], gi.outputs["NFlipY"]))
        elif mode == 'P':
            repl = _replace_normal_maps(N, L, gi.outputs["Plane"],
                                        (gi.outputs["NAngle"], gi.outputs["NFlip"], gi.outputs["NFlipY"]))
        if mode == 'T':
            _make_procedurals_periodic(N, L, gi)

        def resolve(fs):
            dn = names.get(fs.node.name)
            if dn is None:
                return None
            if dn in repl:
                return repl[dn].outputs[0]
            node = N.get(dn)
            return None if node is None else _sock_by_id(node.outputs, fs.identifier)

        if bsdf is not None:
            for key_, sn in (("Color", "Base Color"), ("Roughness", "Roughness"),
                             ("Metallic", "Metallic"), ("Normal", "Normal")):
                si = bsdf.inputs.get(sn)
                if si is None:
                    continue
                fs = _src(si)
                if fs is not None:
                    r = resolve(fs)
                    if r is not None:
                        vals[key_] = r
                elif key_ == "Color":
                    vals[key_] = tuple(si.default_value)
                elif key_ != "Normal":
                    vals[key_] = si.default_value
            # 高さの元：ディスプレイスメント → なければバンプの高さ
            hs = None
            if out is not None:
                ds = _src(_sock_by_id(out.inputs, "Displacement"))
                if ds is not None and ds.node.type == 'DISPLACEMENT':
                    hs = _src(ds.node.inputs.get("Height"))
            if hs is None:
                ns = _src(bsdf.inputs.get("Normal"))
                hops = 0
                while ns is not None and hops < 4 and hs is None:
                    if ns.node.type == 'BUMP':
                        hs = _src(ns.node.inputs.get("Height"))
                        break
                    ni = ns.node.inputs.get("Normal")
                    ns = _src(ni) if ni is not None else None
                    hops += 1
            if hs is not None:
                r = resolve(hs)
                if r is not None:
                    vals["Height"] = r
            if bsdf_dst is not None and N.get(bsdf_dst) is not None:
                N.remove(N[bsdf_dst])
    v = vals["Color"]
    if not isinstance(v, bpy.types.NodeSocket):
        rgb = N.new("ShaderNodeRGB"); rgb.location = (500, 300)
        rgb.outputs[0].default_value = v
        v = rgb.outputs[0]
    color_sock = v
    L.new(v, go.inputs["Color"])
    for k in ("Roughness", "Metallic"):
        v = vals[k]
        if not isinstance(v, bpy.types.NodeSocket):
            vn = N.new("ShaderNodeValue"); vn.outputs[0].default_value = v
            v = vn.outputs[0]
        L.new(v, go.inputs[k])
    hv = vals["Height"]
    if hv is None:
        bw = N.new("ShaderNodeRGBToBW"); L.new(color_sock, bw.inputs[0])
        pw = N.new("ShaderNodeMath"); pw.operation = 'POWER'; pw.inputs[1].default_value = 0.4545
        L.new(bw.outputs[0], pw.inputs[0])
        hv = pw.outputs[0]
    L.new(hv, go.inputs["Height"])
    v = vals["Normal"]
    if v is None:
        v = N.new("ShaderNodeNewGeometry").outputs["Normal"]
    if bump:
        bn = N.new("ShaderNodeBump"); bn.location = (700, -300)
        bn.inputs["Strength"].default_value = 1.0
        L.new(gi.outputs["BumpDist"], bn.inputs["Distance"])
        L.new(hv, bn.inputs["Height"])
        L.new(v, bn.inputs["Normal"])
        v = bn.outputs["Normal"]
    L.new(v, go.inputs["Normal"])
    g["cmx_src"] = mat.name
    return g


# ------------------------------------------------------------------ textures -> material
TEX_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".tga", ".bmp", ".webp", ".hdr")
_CH_TOKENS = {
    'ARM': {"arm", "orm"},
    'NORMAL': {"normal", "normals", "nor", "nrm", "norm", "nrml", "normalgl", "normaldx"},
    'HEIGHT': {"height", "displacement", "disp", "displace", "bump", "hgt"},
    'ROUGH': {"roughness", "rough", "rgh"},
    'GLOSS': {"gloss", "glossiness"},
    'METAL': {"metallic", "metalness", "metal", "mtl"},
    'AO': {"ao", "ambientocclusion", "ambient", "occlusion", "occ"},
    'SKIP': {"opacity", "alpha", "mask", "spec", "specular", "emission", "emissive", "translucency",
             "translucent", "sss", "cavity", "preview", "thumb", "thumbnail", "id", "fuzz"},
    'BASE': {"basecolor", "albedo", "diffuse", "diff", "color", "colour", "col", "basecol", "base"},
}
_CH_ORDER = ('ARM', 'NORMAL', 'HEIGHT', 'ROUGH', 'GLOSS', 'METAL', 'AO', 'SKIP', 'BASE')
_ALL_CH = set().union(*_CH_TOKENS.values())
_JUNK = {"gl", "dx", "directx", "opengl", "jpg", "jpeg", "png", "exr", "tif", "tiff", "tga", "map", "tex", "texture"}


def _classify_texture(path):
    """(channel, is_directx_normal, set_name). The channel word is the last one in the file name,
    e.g. rock_wall_nor_gl_2k -> NORMAL / rock_wall, Metal_Plate_Color -> BASE / Metal_Plate."""
    stem = os.path.splitext(os.path.basename(path))[0]
    toks = [t for t in re.split(r"[\s_\-\.]+", stem) if t]
    low = [t.lower() for t in toks]
    k = None
    for j in range(len(low) - 1, -1, -1):
        if low[j] in _ALL_CH:
            k = j
            break
    ch = None
    drop = set()
    if k is not None:
        ch = next(c for c in _CH_ORDER if low[k] in _CH_TOKENS[c])
        drop.add(k)
        j = k - 1
        while j >= 0 and low[j] in _CH_TOKENS[ch]:
            drop.add(j)
            j -= 1
    dx = ch == 'NORMAL' and any(t in ("dx", "directx", "normaldx") for t in low)
    keep = [t for j, (t, tl) in enumerate(zip(toks, low))
            if j not in drop and tl not in _JUNK and not re.fullmatch(r"\d+k", tl)]
    return ch, dx, ("_".join(keep) or "Texture")


def group_texture_files(paths):
    groups = {}
    for p in paths:
        if os.path.splitext(p)[1].lower() not in TEX_EXTS or not os.path.isfile(p):
            continue
        ch, dx, name = _classify_texture(p)
        if ch == 'SKIP':
            continue
        if ch is None:
            ch = 'BASE'
        g = groups.setdefault(name, {})
        if ch == 'NORMAL':
            if 'NORMAL' in g and not g.get('NORMAL_DX') and dx:
                continue
            g['NORMAL'] = p
            g['NORMAL_DX'] = dx
        elif ch not in g:
            g[ch] = p
    return groups


def build_material_from_textures(name, g):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    N, L = nt.nodes, nt.links
    bsdf = next(n for n in N if n.type == 'BSDF_PRINCIPLED')
    out = next(n for n in N if n.type == 'OUTPUT_MATERIAL')
    bsdf.location = (300, 0)
    out.location = (650, 0)
    tc = N.new("ShaderNodeTexCoord"); tc.location = (-1100, 0)
    y = [400]

    def tex(path, data):
        img = bpy.data.images.load(path, check_existing=True)
        if data:
            try:
                img.colorspace_settings.name = "Non-Color"
            except TypeError:
                pass
        n = N.new("ShaderNodeTexImage")
        n.image = img
        n.location = (-800, y[0])
        y[0] -= 300
        L.new(tc.outputs["UV"], n.inputs["Vector"])
        return n

    ao = None
    if 'ARM' in g:
        t = tex(g['ARM'], True)
        sep = N.new("ShaderNodeSeparateColor"); sep.location = (-500, y[0] + 300)
        L.new(t.outputs["Color"], sep.inputs[0])
        ao = sep.outputs[0]
        if 'ROUGH' not in g and 'GLOSS' not in g:
            L.new(sep.outputs[1], bsdf.inputs["Roughness"])
        if 'METAL' not in g:
            L.new(sep.outputs[2], bsdf.inputs["Metallic"])
    if 'AO' in g:
        ao = tex(g['AO'], True).outputs["Color"]
    if 'BASE' in g:
        col = tex(g['BASE'], False).outputs["Color"]
        if ao is not None:
            mx = N.new("ShaderNodeMix"); mx.data_type = 'RGBA'; mx.blend_type = 'MULTIPLY'
            mx.location = (-200, 300)
            mx.inputs[0].default_value = 0.5
            L.new(col, mx.inputs[6]); L.new(ao, mx.inputs[7])
            col = mx.outputs[2]
        L.new(col, bsdf.inputs["Base Color"])
    if 'ROUGH' in g:
        L.new(tex(g['ROUGH'], True).outputs["Color"], bsdf.inputs["Roughness"])
    elif 'GLOSS' in g:
        inv = N.new("ShaderNodeInvert"); inv.location = (-300, y[0] + 300)
        L.new(tex(g['GLOSS'], True).outputs["Color"], inv.inputs["Color"])
        L.new(inv.outputs["Color"], bsdf.inputs["Roughness"])
    if 'METAL' in g:
        L.new(tex(g['METAL'], True).outputs["Color"], bsdf.inputs["Metallic"])
    if 'NORMAL' in g:
        t = tex(g['NORMAL'], True)
        src = t.outputs["Color"]
        if g.get('NORMAL_DX'):
            sep = N.new("ShaderNodeSeparateColor")
            cmb = N.new("ShaderNodeCombineColor")
            inv = N.new("ShaderNodeMath"); inv.operation = 'SUBTRACT'; inv.inputs[0].default_value = 1.0
            L.new(src, sep.inputs[0])
            L.new(sep.outputs[0], cmb.inputs[0])
            L.new(sep.outputs[1], inv.inputs[1])
            L.new(inv.outputs[0], cmb.inputs[1])
            L.new(sep.outputs[2], cmb.inputs[2])
            src = cmb.outputs[0]
        nm = N.new("ShaderNodeNormalMap"); nm.location = (0, -400)
        L.new(src, nm.inputs["Color"])
        L.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if 'HEIGHT' in g:
        t = tex(g['HEIGHT'], True)
        dp = N.new("ShaderNodeDisplacement"); dp.location = (300, -500)
        dp.inputs["Scale"].default_value = 0.05
        L.new(t.outputs["Color"], dp.inputs["Height"])
        L.new(dp.outputs[0], out.inputs["Displacement"])
    return mat


# ------------------------------------------------------------------ surface masks（他のツールでも使い回せる部品）
#   ensure_surface_mask_group()   … エッジ・くぼみ・AO を頂点に計算するジオメトリノード
#   ensure_mask_modifier(obj, …)  … そのモディファイアを物の一番最後に付ける（属性 "CMX_Masks"）
#   bake_surface_masks(…)         … Cycles で同じ3つを画像に焼く（ローポリでもきれい）
#   シェーダーでは Attribute ノード（CMX_Masks）か焼いた画像の R=エッジ, G=くぼみ, B=AO を読む。
def ensure_surface_mask_group():
    g = bpy.data.node_groups.get(SM_GROUP)
    if g is not None and g.get("cmx_ver") == SM_VER:
        return g
    if g is None:
        g = bpy.data.node_groups.new(SM_GROUP, "GeometryNodeTree")
    g.nodes.clear()
    g["cmx_ver"] = SM_VER
    if _ensure_interface(g, [("Geometry", 'INPUT', 'NodeSocketGeometry'), ("Edge Radius", 'INPUT', 'NodeSocketFloat'),
                             ("Spread", 'INPUT', 'NodeSocketInt'), ("AO Distance", 'INPUT', 'NodeSocketFloat'),
                             ("Geometry", 'OUTPUT', 'NodeSocketGeometry')]):
        sk = _iface_socket(g, "Edge Radius"); sk.default_value = 0.03; sk.min_value = 0.0
        sk = _iface_socket(g, "Spread"); sk.default_value = 2; sk.min_value = 0; sk.max_value = 100
        sk = _iface_socket(g, "AO Distance"); sk.default_value = 0.3; sk.min_value = 0.0
    N, L = g.nodes, g.links
    gi = N.new("NodeGroupInput"); gi.location = (-1600, 0)
    go = N.new("NodeGroupOutput"); go.location = (1400, 0)
    pos = N.new("GeometryNodeInputPosition"); pos.location = (-1400, -300)
    nrm = N.new("GeometryNodeInputNormal"); nrm.location = (-1400, -400)
    G = gi.outputs["Geometry"]

    def math(op, a, b=None, clamp=False):
        n = N.new("ShaderNodeMath"); n.operation = op; n.use_clamp = clamp
        for idx, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, bpy.types.NodeSocket):
                L.new(v, n.inputs[idx])
            else:
                n.inputs[idx].default_value = v
        return n.outputs[0]

    def vmath(op, a, b=None):
        n = N.new("ShaderNodeVectorMath"); n.operation = op
        for idx, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, bpy.types.NodeSocket):
                L.new(v, n.inputs[idx])
            else:
                n.inputs[idx].default_value = v
        return n.outputs["Value"] if op in ('DOT_PRODUCT', 'LENGTH') else n.outputs[0]

    # --- 曲率：辺ごとに κ = (N1-N2)・(P1-P2) / |P1-P2|²（凸で＋, 凹で−、単位は 1/m）
    ev = N.new("GeometryNodeInputMeshEdgeVertices"); ev.location = (-1400, 200)
    smp = []
    for k in (0, 1):
        si = N.new("GeometryNodeSampleIndex"); si.data_type = 'FLOAT_VECTOR'; si.domain = 'POINT'
        si.location = (-1150, 300 - k * 200)
        L.new(G, si.inputs["Geometry"])
        L.new(nrm.outputs["Normal"], si.inputs["Value"])
        L.new(ev.outputs[k], si.inputs["Index"])
        smp.append(si.outputs[0])
    dn = vmath('SUBTRACT', smp[0], smp[1])
    dp = vmath('SUBTRACT', ev.outputs["Position 1"], ev.outputs["Position 2"])
    kap = math('DIVIDE', vmath('DOT_PRODUCT', dn, dp), math('MAXIMUM', vmath('DOT_PRODUCT', dp, dp), 1e-12))
    fod = N.new("GeometryNodeFieldOnDomain"); fod.data_type = 'FLOAT'; fod.domain = 'EDGE'
    fod.location = (-600, 200)
    L.new(kap, fod.inputs[0])
    x = math('MULTIPLY', fod.outputs[0], gi.outputs["Edge Radius"])
    curv = []
    for sign in (1.0, -1.0):
        c = math('MULTIPLY', x, sign, clamp=True)
        bl = N.new("GeometryNodeBlurAttribute"); bl.data_type = 'FLOAT'
        bl.location = (-200, 300 if sign > 0 else 100)
        L.new(c, bl.inputs["Value"])
        L.new(gi.outputs["Spread"], bl.inputs["Iterations"])
        bl.inputs["Weight"].default_value = 1.0
        curv.append(bl.outputs[0])
    # --- AO：法線まわりのコサイン分布で AO_RAYS 本のレイを飛ばす
    dist = gi.outputs["AO Distance"]
    eps = math('MULTIPLY', dist, 0.002)
    off = N.new("ShaderNodeVectorMath"); off.operation = 'SCALE'
    L.new(nrm.outputs["Normal"], off.inputs[0]); L.new(eps, off.inputs["Scale"])
    src = vmath('ADD', pos.outputs["Position"], off.outputs[0])
    # すぐ隣の面に当たったレイは数えない（荒いメッシュで、凸な所が黒くなるのを防ぐ）
    near = math('MULTIPLY', dist, 0.06)
    acc = None
    for k in range(AO_RAYS):
        rv = N.new("FunctionNodeRandomValue"); rv.data_type = 'FLOAT_VECTOR'
        rv.location = (-800, -600 - k * 120)
        rv.inputs["Min"].default_value = (-1.0, -1.0, -1.0)
        rv.inputs["Max"].default_value = (1.0, 1.0, 1.0)
        rv.inputs["Seed"].default_value = 17 + k * 31
        d = vmath('NORMALIZE', vmath('ADD', nrm.outputs["Normal"], vmath('NORMALIZE', rv.outputs["Value"])))
        rc = N.new("GeometryNodeRaycast"); rc.location = (-300, -600 - k * 120)
        L.new(G, rc.inputs["Target Geometry"])
        L.new(src, rc.inputs["Source Position"])
        L.new(d, rc.inputs["Ray Direction"])
        L.new(dist, rc.inputs["Ray Length"])
        h = math('MULTIPLY', rc.outputs["Is Hit"], math('GREATER_THAN', rc.outputs["Hit Distance"], near))
        acc = h if acc is None else math('ADD', acc, h)
    ao = math('SUBTRACT', 1.0, math('DIVIDE', acc, float(AO_RAYS)), clamp=True)
    cc = N.new("FunctionNodeCombineColor"); cc.location = (600, 0)
    L.new(curv[0], cc.inputs[0]); L.new(curv[1], cc.inputs[1]); L.new(ao, cc.inputs[2])
    st = N.new("GeometryNodeStoreNamedAttribute"); st.location = (1000, 0)
    st.data_type = 'FLOAT_COLOR'
    st.domain = 'POINT'
    st.inputs["Name"].default_value = SM_ATTR
    L.new(G, st.inputs["Geometry"])
    L.new(cc.outputs[0], st.inputs["Value"])
    L.new(st.outputs[0], go.inputs["Geometry"])
    return g


def _mod_inputs(md):
    """{ソケット名: 識別子} for a geometry-nodes modifier."""
    res = {}
    if md.node_group is None:
        return res
    for it in md.node_group.interface.items_tree:
        if getattr(it, "in_out", None) == 'INPUT' and it.item_type == 'SOCKET':
            res[it.name] = it.identifier
    return res


def ensure_mask_modifier(obj, edge_radius=None, spread=None, ao_distance=None):
    """物の一番最後にマスク計算のモディファイアを付ける（なければ作る）。"""
    g = ensure_surface_mask_group()
    md = obj.modifiers.get(SM_MOD)
    if md is None or md.type != 'NODES':
        md = obj.modifiers.new(SM_MOD, 'NODES')
        md.show_in_editmode = False
        md.show_expanded = False
    if md.node_group != g:
        md.node_group = g
    md.show_viewport = True
    md.show_render = True
    idx = obj.modifiers.find(md.name)
    last = len(obj.modifiers) - 1
    if idx != last:
        try:
            obj.modifiers.move(idx, last)
        except Exception as e:
            print("CMX: modifier move failed", e)
    ids = _mod_inputs(md)
    for nm, v in (("Edge Radius", edge_radius), ("Spread", spread), ("AO Distance", ao_distance)):
        if v is not None and nm in ids:
            md[ids[nm]] = v
    obj.update_tag()
    return md


def _local_diag(obj):
    try:
        bb = np.array([tuple(c) for c in obj.bound_box])
        return float(np.linalg.norm(bb.max(0) - bb.min(0))) or 1.0
    except Exception:
        return 1.0


class _CyclesBakeEnv:
    """Cycles での焼き込み中だけ設定を変え、終わったら必ず元に戻す。
    （レンダーエンジン・サンプル数・デバイス・選択状態）"""

    def __init__(self, context, obj, samples=16, use_gpu=True):
        self.ctx = context
        self.obj = obj
        self.samples = samples
        self.use_gpu = use_gpu

    def __enter__(self):
        sc = self.ctx.scene
        # 3×3の確認用の並びと、ハイトで動かした形は焼き込みに入れない
        self.preview = []
        for nm in ("CMX_TilePreview_X", "CMX_TilePreview_Y", DISP_MOD):
            md = self.obj.modifiers.get(nm)
            if md is not None:
                self.preview.append((md, md.show_viewport, md.show_render))
                md.show_viewport = False
                md.show_render = False
        self.engine = sc.render.engine
        sc.render.engine = 'CYCLES'
        cy = sc.cycles
        self.cy = {k: getattr(cy, k) for k in ("samples", "device", "use_denoising")}
        cy.samples = self.samples
        cy.use_denoising = False
        self.prefs = None
        if self.use_gpu:
            try:
                cp = bpy.context.preferences.addons["cycles"].preferences
                old = cp.compute_device_type
                if old == 'NONE':
                    for t in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'):
                        try:
                            cp.compute_device_type = t
                        except TypeError:
                            continue
                        try:
                            cp.get_devices_for_type(t)
                        except Exception:
                            pass
                        if any(d.use and d.type == t for d in cp.devices):
                            self.prefs = (cp, old)
                            break
                    else:
                        cp.compute_device_type = old
                    if self.prefs is None:
                        cp.compute_device_type = old
                if cp.compute_device_type != 'NONE':
                    cy.device = 'GPU'
            except Exception as e:
                print("CMX: GPU setup skipped", e)
        return self

    def bake(self, **kw):
        with _SoloActive(self.ctx, self.obj):
            return bpy.ops.object.bake(**kw)

    def __exit__(self, *exc):
        sc = self.ctx.scene
        for k, v in self.cy.items():
            try:
                setattr(sc.cycles, k, v)
            except Exception:
                pass
        if self.prefs is not None:
            cp, old = self.prefs
            try:
                cp.compute_device_type = old
            except Exception:
                pass
        sc.render.engine = self.engine
        for md, sv, sr in self.preview:
            try:
                md.show_viewport = sv
                md.show_render = sr
            except Exception:
                pass
        return False


def _new_data_image(name, res, color=(0.0, 0.0, 0.0, 1.0), float_buffer=False):
    img = bpy.data.images.get(name)
    if img is not None and (tuple(img.size) != (res, res) or img.is_float != float_buffer):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, res, res, alpha=False, float_buffer=float_buffer)
    try:
        img.colorspace_settings.name = "Non-Color"
    except TypeError:
        pass
    px = np.empty((res * res, 4), dtype=np.float32)
    px[:] = color
    img.pixels.foreach_set(px.ravel())
    img.update()
    return img


class _TempBakeNodes:
    """物のマテリアルに、焼き込み用の一時ノード（出力・画像）を足す。終わったら消して元に戻す。"""

    def __init__(self, obj, image):
        self.obj = obj
        self.image = image
        self.mats = []
        self.added = []      # (tree, [node names])
        self.outs = []       # (tree, previously active output name)
        self.active = []     # (tree, previously active node name)

    def __enter__(self):
        seen = set()
        for slot in self.obj.material_slots:
            m = slot.material
            if m is None or m.name in seen or not m.use_nodes:
                continue
            seen.add(m.name)
            self.mats.append(m)
        return self

    def add(self, mat, build=None):
        """build(tree) -> surface socket（一時的な出力につなぐ）。None ならマテリアルの出力のまま（ノーマルの焼き込み用）。"""
        nt = mat.node_tree
        prev_out = _find_output(nt)
        self.outs.append((nt, prev_out.name if prev_out else None))
        self.active.append((nt, nt.nodes.active.name if nt.nodes.active else None))
        created_before = {n.name for n in nt.nodes}
        out = None
        if build is not None:
            surf = build(nt)
            out = nt.nodes.new("ShaderNodeOutputMaterial")
            out.target = 'ALL'
            out.location = (-3000, -3000)
            nt.links.new(surf, out.inputs[0])
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = self.image
        tex.location = (-3000, -3300)
        for n in nt.nodes:
            n.select = False
        tex.select = True
        nt.nodes.active = tex
        if out is not None:
            out.is_active_output = True
        names = [n.name for n in nt.nodes if n.name not in created_before]
        self.added.append((nt, names))

    def __exit__(self, *exc):
        for nt, names in self.added:
            for nm in names:
                n = nt.nodes.get(nm)
                if n is not None:
                    nt.nodes.remove(n)
        for nt, nm in self.outs:
            if nm and nt.nodes.get(nm) is not None:
                nt.nodes[nm].is_active_output = True
        for nt, nm in self.active:
            if nm and nt.nodes.get(nm) is not None:
                nt.nodes.active = nt.nodes[nm]
        return False


def _normalize_channels(img, chans, pct=98.0, max_gain=8.0):
    """エッジ・くぼみは形によって値の大きさがまちまちなので、強い所が 1 付近になるようそろえる。"""
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    a = a.reshape(-1, 4)
    for c in chans:
        v = a[:, c]
        nz = v[v > 0.02]
        if nz.size < 50:
            continue
        p = float(np.percentile(nz, pct))
        if p <= 1e-4:
            continue
        a[:, c] = np.clip(v * min(max_gain, max(1.0, 1.0 / p)), 0.0, 1.0)
    img.pixels.foreach_set(a.ravel())
    img.update()


def bake_surface_masks(context, obj, res=1024, edge_width=0.02, cavity_width=0.03, ao_distance=0.3,
                       ao_others=False, samples=16, uv_name=PAINT_UV, image_name=None):
    """エッジ・くぼみ・AO を Cycles で画像に焼く（R=エッジ, G=くぼみ, B=AO）。
    ローポリの角でもきれいに出る。物は材質スロットを持ち、uv_name のUVが必要。"""
    if obj.data.uv_layers.get(uv_name) is None:
        raise RuntimeError("UVマップ %s がありません" % uv_name)
    img = _new_data_image(image_name or ("CMX_%s_Masks" % obj.name), res)

    def build(nt):
        N, L = nt.nodes, nt.links

        def ao_node(inside, dist, local):
            n = N.new("ShaderNodeAmbientOcclusion")
            n.inside = inside
            n.only_local = local
            n.samples = 16
            n.inputs["Distance"].default_value = dist
            n.location = (-3400, -3000)
            return n.outputs["AO"]

        def gain(sock):
            m = N.new("ShaderNodeMath"); m.operation = 'MULTIPLY'; m.use_clamp = True
            m.location = (-3200, -3000)
            inv = N.new("ShaderNodeMath"); inv.operation = 'SUBTRACT'; inv.inputs[0].default_value = 1.0
            inv.location = (-3300, -3000)
            L.new(sock, inv.inputs[1])
            L.new(inv.outputs[0], m.inputs[0])
            m.inputs[1].default_value = 2.0
            return m.outputs[0]

        edge = gain(ao_node(True, edge_width, True))
        cav = gain(ao_node(False, cavity_width, True))
        ao = ao_node(False, ao_distance, not ao_others)
        cc = N.new("ShaderNodeCombineColor"); cc.location = (-3100, -3000)
        L.new(edge, cc.inputs[0]); L.new(cav, cc.inputs[1]); L.new(ao, cc.inputs[2])
        em = N.new("ShaderNodeEmission"); em.location = (-3050, -3000)
        L.new(cc.outputs[0], em.inputs["Color"])
        return em.outputs[0]

    with _TempBakeNodes(obj, img) as tb:
        if not tb.mats:
            raise RuntimeError("マテリアルがありません")
        for m in tb.mats:
            tb.add(m, build)
        with _CyclesBakeEnv(context, obj, samples=samples) as env:
            env.bake(type='EMIT', margin=8, use_clear=True, target='IMAGE_TEXTURES',
                     save_mode='INTERNAL', uv_layer=uv_name)
    _normalize_channels(img, (0, 1))
    try:
        img.pack()
    except Exception as e:
        print("CMX: pack failed", e)
    return img


# ------------------------------------------------------------------ paint UV
def ensure_paint_uv(context, obj):
    """塗り・焼き込み用の重ならないUV（CMX_UV）を作る。物の元のUVは変えない。"""
    me = obj.data
    uv = me.uv_layers.get(PAINT_UV)
    if uv is not None and me.get("cmx_uv_ok"):
        return uv
    if uv is None and len(me.uv_layers) >= 8:
        raise RuntimeError("UVマップを追加できません（上限8個）")
    prev_active = me.uv_layers.active.name if me.uv_layers.active else None
    if uv is None:
        uv = me.uv_layers.new(name=PAINT_UV, do_init=False)
    me.uv_layers.active = uv
    flags = {}
    for coll, attrs in ((me.vertices, ("select", "hide")), (me.edges, ("select", "hide")),
                        (me.polygons, ("select", "hide"))):
        for a in attrs:
            arr = np.empty(len(coll), dtype=bool)
            coll.foreach_get(a, arr)
            flags[(id(coll), a)] = (coll, a, arr)
    try:
        # 編集モードの操作は「本当のアクティブ」にしか効かないので、一瞬だけこの物だけを選んでアクティブにする
        with _SoloActive(context, obj):
            bpy.ops.object.mode_set(mode='EDIT')
            try:
                bpy.ops.mesh.reveal(select=False)
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.01,
                                         area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
            finally:
                bpy.ops.object.mode_set(mode='OBJECT')
        me = obj.data
        me["cmx_uv_ok"] = True
    finally:
        me = obj.data
        for coll, a, arr in flags.values():
            try:
                coll.foreach_set(a, arr)
            except Exception:
                pass
        if prev_active and me.uv_layers.get(prev_active) is not None:
            me.uv_layers.active = me.uv_layers[prev_active]
        me.update()
    return me.uv_layers.get(PAINT_UV)


class _SoloActive:
    """編集モードやベイクなど「アクティブな物」にしか効かない操作のために、
    一時的にこの物だけを選択・アクティブにする。抜けるときに選択とアクティブを必ず元に戻す。"""

    def __init__(self, context, obj):
        self.ctx = context
        self.obj = obj

    def __enter__(self):
        vl = self.ctx.view_layer
        obj = self.obj
        self.prev_active = vl.objects.active
        self.prev_sel = [o for o in vl.objects if o.select_get()]
        pa = self.prev_active
        if pa is not None and pa.mode != 'OBJECT':
            if pa == obj:
                bpy.ops.object.mode_set(mode='OBJECT')
            else:
                raise RuntimeError("オブジェクトモードにしてから実行してください")
        self.hide_sel = obj.hide_select
        obj.hide_select = False
        for o in self.prev_sel:
            o.select_set(False)
        obj.select_set(True)
        vl.objects.active = obj
        return self

    def __exit__(self, *exc):
        vl = self.ctx.view_layer
        try:
            if vl.objects.active is not None and vl.objects.active.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception as e:
            print("CMX: mode restore", e)
        try:
            self.obj.select_set(False)
            for o in self.prev_sel:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            vl.objects.active = self.prev_active
            self.obj.hide_select = self.hide_sel
        except Exception as e:
            print("CMX: selection restore", e)
        return False


def _default_uv(obj):
    """UV投影で使う既定のUV：レンダー用のUV（CMX_UV 以外を優先）。"""
    uvs = obj.data.uv_layers
    for u in uvs:
        if u.active_render and u.name != PAINT_UV:
            return u.name
    for u in uvs:
        if u.name != PAINT_UV:
            return u.name
    return PAINT_UV if uvs.get(PAINT_UV) is not None else ""


# ------------------------------------------------------------------ texture bake & export
#   (key, パネルの表示名, ファイル名の末尾)
BAKE_CHANNELS = [
    ('COLOR', "ベースカラー", "BaseColor"),
    ('ROUGH', "ラフネス", "Roughness"),
    ('METAL', "メタル", "Metallic"),
    ('NORMAL', "ノーマル", "Normal"),
    ('HEIGHT', "ハイト", "Height"),
    ('AO', "AO", "AO"),
]
_BAKE_SUFFIX = {k: suf for k, _l, suf in BAKE_CHANNELS}
_BAKE_OUT = {'COLOR': "OUT_Color", 'ROUGH': "OUT_Roughness", 'METAL': "OUT_Metallic", 'HEIGHT': "OUT_Height"}


def _safe_name(n):
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n).strip().rstrip(".")
    return n or "object"


def export_dir(obj):
    """書き出し先：指定フォルダ（なければ blend の隣の textures、blend 未保存ならドキュメント\\CMX_Textures）\\物の名前"""
    s = obj.cmx
    if s.export_dir.strip():
        base = bpy.path.abspath(s.export_dir.strip())
    elif bpy.data.filepath:
        base = os.path.join(os.path.dirname(bpy.data.filepath), "textures")
    else:
        base = os.path.join(os.path.expanduser("~"), "Documents", "CMX_Textures")
    return os.path.normpath(os.path.join(base, _safe_name(obj.name)))


def _bake_target(name, res, srgb=False, float_buffer=False):
    img = bpy.data.images.get(name)
    if img is not None and (tuple(img.size) != (res, res) or img.is_float != float_buffer
                            or img.source != 'GENERATED'):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, res, res, alpha=False, float_buffer=float_buffer)
    try:
        img.colorspace_settings.name = "sRGB" if srgb else "Non-Color"
    except TypeError:
        pass
    return img


def _is_blank(img, flat=False):
    """焼いた画像がまっ黒（0）か（flat=True なら、全部同じ色か）。シェーダーが Cycles に大きすぎると、
    エラーのまま黒や平らなノーマルに焼けてしまうので、その検出に使う。"""
    px = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(px)
    rgb = px.reshape(-1, 4)[:, :3]
    if flat:
        return float(rgb.std(axis=0).max()) < 1e-5
    return float(rgb.max()) < 1e-6


def bake_textures(context, obj, res=2048, uv_name=PAINT_UV, channels=None, samples=4, ao_samples=16,
                  ao_distance=0.3, ao_others=False):
    """ミキサーの仕上がりを Cycles でテクスチャに焼く。{key: image} を返す（保存はしない）。"""
    s = obj.cmx
    me = obj.data
    if me.uv_layers.get(uv_name) is None:
        raise RuntimeError("UVマップ %s がありません" % uv_name)
    channels = channels or [c[0] for c in BAKE_CHANNELS]
    margin = max(4, res // 128)
    old = (s.use_baked, s.view_mode)
    _SUSPEND[0] += 1
    try:
        s.use_baked = False
        s.view_mode = 'NORMAL'
    finally:
        _SUSPEND[0] -= 1
    rebuild_material(obj, light=False)
    uvs = me.uv_layers
    prev_active = uvs.active.name if uvs.active else None
    prev_render = next((u.name for u in uvs if u.active_render), None)
    images = {}
    try:
        # ノーマルの接線は「アクティブなUV」から作られるので、焼く間だけ焼き込み用UVをアクティブにする
        uvs.active = uvs[uv_name]
        uvs[uv_name].active_render = True
        with _CyclesBakeEnv(context, obj, samples=samples) as env:
            for key in channels:
                img = _bake_target("CMX_%s_%s" % (obj.name, _BAKE_SUFFIX[key]), res,
                                   srgb=(key == 'COLOR'), float_buffer=(key == 'HEIGHT'))
                build = None
                btype = 'EMIT'
                if key == 'NORMAL':
                    btype = 'NORMAL'
                elif key == 'AO':
                    def build(nt):
                        a = nt.nodes.new("ShaderNodeAmbientOcclusion")
                        a.only_local = not ao_others
                        a.samples = 16
                        a.inputs["Distance"].default_value = ao_distance
                        em = nt.nodes.new("ShaderNodeEmission")
                        nt.links.new(a.outputs["AO"], em.inputs["Color"])
                        return em.outputs[0]
                else:
                    def build(nt, out_name=_BAKE_OUT[key]):
                        rr = nt.nodes.get(out_name)
                        em = nt.nodes.new("ShaderNodeEmission")
                        if rr is not None:
                            nt.links.new(rr.outputs[0], em.inputs["Color"])
                        return em.outputs[0]
                context.scene.cycles.samples = ao_samples if key == 'AO' else samples
                with _TempBakeNodes(obj, img) as tb:
                    if not tb.mats:
                        raise RuntimeError("マテリアルがありません")
                    for m in tb.mats:
                        tb.add(m, build)
                    kw = dict(type=btype, margin=margin, margin_type='EXTEND', use_clear=True,
                              target='IMAGE_TEXTURES', save_mode='INTERNAL', uv_layer=uv_name)
                    if btype == 'NORMAL':
                        kw["normal_space"] = 'TANGENT'
                    env.bake(**kw)
                if (key == 'COLOR' and _is_blank(img)) or \
                        (key == 'NORMAL' and _heavy_shader(s) and _is_blank(img, flat=True)):
                    raise RuntimeError("Cycles でシェーダーが大きすぎて焼けませんでした。タイリング崩し・全体の凹凸・"
                                       "トリプラナーのレイヤーを減らしてみてください（焼き込み以外の表示は問題ありません）")
                images[key] = img
    finally:
        if prev_active and uvs.get(prev_active) is not None:
            uvs.active = uvs[prev_active]
        if prev_render and uvs.get(prev_render) is not None:
            uvs[prev_render].active_render = True
        _SUSPEND[0] += 1
        try:
            s.use_baked, s.view_mode = old
        finally:
            _SUSPEND[0] -= 1
    return images


def save_baked_images(obj, images, directory):
    """PNG で保存し、画像をそのファイルにつなぐ。保存できなかった画像は blend 内にパックする。"""
    saved, failed = [], []
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as e:
        failed.append(str(e))
        directory = None
    for key, img in images.items():
        if directory is None:
            img.pack()
            continue
        path = os.path.join(directory, "%s_%s.png" % (_safe_name(obj.name), _BAKE_SUFFIX[key]))
        try:
            img.filepath_raw = path
            img.file_format = 'PNG'
            img.save()
            if img.source != 'FILE':
                img.source = 'FILE'
            img.filepath = path
            saved.append(path)
        except Exception as e:
            failed.append("%s: %s" % (os.path.basename(path), e))
            try:
                img.pack()
            except Exception:
                pass
    return saved, failed


def build_baked_material(obj, images, uv_name):
    """焼いたテクスチャだけを使う、普通の軽いマテリアル。"""
    name = "CMX_%s_Baked" % obj.name
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    mat["cmx_baked"] = True
    nt = mat.node_tree
    N, L = nt.nodes, nt.links
    N.clear()
    out = N.new("ShaderNodeOutputMaterial"); out.location = (700, 0)
    bsdf = N.new("ShaderNodeBsdfPrincipled"); bsdf.location = (350, 0)
    L.new(bsdf.outputs[0], out.inputs[0])
    uvn = N.new("ShaderNodeUVMap"); uvn.uv_map = uv_name; uvn.location = (-900, 0)
    y = [400]

    def tex(key):
        n = N.new("ShaderNodeTexImage")
        n.image = images[key]
        n.label = _BAKE_SUFFIX[key]
        n.location = (-600, y[0])
        y[0] -= 290
        L.new(uvn.outputs["UV"], n.inputs["Vector"])
        return n

    col = None
    if 'COLOR' in images:
        col = tex('COLOR').outputs["Color"]
    if 'AO' in images:
        ao = tex('AO').outputs["Color"]
        if col is not None:
            mx = N.new("ShaderNodeMix"); mx.data_type = 'RGBA'; mx.blend_type = 'MULTIPLY'
            mx.label = "AOの強さ（0 = なし）"
            mx.location = (0, 350)
            mx.inputs[0].default_value = 0.0
            L.new(col, mx.inputs[6]); L.new(ao, mx.inputs[7])
            col = mx.outputs[2]
    if col is not None:
        L.new(col, bsdf.inputs["Base Color"])
    if 'ROUGH' in images:
        L.new(tex('ROUGH').outputs["Color"], bsdf.inputs["Roughness"])
    if 'METAL' in images:
        L.new(tex('METAL').outputs["Color"], bsdf.inputs["Metallic"])
    if 'NORMAL' in images:
        nm = N.new("ShaderNodeNormalMap"); nm.uv_map = uv_name; nm.location = (0, -350)
        L.new(tex('NORMAL').outputs["Color"], nm.inputs["Color"])
        L.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if 'HEIGHT' in images:
        dp = N.new("ShaderNodeDisplacement"); dp.location = (350, -500)
        dp.label = "ハイト（使うときは出力の Displacement につなぐ）"
        dp.inputs["Scale"].default_value = 0.02
        L.new(tex('HEIGHT').outputs["Color"], dp.inputs["Height"])
    return mat


# ------------------------------------------------------------------ smart materials（レイヤー構成のプリセット）
#   ユーザー設定フォルダ（どの blend からでも使える）に
#     <名前>.json  … レイヤー・自動マスク・調整の設定
#     <名前>.blend … 使っているマテリアル（ほかの blend で使うときに読み込む）
#   を保存する。塗り（足す・消す）の画像は引き継がない。
SMART_VER = 1
_SER_SKIP = {"rna_type", "image", "uid", "preview_set"}


def smart_dir():
    return bpy.utils.user_resource('CONFIG', path=os.path.join("concept_material_mixer", "smart_materials"),
                                   create=True)


def list_smart():
    d = smart_dir()
    try:
        names = sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith(".json"))
    except Exception:
        names = []
    return names


def pg_to_dict(pg, skip=_SER_SKIP):
    d = {}
    for p in pg.bl_rna.properties:
        k = p.identifier
        if k in skip:
            continue
        v = getattr(pg, k)
        if p.type == 'COLLECTION':
            d[k] = [pg_to_dict(it, skip) for it in v]
        elif p.type == 'POINTER':
            if p.fixed_type is not None and p.fixed_type.identifier == 'Material':
                d[k] = v.name if v is not None else None
        elif p.type in ('BOOLEAN', 'INT', 'FLOAT') and getattr(p, "array_length", 0) > 0:
            d[k] = [x for x in v]
        elif p.type == 'ENUM' and p.is_enum_flag:
            d[k] = sorted(v)
        else:
            d[k] = v
    return d


def dict_to_pg(d, pg):
    for k, v in d.items():
        p = pg.bl_rna.properties.get(k)
        if p is None or k in _SER_SKIP:
            continue
        if p.type == 'COLLECTION':
            coll = getattr(pg, k)
            coll.clear()
            for it in v:
                dict_to_pg(it, coll.add())
        elif p.type == 'POINTER':
            if p.fixed_type is not None and p.fixed_type.identifier == 'Material':
                setattr(pg, k, bpy.data.materials.get(v) if v else None)
        elif not p.is_readonly:
            try:
                setattr(pg, k, set(v) if (p.type == 'ENUM' and p.is_enum_flag) else v)
            except Exception:
                pass      # 例：保存先にないUVマップ名 → 既定のまま


def save_smart(obj, name):
    """今の構成を保存。戻り値は保存した名前。"""
    s = obj.cmx
    name = _safe_name(name)
    d = smart_dir()
    data = {"version": SMART_VER, "name": name, "source_object": obj.name,
            "layers": [pg_to_dict(l) for l in s.layers],
            "settings": {"height_strength": s.height_strength, "mask_source": s.mask_source,
                         "macro_amount": s.macro_amount, "macro_scale": s.macro_scale,
                         "bump_strength": s.bump_strength, "bump_distance": s.bump_distance}}
    mats = {l.material for l in s.layers if l.material is not None}
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    blend = os.path.join(d, name + ".blend")
    if mats:
        bpy.data.libraries.write(blend, mats, path_remap='ABSOLUTE', fake_user=True)
    elif os.path.exists(blend):
        os.remove(blend)
    return name


def load_smart(name):
    d = smart_dir()
    with open(os.path.join(d, name + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    # 足りないマテリアルを、プリセットの blend から読み込む（同じ名前があればそれを使う）
    need = set()
    for l in data.get("layers", []):
        if l.get("material"):
            need.add(l["material"])
    missing = [n for n in need if bpy.data.materials.get(n) is None]
    blend = os.path.join(d, name + ".blend")
    if missing and os.path.isfile(blend):
        with bpy.data.libraries.load(blend, link=False) as (src, dst):
            dst.materials = [n for n in missing if n in src.materials]
    return data


def apply_smart(context, obj, data, keep_base=False):
    """プリセットを物に適用する（塗りはリセット）。(成功したか, メッセージ) を返す。"""
    s = obj.cmx
    if not s.enabled:
        err = enable_mixer(context, obj, build=False)
        if err:
            return False, err
    old_imgs = [l.image for l in s.layers if l.image is not None]
    base = pg_to_dict(s.layers[0]) if (keep_base and len(s.layers)) else None
    _SUSPEND[0] += 1
    try:
        s.layers.clear()
        for k, ld in enumerate(data.get("layers", [])):
            l = s.layers.add()
            dict_to_pg(base if (k == 0 and base is not None) else ld, l)
            l.uid = uuid.uuid4().hex[:8]
            l.preview_set = False
            l.image = None
        if len(s.layers) == 0:
            l = s.layers.add()
            l.uid = uuid.uuid4().hex[:8]
            l.name = "ベース"
        st = data.get("settings", {})
        if "height_strength" in st:
            s.height_strength = st["height_strength"]
        for k in ("macro_amount", "macro_scale", "bump_strength", "bump_distance"):
            if k in st:
                setattr(s, k, st[k])
        s.active_index = len(s.layers) - 1
        s.use_baked = False
        s.view_mode = 'NORMAL'
    finally:
        _SUSPEND[0] -= 1
    msg = None
    if st.get("mask_source") == 'BAKED' and _needs_mask_data(s):
        # 元が「焼き込み」マスクなら、適用先でも焼いて同じ見え方にそろえる
        try:
            img = bake_surface_masks(context, obj, res=int(s.bake_res), edge_width=s.bake_edge,
                                     cavity_width=s.bake_cavity, ao_distance=s.bake_ao,
                                     ao_others=s.bake_ao_others, samples=s.bake_samples)
            _SUSPEND[0] += 1
            try:
                s.mask_image = img
                s.mask_source = 'BAKED'
            finally:
                _SUSPEND[0] -= 1
        except Exception as e:
            msg = "マスクの焼き込みに失敗（頂点の値で表示）: %s" % e
    rebuild_material(obj)
    for img in old_imgs:
        if img.users == 0:
            bpy.data.images.remove(img)
    return True, msg


# ------------------------------------------------------------------ tiling material（継ぎ目なしテクスチャを作る）
def create_tile_object(context, name="タイル素材", size=2.0):
    """平らな正方形1枚の「タイル素材」用の物を作る。UV（CMX_UV）は0〜1にぴったり合わせる。"""
    h = size / 2.0
    me = bpy.data.meshes.new("CMX_Tile_" + name)
    me.from_pydata([(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)], [], [(0, 1, 2, 3)])
    me.update()
    me.uv_layers.new(name=PAINT_UV)
    ob = bpy.data.objects.new("CMX_Tile_" + name, me)
    coll = context.collection or context.scene.collection
    coll.objects.link(ob)
    ob.location = context.scene.cursor.location
    _set_tile_geometry(ob, size)
    me["cmx_uv_ok"] = True
    s = ob.cmx
    _SUSPEND[0] += 1
    try:
        s.tile_mode = True
        s.tile_size_m = size
    finally:
        _SUSPEND[0] -= 1
    err = enable_mixer(context, ob)
    if err:
        raise RuntimeError(err)
    return ob


# ------------------------------------------------------------------ ground paint import（地面ペイントのレイヤーを取り込む）
_GP_COPY = ("height_contrast", "height_offset", "opacity", "color", "roughness", "preview_color", "preview_set",
            "adj_type", "adj_hue", "adj_sat", "adj_val", "adj_rough", "adj_metal", "wet_dark", "wet_rough",
            "wet_flat", "tint_color", "tint_blend", "tile_size")


def ground_paint_layers(obj):
    cg = getattr(obj, "cgp", None) if obj is not None else None
    try:
        return list(cg.layers) if cg is not None else []
    except Exception:
        return []


def import_ground_paint(context, obj):
    """地面ペイント（concept_ground_paint）のレイヤーをミキサーのレイヤーにする。地面ペイント側のデータは変えない。
    まだミキサーを始めていなければベースも含めて置き換え、始めていれば上に追加する。(成功, メッセージ) を返す。"""
    src = ground_paint_layers(obj)
    if not src:
        return False, "地面ペイントのレイヤーがありません"
    cg = obj.cgp
    fresh = not obj.cmx.enabled
    if fresh:
        err = enable_mixer(context, obj, build=False)
        if err:
            return False, err
    s = obj.cmx
    pairs = []
    _SUSPEND[0] += 1
    try:
        for i, cl in enumerate(src):
            if i == 0 and not fresh:
                continue
            if i == 0:
                l = s.layers[0]
            else:
                l = s.layers.add()
                l.uid = uuid.uuid4().hex[:8]
                l.kind = getattr(cl, "kind", 'MATERIAL')
            l.name = cl.name
            l.material = getattr(cl, "material", None)
            l.projection = 'TOP' if getattr(cl, "coord_mode", 'WORLD') == 'WORLD' else 'ORIGINAL'
            for k in _GP_COPY:
                if hasattr(cl, k):
                    try:
                        setattr(l, k, getattr(cl, k))
                    except Exception:
                        pass
            l.use_auto = False          # 地面ペイントは「塗った所だけ」
            if i > 0 and getattr(cl, "image", None) is not None:
                pairs.append((l.uid, cl.image, int(getattr(cl, "fmt", 1))))
        if fresh:
            s.height_strength = getattr(cg, "height_strength", s.height_strength)
            s.noise_scale = getattr(cg, "edge_noise_scale", s.noise_scale)
        s.active_index = len(s.layers) - 1
    finally:
        _SUSPEND[0] -= 1
    rebuild_material(obj)
    if not pairs:
        return True, None
    uv_src = "CGP_UV" if obj.data.uv_layers.get("CGP_UV") is not None else None
    if uv_src is None:
        return True, "地面ペイントのUV（CGP_UV）が見つからないため、塗りは取り込めませんでした"
    by_uid = {l.uid: l for l in s.layers}
    with _CyclesBakeEnv(context, obj, samples=1) as env:
        for uid, src_img, fmt in pairs:
            l = by_uid.get(uid)
            if l is None:
                continue
            dst = ensure_layer_image(obj, l)

            def build(nt, src_img=src_img, fmt=fmt):
                # 地面ペイント：R=重み, G=やわらかさ×重み, B=ノイズ×重み → ミキサー：R=0.5+0.5×重み（足す）, G, B はそのまま
                N, L = nt.nodes, nt.links
                uvn = N.new("ShaderNodeUVMap"); uvn.uv_map = uv_src
                t = N.new("ShaderNodeTexImage"); t.image = src_img; t.interpolation = 'Linear'; t.extension = 'EXTEND'
                L.new(uvn.outputs["UV"], t.inputs["Vector"])
                sp = N.new("ShaderNodeSeparateColor"); L.new(t.outputs["Color"], sp.inputs[0])
                r = N.new("ShaderNodeMath"); r.operation = 'MULTIPLY_ADD'
                L.new(sp.outputs[0], r.inputs[0]); r.inputs[1].default_value = 0.5; r.inputs[2].default_value = 0.5
                cc = N.new("ShaderNodeCombineColor"); L.new(r.outputs[0], cc.inputs[0])
                if fmt >= 1:
                    L.new(sp.outputs[1], cc.inputs[1]); L.new(sp.outputs[2], cc.inputs[2])
                else:          # 古い形式（白黒の重みだけ）
                    for ch, f in ((1, 0.15), (2, 0.5)):
                        mm = N.new("ShaderNodeMath"); mm.operation = 'MULTIPLY'
                        L.new(sp.outputs[0], mm.inputs[0]); mm.inputs[1].default_value = f
                        L.new(mm.outputs[0], cc.inputs[ch])
                em = N.new("ShaderNodeEmission"); L.new(cc.outputs[0], em.inputs["Color"])
                return em.outputs[0]

            with _TempBakeNodes(obj, dst) as tb:
                for m in tb.mats:
                    tb.add(m, build)
                env.bake(type='EMIT', margin=4, margin_type='EXTEND', use_clear=False, target='IMAGE_TEXTURES',
                         save_mode='INTERNAL', uv_layer=PAINT_UV)
            try:
                dst.pack()
            except Exception:
                pass
    return True, None


# ------------------------------------------------------------------ paint images
def ensure_layer_image(obj, layer):
    """塗り画像：R = 足す/消す（0.5 = 自動のまま, 1 = 足す, 0 = 消す）,
    G = やわらかさ×塗りの量, B = ノイズ×塗りの量（乗算済みなので筆の端でも値が保たれる）"""
    img = layer.image
    if img is None:
        res = int(obj.cmx.resolution)
        img = _new_data_image("CMX_%s_%s" % (obj.name, layer.uid), res, color=(0.5, 0.0, 0.0, 1.0))
        try:
            img.pack()
        except Exception:
            pass
        layer.image = img
    return img


def fill_layer_image(img, mode, softness=0.15, noise=0.5):
    w, h = img.size
    px = np.empty((w * h, 4), dtype=np.float32)
    if mode == 'ADD':
        px[:] = (1.0, softness, noise, 1.0)
    elif mode == 'ERASE':
        px[:] = (0.0, softness, noise, 1.0)
    else:
        px[:] = (0.5, 0.0, 0.0, 1.0)
    img.pixels.foreach_set(px.ravel())
    img.update()
    try:
        img.pack()
    except Exception:
        pass


# ------------------------------------------------------------------ preview colour guess (簡易表示)
def _avg_image(img):
    tmp = None
    try:
        if img is None or img.size[0] == 0:
            return None
        tmp = img.copy()
        tmp.scale(16, 16)
        a = np.empty(16 * 16 * 4, np.float32)
        tmp.pixels.foreach_get(a)
        c = a.reshape(-1, 4)[:, :3].mean(0)
        if not img.is_float and img.colorspace_settings.name.lower().startswith("srgb"):
            c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
        return c
    except Exception:
        return None
    finally:
        if tmp is not None:
            bpy.data.images.remove(tmp)


def _guess_color(sock, depth=0):
    if sock is None or depth > 8:
        return None
    src = _src(sock)
    if src is None:
        v = getattr(sock, "default_value", None)
        if v is None:
            return None
        try:
            return np.array(tuple(v)[:3], dtype=np.float32) if len(v) >= 3 else None
        except TypeError:
            return np.array([v, v, v], dtype=np.float32)
    n = src.node
    t = n.type
    if t == 'TEX_IMAGE':
        return _avg_image(n.image)
    if t == 'VALTORGB':
        r = n.color_ramp
        return np.mean([tuple(r.evaluate(k / 15.0))[:3] for k in range(16)], 0)
    if t == 'RGB':
        return np.array(tuple(n.outputs[0].default_value)[:3])
    if t in ('MIX', 'MIX_RGB'):
        if t == 'MIX':
            if n.data_type != 'RGBA':
                return None
            fa, a, b = n.inputs[0], n.inputs[6], n.inputs[7]
        else:
            fa, a, b = n.inputs[0], n.inputs[1], n.inputs[2]
        ga = _guess_color(a, depth + 1)
        gb = _guess_color(b, depth + 1)
        if ga is None:
            return gb
        if gb is None:
            return ga
        f = 0.5 if fa.is_linked else float(fa.default_value)
        if n.blend_type == 'MULTIPLY':
            return ga * (1 - f + f * gb)
        return ga * (1 - f) + gb * f
    if t in ('HUE_SAT', 'BRIGHTCONTRAST', 'GAMMA', 'INVERT', 'CURVE_RGB', 'REROUTE'):
        inp = n.inputs.get("Color")
        if inp is None and len(n.inputs):
            inp = n.inputs[0]
        g = _guess_color(inp, depth + 1)
        if g is not None and t == 'INVERT':
            g = 1.0 - g
        return g
    return None


def guess_material_color(mat):
    try:
        if mat.use_nodes and mat.node_tree:
            b = _find_bsdf(mat.node_tree)
            if b is not None:
                c = _guess_color(b.inputs.get("Base Color"))
                if c is not None:
                    return tuple(float(x) for x in np.clip(c, 0.0, 1.0))
    except Exception as e:
        print("CMX: colour guess failed", mat.name, e)
    return tuple(mat.diffuse_color[:3])


# ------------------------------------------------------------------ material builder
class _Shared:
    """1つのマテリアル内で全レイヤーが共有するノード（作るのは必要になった時だけ）。"""

    def __init__(self, nb, obj, s):
        self.nb = nb
        self.obj = obj
        self.s = s
        nb.x, nb.y = -1600, 800
        self.tc = nb.node("ShaderNodeTexCoord")
        self.geo_n = nb.node("ShaderNodeNewGeometry").outputs["Normal"]
        self.g_hs = nb.value("G_hs", s.height_strength)
        self.g_dbg = nb.value("G_debug", 1.0 if s.view_mode == 'LAYERS' else 0.0)
        self.g_nzs = nb.value("G_noise_scale", s.noise_scale)
        self._uv = None
        self.tile = bool(s.tile_mode)
        self._torus = None
        # 境界を崩す共有ノイズ（3チャンネル）。タイル素材では継ぎ目が出ないよう4Dのドーナツ状の座標を使う
        nz = nb.node("ShaderNodeTexNoise", "G_noise")
        nz.hide = True
        if self.tile:
            nz.noise_dimensions = '4D'
            xyz, w = self.torus(s.tile_size_m / (2.0 * math.pi))
            nb.L.new(xyz, nz.inputs["Vector"])
            nb.L.new(w, nz.inputs["W"])
        else:
            nb.L.new(self.tc.outputs["Object"], nz.inputs["Vector"])
        nb.L.new(self.g_nzs, nz.inputs["Scale"])
        nz.inputs["Detail"].default_value = 6.0
        nz.inputs["Roughness"].default_value = 0.65
        sep = nb.node("ShaderNodeSeparateColor")
        sep.hide = True
        nb.L.new(nz.outputs["Color"], sep.inputs[0])
        self.noise = sep.outputs
        self._md = None
        self._genz = None

    def torus(self, radius):
        """タイル素材用：UV(0〜1)を4次元のドーナツ面に写した座標 (xyz, w)。u・v どちらの端でもぴったりつながる。
        radius は「1周の長さ ÷ 2π」。"""
        nb = self.nb
        if self._torus is None:
            x, y = nb.x, nb.y
            nb.x, nb.y = -1900, 200
            u, v, _z = nb.sep_xyz(self.paint_uv())
            a = nb.math('MULTIPLY', u, 2.0 * math.pi)
            b = nb.math('MULTIPLY', v, 2.0 * math.pi)
            xyz = nb.comb_xyz(nb.math('COSINE', a), nb.math('SINE', a), nb.math('COSINE', b))
            self._torus = (xyz, nb.math('SINE', b))
            nb.x, nb.y = x, y
        xyz, w = self._torus
        return nb.vmath('SCALE', xyz, scale=radius), nb.math('MULTIPLY', w, radius)

    def torus2(self, rx, ry, ua, va):
        """torus() の一般形：u・v それぞれの周期（座標 ua, va は1周で1.0増える）と半径 rx, ry を別々に指定する。
        タイル素材で、回転・ずらし・X/Y別々の繰り返しを入れたあとも継ぎ目なしにするために使う。"""
        nb = self.nb
        x, y = nb.x, nb.y
        nb.x, nb.y = -1900, 0
        a = nb.math('MULTIPLY', ua, 2.0 * math.pi)
        b = nb.math('MULTIPLY', va, 2.0 * math.pi)
        xyz = nb.comb_xyz(nb.math('MULTIPLY', nb.math('COSINE', a), rx),
                          nb.math('MULTIPLY', nb.math('SINE', a), rx),
                          nb.math('MULTIPLY', nb.math('COSINE', b), ry))
        w = nb.math('MULTIPLY', nb.math('SINE', b), ry)
        nb.x, nb.y = x, y
        return xyz, w

    def paint_uv(self):
        if self._uv is None:
            x, y = self.nb.x, self.nb.y
            self.nb.x, self.nb.y = -1600, 300
            n = self.nb.node("ShaderNodeUVMap", "G_paint_uv")
            n.uv_map = PAINT_UV
            self._uv = n.outputs["UV"]
            self.nb.x, self.nb.y = x, y
        return self._uv

    def mask_data(self):
        """(エッジ, くぼみ, AO) のソケット。"""
        if self._md is None:
            nb, s = self.nb, self.s
            x, y = nb.x, nb.y
            nb.x, nb.y = -1600, 0
            if s.mask_source == 'BAKED' and s.mask_image is not None:
                t = nb.node("ShaderNodeTexImage", "G_mask_tex")
                t.image = s.mask_image
                t.interpolation = 'Linear'
                nb.L.new(self.paint_uv(), t.inputs["Vector"])
                col = t.outputs["Color"]
            else:
                a = nb.node("ShaderNodeAttribute", "G_mask_attr")
                a.attribute_type = 'GEOMETRY'
                a.attribute_name = SM_ATTR
                col = a.outputs["Color"]
            sep = nb.node("ShaderNodeSeparateColor")
            nb.L.new(col, sep.inputs[0])
            self._md = sep.outputs
            nb.x, nb.y = x, y
        return self._md

    def gen_z(self):
        if self._genz is None:
            self._genz = self.nb.sep_xyz(self.tc.outputs["Generated"])[2]
        return self._genz


def _mask_value(nb, sh, i, j, mk, prev):
    t = mk.mtype
    p = "L%d_M%d_" % (i, j)
    prev_n = prev["Normal"] if prev is not None else None
    if t in MASK_NEEDS_DATA:
        md = sh.mask_data()
        if t == 'EDGE':
            v = md[0]
        elif t == 'CAVITY':
            v = md[1]
        else:
            v = nb.math('SUBTRACT', 1.0, md[2])
    elif t in ('UP', 'SLOPE'):
        n = prev_n if (mk.use_detail_normal and prev_n is not None) else sh.geo_n
        z = nb.sep_xyz(n)[2]
        if t == 'UP':
            v = z
        else:
            v = nb.math('MULTIPLY', nb.math('ARCCOSINE', nb.math('MINIMUM', nb.math('MAXIMUM', z, -1.0), 1.0)),
                        2.0 / math.pi)
    elif t == 'HEIGHT':
        v = sh.gen_z()
    elif t == 'LAYER_H':
        v = prev["Height"] if prev is not None else 0.5
    else:  # NOISE
        nz = nb.node("ShaderNodeTexNoise", p + "tex")
        nz.noise_dimensions = '4D'
        nz.hide = True
        seed = (j * 7.31 + i * 3.17) % 100.0
        if sh.tile:
            xyz, w = sh.torus(sh.s.tile_size_m / (2.0 * math.pi))
            nb.L.new(xyz, nz.inputs["Vector"])
            nb.L.new(nb.math('ADD', w, seed), nz.inputs["W"])
        else:
            nb.L.new(sh.tc.outputs["Object"], nz.inputs["Vector"])
            nz.inputs["W"].default_value = seed
        nb.L.new(nb.value(p + "nscale", mk.noise_scale), nz.inputs["Scale"])
        nb.L.new(nb.value(p + "ndetail", mk.noise_detail), nz.inputs["Detail"])
        nz.inputs["Roughness"].default_value = 0.6
        v = nb.math('MULTIPLY_ADD', nz.outputs["Fac"], 2.5, c=-0.75)
    # ノイズで崩す（共有ノイズ）
    nzc = sh.noise[(i + j) % 3]
    amt = nb.value(p + "noise", mk.noise)
    v = nb.math('ADD', v, nb.math('MULTIPLY', nb.math('SUBTRACT', nzc, 0.5), nb.math('MULTIPLY', amt, 2.0)))
    # 範囲・くっきり
    amount = nb.value(p + "amount", mk.amount)
    contrast = nb.value(p + "contrast", mk.contrast)
    width = nb.math('MAXIMUM', nb.math('SUBTRACT', 1.0, contrast), 0.005)
    half = nb.math('MULTIPLY', width, 0.5)
    th = nb.math('SUBTRACT', 1.0, amount)
    m = nb.smooth(v, nb.math('SUBTRACT', th, half), nb.math('ADD', th, half))
    inv = nb.value(p + "inv", 1.0 if mk.invert else 0.0)
    return nb.math('ABSOLUTE', nb.math('SUBTRACT', inv, m))


def _auto_mask(nb, sh, i, layer, prev, view):
    """自動マスクを重ねた値。マスクがなければ 1（全面）、自動マスクを使わない設定なら 0。"""
    if not layer.use_auto:
        return 0.0
    acc = None
    for j, mk in enumerate(layer.masks):
        if not mk.enabled:
            continue
        m = _mask_value(nb, sh, i, j, mk, prev)
        if view is not None and view.get("item") == (i, j):
            view["sock"] = m
        st = nb.value("L%d_M%d_strength" % (i, j), mk.strength)
        if acc is None:
            acc = nb.mix('FLOAT', st, 1.0, m)
            continue
        b = mk.blend
        if b == 'MULTIPLY':
            r = nb.math('MULTIPLY', acc, m)
        elif b == 'ADD':
            r = nb.math('ADD', acc, m, clamp=True)
        elif b == 'SUBTRACT':
            r = nb.math('SUBTRACT', acc, m, clamp=True)
        elif b == 'MAX':
            r = nb.math('MAXIMUM', acc, m)
        else:
            r = nb.math('MINIMUM', acc, m)
        acc = nb.mix('FLOAT', st, acc, r)
    return 1.0 if acc is None else acc


def _paint_mix(nb, sh, i, layer, img, auto, noise_ch):
    """自動マスクに塗り（足す・消す）を合わせる。戻り値 (重み, やわらかさ)。"""
    tex = nb.node("ShaderNodeTexImage", "L%d_paint" % i)
    tex.image = img
    tex.interpolation = 'Linear'
    tex.extension = 'EXTEND'
    tex.hide = True
    nb.L.new(sh.paint_uv(), tex.inputs["Vector"])
    sep = nb.node("ShaderNodeSeparateColor")
    sep.hide = True
    nb.L.new(tex.outputs["Color"], sep.inputs[0])
    R, G, B = sep.outputs[0], sep.outputs[1], sep.outputs[2]
    d = nb.math('MULTIPLY_ADD', R, 2.0, c=-1.0)
    ad = nb.math('ABSOLUTE', d)
    p = nb.math('MULTIPLY', nb.math('MAXIMUM', nb.math('SUBTRACT', ad, 0.02), 0.0), 1.0204, clamp=True)
    den = nb.math('MAXIMUM', ad, 0.001)
    psoft = nb.math('MAXIMUM', nb.math('DIVIDE', G, den, clamp=True), 0.001)
    pnoise = nb.math('DIVIDE', B, den, clamp=True)
    # ノイズは中間の値だけ動かす（0 と 1 はそのまま）
    nzc = nb.math('MULTIPLY', nb.math('MULTIPLY', nb.math('SUBTRACT', noise_ch, 0.5), pnoise), 2.0)
    env = nb.math('MULTIPLY', nb.math('MULTIPLY', p, nb.math('SUBTRACT', 1.0, p)), 4.0)
    p2 = nb.math('ADD', p, nb.math('MULTIPLY', nzc, env), clamp=True)
    pos = nb.math('GREATER_THAN', d, 0.0)
    add = nb.math('MULTIPLY', p2, pos)
    erase = nb.math('SUBTRACT', p2, add)
    w1 = nb.math('MULTIPLY', auto, nb.math('SUBTRACT', 1.0, erase))
    w = nb.math('ADD', w1, nb.math('MULTIPLY', add, nb.math('SUBTRACT', 1.0, w1)), clamp=True)
    w = nb.math('MULTIPLY', w, layer.opacity, clamp=True, name="L%d_opacity" % i)
    pf = nb.math('MULTIPLY', p, 25.0, clamp=True)
    soft = nb.mix('FLOAT', pf, nb.value("L%d_soft" % i, layer.softness), psoft)
    return w, soft


# ---- 配置（Placement）・タイリング崩し・凹凸の補正
def _eff_rot(s, layer):
    """実際に使う回転（ラジアン）。90°刻みの設定、またはタイル素材なら 90° の倍数にそろえる。"""
    r = layer.place_rot
    if layer.place_snap or s.tile_mode:
        q = math.pi / 2.0
        r = round(r / q) * q
    return r


def _bump_dist(s, layer):
    """normal_from_height のときの凹凸の高さ (m)。テクスチャ1枚の大きさの 5% × ハイトの強さ。"""
    size = 1.0
    if layer.projection in ('TRIPLANAR', 'TOP') or s.tile_mode:
        size = max(layer.tile_size, 1e-4)
    return 0.05 * size * layer.height_strength_layer


class _Place:
    """レイヤーの配置：位置ずらし・回転・X/Y別々の繰り返し。値は Value ノードなので、動かしても再コンパイルされない
    （タイル素材だけは整数回のくり返しが要るので作り直す）。
    テクスチャ座標 t = S · R(−θ) · p + o。ノーマルマップは XY を R(θ)·F·Y で回す（F = セルの左右反転、Y = DirectX 用の上下反転）。"""

    def __init__(self, nb, sh, i, layer, tile=False):
        self.nb = nb
        self.sh = sh
        self.i = i
        self.layer = layer
        self.tile = tile
        x, y = nb.x, nb.y
        nb.x, nb.y = i * 700, 1150
        self.rot = nb.value("L%d_pl_rot" % i, _eff_rot(sh.s, layer))
        self.C = nb.math('COSINE', self.rot)
        self.S = nb.math('SINE', self.rot)
        self.nS = nb.math('MULTIPLY', self.S, -1.0)
        self.ox = nb.value("L%d_pl_x" % i, layer.place_x)
        self.oy = nb.value("L%d_pl_y" % i, layer.place_y)
        if not tile:
            self.sx = nb.value("L%d_pl_sx" % i, layer.place_sx)
            self.sy = nb.value("L%d_pl_sy" % i, layer.place_sy)
        self.fy = nb.value("L%d_nfy" % i, -1.0 if layer.normal_flip_y else 1.0)
        nb.x, nb.y = x, y

    def uv(self, u, v):
        """平面の座標 (u, v) → テクスチャ座標 (tu, tv)。"""
        nb = self.nb
        ru = nb.math('MULTIPLY_ADD', self.S, v, c=nb.math('MULTIPLY', self.C, u))
        rv = nb.math('MULTIPLY_ADD', self.C, v, c=nb.math('MULTIPLY', self.nS, u))
        return (nb.math('MULTIPLY_ADD', ru, self.sx, c=self.ox),
                nb.math('MULTIPLY_ADD', rv, self.sy, c=self.oy))

    def tile_uv(self, uv, kx, ky):
        """タイル素材：UV(0〜1) → (周期座標 u2, v2, テクスチャ座標 tu, tv)。u2, v2 は uv が1増えると整数 kx, ky ではなく 1 だけ増える
        （どちらも uv の線形式なので、uv の端と端で継ぎ目なしにつながる）。"""
        nb = self.nb
        u, v, _z = nb.sep_xyz(uv)
        u2 = nb.math('MULTIPLY_ADD', self.C, u, c=nb.math('MULTIPLY_ADD', self.S, v, c=nb.math('DIVIDE', self.ox, float(kx))))
        v2 = nb.math('MULTIPLY_ADD', self.C, v, c=nb.math('MULTIPLY_ADD', self.nS, u, c=nb.math('DIVIDE', self.oy, float(ky))))
        return u2, v2, nb.math('MULTIPLY', u2, float(kx)), nb.math('MULTIPLY', v2, float(ky))

    def xf(self):
        """ノーマルの向きの補正（回転角・左右反転・上下反転）。セルごとの回転は ATVertex が足す。"""
        return (self.rot, 1.0, self.fy)


def _wsum(nb, outs, ws):
    """複数のサンプルを重みで足し合わせる（重みの合計は 1）。"""
    res = {}
    for key in KEYS:
        if key in ("Color", "Normal"):
            acc = nb.vmath('SCALE', outs[0][key], scale=ws[0])
            for o, w in zip(outs[1:], ws[1:]):
                acc = nb.vmath('ADD', acc, nb.vmath('SCALE', o[key], scale=w))
            if key == "Normal":
                acc = nb.vmath('NORMALIZE', acc)
        else:
            acc = nb.math('MULTIPLY', outs[0][key], ws[0])
            for o, w in zip(outs[1:], ws[1:]):
                acc = nb.math('MULTIPLY_ADD', o[key], w, c=acc)
        res[key] = acc
    return res


def _lattice_square(nb, uL, vL):
    """四角いセル：まわりの4つのセルと、その重み。セル(i, j)の中心は (i+0.5, j+0.5)。"""
    gu = nb.math('SUBTRACT', uL, 0.5)
    gv = nb.math('SUBTRACT', vL, 0.5)
    iu, iv = nb.math('FLOOR', gu), nb.math('FLOOR', gv)
    fu, fv = nb.math('FRACT', gu), nb.math('FRACT', gv)
    gus = (nb.math('SUBTRACT', 1.0, fu), fu)
    gvs = (nb.math('SUBTRACT', 1.0, fv), fv)
    out = []
    for du in (0, 1):
        for dv in (0, 1):
            out.append((nb.math('ADD', iu, float(du)), nb.math('ADD', iv, float(dv)),
                        nb.math('MULTIPLY', gus[du], gvs[dv])))
    return out


def _lattice_hex(nb, uL, vL, a, b):
    """六角形のセル（三角形の格子の頂点 3 つと重み）。格子の基底は (1/a, 0), (1/(2a), 1/b)。a, b は数値でもソケットでもよい。"""
    x1 = nb.math('MULTIPLY_ADD', vL, nb.math('MULTIPLY', b, -0.5), c=nb.math('MULTIPLY', uL, a))
    x2 = nb.math('MULTIPLY', vL, b)
    b1, b2 = nb.math('FLOOR', x1), nb.math('FLOOR', x2)
    f1, f2 = nb.math('FRACT', x1), nb.math('FRACT', x2)
    z = nb.math('SUBTRACT', nb.math('SUBTRACT', 1.0, f1), f2)
    q = nb.math('GREATER_THAN', z, 0.0)
    nq = nb.math('SUBTRACT', 1.0, q)
    v1 = (nb.math('ADD', b1, nq), nb.math('ADD', b2, nq), nb.math('ABSOLUTE', z))
    v2 = (nb.math('ADD', b1, nq), nb.math('ADD', b2, q),
          nb.mix('FLOAT', q, nb.math('SUBTRACT', 1.0, f2), f2))
    v3 = (nb.math('ADD', b1, q), nb.math('ADD', b2, nq),
          nb.mix('FLOAT', q, nb.math('SUBTRACT', 1.0, f1), f1))
    return [v1, v2, v3]


# タイリング崩し用の共有ノードグループ（ノードを何百個も並べると作り直しが遅いので、部品にしてある）
AT_VER = 3
AT_SQ, AT_HX, AT_VERTEX, AT_ACCUM = "CMX_ATLattice_SQ", "CMX_ATLattice_HX", "CMX_ATVertex", "CMX_ATAccum"
_F, _V = "NodeSocketFloat", "NodeSocketVector"


def _fresh_group(name, ver, spec, defaults=None):
    """共有グループを用意する。バージョンが同じなら (グループ, False)。作り直すときは中身を空にして (グループ, True)。"""
    g = bpy.data.node_groups.get(name)
    if g is not None and g.get("cmx_ver") == ver:
        return g, False
    if g is None:
        g = bpy.data.node_groups.new(name, "ShaderNodeTree")
    g.nodes.clear()
    g["cmx_ver"] = ver
    _ensure_interface(g, spec, defaults)
    return g, True


def _group_io(g):
    gi = g.nodes.new("NodeGroupInput"); gi.location = (-1600, 0)
    go = g.nodes.new("NodeGroupOutput"); go.location = (1600, 0)
    return gi, go


def ensure_at_lattice(hexa):
    n = 3 if hexa else 4
    spec = [(nm, 'INPUT', _F) for nm in (("U", "V", "A", "B") if hexa else ("U", "V"))]
    spec += [("%s%d" % (c, k), 'OUTPUT', _F) for k in range(n) for c in ("X", "Y", "W")]
    g, fresh = _fresh_group(AT_HX if hexa else AT_SQ, AT_VER, spec, {"A": 1.0, "B": 2.0 / math.sqrt(3.0)})
    if fresh:
        gi, go = _group_io(g)
        nb = _NB(g)
        nb.x, nb.y = -1400, 300
        vs = _lattice_hex(nb, gi.outputs["U"], gi.outputs["V"], gi.outputs["A"], gi.outputs["B"]) if hexa \
            else _lattice_square(nb, gi.outputs["U"], gi.outputs["V"])
        for k, v in enumerate(vs):
            for c, sock in zip(("X", "Y", "W"), v):
                g.links.new(sock, go.inputs["%s%d" % (c, k)])
    return g


def ensure_at_vertex():
    """セル1つぶん：乱数で「ずらし・回転・反転」を決め、そのセルで素材を読むための座標とノーマルの行列を返す。"""
    spec = [(nm, 'INPUT', _F) for nm in ("VX", "VY", "Wrap", "A", "B", "Seed", "Offset", "RotMode", "Flip",
                                         "TU", "TV", "Extra", "Rot")]
    spec += [("Vector", 'OUTPUT', _V), ("NAngle", 'OUTPUT', _F), ("NFlip", 'OUTPUT', _F)]
    g, fresh = _fresh_group(AT_VERTEX, AT_VER, spec, {"A": 1.0, "B": 1.0})
    if fresh:
        gi, go = _group_io(g)
        I = gi.outputs
        nb = _NB(g)
        nb.x, nb.y = -1400, 400
        # セル番号をタイルの周期で巻き戻す（Wrap: 0 = しない、1 = 四角、2 = 六角形）
        Ae, Be = nb.math('MAXIMUM', I["A"], 1.0), nb.math('MAXIMUM', I["B"], 1.0)
        sqx, sqy = nb.math('FLOORED_MODULO', I["VX"], Ae), nb.math('FLOORED_MODULO', I["VY"], Be)
        jq = nb.math('FLOOR', nb.math('DIVIDE', I["VY"], Be))
        hxx = nb.math('FLOORED_MODULO', nb.math('MULTIPLY_ADD', jq, nb.math('MULTIPLY', Be, 0.5), c=I["VX"]), Ae)
        is_sq = nb.math('MULTIPLY', nb.math('GREATER_THAN', I["Wrap"], 0.5), nb.math('LESS_THAN', I["Wrap"], 1.5))
        is_hx = nb.math('GREATER_THAN', I["Wrap"], 1.5)
        wx = nb.math('ADD', I["VX"], nb.math('ADD', nb.math('MULTIPLY', is_sq, nb.math('SUBTRACT', sqx, I["VX"])),
                                              nb.math('MULTIPLY', is_hx, nb.math('SUBTRACT', hxx, I["VX"]))))
        wy = nb.math('ADD', I["VY"], nb.math('MULTIPLY', nb.math('ADD', is_sq, is_hx), nb.math('SUBTRACT', sqy, I["VY"])))
        wn = nb.node("ShaderNodeTexWhiteNoise")
        wn.noise_dimensions = '3D'
        nb.L.new(nb.comb_xyz(wx, wy, I["Seed"]), wn.inputs["Vector"])
        rx, ry, rz = nb.sep_xyz(wn.outputs["Color"])
        off_x, off_y = nb.math('MULTIPLY', rx, I["Offset"]), nb.math('MULTIPLY', ry, I["Offset"])
        # 回転：RotMode 0 = なし、1 = 90°刻み、2 = 自由
        step = nb.math('MULTIPLY', nb.math('FLOOR', nb.math('MULTIPLY', rz, 4.0)), math.pi / 2.0)
        free = nb.math('MULTIPLY', rz, 2.0 * math.pi)
        phi = nb.math('MULTIPLY', nb.math('GREATER_THAN', I["RotMode"], 0.5),
                      nb.mix('FLOAT', nb.math('GREATER_THAN', I["RotMode"], 1.5), step, free))
        f = nb.math('SUBTRACT', 1.0, nb.math('MULTIPLY', I["Flip"],
                                             nb.math('MULTIPLY', nb.math('GREATER_THAN', wn.outputs["Value"], 0.5), 2.0)))
        cu, su = nb.math('COSINE', phi), nb.math('SINE', phi)
        ra = nb.math('MULTIPLY_ADD', cu, I["TU"], c=nb.math('MULTIPLY', su, I["TV"]))
        rb = nb.math('MULTIPLY_ADD', cu, I["TV"], c=nb.math('MULTIPLY', nb.math('MULTIPLY', su, -1.0), I["TU"]))
        vec = nb.comb_xyz(nb.math('ADD', nb.math('MULTIPLY', ra, f), off_x), nb.math('ADD', rb, off_y), I["Extra"])
        g.links.new(vec, go.inputs["Vector"])
        g.links.new(nb.math('ADD', I["Rot"], phi), go.inputs["NAngle"])
        g.links.new(f, go.inputs["NFlip"])
    return g


def ensure_at_accum():
    """セルのサンプルを 1 つずつ足し込む（Cycles の SVM スタックを使い切らないよう、サンプルを読んだらすぐ消費する）。
    重み e = (セルの重み)^べき乗 × exp(3 × ハイト)。ハイトで混ぜるときは高い所が勝ち、ノイズで崩すときは重みにノイズを足す。
    戻り値の合計を e の合計で割れば、混ぜた結果になる。"""
    spec = []
    for pre in ("Acc", ""):
        spec += [(pre + "Color", 'INPUT', "NodeSocketColor"), (pre + "Roughness", 'INPUT', _F),
                 (pre + "Metallic", 'INPUT', _F), (pre + "Normal", 'INPUT', _V), (pre + "Height", 'INPUT', _F)]
    spec += [("AccWeight", 'INPUT', _F), ("Weight", 'INPUT', _F), ("Noise", 'INPUT', _F), ("Power", 'INPUT', _F),
             ("Mix", 'INPUT', _F)]
    spec += [(pre + k, 'OUTPUT', SOCK_TYPES[k]) for pre in ("Acc",) for k in KEYS] + [("AccWeight", 'OUTPUT', _F)]
    g, fresh = _fresh_group(AT_ACCUM, AT_VER, spec, {"Power": 1.0})
    if fresh:
        gi, go = _group_io(g)
        I = gi.outputs
        nb = _NB(g)
        nb.x, nb.y = -1400, 400
        noisy = nb.math('MAXIMUM', nb.math('ADD', I["Weight"], nb.math('MULTIPLY', nb.math('SUBTRACT', I["Noise"], 0.5), 0.6)), 0.0)
        w = nb.math('POWER', nb.mix('FLOAT', I["Mix"], I["Weight"], noisy), I["Power"])
        # ハイトの影響：exp(3 h)。Mix = 1（ノイズ）のときは 1
        hexp = nb.math('EXPONENT', nb.math('MULTIPLY', nb.math('MULTIPLY', I["Height"], 3.0), nb.math('SUBTRACT', 1.0, I["Mix"])))
        e = nb.math('MULTIPLY', w, hexp)
        for k in KEYS:
            if k in ("Color", "Normal"):
                r = nb.vmath('ADD', I["Acc" + k], nb.vmath('SCALE', I[k], scale=e))
            else:
                r = nb.math('MULTIPLY_ADD', I[k], e, c=I["Acc" + k])
            g.links.new(r, go.inputs["Acc" + k])
        g.links.new(nb.math('ADD', I["AccWeight"], e), go.inputs["AccWeight"])
    return g


def _anti_tile(nb, sh, i, layer, pl, tu, tv, extra, plane, tag, lat, inst):
    """タイリング崩し（ランダムシート）。(tu, tv) = 配置済みのテクスチャ座標。
    まわりのセルごとに乱数で「ずらし・回転・左右反転」を決め、それぞれで素材を読んで、境目だけ混ぜる。
    戻り値は {Color, Roughness, Metallic, Normal, Height}。"""
    hexa = layer.at_shape == 'HEX'
    tile = lat["tile"]
    # ノードは「入力を作ってから使う側を作る」順に作る（Cycles は作った順に処理するので、順番が乱れるとスタックを余計に使う）
    if tile:
        wrap = 2.0 if hexa else 1.0
        if hexa:
            lu, lv = lat["u2"], lat["v2"]
            pa, pb = float(lat["a"]), float(lat["b"])
        else:
            lu = nb.math('MULTIPLY', lat["u2"], float(lat["nu"]))
            lv = nb.math('MULTIPLY', lat["v2"], float(lat["nv"]))
            pa, pb = float(lat["nu"]), float(lat["nv"])
    else:
        wrap, pa, pb = 0.0, 1.0, 1.0
        lu, lv = nb.math('DIVIDE', tu, lat["cell"]), nb.math('DIVIDE', tv, lat["cell"])
    lg = nb.node("ShaderNodeGroup", "L%d_atlat%s" % (i, tag))
    lg.node_tree = ensure_at_lattice(hexa)
    nb.link(lu, lg.inputs["U"])
    nb.link(lv, lg.inputs["V"])
    if tile and hexa:
        lg.inputs["A"].default_value = pa
        lg.inputs["B"].default_value = pb
    rotmode = {'NONE': 0.0, 'STEP': 1.0, 'FREE': 2.0}[layer.at_rot]
    if tile and rotmode == 2.0:
        rotmode = 1.0              # 自由な角度だと、タイルの端で継ぎ目が出る
    seed = layer.at_seed * 1.37 + plane * 5.11 + i * 3.7
    vg = ensure_at_vertex()
    ag = ensure_at_accum()
    n = 3 if hexa else 4
    acc = None
    for k in range(n):
        vn = nb.node("ShaderNodeGroup", "L%d_atv%s%d" % (i, tag, k))
        vn.node_tree = vg
        nb.link(lg.outputs["X%d" % k], vn.inputs["VX"])
        nb.link(lg.outputs["Y%d" % k], vn.inputs["VY"])
        for nm, v in (("Wrap", wrap), ("A", pa), ("B", pb), ("Seed", seed), ("RotMode", rotmode),
                      ("Flip", 1.0 if layer.at_flip else 0.0)):
            vn.inputs[nm].default_value = v
        nb.link(lat["amt"], vn.inputs["Offset"])
        nb.link(tu, vn.inputs["TU"])
        nb.link(tv, vn.inputs["TV"])
        nb.link(extra, vn.inputs["Extra"])
        nb.link(pl.rot, vn.inputs["Rot"])
        o = inst(vn.outputs["Vector"], plane, "%s%d" % (tag, k), (vn.outputs["NAngle"], vn.outputs["NFlip"], pl.fy))
        an = nb.node("ShaderNodeGroup", "L%d_ata%s%d" % (i, tag, k))
        an.node_tree = ag
        for key in KEYS:
            nb.link(o[key], an.inputs[key])
            if acc is not None:
                nb.link(acc[key], an.inputs["Acc" + key])
        if acc is not None:
            nb.link(acc["Weight"], an.inputs["AccWeight"])
        nb.link(lg.outputs["W%d" % k], an.inputs["Weight"])
        nb.link(sh.noise[k % 3], an.inputs["Noise"])
        nb.link(lat["pw"], an.inputs["Power"])
        an.inputs["Mix"].default_value = 1.0 if layer.at_mix == 'NOISE' else 0.0
        acc = {key: an.outputs["Acc" + key] for key in KEYS}
        acc["Weight"] = an.outputs["AccWeight"]
    inv = nb.math('DIVIDE', 1.0, nb.math('MAXIMUM', acc["Weight"], 1e-30))
    res = {"Color": nb.vmath('SCALE', acc["Color"], scale=inv), "Normal": nb.vmath('NORMALIZE', acc["Normal"])}
    for key in ("Roughness", "Metallic", "Height"):
        res[key] = nb.math('MULTIPLY', acc[key], inv)
    return res


def _normal_strength(nb, sh, i, layer, n):
    """ノーマルの強さ（Blender のノーマルマップの Strength と同じ式）。1 でそのまま、0 で面の向きだけ、2 で 2 倍。"""
    k = nb.value("L%d_nstr" % i, layer.normal_strength)
    d = nb.vmath('SUBTRACT', n, sh.geo_n)
    return nb.vmath('NORMALIZE', nb.vmath('ADD', sh.geo_n, nb.vmath('SCALE', d, scale=k)))


def _material_sockets(nb, sh, i, layer, cache):
    outs = _material_sockets_raw(nb, sh, i, layer, cache)
    if layer.material is not None:
        outs["Normal"] = _normal_strength(nb, sh, i, layer, outs["Normal"])
    return outs


def _material_sockets_raw(nb, sh, i, layer, cache):
    obj = sh.obj
    s = sh.s
    mat = layer.material
    if mat is None:
        rgb = nb.node("ShaderNodeRGB", "L%d_rgb" % i)
        rgb.outputs[0].default_value = (*layer.color, 1.0)
        return {"Color": rgb.outputs[0], "Roughness": nb.value("L%d_rough" % i, layer.roughness),
                "Metallic": nb.value("L%d_metal" % i, layer.metallic), "Normal": sh.geo_n,
                "Height": sh.noise[i % 3]}

    bump = bool(layer.normal_from_height)
    bd = nb.value("L%d_bumpd" % i, _bump_dist(s, layer)) if bump else None

    def group(mode, uv=""):
        key = (mat.name, mode, uv, bump)
        g = cache.get(key)
        if g is None:
            g = build_layer_group(mat, mode, uv, bump)
            cache[key] = g
        return g

    def inst(g, vec, plane, tag, torus=None, xf=None):
        gn = nb.node("ShaderNodeGroup", "L%d_group%s" % (i, tag))
        gn.node_tree = g
        nb.link(vec, gn.inputs["Vector"])
        gn.inputs["Plane"].default_value = plane
        if torus is not None:
            nb.link(torus[0], gn.inputs["TorusXYZ"])
            nb.link(torus[1], gn.inputs["TorusW"])
        if xf is not None:
            for nm, sk in zip(("NAngle", "NFlip", "NFlipY"), xf):
                nb.link(sk, gn.inputs[nm])
        if bd is not None:
            nb.link(bd, gn.inputs["BumpDist"])
        return {k: gn.outputs[k] for k in KEYS}

    proj = layer.projection
    use_at = bool(layer.anti_tile) and _has_images(mat) and (sh.tile or proj != 'ORIGINAL')
    lat_common = None
    if use_at:
        lat_common = {"amt": nb.value("L%d_at_off" % i, layer.at_offset)}
        b = nb.value("L%d_at_blend" % i, layer.at_blend)
        # くっきり度：境目の幅 0 → 指数 16（ほぼ切り替え）、1 → 指数 1（なめらか）
        sharp = nb.math('SUBTRACT', 1.0, b)
        lat_common["pw"] = nb.math('MULTIPLY_ADD', nb.math('MULTIPLY', sharp, sharp), 15.0, c=1.0)

    if sh.tile:
        # タイル素材：UV（0〜1）を整数回くり返し、ノイズ類は4Dドーナツ座標で継ぎ目なし
        k = tile_repeats(s, layer)
        kx = max(1, int(round(k * layer.place_sx)))
        ky = max(1, int(round(k * layer.place_sy)))
        pl = _Place(nb, sh, i, layer, tile=True)
        u2, v2, tu, tv = pl.tile_uv(sh.paint_uv(), kx, ky)
        xyz, w = sh.torus2(kx / (2.0 * math.pi), ky / (2.0 * math.pi), u2, v2)
        g = group('T', PAINT_UV)
        if not use_at:
            return inst(g, nb.comb_xyz(tu, tv, 0.0), 2.0, "", torus=(xyz, w), xf=pl.xf())
        nu = max(1, int(round(kx / max(layer.at_cell, 0.05))))
        nv = max(1, int(round(ky / max(layer.at_cell, 0.05))))
        lat = dict(lat_common, tile=True, u2=u2, v2=v2, nu=nu, nv=nv, a=nu,
                   b=max(2, 2 * int(round(nu * 2.0 / math.sqrt(3.0) / 2.0))))
        return _anti_tile(nb, sh, i, layer, pl, tu, tv, 0.0, 0.0, "",  lat,
                          lambda vec, plane, tag, xf: inst(g, vec, 2.0, tag, torus=(xyz, w), xf=xf))

    if proj == 'ORIGINAL':
        return inst(group('O'), None, 2.0, "")
    lat = dict(lat_common, tile=False, cell=nb.value("L%d_at_cell" % i, max(layer.at_cell, 0.05))) if use_at else None
    pl = _Place(nb, sh, i, layer)
    if proj == 'UV':
        uvname = layer.uv_map if obj.data.uv_layers.get(layer.uv_map) is not None else _default_uv(obj)
        uvn = nb.node("ShaderNodeUVMap")
        uvn.uv_map = uvname
        sc = nb.node("ShaderNodeVectorMath", "L%d_tile" % i)
        sc.operation = 'SCALE'
        sc.hide = True
        nb.L.new(uvn.outputs["UV"], sc.inputs[0])
        sc.inputs["Scale"].default_value = layer.uv_repeat
        g = group('U', uvname)
        u, v, z = nb.sep_xyz(sc.outputs[0])
        tu, tv = pl.uv(u, v)
        if use_at:
            return _anti_tile(nb, sh, i, layer, pl, tu, tv, z, 2.0, "", lat,
                              lambda vec, plane, tag, xf: inst(g, vec, 2.0, tag, xf=xf))
        return inst(g, nb.comb_xyz(tu, tv, z), 2.0, "", xf=pl.xf())
    sc = nb.node("ShaderNodeVectorMath", "L%d_tile" % i)
    sc.operation = 'SCALE'
    sc.hide = True
    nb.L.new(sh.tc.outputs["Object"], sc.inputs[0])
    sc.inputs["Scale"].default_value = 1.0 / max(layer.tile_size, 1e-4)
    g = group('P')
    x, y, z = nb.sep_xyz(sc.outputs[0])
    if proj == 'TOP' or not _has_images(mat):
        tu, tv = pl.uv(x, y)
        if use_at:
            return _anti_tile(nb, sh, i, layer, pl, tu, tv, z, 2.0, "", lat,
                              lambda vec, plane, tag, xf: inst(g, vec, 2.0, tag, xf=xf))
        return inst(g, nb.comb_xyz(tu, tv, z), 2.0, "", xf=pl.xf())
    # トリプラナー：3方向から投影して法線の向きで混ぜる（X面は (y, z)、Y面は (x, z)、Z面は (x, y) を平面の座標にする）
    outs = []
    for (a, b_, c_, plane, tg) in ((y, z, x, 0.0, "X"), (x, z, y, 1.0, "Y"), (x, y, z, 2.0, "Z")):
        tu, tv = pl.uv(a, b_)
        if use_at:
            outs.append(_anti_tile(nb, sh, i, layer, pl, tu, tv, c_, plane, tg, lat,
                                   lambda vec, pln, tag, xf: inst(g, vec, pln, tag, xf=xf)))
        else:
            outs.append(inst(g, nb.comb_xyz(tu, tv, c_), plane, tg, xf=pl.xf()))
    k = nb.value("L%d_tri_k" % i, layer.tri_sharp)
    pw = nb.vmath('POWER', nb.vmath('ABSOLUTE', sh.tc.outputs["Normal"]), nb.comb_xyz(k, k, k))
    tot = nb.vmath('DOT_PRODUCT', pw, (1.0, 1.0, 1.0))
    wv = nb.vmath('SCALE', pw, scale=nb.math('DIVIDE', 1.0, nb.math('MAXIMUM', tot, 1e-6)))
    ws = nb.sep_xyz(wv)
    return _wsum(nb, outs, [ws[0], ws[1], ws[2]])


def tile_repeats(s, layer):
    """タイル素材で、このレイヤーのテクスチャを何回くり返すか（継ぎ目なしにするため整数）。"""
    return max(1, int(round(s.tile_size_m / max(layer.tile_size, 1e-4))))


def _light_sockets(nb, sh, i, layer):
    """塗っている間の簡易表示：各マテリアルを単色にする。"""
    if layer.material is None:
        rgb = nb.node("ShaderNodeRGB", "L%d_rgb" % i)
        rgb.outputs[0].default_value = (*layer.color, 1.0)
        rough = nb.value("L%d_rough" % i, layer.roughness)
        metal = nb.value("L%d_metal" % i, layer.metallic)
    else:
        if not layer.preview_set:
            layer.preview_color = guess_material_color(layer.material)
            layer.preview_set = True
        rgb = nb.node("ShaderNodeRGB", "L%d_prev" % i)
        rgb.outputs[0].default_value = (*layer.preview_color, 1.0)
        rough = 0.7
        metal = 0.0
    return {"Color": rgb.outputs[0], "Roughness": rough, "Metallic": metal, "Normal": sh.geo_n,
            "Height": 0.5}


def _adjust_mask(nb, w, soft):
    half = nb.math('MULTIPLY', soft, 0.5)
    return nb.smooth(w, nb.math('SUBTRACT', 0.5, half), nb.math('ADD', 0.5, half))


def _apply_adjust(nb, i, layer, prev, mask):
    out = dict(prev)
    t = layer.adj_type
    if t == 'HSV':
        hs = nb.node("ShaderNodeHueSaturation")
        nb.L.new(nb.math('ADD', nb.value("L%d_adj_hue" % i, layer.adj_hue), 0.5), hs.inputs["Hue"])
        nb.L.new(nb.value("L%d_adj_sat" % i, layer.adj_sat), hs.inputs["Saturation"])
        nb.L.new(nb.value("L%d_adj_val" % i, layer.adj_val), hs.inputs["Value"])
        nb.L.new(mask, hs.inputs["Fac"])
        nb.link(prev["Color"], hs.inputs["Color"])
        out["Color"] = hs.outputs["Color"]
    elif t == 'ROUGH':
        out["Roughness"] = nb.mix('FLOAT', mask, prev["Roughness"], nb.value("L%d_adj_rough" % i, layer.adj_rough))
    elif t == 'METAL':
        out["Metallic"] = nb.mix('FLOAT', mask, prev["Metallic"], nb.value("L%d_adj_metal" % i, layer.adj_metal))
    elif t == 'WET':
        keep = nb.math('SUBTRACT', 1.0, nb.value("L%d_wet_dark" % i, layer.wet_dark))
        out["Color"] = nb.mix('RGBA', mask, prev["Color"], keep, blend='MULTIPLY')
        out["Roughness"] = nb.mix('FLOAT', mask, prev["Roughness"], nb.value("L%d_wet_rough" % i, layer.wet_rough))
        geo = nb.node("ShaderNodeNewGeometry")
        flat = nb.math('MULTIPLY', mask, nb.value("L%d_wet_flat" % i, layer.wet_flat))
        out["Normal"] = nb.vmath('NORMALIZE', nb.mix('VECTOR', flat, prev["Normal"], geo.outputs["Normal"]))
        if "Relief" in prev:
            out["Relief"] = nb.mix('FLOAT', flat, prev["Relief"], 0.5)      # 濡れた所は凹凸も平らに
    elif t == 'TINT':
        rgb = nb.node("ShaderNodeRGB", "L%d_tint_color" % i)
        rgb.outputs[0].default_value = (*layer.tint_color, 1.0)
        out["Color"] = nb.mix('RGBA', mask, prev["Color"], rgb.outputs[0], blend=layer.tint_blend)
    return out


def _stack_bias(layer):
    """重ね方（上から積もる＝+、下から出る＝−）に量を掛けたもの。"""
    return layer.stack_amount * {'SETTLE': 1.0, 'EMERGE': -1.0}.get(layer.stack_mode, 0.0)


def _macro_variation(nb, sh, s, prev):
    """全体の「大きなムラ」：数 m 単位のノイズで、最終の明るさ・色相・ラフネスをわずかに揺らす。
    タイリング感（同じ模様の繰り返し）を目立たなくする。macro_amount = 0 なら何も作らない。"""
    if s.macro_amount <= 0.0:
        return
    nz = nb.node("ShaderNodeTexNoise", "G_macro_noise")
    nz.hide = True
    nz.inputs["Detail"].default_value = 2.0
    nz.inputs["Roughness"].default_value = 0.5
    if sh.tile:
        # タイル素材：タイル1枚にちょうど整数個のムラが入るようにして、継ぎ目なしにする
        n = max(1, int(round(s.tile_size_m / max(s.macro_scale, 0.01))))
        nz.noise_dimensions = '4D'
        xyz, w = sh.torus(n / (2.0 * math.pi))
        nb.L.new(xyz, nz.inputs["Vector"])
        nb.L.new(w, nz.inputs["W"])
        nz.inputs["Scale"].default_value = 1.0
    else:
        nb.L.new(sh.tc.outputs["Object"], nz.inputs["Vector"])
        nb.L.new(nb.value("G_macro_scale", 1.0 / max(s.macro_scale, 0.01)), nz.inputs["Scale"])
    sep = nb.node("ShaderNodeSeparateColor")
    sep.hide = True
    nb.L.new(nz.outputs["Color"], sep.inputs[0])
    amt = nb.value("G_macro_amt", s.macro_amount)
    d = [nb.math('MULTIPLY', nb.math('SUBTRACT', c, 0.5), 2.0) for c in sep.outputs]
    hs = nb.node("ShaderNodeHueSaturation")
    hs.hide = True
    nb.L.new(nb.math('MULTIPLY_ADD', d[1], nb.math('MULTIPLY', amt, 0.1), c=0.5), hs.inputs["Hue"])
    nb.L.new(nb.math('MULTIPLY_ADD', d[0], amt, c=1.0), hs.inputs["Value"])
    nb.link(prev["Color"], hs.inputs["Color"])
    prev["Color"] = hs.outputs["Color"]
    prev["Roughness"] = nb.math('ADD', prev["Roughness"], nb.math('MULTIPLY', d[2], nb.math('MULTIPLY', amt, 0.5)),
                                clamp=True)


def _want_light(obj):
    return obj.cmx.light_paint and obj.mode == 'TEXTURE_PAINT'


def _is_light(obj):
    m = obj.cmx.material
    return m is not None and bool(m.get("cmx_light", False))


def _needs_mask_data(s):
    for l in s.layers:
        if l.enabled and l.use_auto:
            for mk in l.masks:
                if mk.enabled and mk.mtype in MASK_NEEDS_DATA:
                    return True
    return False


def sync_mask_modifier(obj):
    s = obj.cmx
    md = obj.modifiers.get(SM_MOD)
    want = s.enabled and not s.use_baked and s.mask_source == 'LIVE' and _needs_mask_data(s)
    if want:
        ensure_mask_modifier(obj, s.edge_radius, s.edge_spread, s.ao_distance)
    elif md is not None and (md.show_viewport or md.show_render):
        md.show_viewport = False
        md.show_render = False


def assign_slots(obj):
    """全スロットに、ミキサーのマテリアルか焼き込んだ軽いマテリアルを入れる。"""
    s = obj.cmx
    mat = s.baked_material if (s.use_baked and s.baked_material is not None) else s.material
    if mat is None:
        return
    if len(obj.material_slots) == 0:
        obj.data.materials.append(mat)
    else:
        for slot in obj.material_slots:
            if slot.material != mat:
                slot.material = mat


def rebuild_material(obj, light=None):
    s = obj.cmx
    if light is None:
        light = _want_light(obj)
    mat = s.material
    if mat is None:
        mat = bpy.data.materials.new("CMX_%s" % obj.name)
        mat.use_nodes = True
        s.material = mat
    mat["cmx_mixer"] = True
    nt = mat.node_tree
    nt.nodes.clear()
    nb = _NB(nt)
    hl = ensure_heightlerp()
    layers = list(s.layers)
    n_layers = len(layers)
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (n_layers * 700 + 600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (n_layers * 700 + 250, 0)
    sh = _Shared(nb, obj, s)
    view = None
    ai = s.active_index
    if s.view_mode in ('LAYER_MASK', 'ITEM_MASK') and 0 <= ai < n_layers:
        view = {"layer": ai, "sock": None, "item": None}
        if s.view_mode == 'ITEM_MASK' and 0 <= layers[ai].active_mask < len(layers[ai].masks):
            view["item"] = (ai, layers[ai].active_mask)
    cache = {}
    prev = None
    for i, layer in enumerate(layers):
        if i > 0 and not layer.enabled:
            continue
        nb.x, nb.y = i * 700, 800
        dbg_col = (*DEBUG_COLORS[i % len(DEBUG_COLORS)], 1.0)
        is_mat = i == 0 or layer.kind == 'MATERIAL'
        if is_mat:
            outs = _light_sockets(nb, sh, i, layer) if light else _material_sockets(nb, sh, i, layer, cache)
            inv = nb.value("L%d_hinv" % i, 1.0 if layer.height_invert else 0.0)
            hraw = nb.math('MULTIPLY_ADD', outs["Height"], nb.math('SUBTRACT', 1.0, nb.math('MULTIPLY', inv, 2.0)), c=inv)
            outs["Height"] = nb.math('MULTIPLY_ADD', hraw, layer.height_contrast, clamp=True,
                                     name="L%d_h" % i, c=layer.height_offset)
            # 凹凸の高さ（全体の凹凸・ディスプレイスメント・ハイトの書き出し用）。強さ 1 なら「ハイト」そのまま
            hs_l = nb.value("L%d_hs" % i, layer.height_strength_layer)
            outs["Relief"] = nb.math('MULTIPLY_ADD', outs["Height"], hs_l,
                                     c=nb.math('SUBTRACT', 0.5, nb.math('MULTIPLY', hs_l, 0.5)))
            outs["Color"] = nb.mix('RGBA', sh.g_dbg, outs["Color"], dbg_col)
        if i == 0 or prev is None:
            prev = outs if is_mat else prev
            if view is not None and view["layer"] == i:
                view["sock"] = 1.0
            continue
        auto = _auto_mask(nb, sh, i, layer, prev, view)
        img = ensure_layer_image(obj, layer)
        noise_ch = sh.noise[(i + 1) % 3]
        if (i // 3) % 2:
            noise_ch = nb.math('SUBTRACT', 1.0, noise_ch)
        w, soft = _paint_mix(nb, sh, i, layer, img, auto, noise_ch)
        if is_mat:
            b_in = dict(outs)
            relief_b = outs["Relief"]
            if layer.stack_mode != 'NORMAL':
                # 上から積もる（下の低い所に出やすい）／下から出る（下の高い所に出やすい）：重ねるときのハイトを偏らせる
                sb = nb.value("L%d_stack" % i, _stack_bias(layer))
                dh = nb.math('MULTIPLY', nb.math('SUBTRACT', 0.5, prev["Height"]), sb)
                b_in["Height"] = nb.math('ADD', outs["Height"], dh, clamp=True)
            if layer.wrap_amount > 0.0:
                # 下の形に沿わせる：下の凹凸（ノーマル・高さ）を、このレイヤーにも足す
                wa = nb.value("L%d_wrap" % i, layer.wrap_amount)
                dn = nb.vmath('SCALE', nb.vmath('SUBTRACT', prev["Normal"], sh.geo_n), scale=wa)
                b_in["Normal"] = nb.vmath('NORMALIZE', nb.vmath('ADD', outs["Normal"], dn))
                relief_b = nb.math('MULTIPLY_ADD', nb.math('SUBTRACT', prev["Relief"], 0.5), wa, c=relief_b)
            hn = nb.node("ShaderNodeGroup", "L%d_blend" % i)
            hn.node_tree = hl
            for k in KEYS:
                nb.link(prev[k], hn.inputs["A " + k])
                nb.link(b_in[k], hn.inputs["B " + k])
            nb.L.new(w, hn.inputs["Weight"])
            nb.L.new(soft, hn.inputs["Softness"])
            nb.L.new(sh.g_hs, hn.inputs["Height Strength"])
            prev_relief = prev["Relief"]
            prev = {k: hn.outputs[k] for k in KEYS}
            prev["Relief"] = nb.mix('FLOAT', hn.outputs["Factor"], prev_relief, relief_b)
            vis = hn.outputs["Factor"]
        else:
            mask = _adjust_mask(nb, w, soft)
            prev = _apply_adjust(nb, i, layer, prev, mask)
            prev["Color"] = nb.mix('RGBA', nb.math('MULTIPLY', mask, sh.g_dbg), prev["Color"], dbg_col)
            vis = mask
        if view is not None and view["layer"] == i and view["item"] is None:
            view["sock"] = vis
    if prev is not None:
        nb.x, nb.y = n_layers * 700 + 50, 600
        if not light:
            _macro_variation(nb, sh, s, prev)
            if s.bump_strength > 0.0:
                # 全体の凹凸：最終の凹凸の高さから Bump を作り、最終のノーマルに重ねる（苔が岩の上に盛り上がって見える）
                bn = nb.node("ShaderNodeBump", "G_bump")
                nb.L.new(nb.value("G_bump_str", s.bump_strength), bn.inputs["Strength"])
                nb.L.new(nb.value("G_bump_dist", s.bump_distance), bn.inputs["Distance"])
                nb.L.new(prev["Relief"], bn.inputs["Height"])
                nb.link(prev["Normal"], bn.inputs["Normal"])
                prev["Normal"] = bn.outputs["Normal"]
        prev["Height"] = prev["Relief"]       # 書き出すハイトは「凹凸の高さ」（強さ 1・設定なしなら今までと同じ）
        # 焼き込み用の出口（OUT_Color など）を経由して BSDF につなぐ
        nb.x, nb.y = n_layers * 700 + 50, 300
        for k in KEYS:
            rr = nb.node("NodeReroute", "OUT_" + k)
            if not isinstance(prev[k], bpy.types.NodeSocket):      # 簡易表示でベースだけのとき、定数のことがある
                prev[k] = nb.value("OUT_const_" + k, prev[k])
            nb.link(prev[k], rr.inputs[0])
            prev[k] = rr.outputs[0]
        nb.link(prev["Color"], bsdf.inputs["Base Color"])
        nb.link(prev["Roughness"], bsdf.inputs["Roughness"])
        nb.link(prev["Metallic"], bsdf.inputs["Metallic"])
        # 長さ0の法線が EEVEE で白く飛ぶのを防ぐ（ごくわずかに面の向きを足してから正規化）
        safe_n = nb.vmath('NORMALIZE', nb.vmath('ADD', prev["Normal"], nb.vmath('SCALE', sh.geo_n, scale=0.001)))
        nb.link(safe_n, bsdf.inputs["Normal"])
    surf = bsdf.outputs[0]
    if view is not None and view["sock"] is not None:
        em = nt.nodes.new("ShaderNodeEmission")
        em.location = (n_layers * 700 + 250, 400)
        v = view["sock"]
        if isinstance(v, bpy.types.NodeSocket):
            nt.links.new(v, em.inputs["Color"])
        else:
            em.inputs["Color"].default_value = (v, v, v, 1.0)
        surf = em.outputs[0]
    nt.links.new(surf, out.inputs[0])
    assign_slots(obj)
    mat["cmx_light"] = bool(light)
    if light:
        _watch_obj(obj)
    sync_mask_modifier(obj)
    _purge_layer_groups()
    return mat


def _purge_layer_groups():
    """どこからも使われなくなった CMX_Layer:: グループを消す（設定を切り替えるたびに増えないように）。"""
    for g in list(bpy.data.node_groups):
        if g.name.startswith("CMX_Layer::") and g.users == 0:
            bpy.data.node_groups.remove(g)


# ------------------------------------------------------------------ lighter display while painting
_watch = set()


def _lower_subdiv(obj):
    s = obj.cmx
    if not s.paint_lower_subdiv or obj.get("cmx_subdiv_backup") is not None:
        return
    data = {}
    for md in obj.modifiers:
        if md.type in ('SUBSURF', 'MULTIRES') and md.levels > s.paint_subdiv_level:
            data[md.name] = md.levels
            md.levels = s.paint_subdiv_level
    if data:
        obj["cmx_subdiv_backup"] = data
        _watch_obj(obj)


def _restore_subdiv(obj):
    data = obj.get("cmx_subdiv_backup")
    if data is None:
        return
    for name in list(data.keys()):
        md = obj.modifiers.get(name)
        if md is not None:
            md.levels = int(data[name])
    del obj["cmx_subdiv_backup"]


def _restore_uv(obj):
    nm = obj.get("cmx_prev_uv")
    if nm is None:
        return
    uv = obj.data.uv_layers.get(nm)
    if uv is not None:
        obj.data.uv_layers.active = uv
    del obj["cmx_prev_uv"]


def _watch_obj(obj):
    """テクスチャペイントを抜けるまで見張り、抜けたら塗り中だけの変更を元に戻す。"""
    _watch.add(obj.name)
    if not bpy.app.timers.is_registered(_poll_restore):
        bpy.app.timers.register(_poll_restore, first_interval=0.5)


def _end_paint(obj):
    _restore_subdiv(obj)
    _restore_uv(obj)
    # 塗った画像をファイル内に保存（パック）しておく。これで読み直しが起きても塗りが消えない
    for l in obj.cmx.layers:
        img = l.image
        if img is not None and img.is_dirty:
            try:
                img.pack()
            except Exception as e:
                print("CMX: pack failed", img.name, e)
    if obj.cmx.enabled and _is_light(obj):
        rebuild_material(obj, light=False)
    _watch.discard(obj.name)


def _poll_restore():
    for name in list(_watch):
        obj = bpy.data.objects.get(name)
        if obj is None:
            _watch.discard(name)
            continue
        if obj.mode != 'TEXTURE_PAINT':
            try:
                _end_paint(obj)
            except Exception as e:
                print("CMX: end paint failed", e)
                _watch.discard(name)
    return 0.5 if _watch else None


# ------------------------------------------------------------------ property callbacks
def _layer_index(layer):
    obj = layer.id_data
    for i, l in enumerate(obj.cmx.layers):
        if l.as_pointer() == layer.as_pointer():
            return obj, i
    return obj, -1


def _mask_index(mask):
    obj = mask.id_data
    p = mask.as_pointer()
    for i, l in enumerate(obj.cmx.layers):
        for j, m in enumerate(l.masks):
            if m.as_pointer() == p:
                return obj, i, j
    return obj, -1, -1


def _node(obj, name):
    mat = obj.cmx.material
    if mat is not None and mat.node_tree is not None:
        return mat.node_tree.nodes.get(name)
    return None


def _set_val(obj, name, v):
    """名前の付いた Value ノードの値だけ書き換える（再コンパイルなし）。ノードがなければ False。"""
    n = _node(obj, name)
    if n is not None:
        n.outputs[0].default_value = v
        return True
    return False


def _upd_rebuild(self, context):
    obj = self.id_data
    if obj.cmx.enabled and not _SUSPEND[0]:
        rebuild_material(obj)


def _upd_material(self, context):
    self.preview_set = False
    _upd_rebuild(self, context)


def _upd_preview(self, context):
    obj, i = _layer_index(self)
    n = _node(obj, "L%d_prev" % i)
    if n is not None:
        n.outputs[0].default_value = (*self.preview_color, 1.0)


def _upd_light(self, context):
    obj = self.id_data
    if self.enabled and obj.mode == 'TEXTURE_PAINT':
        rebuild_material(obj)


def _upd_tile(self, context):
    obj, i = _layer_index(self)
    if obj.cmx.tile_mode:
        if obj.cmx.enabled and not _SUSPEND[0]:
            rebuild_material(obj)      # くり返し回数が変わるとドーナツ座標も変わるので作り直す
        return
    n = _node(obj, "L%d_tile" % i)
    if n is not None:
        n.inputs["Scale"].default_value = (self.uv_repeat if self.projection == 'UV'
                                           else 1.0 / max(self.tile_size, 1e-4))
    _sync_bump(obj, i, self)


def _upd_tri(self, context):
    obj, i = _layer_index(self)
    _set_val(obj, "L%d_tri_k" % i, self.tri_sharp)


def _upd_height(self, context):
    obj, i = _layer_index(self)
    n = _node(obj, "L%d_h" % i)
    if n is not None:
        n.inputs[1].default_value = self.height_contrast
        n.inputs[2].default_value = self.height_offset


def _live(obj, pairs):
    """Value ノードの値だけ書き換える。1つでもノードが見つからなければ（まだ作られていない）作り直す。"""
    if not obj.cmx.enabled or _SUSPEND[0]:
        return
    ok = True
    for name, v in pairs:
        if not _set_val(obj, name, v):
            ok = False
    if not ok:
        rebuild_material(obj)


def _layer_live_ok(obj, layer):
    """この設定が、今のマテリアルのノードに出ているか。単色・簡易表示中・非表示のレイヤーでは何も作られない。"""
    return (layer.kind == 'MATERIAL' and layer.material is not None and not _is_light(obj) and
            (layer.enabled or obj.cmx.layers[0].as_pointer() == layer.as_pointer()))


def _sync_bump(obj, i, layer):
    _set_val(obj, "L%d_bumpd" % i, _bump_dist(obj.cmx, layer))


def _upd_place(self, context):
    """配置（位置ずらし・回転・X/Y別々の繰り返し）。タイル素材は整数回のくり返しなので作り直す。"""
    obj, i = _layer_index(self)
    s = obj.cmx
    if i < 0 or not s.enabled or _SUSPEND[0] or not _layer_live_ok(obj, self):
        return
    if s.tile_mode:
        rebuild_material(obj)
        return
    if self.projection == 'ORIGINAL':
        return
    _live(obj, [("L%d_pl_rot" % i, _eff_rot(s, self)), ("L%d_pl_x" % i, self.place_x),
                ("L%d_pl_y" % i, self.place_y), ("L%d_pl_sx" % i, self.place_sx),
                ("L%d_pl_sy" % i, self.place_sy)])


def _upd_relief(self, context):
    """ノーマルの強さ・ハイトの強さ・ハイトの反転・DirectX 形式・重ね方の量（Value ノードだけ書き換え）。"""
    obj, i = _layer_index(self)
    if i < 0 or not obj.cmx.enabled or _SUSPEND[0] or _is_light(obj) or self.kind != 'MATERIAL':
        return
    if not (self.enabled or i == 0):
        return
    pairs = [("L%d_hinv" % i, 1.0 if self.height_invert else 0.0), ("L%d_hs" % i, self.height_strength_layer)]
    if self.material is not None:
        pairs += [("L%d_nstr" % i, self.normal_strength), ("L%d_nfy" % i, -1.0 if self.normal_flip_y else 1.0)]
    if self.normal_from_height and self.material is not None:
        pairs.append(("L%d_bumpd" % i, _bump_dist(obj.cmx, self)))
    if self.stack_mode != 'NORMAL' and i > 0:
        pairs.append(("L%d_stack" % i, _stack_bias(self)))
    if self.wrap_amount > 0.0 and i > 0:
        pairs.append(("L%d_wrap" % i, self.wrap_amount))
    _live(obj, pairs)


def _upd_at_live(self, context):
    """タイリング崩しの、位置ずらし・境目の幅（Value ノードだけ書き換え）。"""
    obj, i = _layer_index(self)
    if i < 0 or not obj.cmx.enabled or _SUSPEND[0] or not _layer_live_ok(obj, self) or not self.anti_tile:
        return
    _live(obj, [("L%d_at_off" % i, self.at_offset), ("L%d_at_blend" % i, self.at_blend)])


def _upd_at_cell(self, context):
    """セルの大きさ。タイル素材のセルは整数個なので作り直す。"""
    obj, i = _layer_index(self)
    s = obj.cmx
    if i < 0 or not s.enabled or _SUSPEND[0] or not _layer_live_ok(obj, self) or not self.anti_tile:
        return
    if s.tile_mode:
        rebuild_material(obj)
        return
    _live(obj, [("L%d_at_cell" % i, max(self.at_cell, 0.05))])


def _upd_struct(self, context):
    """ノードの作りが変わる設定（タイリング崩しの入り切り・形・回転、ハイトから凹凸、重ね方の種類など）。"""
    if self.id_data.cmx.enabled and not _SUSPEND[0]:
        rebuild_material(self.id_data)


def _upd_macro(self, context):
    obj = self.id_data
    if not self.enabled or _SUSPEND[0]:
        return
    if self.macro_amount <= 0.0:
        if _node(obj, "G_macro_noise") is not None:
            rebuild_material(obj)
        return
    if self.tile_mode:
        rebuild_material(obj)       # タイル素材は、1枚に入るムラの数（整数）が変わるので作り直す
        return
    _live(obj, [("G_macro_amt", self.macro_amount), ("G_macro_scale", 1.0 / max(self.macro_scale, 0.01))])


def _upd_bump(self, context):
    obj = self.id_data
    if not self.enabled or _SUSPEND[0]:
        return
    if self.bump_strength <= 0.0:
        if _node(obj, "G_bump") is not None:
            rebuild_material(obj)       # 0 に戻したら Bump ノードごと外す（重いので）
        return
    _live(obj, [("G_bump_str", self.bump_strength), ("G_bump_dist", self.bump_distance)])


def _upd_disp(self, context):
    md = self.id_data.modifiers.get(DISP_MOD)
    if md is not None:
        md.strength = self.disp_strength
        md.mid_level = self.disp_mid


def _upd_opacity(self, context):
    obj, i = _layer_index(self)
    n = _node(obj, "L%d_opacity" % i)
    if n is not None:
        n.inputs[1].default_value = self.opacity


def _upd_soft(self, context):
    obj, i = _layer_index(self)
    _set_val(obj, "L%d_soft" % i, self.softness)


def _upd_color(self, context):
    obj, i = _layer_index(self)
    n = _node(obj, "L%d_rgb" % i)
    if n is not None:
        n.outputs[0].default_value = (*self.color, 1.0)
    _set_val(obj, "L%d_rough" % i, self.roughness)
    _set_val(obj, "L%d_metal" % i, self.metallic)


def _upd_param(self, context):
    obj, i = _layer_index(self)
    for p in ADJ_PARAMS:
        n = _node(obj, "L%d_%s" % (i, p))
        if n is None:
            continue
        v = getattr(self, p)
        n.outputs[0].default_value = (*v, 1.0) if p == "tint_color" else v


def _upd_mask_param(self, context):
    obj, i, j = _mask_index(self)
    if i < 0:
        return
    p = "L%d_M%d_" % (i, j)
    for nm, v in (("amount", self.amount), ("contrast", self.contrast), ("inv", 1.0 if self.invert else 0.0),
                  ("noise", self.noise), ("strength", self.strength), ("nscale", self.noise_scale),
                  ("ndetail", self.noise_detail)):
        _set_val(obj, p + nm, v)


def _upd_mask_type(self, context):
    if not _SUSPEND[0]:
        a, c = MASK_DEFAULTS.get(self.mtype, (0.5, 0.5))
        _SUSPEND[0] += 1
        try:
            self.amount = a
            self.contrast = c
        finally:
            _SUSPEND[0] -= 1
    _upd_rebuild(self, context)


def _upd_global(self, context):
    obj = self.id_data
    _set_val(obj, "G_hs", self.height_strength)
    _set_val(obj, "G_noise_scale", self.noise_scale)


def _upd_view(self, context):
    obj = self.id_data
    if self.enabled:
        rebuild_material(obj)


def _upd_mask_mod(self, context):
    obj = self.id_data
    md = obj.modifiers.get(SM_MOD)
    if md is not None and md.node_group is not None:
        ids = _mod_inputs(md)
        for nm, v in (("Edge Radius", self.edge_radius), ("Spread", self.edge_spread),
                      ("AO Distance", self.ao_distance)):
            if nm in ids:
                md[ids[nm]] = v
        obj.update_tag()


def _brush_targets(s):
    return ((1.0, s.brush_softness, s.brush_noise), (0.0, s.brush_softness, s.brush_noise), (0.5, 0.0, 0.0))


def current_paint_state(context):
    r = context.scene.tool_settings.unified_paint_settings.color[0]
    return 'ADD' if r > 0.75 else ('ERASE' if r < 0.25 else 'RESET')


def _apply_brush_colors(context, s, mode=None):
    ups = context.scene.tool_settings.unified_paint_settings
    ups.use_unified_color = True
    add, erase, reset = _brush_targets(s)
    if mode is None:
        mode = current_paint_state(context)
    if mode == 'ADD':
        fg, bg = add, erase
    elif mode == 'ERASE':
        fg, bg = erase, add
    else:
        fg, bg = reset, add
    ups.color = fg
    ups.secondary_color = bg


def _set_canvas(context, img, s, mode=None):
    ip = context.scene.tool_settings.image_paint
    ip.mode = 'IMAGE'
    ip.canvas = img
    _apply_brush_colors(context, s, mode)


def _upd_brush(self, context):
    obj = self.id_data
    if obj.mode == 'TEXTURE_PAINT':
        _apply_brush_colors(context, self)


def _upd_active(self, context):
    obj = self.id_data
    i = self.active_index
    if obj.mode == 'TEXTURE_PAINT' and 0 < i < len(self.layers):
        img = ensure_layer_image(obj, self.layers[i])
        _set_canvas(context, img, self)
    if self.enabled and self.view_mode in ('LAYER_MASK', 'ITEM_MASK'):
        rebuild_material(obj)


def _upd_active_mask(self, context):
    obj = self.id_data
    if obj.cmx.enabled and obj.cmx.view_mode == 'ITEM_MASK':
        rebuild_material(obj)


def _upd_res(self, context):
    res = int(self.resolution)
    for l in self.layers:
        if l.image is not None and tuple(l.image.size) != (res, res):
            l.image.scale(res, res)


def _upd_subdiv_level(self, context):
    obj = self.id_data
    data = obj.get("cmx_subdiv_backup")
    if data is None:
        return
    for name in list(data.keys()):
        md = obj.modifiers.get(name)
        if md is not None:
            md.levels = min(int(data[name]), self.paint_subdiv_level)


def _mat_poll(self, m):
    return not _is_mixer_material(m)


TILE_PREVIEW_MODS = ("CMX_TilePreview_X", "CMX_TilePreview_Y")


def _set_tile_geometry(obj, size):
    me = obj.data
    h = size / 2.0
    if len(me.vertices) == 4:
        co = [(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)]
        for v, c in zip(me.vertices, co):
            v.co = c
        me.update()
    uv = me.uv_layers.get(PAINT_UV)
    if uv is not None:
        for poly in me.polygons:
            for li in poly.loop_indices:
                c = me.vertices[me.loops[li].vertex_index].co
                uv.data[li].uv = (c.x / size + 0.5, c.y / size + 0.5)


def _upd_tile_size(self, context):
    obj = self.id_data
    if not self.tile_mode or _SUSPEND[0]:
        return
    _set_tile_geometry(obj, self.tile_size_m)
    if self.enabled:
        rebuild_material(obj)


def _upd_tile_preview(self, context):
    obj = self.id_data
    for axis, nm in enumerate(TILE_PREVIEW_MODS):
        md = obj.modifiers.get(nm)
        if self.tile_preview:
            if md is None:
                md = obj.modifiers.new(nm, 'ARRAY')
                md.count = 3
                md.use_relative_offset = True
                md.relative_offset_displace = (1.0, 0.0, 0.0) if axis == 0 else (0.0, 1.0, 0.0)
                md.show_expanded = False
            md.show_viewport = True
            md.show_render = False
        elif md is not None:
            obj.modifiers.remove(md)
    if self.enabled:
        sync_mask_modifier(obj)


def _upd_use_baked(self, context):
    obj = self.id_data
    if not self.enabled or _SUSPEND[0]:
        return
    if self.use_baked and self.baked_material is None:
        return
    assign_slots(obj)
    sync_mask_modifier(obj)


def _bake_uv_items(self, context):
    obj = self.id_data
    items = [("", "%s（塗り用・おすすめ）" % PAINT_UV, "重なりのない塗り用UV")]
    try:
        for u in obj.data.uv_layers:
            if u.name != PAINT_UV:
                items.append((u.name, u.name, "物のUV（重なっていると正しく焼けません）"))
    except Exception:
        pass
    _ENUM_KEEP["bake_uv"] = items   # 動的な項目は参照を持っておかないと文字化けする
    return items


def _uv_items(self, context):
    obj = self.id_data
    items = [("", "（既定のUV）", "物のレンダー用UVを使う")]
    try:
        for u in obj.data.uv_layers:
            items.append((u.name, u.name, ""))
    except Exception:
        pass
    _ENUM_KEEP["uv"] = items
    return items


# ------------------------------------------------------------------ properties
class CMX_Mask(bpy.types.PropertyGroup):
    mtype: EnumProperty(name="種類", items=MASK_TYPES, default='EDGE', update=_upd_mask_type)
    enabled: BoolProperty(name="使う", default=True, update=_upd_rebuild)
    blend: EnumProperty(name="重ね方", items=MASK_BLENDS, default='MULTIPLY', update=_upd_rebuild,
                        description="上のマスクとの重ね方")
    amount: FloatProperty(name="範囲", default=0.4, min=0.0, max=1.0, subtype='FACTOR', update=_upd_mask_param,
                          description="大きいほど広く出る")
    contrast: FloatProperty(name="くっきり", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                            update=_upd_mask_param, description="大きいほど境界がくっきり、小さいほどぼんやり")
    invert: BoolProperty(name="反転", default=False, update=_upd_mask_param,
                         description="出る所と出ない所を入れ替える")
    noise: FloatProperty(name="ノイズで崩す", default=0.3, min=0.0, max=1.0, subtype='FACTOR',
                         update=_upd_mask_param, description="境界をノイズで崩す強さ")
    strength: FloatProperty(name="強さ", default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd_mask_param,
                            description="このマスクの効き具合（0で無効と同じ）")
    use_detail_normal: BoolProperty(name="下のレイヤーの凹凸も使う", default=True, update=_upd_rebuild,
                                    description="下のマテリアルの細かい凹凸の向きも見て判定する（積もり方が自然になる）")
    noise_scale: FloatProperty(name="模様の細かさ", default=2.0, min=0.001, soft_max=50.0, update=_upd_mask_param,
                               description="1mあたりの模様の数")
    noise_detail: FloatProperty(name="模様の細部", default=4.0, min=0.0, max=15.0, update=_upd_mask_param)


class CMX_Layer(bpy.types.PropertyGroup):
    name: StringProperty(name="名前", default="レイヤー")
    uid: StringProperty()
    enabled: BoolProperty(name="表示", default=True, update=_upd_rebuild, description="このレイヤーを表示する")
    kind: EnumProperty(name="種類", default='MATERIAL', update=_upd_rebuild, items=[
        ('MATERIAL', "マテリアル", "マテリアル（または単色）を重ねるレイヤー"),
        ('ADJUST', "調整", "マスクの所の明るさやラフネスなどを変えるレイヤー")])
    material: PointerProperty(type=bpy.types.Material, name="マテリアル", poll=_mat_poll, update=_upd_material,
                              description="このレイヤーに使うマテリアル。空なら単色")
    preview_color: FloatVectorProperty(name="簡易表示の色", subtype='COLOR', size=3, min=0.0, max=1.0,
                                       default=(0.5, 0.5, 0.5), update=_upd_preview,
                                       description="塗っている間の簡易表示の色（マテリアルから自動で推定。変更もできる）")
    preview_set: BoolProperty(default=False)
    projection: EnumProperty(name="投影", items=PROJECTIONS, default='TRIPLANAR', update=_upd_rebuild)
    uv_map: EnumProperty(name="UVマップ", items=_uv_items, update=_upd_rebuild)
    uv_repeat: FloatProperty(name="繰り返し", default=1.0, min=0.001, soft_max=50.0, update=_upd_tile,
                             description="UVの中でテクスチャを繰り返す回数")
    tile_size: FloatProperty(name="タイルサイズ (m)", default=1.0, min=0.001, soft_max=20.0, update=_upd_tile,
                             description="テクスチャ1枚の大きさ")
    tri_sharp: FloatProperty(name="つなぎ目の鋭さ", default=4.0, min=1.0, max=32.0, update=_upd_tri,
                             description="3方向の投影が切り替わる所の幅。大きいほど切り替わりが狭い")
    height_contrast: FloatProperty(name="高さコントラスト", default=1.0, min=0.0, soft_max=4.0, update=_upd_height,
                                   description="レイヤー境界での凹凸の効き方")
    height_offset: FloatProperty(name="高さオフセット", default=0.0, soft_min=-1.0, soft_max=1.0, update=_upd_height,
                                 description="プラスにすると下のレイヤーの上に出やすくなる")
    opacity: FloatProperty(name="不透明度", default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd_opacity)
    softness: FloatProperty(name="境界のやわらかさ", default=0.25, min=0.0, max=1.0, subtype='FACTOR',
                            update=_upd_soft,
                            description="自動マスクの境界。小さいほど凹凸に沿ってくっきり、大きいほどぼんやり")
    color: FloatVectorProperty(name="色", subtype='COLOR', size=3, min=0.0, max=1.0,
                               default=(0.5, 0.5, 0.5), update=_upd_color)
    roughness: FloatProperty(name="ラフネス", default=0.8, min=0.0, max=1.0, subtype='FACTOR', update=_upd_color)
    metallic: FloatProperty(name="メタル", default=0.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd_color)
    image: PointerProperty(type=bpy.types.Image)
    use_auto: BoolProperty(name="自動マスクを使う", default=True, update=_upd_rebuild,
                           description="オフにすると、塗った所（足す）だけに出る")
    masks: CollectionProperty(type=CMX_Mask)
    active_mask: IntProperty(default=0, update=_upd_active_mask)
    # 調整レイヤー
    adj_type: EnumProperty(name="調整の種類", items=ADJ_TYPES, default='HSV', update=_upd_rebuild)
    adj_hue: FloatProperty(name="色相", default=0.0, min=-0.5, max=0.5, update=_upd_param)
    adj_sat: FloatProperty(name="彩度", default=1.0, min=0.0, soft_max=2.0, update=_upd_param)
    adj_val: FloatProperty(name="明るさ", default=0.7, min=0.0, soft_max=2.0, update=_upd_param)
    adj_rough: FloatProperty(name="ラフネス", default=0.2, min=0.0, max=1.0, subtype='FACTOR', update=_upd_param,
                             description="マスクの所のラフネス（0でツルツル、1でザラザラ）")
    adj_metal: FloatProperty(name="メタル", default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd_param)
    wet_dark: FloatProperty(name="暗さ", default=0.6, min=0.0, max=1.0, subtype='FACTOR', update=_upd_param,
                            description="濡れて色が暗くなる量")
    wet_rough: FloatProperty(name="濡れのラフネス", default=0.05, min=0.0, max=1.0, subtype='FACTOR',
                             update=_upd_param, description="小さいほどツヤツヤ")
    wet_flat: FloatProperty(name="平らにする", default=0.6, min=0.0, max=1.0, subtype='FACTOR', update=_upd_param,
                            description="濡れた所の凹凸を平らにする（水たまりっぽくなる）")
    tint_color: FloatVectorProperty(name="色", subtype='COLOR', size=3, min=0.0, max=1.0,
                                    default=(0.35, 0.18, 0.08), update=_upd_param)
    tint_blend: EnumProperty(name="重ね方", default='MIX', update=_upd_rebuild, items=[
        ('MIX', "通常", ""), ('MULTIPLY', "乗算", ""), ('OVERLAY', "オーバーレイ", ""),
        ('SCREEN', "スクリーン", ""), ('SOFT_LIGHT', "ソフトライト", "")])
    # 配置（Placement）
    place_x: FloatProperty(name="ずらし X", default=0.0, soft_min=-1.0, soft_max=1.0, update=_upd_place,
                           description="テクスチャを横にずらす量（テクスチャ1枚分 = 1.0）")
    place_y: FloatProperty(name="ずらし Y", default=0.0, soft_min=-1.0, soft_max=1.0, update=_upd_place,
                           description="テクスチャを縦にずらす量（テクスチャ1枚分 = 1.0）")
    place_rot: FloatProperty(name="回転", default=0.0, subtype='ANGLE', unit='ROTATION', update=_upd_place,
                             description="テクスチャを回す角度。凹凸（ノーマル）の向きも一緒に回ります")
    place_snap: BoolProperty(name="90°刻み", default=False, update=_upd_place,
                             description="回転を 90° ごとにそろえる（タイル素材では常にこうなります）")
    place_sx: FloatProperty(name="繰り返し X", default=1.0, min=0.01, soft_max=8.0, update=_upd_place,
                            description="横方向のくり返しの倍率。2 にするとテクスチャが横に半分の大きさになる")
    place_sy: FloatProperty(name="繰り返し Y", default=1.0, min=0.01, soft_max=8.0, update=_upd_place,
                            description="縦方向のくり返しの倍率。2 にするとテクスチャが縦に半分の大きさになる")
    # 凹凸（ノーマル・ハイト）
    normal_strength: FloatProperty(name="細かい凹凸（ノーマル）", default=1.0, min=0.0, max=2.0, subtype='FACTOR',
                                   update=_upd_relief,
                                   description="素材のノーマルマップの強さ。0で平ら、1で素材のまま、2で2倍")
    normal_flip_y: BoolProperty(name="DirectX 形式のノーマル", default=False, update=_upd_relief,
                                description="ノーマルマップの上下が逆（DirectX 形式の素材）なときにオン。凹凸が逆に見えたら試す")
    normal_from_height: BoolProperty(name="ハイトから凹凸を作る", default=False, update=_upd_struct,
                                     description="素材のハイト（高さ）から凹凸を作って足す。ノーマルマップのない素材向け。少し重くなります")
    height_strength_layer: FloatProperty(name="大きい凹凸（ハイト）", default=1.0, min=0.0, max=2.0, subtype='FACTOR',
                                         update=_upd_relief,
                                         description="このレイヤーのハイトの強さ。「全体の凹凸」やディスプレイスメント、ハイトの書き出しに効く。1で今まで通り")
    height_invert: BoolProperty(name="ハイトを反転", default=False, update=_upd_relief,
                                description="ハイト（高さ）を逆にする。レイヤーの境目の混ざり方と、凹凸の高さが逆になる")
    stack_mode: EnumProperty(name="重なり方", default='NORMAL', update=_upd_struct, items=[
        ('NORMAL', "標準", "下のレイヤーの形は気にせず重ねる"),
        ('SETTLE', "上から積もる", "下のレイヤーの低い所（くぼみ）に先にたまる。土・砂・水・苔向け"),
        ('EMERGE', "下から出る", "下のレイヤーの高い所（出っ張り）から先に顔を出す。摩耗・剥がれ向け")])
    stack_amount: FloatProperty(name="偏りの強さ", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                                update=_upd_relief, description="下のレイヤーの凹凸に沿って、どれだけ偏って出るか")
    wrap_amount: FloatProperty(name="下の形に沿わせる", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
                               update=_upd_relief,
                               description="下のレイヤーの凹凸（ノーマル・高さ）をこのレイヤーにも写す。苔が岩の形に沿って乗る感じ")
    # タイリング崩し（ランダムシート）
    anti_tile: BoolProperty(name="タイリング崩しを使う", default=False, update=_upd_struct,
                            description="同じテクスチャの繰り返しが目立たないよう、セルごとに位置・向きをランダムに変えて混ぜる。"
                                        "重くなります（トリプラナーでは最大12回サンプル）。簡易表示中は使われません")
    at_cell: FloatProperty(name="セルの大きさ", default=1.0, min=0.05, soft_max=8.0, update=_upd_at_cell,
                           description="ランダムに変える区画の大きさ（テクスチャ何枚分か）。小さいほど細かく入れ替わる")
    at_shape: EnumProperty(name="セルの形", default='HEX', update=_upd_struct, items=[
        ('SQUARE', "四角", "四角いセル（4つ混ぜる）"), ('HEX', "六角形", "六角形のセル（3つ混ぜる。軽くて境目が目立ちにくい）")])
    at_rot: EnumProperty(name="回転", default='STEP', update=_upd_struct, items=[
        ('NONE', "なし", "回さない"), ('STEP', "90°刻み", "セルごとに 90° ずつ回す"),
        ('FREE', "自由", "セルごとに好きな角度に回す（タイル素材では 90° 刻みになります）")])
    at_flip: BoolProperty(name="左右反転も混ぜる", default=True, update=_upd_struct,
                          description="セルごとに左右反転も混ぜる（凹凸の向きは自動で合わせます）")
    at_offset: FloatProperty(name="位置ずらし", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                             update=_upd_at_live, description="セルごとのずらし量。0でずらさない、1でテクスチャ1枚分まで自由にずらす")
    at_blend: FloatProperty(name="境目の幅", default=0.4, min=0.0, max=1.0, subtype='FACTOR',
                            update=_upd_at_live, description="セルの境目をぼかす幅。小さいとくっきり、大きいとなめらか")
    at_mix: EnumProperty(name="境目の混ぜ方", default='HEIGHT', update=_upd_struct, items=[
        ('HEIGHT', "ハイトで混ぜる", "素材の高い所が勝つように混ぜる（境目が自然）"),
        ('NOISE', "ノイズで崩す", "境目をノイズで崩す（ハイトのない素材向け）")])
    at_seed: IntProperty(name="シード", default=0, min=0, max=9999, update=_upd_struct,
                         description="ランダムの種。数字を変えると配置が変わる")


class CMX_MatRef(bpy.types.PropertyGroup):
    material: PointerProperty(type=bpy.types.Material)


class CMX_Settings(bpy.types.PropertyGroup):
    enabled: BoolProperty(default=False)
    initialized: BoolProperty(default=False)
    layers: CollectionProperty(type=CMX_Layer)
    active_index: IntProperty(default=0, update=_upd_active)
    material: PointerProperty(type=bpy.types.Material)
    orig_mats: CollectionProperty(type=CMX_MatRef)
    view_mode: EnumProperty(name="表示", default='NORMAL', update=_upd_view, items=[
        ('NORMAL', "仕上がり", "普通に表示"),
        ('LAYERS', "色分け", "レイヤーごとに色分けして表示"),
        ('LAYER_MASK', "レイヤーのマスク", "選んだレイヤーの最終的なマスクを白黒で表示"),
        ('ITEM_MASK', "選んだ自動マスク", "選んだ自動マスク1つだけを白黒で表示（調整用）")])
    height_strength: FloatProperty(name="高さの影響", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                                   update=_upd_global,
                                   description="レイヤーの境界を各マテリアルの凹凸で崩す強さ（やわらかい境界ほど自動で弱まる）")
    noise_scale: FloatProperty(name="崩しノイズの細かさ", default=2.0, min=0.01, soft_max=50.0,
                               update=_upd_global, description="境界を崩す共有ノイズの、1mあたりの細かさ")
    resolution: EnumProperty(name="塗りの解像度", default='1024', update=_upd_res, items=[
        ('512', "512", ""), ('1024', "1024", ""), ('2048', "2048", ""), ('4096', "4096", "")])
    # マスクの元データ
    mask_source: EnumProperty(name="元データ", default='LIVE', update=_upd_rebuild, items=[
        ('LIVE', "頂点（自動更新）", "頂点ごとに計算。形を変えても自動で更新。細かいメッシュ向け"),
        ('BAKED', "焼き込み（高品質）", "Cyclesで画像に焼く。ローポリの角でもきれい。形を変えたら焼き直す")])
    mask_image: PointerProperty(type=bpy.types.Image)
    edge_radius: FloatProperty(name="エッジ判定の大きさ (m)", default=0.03, min=0.0, soft_max=1.0, precision=3,
                               update=_upd_mask_mod, unit='LENGTH',
                               description="これより小さい丸みを「角」とみなす。大きくすると、ゆるい出っ張りもエッジになる")
    edge_spread: IntProperty(name="広がり", default=2, min=0, max=100, update=_upd_mask_mod,
                             description="エッジ・くぼみをまわりの頂点へ広げる回数")
    ao_distance: FloatProperty(name="AOの距離 (m)", default=0.3, min=0.0, soft_max=10.0, update=_upd_mask_mod,
                               unit='LENGTH', description="この距離までの遮蔽を見る")
    bake_res: EnumProperty(name="解像度", default='1024', items=[
        ('512', "512", ""), ('1024', "1024", ""), ('2048', "2048", ""), ('4096', "4096", "")])
    bake_edge: FloatProperty(name="エッジの幅 (m)", default=0.02, min=0.0001, soft_max=0.5, precision=3,
                             unit='LENGTH')
    bake_cavity: FloatProperty(name="くぼみの幅 (m)", default=0.03, min=0.0001, soft_max=0.5, precision=3,
                               unit='LENGTH')
    bake_ao: FloatProperty(name="AOの距離 (m)", default=0.3, min=0.001, soft_max=10.0, unit='LENGTH')
    bake_ao_others: BoolProperty(name="ほかの物の影も含める", default=False,
                                 description="AOにまわりの物（地面など）による遮蔽も含める")
    bake_samples: IntProperty(name="サンプル数", default=16, min=1, max=1024,
                              description="多いほどなめらか・遅い")
    # ブラシ（これから塗るストロークに入る）
    paint_mode: EnumProperty(name="塗り方", default='ADD', items=[
        ('ADD', "足す", "このレイヤーを足す"), ('ERASE', "消す", "このレイヤーを消す"),
        ('RESET', "自動に戻す", "塗りを消して自動マスクのままに戻す")])
    brush_softness: FloatProperty(name="境界のやわらかさ", default=0.15, min=0.0, max=1.0, subtype='FACTOR',
                                  update=_upd_brush,
                                  description="これから塗るストロークの境界。小さいほど凹凸に沿ってくっきり、大きいほどぼんやり")
    brush_noise: FloatProperty(name="境界のノイズ", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                               update=_upd_brush, description="これから塗るストロークの境界をノイズで崩す強さ")
    # 軽くする
    paint_lower_subdiv: BoolProperty(name="塗るときサブディビジョンを下げる", default=True,
                                     description="ペイント中だけサブディビジョン/マルチレゾの表示レベルを下げて軽くする")
    paint_subdiv_level: IntProperty(name="塗るときのレベル", default=0, min=0, max=6, update=_upd_subdiv_level,
                                    description="0が一番軽い。数字を上げるほど細かく重くなる")
    light_paint: BoolProperty(name="塗っている間は簡易表示", default=True, update=_upd_light,
                              description="ペイント中だけ各マテリアルを単色で表示して軽くする。塗り終了で元の見た目に戻る")
    # タイル素材
    tile_mode: BoolProperty(default=False)
    tile_size_m: FloatProperty(name="タイルの大きさ (m)", default=2.0, min=0.01, soft_max=50.0, unit='LENGTH',
                               update=_upd_tile_size,
                               description="継ぎ目なしテクスチャ1枚が表す実際の大きさ")
    tile_preview: BoolProperty(name="3×3に並べて継ぎ目を確認", default=False, update=_upd_tile_preview,
                               description="同じタイルを3×3に並べて表示し、継ぎ目が出ていないか確かめる（焼き込みには影響しません）")
    # 全体の大きなムラ・全体の凹凸・ディスプレイスメント
    macro_amount: FloatProperty(name="大きなムラ", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
                                update=_upd_macro,
                                description="数 m 単位のゆるいムラで、明るさ・色相・ラフネスをわずかに揺らす。繰り返し感を消すのに効く。0でなし")
    macro_scale: FloatProperty(name="ムラの大きさ (m)", default=3.0, min=0.05, soft_max=50.0, unit='LENGTH',
                               update=_upd_macro, description="ムラ1つぶんの大きさ")
    bump_strength: FloatProperty(name="全体の凹凸", default=0.0, min=0.0, soft_max=2.0, subtype='FACTOR',
                                 update=_upd_bump,
                                 description="レイヤーを重ねた最終のハイトから凹凸を作って足す（苔が岩の上に盛り上がって見える）。0でなし")
    bump_distance: FloatProperty(name="凹凸の高さ (m)", default=0.02, min=0.0001, soft_max=0.5, precision=3,
                                 unit='LENGTH', update=_upd_bump, description="ハイトが 0→1 のとき、実際にどれだけ高さが違うか")
    disp_strength: FloatProperty(name="動かす量 (m)", default=0.05, soft_min=-1.0, soft_max=1.0, precision=3,
                                 unit='LENGTH', update=_upd_disp,
                                 description="ハイトが 0→1 のとき、形をどれだけ動かすか（マイナスで逆向き）")
    disp_mid: FloatProperty(name="中心の値", default=0.5, min=0.0, max=1.0, subtype='FACTOR', update=_upd_disp,
                            description="この値のハイトでは形を動かさない。0.5なら、ハイトが高い所は外へ、低い所は内へ動く")
    # 焼き込み・書き出し
    use_baked: BoolProperty(name="焼き込んだ軽いマテリアルで表示", default=False, update=_upd_use_baked,
                            description="オンで焼き込んだテクスチャだけの軽いマテリアルに置き換える。オフでミキサーに戻る（設定はそのまま）")
    baked_material: PointerProperty(type=bpy.types.Material)
    tex_res: EnumProperty(name="解像度", default='2048', items=[
        ('512', "512", ""), ('1024', "1024", ""), ('2048', "2048", ""), ('4096', "4096", "")])
    bake_uv: EnumProperty(name="UVマップ", items=_bake_uv_items,
                          description="焼くテクスチャのUV。ほかのソフトで使うなら物のUV（重なりのないもの）を選ぶ")
    export_save: BoolProperty(name="ファイルに保存（PNG）", default=True,
                              description="オフにすると blend ファイルの中だけに入れる")
    export_dir: StringProperty(name="保存先", subtype='DIR_PATH', default="",
                               description="空なら blend の隣の textures（未保存ならドキュメント\\CMX_Textures）。物ごとにフォルダを作ります")
    last_export: StringProperty(default="")
    bake_ch_color: BoolProperty(name="ベースカラー", default=True)
    bake_ch_rough: BoolProperty(name="ラフネス", default=True)
    bake_ch_metal: BoolProperty(name="メタル", default=True)
    bake_ch_normal: BoolProperty(name="ノーマル", default=True)
    bake_ch_height: BoolProperty(name="ハイト", default=True)
    bake_ch_ao: BoolProperty(name="AO", default=True)


# ------------------------------------------------------------------ operators
def _mixer_obj(context):
    obj = context.active_object
    if obj is not None and obj.type == 'MESH' and obj.cmx.enabled:
        return obj
    return None


class _MixerOp:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None


def copy_props(src, dst, skip=()):
    """PropertyGroup の値をまるごと写す（コレクションも）。"""
    for p in src.bl_rna.properties:
        k = p.identifier
        if k in ("rna_type",) or k in skip:
            continue
        if p.type == 'COLLECTION':
            dc = getattr(dst, k)
            dc.clear()
            for it in getattr(src, k):
                copy_props(it, dc.add())
        elif p.type == 'POINTER':
            if p.is_readonly:
                copy_props(getattr(src, k), getattr(dst, k))
            else:
                setattr(dst, k, getattr(src, k))
        elif not p.is_readonly:
            try:
                setattr(dst, k, getattr(src, k))
            except Exception:
                pass


class CMX_OT_enable(bpy.types.Operator):
    bl_idname = "cmx.enable"
    bl_label = "マテリアルミキサーを始める"
    bl_description = "選択中のメッシュをマテリアルミキサー用にセットアップ（今のマテリアルがベースレイヤーになります）"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        err = enable_mixer(context, obj)
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


def enable_mixer(context, obj, build=True):
    """物をミキサー用にセットアップする。問題があればメッセージを返す。"""
    s = obj.cmx
    if obj.type != 'MESH' or len(obj.data.polygons) == 0:
        return "面のないメッシュには使えません"
    for k, slot in enumerate(obj.material_slots):
        m = slot.material
        if _is_mixer_material(m) or (m is not None and m.get("cmx_baked")):
            continue            # ミキサーが入れたマテリアルは「元のマテリアル」として覚えない
        while len(s.orig_mats) <= k:
            s.orig_mats.add()
        s.orig_mats[k].material = m
    try:
        ensure_paint_uv(context, obj)
    except Exception as e:
        return "塗り用UVを作れませんでした: %s" % e
    _SUSPEND[0] += 1
    try:
        if not s.initialized:
            d = _local_diag(obj)
            s.edge_radius = round(0.02 * d, 4)
            s.ao_distance = round(0.15 * d, 3)
            s.bake_edge = round(0.025 * d, 4)
            s.bake_cavity = round(0.03 * d, 4)
            s.bake_ao = round(0.15 * d, 3)
            s.noise_scale = round(max(0.2, 3.0 / d), 2)
            s.initialized = True
        if len(s.layers) == 0:
            orig = s.orig_mats[0].material if len(s.orig_mats) else None
            base = s.layers.add()
            base.uid = uuid.uuid4().hex[:8]
            base.name = "ベース"
            base.color = (0.5, 0.5, 0.5)
            base.material = orig
            base.projection = 'ORIGINAL' if orig is not None else 'TRIPLANAR'
        s.enabled = True
    finally:
        _SUSPEND[0] -= 1
    if build:
        rebuild_material(obj)
    return None


class CMX_OT_disable(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.disable"
    bl_label = "ミキサーを解除"
    bl_description = "元のマテリアルに戻す（レイヤーや塗りは残るので、もう一度始めれば戻せます）"

    def execute(self, context):
        obj = _mixer_obj(context)
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        _restore_subdiv(obj)
        _restore_uv(obj)
        s = obj.cmx
        s.enabled = False
        for k, slot in enumerate(obj.material_slots):
            slot.material = s.orig_mats[k].material if k < len(s.orig_mats) else None
        sync_mask_modifier(obj)
        return {'FINISHED'}


def _new_layer(s, kind, adj_type='HSV'):
    l = s.layers.add()
    l.uid = uuid.uuid4().hex[:8]
    l.kind = kind
    if kind == 'ADJUST':
        l.adj_type = adj_type
        l.name = ADJ_LABELS[adj_type]
    else:
        l.name = "レイヤー %d" % (len(s.layers) - 1)
        l.color = DEBUG_COLORS[(len(s.layers) - 1) % len(DEBUG_COLORS)]
    return l


def _new_mask(layer, mtype):
    mk = layer.masks.add()
    mk.mtype = mtype
    a, c = MASK_DEFAULTS.get(mtype, (0.5, 0.5))
    mk.amount = a
    mk.contrast = c
    return mk


class CMX_OT_layer_add(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.layer_add"
    bl_label = "レイヤーを追加"
    bl_description = "新しいレイヤーを一番上に追加"
    kind: EnumProperty(items=[('MATERIAL', "マテリアル", ""), ('ADJUST', "調整", "")], default='MATERIAL')
    adj_type: EnumProperty(items=ADJ_TYPES, default='HSV')

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        _SUSPEND[0] += 1
        try:
            _new_layer(s, self.kind, self.adj_type)
            s.active_index = len(s.layers) - 1
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_layer_remove(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.layer_remove"
    bl_label = "レイヤーを削除"

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0 or i >= len(s.layers):
            self.report({'WARNING'}, "ベースレイヤーは削除できません")
            return {'CANCELLED'}
        img = s.layers[i].image
        _SUSPEND[0] += 1
        try:
            s.layers.remove(i)
            s.active_index = min(i, len(s.layers) - 1)
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        if img is not None and img.users == 0:
            bpy.data.images.remove(img)
        return {'FINISHED'}


class CMX_OT_layer_move(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.layer_move"
    bl_label = "レイヤーを移動"
    direction: EnumProperty(items=[('UP', "上へ", ""), ('DOWN', "下へ", "")])

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        j = i + 1 if self.direction == 'UP' else i - 1
        if i <= 0 or j <= 0 or j >= len(s.layers):
            return {'CANCELLED'}
        _SUSPEND[0] += 1
        try:
            s.layers.move(i, j)
            s.active_index = j
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_layer_duplicate(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.layer_duplicate"
    bl_label = "レイヤーを複製"
    bl_description = "選んだレイヤーを設定・塗りごと複製"

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0 or i >= len(s.layers):
            return {'CANCELLED'}
        _SUSPEND[0] += 1
        try:
            src = s.layers[i]
            dst = s.layers.add()
            copy_props(src, dst)
            dst.uid = uuid.uuid4().hex[:8]
            dst.name = src.name + " コピー"
            if src.image is not None:
                img = src.image.copy()
                img.name = "CMX_%s_%s" % (obj.name, dst.uid)
                try:
                    img.pack()
                except Exception:
                    pass
                dst.image = img
            n = len(s.layers) - 1
            s.layers.move(n, i + 1)
            s.active_index = i + 1
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_mask_add(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.mask_add"
    bl_label = "自動マスクを追加"
    mtype: EnumProperty(items=MASK_TYPES, default='EDGE')

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0 or i >= len(s.layers):
            return {'CANCELLED'}
        layer = s.layers[i]
        _SUSPEND[0] += 1
        try:
            _new_mask(layer, self.mtype)
            layer.active_mask = len(layer.masks) - 1
            layer.use_auto = True
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_mask_remove(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.mask_remove"
    bl_label = "自動マスクを削除"

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0 or i >= len(s.layers):
            return {'CANCELLED'}
        layer = s.layers[i]
        j = layer.active_mask
        if not 0 <= j < len(layer.masks):
            return {'CANCELLED'}
        _SUSPEND[0] += 1
        try:
            layer.masks.remove(j)
            layer.active_mask = max(0, min(j, len(layer.masks) - 1))
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_mask_move(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.mask_move"
    bl_label = "自動マスクを移動"
    direction: EnumProperty(items=[('UP', "上へ", ""), ('DOWN', "下へ", "")])

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0 or i >= len(s.layers):
            return {'CANCELLED'}
        layer = s.layers[i]
        j = layer.active_mask
        k = j - 1 if self.direction == 'UP' else j + 1
        if not (0 <= j < len(layer.masks) and 0 <= k < len(layer.masks)):
            return {'CANCELLED'}
        _SUSPEND[0] += 1
        try:
            layer.masks.move(j, k)
            layer.active_mask = k
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_paint_layer(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.paint_layer"
    bl_label = "このレイヤーを塗る"
    bl_description = "テクスチャペイントモードに入り、選んだレイヤーに足す／消すを塗れる状態にする"

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0:
            self.report({'WARNING'}, "ベースは塗れません。上のレイヤーを選んでください")
            return {'CANCELLED'}
        img = ensure_layer_image(obj, s.layers[i])
        me = obj.data
        uv = me.uv_layers.get(PAINT_UV)
        if uv is None:
            try:
                uv = ensure_paint_uv(context, obj)
            except Exception as e:
                self.report({'ERROR'}, "塗り用UVを作れませんでした: %s" % e)
                return {'CANCELLED'}
        if me.uv_layers.active is not None and me.uv_layers.active.name != PAINT_UV and obj.get("cmx_prev_uv") is None:
            obj["cmx_prev_uv"] = me.uv_layers.active.name
        me.uv_layers.active = uv
        if obj.mode != 'TEXTURE_PAINT':
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        _lower_subdiv(obj)
        if s.light_paint and not _is_light(obj):
            rebuild_material(obj, light=True)
        _watch_obj(obj)
        _set_canvas(context, img, s, s.paint_mode)
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for sp in area.spaces:
                    if sp.type == 'VIEW_3D' and sp.shading.type in ('SOLID', 'WIREFRAME'):
                        sp.shading.type = 'MATERIAL'
        return {'FINISHED'}


class CMX_OT_paint_exit(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.paint_exit"
    bl_label = "塗り終了"
    bl_description = "オブジェクトモードに戻り、簡易表示やサブディビジョンを元に戻す"

    def execute(self, context):
        obj = _mixer_obj(context)
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        _end_paint(obj)
        return {'FINISHED'}


class CMX_OT_paint_mode(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.paint_mode"
    bl_label = "塗り方"
    bl_description = "これから塗るのを「足す・消す・自動に戻す」のどれにするか（Xキーで足す⇔消すを切り替え）"
    mode: EnumProperty(items=[('ADD', "足す", ""), ('ERASE', "消す", ""), ('RESET', "自動に戻す", "")])

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        s.paint_mode = self.mode
        _apply_brush_colors(context, s, self.mode)
        return {'FINISHED'}


class CMX_OT_fill(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.fill"
    bl_label = "全面"
    bl_description = "選んだレイヤーの塗りを全面まとめて変える"
    mode: EnumProperty(items=[('ADD', "全面に足す", "このレイヤーを全面に出す"),
                              ('ERASE', "全面を消す", "このレイヤーを全面から消す（その上から「足す」で塗って出せる）"),
                              ('RESET', "塗りをリセット", "塗りを全部消して自動マスクだけに戻す")])

    @classmethod
    def description(cls, context, props):
        return {'ADD': "このレイヤーを全面に出す",
                'ERASE': "このレイヤーを全面から消す（その上から「足す」で塗って出せる）",
                'RESET': "塗りを全部消して自動マスクだけに戻す"}.get(props.mode, "")

    def execute(self, context):
        obj = _mixer_obj(context)
        s = obj.cmx
        i = s.active_index
        if i <= 0:
            return {'CANCELLED'}
        img = ensure_layer_image(obj, s.layers[i])
        fill_layer_image(img, self.mode, s.brush_softness, s.brush_noise)
        return {'FINISHED'}


class CMX_OT_rebuild(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.rebuild"
    bl_label = "マテリアルを再構築"
    bl_description = "レイヤーに使っているマテリアルを編集したときに押すと反映されます"

    def execute(self, context):
        obj = _mixer_obj(context)
        for l in obj.cmx.layers:
            l.preview_set = False
        rebuild_material(obj)
        return {'FINISHED'}


class CMX_OT_mask_bake(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.mask_bake"
    bl_label = "マスクを焼き込む"
    bl_description = ("エッジ・くぼみ・AO を Cycles で画像に焼きます（ふつう数秒。初回だけGPUの準備で1〜2分かかることがあります）。"
                      "ローポリの角でもきれいに出ます。形を変えたら焼き直してください")

    def execute(self, context):
        import time
        obj = _mixer_obj(context)
        s = obj.cmx
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        t0 = time.time()
        try:
            ensure_paint_uv(context, obj)
            img = bake_surface_masks(context, obj, res=int(s.bake_res), edge_width=s.bake_edge,
                                     cavity_width=s.bake_cavity, ao_distance=s.bake_ao,
                                     ao_others=s.bake_ao_others, samples=s.bake_samples)
        except Exception as e:
            self.report({'ERROR'}, "焼き込みに失敗しました: %s" % e)
            return {'CANCELLED'}
        _SUSPEND[0] += 1
        try:
            s.mask_image = img
            s.mask_source = 'BAKED'
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        self.report({'INFO'}, "マスクを焼き込みました（%.1f 秒）" % (time.time() - t0))
        return {'FINISHED'}


class CMX_OT_bake_textures(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.bake_textures"
    bl_label = "テクスチャに焼き込む"
    bl_description = ("今の仕上がりを Cycles でテクスチャ（ベースカラー・ラフネス・メタル・ノーマル・ハイト・AO）に焼き、"
                      "それだけを使う軽いマテリアルに置き換えます。ミキサーの設定は残るので、いつでも戻せます")

    def execute(self, context):
        import time
        obj = _mixer_obj(context)
        s = obj.cmx
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
            _end_paint(obj)
        keys = [k for k, _l, _s in BAKE_CHANNELS if getattr(s, "bake_ch_" + k.lower())]
        if not keys:
            self.report({'WARNING'}, "焼くものを1つ以上選んでください")
            return {'CANCELLED'}
        t0 = time.time()
        try:
            ensure_paint_uv(context, obj)
            uv = s.bake_uv if (s.bake_uv and obj.data.uv_layers.get(s.bake_uv) is not None) else PAINT_UV
            images = bake_textures(context, obj, res=int(s.tex_res), uv_name=uv, channels=keys,
                                   ao_distance=s.bake_ao, ao_others=s.bake_ao_others)
        except Exception as e:
            self.report({'ERROR'}, "焼き込みに失敗しました: %s" % e)
            return {'CANCELLED'}
        msg = ""
        if s.export_save:
            d = export_dir(obj)
            saved, failed = save_baked_images(obj, images, d)
            s.last_export = d if saved else ""
            if failed:
                self.report({'WARNING'}, "保存できなかったもの（blend内に入れました）: " + " / ".join(failed))
            msg = " → %s" % d
        else:
            for img in images.values():
                img.pack()
            s.last_export = ""
        s.baked_material = build_baked_material(obj, images, uv)
        s["cmx_bake_uv"] = uv        # ハイトで形を動かすとき、同じUVを使う
        s.use_baked = True
        self.report({'INFO'}, "%d 枚焼き込みました（%.1f 秒）%s" % (len(images), time.time() - t0, msg))
        return {'FINISHED'}


def height_image(obj):
    """焼いたハイトの画像（なければ None）。"""
    return bpy.data.images.get("CMX_%s_%s" % (obj.name, _BAKE_SUFFIX['HEIGHT']))


def add_displace(obj):
    """焼いたハイトで形を動かす Displace モディファイア（CMX_Displace）を付ける。失敗したときはメッセージを返す。"""
    s = obj.cmx
    img = height_image(obj)
    if img is None:
        return "先に「焼き込み」でハイトを焼いてください（焼くものの「ハイト」をオンに）"
    uv = s.get("cmx_bake_uv", PAINT_UV)
    if obj.data.uv_layers.get(uv) is None:
        return "焼いたときのUVマップ（%s）がありません。焼き直してください" % uv
    tname = "CMX_Disp_%s" % obj.name
    tex = bpy.data.textures.get(tname) or bpy.data.textures.new(tname, 'IMAGE')
    tex.image = img
    md = obj.modifiers.get(DISP_MOD)
    if md is None:
        md = obj.modifiers.new(DISP_MOD, 'DISPLACE')
    md.texture = tex
    md.texture_coords = 'UV'
    md.uv_layer = uv
    md.direction = 'NORMAL'
    md.strength = s.disp_strength
    md.mid_level = s.disp_mid
    md.show_viewport = True
    md.show_render = True
    # エッジ・くぼみの計算（CMX_Masks）より前に置く（スタックの最後はマスクの計算用）
    mm = obj.modifiers.get(SM_MOD)
    if mm is not None:
        try:
            obj.modifiers.move(list(obj.modifiers).index(md), list(obj.modifiers).index(mm))
        except Exception:
            pass
    return None


def remove_displace(obj):
    md = obj.modifiers.get(DISP_MOD)
    if md is not None:
        obj.modifiers.remove(md)
    tex = bpy.data.textures.get("CMX_Disp_%s" % obj.name)
    if tex is not None and tex.users == 0:
        bpy.data.textures.remove(tex)


class CMX_OT_displace_add(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.displace_add"
    bl_label = "ハイトで形を動かす"
    bl_description = ("焼き込んだハイトを使って、メッシュの形そのものを動かします（Displace モディファイア）。"
                      "サブディビジョンなどで細かく分割してあると、きれいに出ます。焼き込み中は一時的に外れます")

    def execute(self, context):
        err = add_displace(_mixer_obj(context))
        if err:
            self.report({'WARNING'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class CMX_OT_displace_remove(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.displace_remove"
    bl_label = "形の変形を外す"
    bl_description = "ハイトで形を動かすモディファイア（CMX_Displace）を外す"

    @classmethod
    def poll(cls, context):
        o = _mixer_obj(context)
        return o is not None and o.modifiers.get(DISP_MOD) is not None

    def execute(self, context):
        remove_displace(_mixer_obj(context))
        return {'FINISHED'}


class CMX_OT_open_export(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.open_export"
    bl_label = "保存先を開く"
    bl_description = "書き出したテクスチャのフォルダをエクスプローラーで開く"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = _mixer_obj(context)
        d = obj.cmx.last_export or export_dir(obj)
        if not os.path.isdir(d):
            self.report({'WARNING'}, "フォルダがまだありません: %s" % d)
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=d)
        return {'FINISHED'}


def _smart_items(self, context):
    items = [(n, n, "") for n in list_smart()] or [("", "（まだありません）", "")]
    _ENUM_KEEP["smart"] = items
    return items


def _smart_targets(context):
    objs = [o for o in context.selected_objects if o.type == 'MESH']
    a = context.active_object
    if a is not None and a.type == 'MESH' and a not in objs:
        objs.append(a)
    return objs


class CMX_OT_smart_save(_MixerOp, bpy.types.Operator):
    bl_idname = "cmx.smart_save"
    bl_label = "スマートマテリアルとして保存"
    bl_description = "今のレイヤー構成（マテリアル・自動マスク・調整）を保存し、ほかの物や別のblendでも使えるようにする（塗りは保存しない）"
    bl_options = {'REGISTER'}
    name: StringProperty(name="名前", default="")

    def invoke(self, context, event):
        if not self.name:
            self.name = _mixer_obj(context).name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = _mixer_obj(context)
        if not self.name.strip():
            self.report({'WARNING'}, "名前を入れてください")
            return {'CANCELLED'}
        existed = _safe_name(self.name) in list_smart()
        try:
            name = save_smart(obj, self.name)
        except Exception as e:
            self.report({'ERROR'}, "保存できませんでした: %s" % e)
            return {'CANCELLED'}
        try:
            context.window_manager.cmx_smart = name
        except Exception:
            pass
        self.report({'INFO'}, ("上書き保存しました: %s" if existed else "保存しました: %s") % name)
        return {'FINISHED'}


class CMX_OT_smart_apply(bpy.types.Operator):
    bl_idname = "cmx.smart_apply"
    bl_label = "選んだ物に適用"
    bl_description = ("選んだスマートマテリアルを、選択中のメッシュすべてに適用します"
                      "（ミキサーを始めていない物は自動で始めます。今のレイヤーと塗りは置き換わります）")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(context.window_manager.cmx_smart) and len(_smart_targets(context)) > 0

    def execute(self, context):
        wm = context.window_manager
        a = context.active_object
        if a is not None and a.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            data = load_smart(wm.cmx_smart)
        except Exception as e:
            self.report({'ERROR'}, "読み込めませんでした: %s" % e)
            return {'CANCELLED'}
        done, errs = [], []
        for o in _smart_targets(context):
            ok, msg = apply_smart(context, o, data, keep_base=wm.cmx_smart_keep_base)
            if ok:
                done.append(o.name)
            if msg:
                errs.append("%s: %s" % (o.name, msg))
        if errs:
            self.report({'WARNING'}, " / ".join(errs))
        if done:
            self.report({'INFO'}, "「%s」を %d 個に適用しました" % (wm.cmx_smart, len(done)))
        return {'FINISHED'} if done else {'CANCELLED'}


class CMX_OT_smart_delete(bpy.types.Operator):
    bl_idname = "cmx.smart_delete"
    bl_label = "スマートマテリアルを削除"
    bl_description = "選んでいるスマートマテリアルのファイルを削除する"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return bool(context.window_manager.cmx_smart)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        name = context.window_manager.cmx_smart
        d = smart_dir()
        for ext in (".json", ".blend", ".blend1"):
            p = os.path.join(d, name + ext)
            if os.path.exists(p):
                os.remove(p)
        self.report({'INFO'}, "削除しました: %s" % name)
        return {'FINISHED'}


class CMX_OT_smart_open(bpy.types.Operator):
    bl_idname = "cmx.smart_open"
    bl_label = "保存フォルダを開く"
    bl_description = "スマートマテリアルの保存フォルダをエクスプローラーで開く（ほかのPCへコピーもできます）"
    bl_options = {'REGISTER'}

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=smart_dir())
        return {'FINISHED'}


class CMX_OT_tile_create(bpy.types.Operator):
    bl_idname = "cmx.tile_create"
    bl_label = "新しいタイル素材"
    bl_description = ("平らな正方形を作り、継ぎ目なしのテクスチャ（タイル素材）をミキサーで作れるようにします。"
                      "ノイズ類は自動で継ぎ目なしになり、「焼き込み」でそのまま繰り返せるテクスチャになります")
    bl_options = {'REGISTER', 'UNDO'}
    name: StringProperty(name="名前", default="タイル素材")
    size: FloatProperty(name="大きさ (m)", default=2.0, min=0.05, soft_max=20.0, unit='LENGTH',
                        description="テクスチャ1枚が表す実際の大きさ")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        a = context.active_object
        if a is not None and a.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            ob = create_tile_object(context, self.name.strip() or "タイル素材", self.size)
        except Exception as e:
            self.report({'ERROR'}, "作れませんでした: %s" % e)
            return {'CANCELLED'}
        for o in context.view_layer.objects:
            if o.select_get():
                o.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        self.report({'INFO'}, "タイル素材を作りました: %s" % ob.name)
        return {'FINISHED'}


class CMX_OT_import_ground_paint(bpy.types.Operator):
    bl_idname = "cmx.import_ground_paint"
    bl_label = "地面ペイントから取り込む"
    bl_description = ("この物の地面ペイントのレイヤー（マテリアル・調整・塗った範囲）をミキサーのレイヤーにします。"
                      "地面ペイント側のデータはそのまま残ります")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH' and len(ground_paint_layers(o)) > 0

    def execute(self, context):
        obj = context.active_object
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        ok, msg = import_ground_paint(context, obj)
        if msg:
            self.report({'WARNING'} if ok else {'ERROR'}, msg)
        if not ok:
            return {'CANCELLED'}
        self.report({'INFO'}, "地面ペイントのレイヤーを取り込みました")
        return {'FINISHED'}


class CMX_OT_import_textures(bpy.types.Operator):
    bl_idname = "cmx.import_textures"
    bl_label = "テクスチャからレイヤー作成"
    bl_description = ("テクスチャ画像（カラー・ノーマル・ラフネス・ハイトなど）を選ぶと、ファイル名から種類を判別して"
                      "マテリアルを作り、新しいレイヤーとして追加します。ビューポートへのドラッグ＆ドロップでも使えます")
    bl_options = {'REGISTER', 'UNDO'}
    directory: StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE', 'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={'SKIP_SAVE', 'HIDDEN'})
    filter_image: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    filter_folder: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None

    def invoke(self, context, event):
        if self.directory and len(self.files) > 0:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = _mixer_obj(context)
        if obj is None:
            return {'CANCELLED'}
        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        groups = group_texture_files(paths)
        if not groups:
            self.report({'WARNING'}, "使える画像が見つかりませんでした")
            return {'CANCELLED'}
        s = obj.cmx
        names = []
        _SUSPEND[0] += 1
        try:
            for name, g in groups.items():
                mat = build_material_from_textures(name, g)
                l = _new_layer(s, 'MATERIAL')
                l.name = mat.name
                l.material = mat
                names.append(mat.name)
            s.active_index = len(s.layers) - 1
        finally:
            _SUSPEND[0] -= 1
        rebuild_material(obj)
        self.report({'INFO'}, "レイヤーを作成: " + ", ".join(names))
        return {'FINISHED'}


class CMX_FH_textures(bpy.types.FileHandler):
    bl_idname = "CMX_FH_textures"
    bl_label = "マテリアルミキサーのレイヤーにする"
    bl_import_operator = "cmx.import_textures"
    bl_file_extensions = ";".join(TEX_EXTS)

    @classmethod
    def poll_drop(cls, context):
        area = getattr(context, "area", None)
        return area is not None and area.type == 'VIEW_3D' and _mixer_obj(context) is not None


# ------------------------------------------------------------------ UI
class CMX_UL_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        if index == 0:
            ic = 'WORLD'
        elif item.kind == 'ADJUST':
            ic = ADJ_ICONS.get(item.adj_type, 'MODIFIER')
        else:
            ic = 'MATERIAL' if item.material else 'COLOR'
        row.prop(item, "name", text="", emboss=False, icon=_icon(ic))
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        if index > 0 and item.kind == 'ADJUST':
            sub.label(text="調整")
        else:
            sub.label(text=item.material.name if item.material else "単色")
        if index > 0:
            sub.prop(item, "enabled", text="", emboss=False,
                     icon=_icon('HIDE_OFF' if item.enabled else 'HIDE_ON'))

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        n = len(items)
        return [self.bitflag_filter_item] * n, [n - 1 - i for i in range(n)]


class CMX_UL_masks(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        lab = MASK_LABELS.get(item.mtype, item.mtype)
        if index > 0:
            short = {'MULTIPLY': "×", 'ADD': "＋", 'MAX': "∨", 'SUBTRACT': "−", 'MIN': "∧"}.get(item.blend, "")
            lab = "%s %s" % (short, lab)
        if item.invert:
            lab += "（反転）"
        row.label(text=lab, icon=_icon(MASK_ICONS.get(item.mtype, 'MOD_MASK')))
        row.prop(item, "enabled", text="", emboss=False, icon=_icon('HIDE_OFF' if item.enabled else 'HIDE_ON'))


class CMX_MT_add_layer(bpy.types.Menu):
    bl_idname = "CMX_MT_add_layer"
    bl_label = "レイヤーを追加"

    def draw(self, context):
        lay = self.layout
        lay.operator_context = 'INVOKE_DEFAULT'
        op = lay.operator("cmx.layer_add", text="マテリアル / 単色レイヤー", icon=_icon('MATERIAL'))
        op.kind = 'MATERIAL'
        lay.operator("cmx.import_textures", text="テクスチャ画像から作成…", icon=_icon('IMAGE_DATA'))
        if ground_paint_layers(context.active_object):
            lay.operator("cmx.import_ground_paint", text="地面ペイントのレイヤーを取り込む", icon=_icon('BRUSH_DATA'))
        lay.separator()
        for ident, label, _d in ADJ_TYPES:
            op = lay.operator("cmx.layer_add", text="調整: " + label, icon=_icon(ADJ_ICONS[ident]))
            op.kind = 'ADJUST'
            op.adj_type = ident


class CMX_MT_add_mask(bpy.types.Menu):
    bl_idname = "CMX_MT_add_mask"
    bl_label = "自動マスクを追加"

    def draw(self, context):
        lay = self.layout
        for ident, label, _d in MASK_TYPES:
            lay.operator("cmx.mask_add", text=label, icon=_icon(MASK_ICONS[ident])).mtype = ident


def _active_layer(context):
    obj = _mixer_obj(context)
    if obj is None:
        return None, None, -1
    s = obj.cmx
    i = s.active_index
    if 0 <= i < len(s.layers):
        return obj, s.layers[i], i
    return obj, None, -1


class _Sub:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Concept"
    bl_parent_id = "CMX_PT_main"


class CMX_PT_main(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Concept"
    bl_label = "マテリアルミキサー"
    bl_idname = "CMX_PT_main"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            layout.label(text="メッシュを選択してください", icon=_icon('INFO'))
            layout.operator("cmx.tile_create", text="新しいタイル素材…", icon=_icon('TEXTURE'))
            return
        s = obj.cmx
        if not s.enabled:
            layout.label(text=obj.name, icon=_icon('MESH_ICOSPHERE'))
            r = layout.row()
            r.scale_y = 1.4
            r.operator("cmx.enable", text="この物でミキサーを始める", icon=_icon('MATERIAL'))
            if len(s.layers) > 0:
                layout.label(text="前のレイヤーと塗りが残っています", icon=_icon('INFO'))
            elif ground_paint_layers(obj):
                layout.operator("cmx.import_ground_paint", text="地面ペイントから取り込んで始める",
                                icon=_icon('BRUSH_DATA'))
            layout.separator()
            layout.operator("cmx.tile_create", text="新しいタイル素材…", icon=_icon('TEXTURE'))
            return
        if s.tile_mode:
            r = layout.row()
            r.label(text="タイル素材（継ぎ目なし・%.2fm）" % s.tile_size_m, icon=_icon('TEXTURE'))
            r.prop(s, "tile_preview", text="", icon=_icon('MOD_ARRAY'))
        row = layout.row()
        row.template_list("CMX_UL_layers", "", s, "layers", s, "active_index", rows=6)
        col = row.column(align=True)
        col.menu("CMX_MT_add_layer", icon=_icon('ADD'), text="")
        col.operator("cmx.layer_remove", icon=_icon('REMOVE'), text="")
        col.separator()
        col.operator("cmx.layer_duplicate", icon=_icon('DUPLICATE'), text="")
        col.separator()
        col.operator("cmx.layer_move", icon=_icon('TRIA_UP'), text="").direction = 'UP'
        col.operator("cmx.layer_move", icon=_icon('TRIA_DOWN'), text="").direction = 'DOWN'
        if s.use_baked and s.baked_material is not None:
            box = layout.box()
            box.label(text="焼き込んだ軽いマテリアルで表示中", icon=_icon('IMAGE_DATA'))
            box.label(text="レイヤーの変更は、ミキサーに戻すか焼き直すと見えます")
            box.prop(s, "use_baked", text="ミキサーに戻す", toggle=True, invert_checkbox=True,
                     icon=_icon('NODE_MATERIAL'))
        else:
            layout.prop(s, "view_mode", text="表示")
        tip = layout.row()
        tip.scale_y = 0.7
        tip.label(text="テクスチャ画像はビューポートにドロップでもレイヤーにできます", icon=_icon('IMAGE_DATA'))


class CMX_PT_layer(_Sub, bpy.types.Panel):
    bl_label = "レイヤー"
    bl_idname = "CMX_PT_layer"

    @classmethod
    def poll(cls, context):
        return _active_layer(context)[1] is not None

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        s = obj.cmx
        if i > 0 and layer.kind == 'ADJUST':
            layout.prop(layer, "adj_type", text="")
            col = layout.column(align=True)
            t = layer.adj_type
            if t == 'HSV':
                col.prop(layer, "adj_val")
                col.prop(layer, "adj_sat")
                col.prop(layer, "adj_hue")
            elif t == 'ROUGH':
                col.prop(layer, "adj_rough")
            elif t == 'METAL':
                col.prop(layer, "adj_metal")
            elif t == 'WET':
                col.prop(layer, "wet_dark")
                col.prop(layer, "wet_rough")
                col.prop(layer, "wet_flat")
            elif t == 'TINT':
                col.prop(layer, "tint_color")
                col.prop(layer, "tint_blend")
        else:
            layout.prop(layer, "material")
            if layer.material is None:
                r = layout.row(align=True)
                r.prop(layer, "color", text="")
                c = layout.column(align=True)
                c.prop(layer, "roughness")
                c.prop(layer, "metallic")
            elif s.tile_mode:
                layout.prop(layer, "tile_size")
                c = layout.column()
                c.scale_y = 0.8
                c.label(text="タイル1枚に %d 回くり返し（継ぎ目なしのため整数）" % tile_repeats(s, layer))
                if s.light_paint:
                    layout.prop(layer, "preview_color")
            else:
                layout.row().prop(layer, "projection", expand=True)
                if layer.projection == 'UV':
                    layout.prop(layer, "uv_map")
                    layout.prop(layer, "uv_repeat")
                elif layer.projection in ('TRIPLANAR', 'TOP'):
                    layout.prop(layer, "tile_size")
                    if layer.projection == 'TRIPLANAR':
                        layout.prop(layer, "tri_sharp")
                if s.light_paint:
                    layout.prop(layer, "preview_color")
            col = layout.column(align=True)
            col.prop(layer, "height_contrast")
            col.prop(layer, "height_offset")
        if i > 0:
            col = layout.column(align=True)
            col.prop(layer, "softness")
            col.prop(layer, "opacity")
        else:
            layout.label(text="ベース：一番下（全面）", icon=_icon('INFO'))


AT_SAMPLE_WARN = 18      # Cycles のシェーダーの限界（SVM スタック）に近づく目安。四角 2 レイヤーのトリプラナーで超える


def at_sample_count(s):
    """タイリング崩しで、素材を読む回数（セルのサンプル数 × 投影の面数 を、レイヤーごとに足した合計）。"""
    n = 0
    for i, l in enumerate(s.layers):
        if i > 0 and not (l.enabled and l.kind == 'MATERIAL'):
            continue
        if not l.anti_tile or l.material is None or not _has_images(l.material):
            continue
        if not s.tile_mode and l.projection == 'ORIGINAL':
            continue
        planes = 3 if (not s.tile_mode and l.projection == 'TRIPLANAR') else 1
        n += planes * (3 if l.at_shape == 'HEX' else 4)
    return n


def _heavy_shader(s):
    """Cycles のシェーダーの限界（SVM スタック）に近づきやすい構成か（タイリング崩しが多い・全体の凹凸がある）。
    こういう構成でノーマルが真っ平らに焼けたら、失敗とみなす。"""
    if at_sample_count(s) > AT_SAMPLE_WARN // 2:
        return True
    return s.bump_strength > 0.0 and any(l.material is not None and _has_images(l.material) for l in s.layers)


def _placeable(context):
    """配置・タイリング崩しを出せるレイヤーか（マテリアルを持つレイヤーで、「そのまま」投影ではない）。"""
    obj, layer, i = _active_layer(context)
    if layer is None or (i > 0 and layer.kind != 'MATERIAL') or layer.material is None:
        return False
    return obj.cmx.tile_mode or layer.projection != 'ORIGINAL'


class CMX_PT_place(_Sub, bpy.types.Panel):
    bl_label = "配置（ずらし・回転・繰り返し）"
    bl_idname = "CMX_PT_place"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _placeable(context)

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        s = obj.cmx
        col = layout.column(align=True)
        col.prop(layer, "place_x")
        col.prop(layer, "place_y")
        r = layout.row(align=True)
        r.prop(layer, "place_rot")
        r.prop(layer, "place_snap", toggle=True)
        col = layout.column(align=True)
        col.prop(layer, "place_sx")
        col.prop(layer, "place_sy")
        if s.tile_mode:
            c = layout.column()
            c.scale_y = 0.8
            c.label(text="タイル素材：回転は 90° 刻み、繰り返しは整数回になります", icon=_icon('INFO'))


class CMX_PT_relief(_Sub, bpy.types.Panel):
    bl_label = "凹凸（ノーマル・ハイト）"
    bl_idname = "CMX_PT_relief"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        obj, layer, i = _active_layer(context)
        return layer is not None and (i == 0 or layer.kind == 'MATERIAL')

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        if layer.material is not None:
            col = layout.column(align=True)
            col.prop(layer, "normal_strength")
            col.prop(layer, "height_strength_layer")
            layout.prop(layer, "normal_flip_y")
            layout.prop(layer, "normal_from_height")
        else:
            layout.prop(layer, "height_strength_layer")
        layout.prop(layer, "height_invert")
        if i > 0:
            box = layout.box()
            box.label(text="下のレイヤーとの重なり方", icon=_icon('MOD_DISPLACE'))
            box.prop(layer, "stack_mode", text="")
            if layer.stack_mode != 'NORMAL':
                box.prop(layer, "stack_amount")
            box.prop(layer, "wrap_amount")
        c = layout.column()
        c.scale_y = 0.8
        c.label(text="全体の凹凸・形の変形は「全体の凹凸・ムラ」で", icon=_icon('INFO'))


class CMX_PT_antitile(_Sub, bpy.types.Panel):
    bl_label = "タイリング崩し"
    bl_idname = "CMX_PT_antitile"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _placeable(context)

    def draw_header(self, context):
        obj, layer, i = _active_layer(context)
        if layer is not None:
            self.layout.prop(layer, "anti_tile", text="")

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        s = obj.cmx
        layout.active = layer.anti_tile
        if not _has_images(layer.material):
            c = layout.column()
            c.scale_y = 0.8
            c.label(text="この素材には画像テクスチャがないので効きません", icon=_icon('INFO'))
        layout.prop(layer, "at_cell")
        layout.row().prop(layer, "at_shape", expand=True)
        layout.prop(layer, "at_rot")
        layout.prop(layer, "at_flip")
        col = layout.column(align=True)
        col.prop(layer, "at_offset")
        col.prop(layer, "at_blend")
        layout.prop(layer, "at_mix")
        layout.prop(layer, "at_seed")
        c = layout.column()
        c.scale_y = 0.8
        if s.tile_mode:
            c.label(text="タイル素材：セルは整数個、回転は 90° 刻みで継ぎ目なし", icon=_icon('INFO'))
        else:
            c.label(text="重くなります。塗っている間の簡易表示では使われません", icon=_icon('INFO'))
        n = at_sample_count(s)
        if n > AT_SAMPLE_WARN:
            c.label(text="多すぎます（%d回読み込み）。焼き込み(Cycles)に失敗することがあります" % n, icon=_icon('ERROR'))
            c.label(text="→ 六角形にする・トリプラナーをやめる・レイヤーを減らす")


class CMX_PT_masks(_Sub, bpy.types.Panel):
    bl_label = "自動マスク"
    bl_idname = "CMX_PT_masks"

    @classmethod
    def poll(cls, context):
        return _active_layer(context)[2] > 0

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        s = obj.cmx
        layout.prop(layer, "use_auto")
        if not layer.use_auto:
            layout.label(text="塗った所（足す）だけに出ます", icon=_icon('INFO'))
            return
        row = layout.row()
        row.template_list("CMX_UL_masks", "", layer, "masks", layer, "active_mask", rows=3)
        col = row.column(align=True)
        col.menu("CMX_MT_add_mask", icon=_icon('ADD'), text="")
        col.operator("cmx.mask_remove", icon=_icon('REMOVE'), text="")
        col.separator()
        col.operator("cmx.mask_move", icon=_icon('TRIA_UP'), text="").direction = 'UP'
        col.operator("cmx.mask_move", icon=_icon('TRIA_DOWN'), text="").direction = 'DOWN'
        if len(layer.masks) == 0:
            layout.label(text="マスクなし＝全面に出ます（＋で追加）", icon=_icon('INFO'))
            return
        j = layer.active_mask
        if not 0 <= j < len(layer.masks):
            return
        mk = layer.masks[j]
        box = layout.box()
        box.prop(mk, "mtype", text="")
        if j > 0:
            box.prop(mk, "blend")
        col = box.column(align=True)
        col.prop(mk, "amount")
        col.prop(mk, "contrast")
        col.prop(mk, "noise")
        col.prop(mk, "strength")
        box.prop(mk, "invert")
        if mk.mtype in ('UP', 'SLOPE'):
            box.prop(mk, "use_detail_normal")
        elif mk.mtype == 'NOISE':
            c = box.column(align=True)
            c.prop(mk, "noise_scale")
            c.prop(mk, "noise_detail")
        elif mk.mtype in MASK_NEEDS_DATA and s.tile_mode:
            c = box.column()
            c.scale_y = 0.8
            c.label(text="平らなタイルでは形の凹凸がないので効きません", icon=_icon('INFO'))
            c.label(text="→「下のレイヤーの高さ」マスクを使ってください")
        elif mk.mtype in MASK_NEEDS_DATA:
            r = box.row()
            r.scale_y = 0.8
            if s.mask_source == 'BAKED' and s.mask_image is not None:
                r.label(text="元データ: 焼き込み画像", icon=_icon('IMAGE_DATA'))
            else:
                r.label(text="元データ: 頂点（下の「マスクの元データ」で調整）", icon=_icon('MOD_VERTEX_WEIGHT'))


class CMX_PT_paint(_Sub, bpy.types.Panel):
    bl_label = "塗り（足す・消す）"
    bl_idname = "CMX_PT_paint"

    @classmethod
    def poll(cls, context):
        return _active_layer(context)[2] > 0

    def draw(self, context):
        layout = self.layout
        obj, layer, i = _active_layer(context)
        s = obj.cmx
        painting = (obj.mode == 'TEXTURE_PAINT' and layer.image is not None and
                    context.scene.tool_settings.image_paint.canvas == layer.image)
        r = layout.row()
        r.scale_y = 1.4
        if painting:
            r.operator("cmx.paint_exit", text="塗り終了", icon=_icon('CHECKMARK'))
        else:
            r.operator("cmx.paint_layer", text="このレイヤーを塗る", icon=_icon('BRUSH_DATA'))
        cur = current_paint_state(context) if painting else s.paint_mode
        r = layout.row(align=True)
        for m, lab, ic in (('ADD', "足す", 'ADD'), ('ERASE', "消す", 'REMOVE'), ('RESET', "自動に戻す", 'LOOP_BACK')):
            r.operator("cmx.paint_mode", text=lab, icon=_icon(ic), depress=(cur == m)).mode = m
        col = layout.column(align=True)
        col.label(text="ブラシ（これから塗るストロークに入る）")
        col.prop(s, "brush_softness")
        col.prop(s, "brush_noise")
        r = layout.row(align=True)
        r.operator("cmx.fill", text="全面に足す", icon=_icon('SNAP_FACE')).mode = 'ADD'
        r.operator("cmx.fill", text="全面を消す", icon=_icon('X')).mode = 'ERASE'
        layout.operator("cmx.fill", text="塗りをリセット（自動マスクだけに戻す）", icon=_icon('LOOP_BACK')).mode = 'RESET'
        if painting:
            hint = layout.column(align=True)
            hint.scale_y = 0.8
            hint.label(text="X: 足す ⇔ 消す を切り替え")
            hint.label(text="F: ブラシサイズ / Shift+F: 強さ")
            hint.label(text="ブラシの色はこのパネルが自動で設定します")


class CMX_PT_maskdata(_Sub, bpy.types.Panel):
    bl_label = "マスクの元データ（エッジ・くぼみ・AO）"
    bl_idname = "CMX_PT_maskdata"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _mixer_obj(context)
        s = obj.cmx
        layout.row().prop(s, "mask_source", expand=True)
        if s.mask_source == 'LIVE':
            col = layout.column(align=True)
            col.prop(s, "edge_radius")
            col.prop(s, "edge_spread")
            col.prop(s, "ao_distance")
            sub = any(m.type in ('SUBSURF', 'MULTIRES') for m in obj.modifiers)
            if len(obj.data.vertices) < 3000 and not sub:
                c = layout.column(align=True)
                c.scale_y = 0.8
                c.label(text="頂点が少ない物は角がぼやけます", icon=_icon('INFO'))
                c.label(text="→「焼き込み（高品質）」がおすすめ")
        else:
            col = layout.column(align=True)
            col.prop(s, "bake_res")
            col.prop(s, "bake_edge")
            col.prop(s, "bake_cavity")
            col.prop(s, "bake_ao")
            col.prop(s, "bake_samples")
            layout.prop(s, "bake_ao_others")
            r = layout.row()
            r.scale_y = 1.3
            r.operator("cmx.mask_bake", text="焼き込む（Cycles）" if s.mask_image is None else "焼き直す（Cycles）",
                       icon=_icon('RENDER_STILL'))
            if s.mask_image is None:
                layout.label(text="まだ焼いていません（焼くまでは頂点の値を使います）", icon=_icon('INFO'))
            else:
                layout.label(text=s.mask_image.name, icon=_icon('IMAGE_DATA'))


class CMX_PT_global(_Sub, bpy.types.Panel):
    bl_label = "全体の凹凸・ムラ"
    bl_idname = "CMX_PT_global"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _mixer_obj(context)
        s = obj.cmx
        box = layout.box()
        box.label(text="大きなムラ（繰り返し感を消す）", icon=_icon('MOD_NOISE'))
        col = box.column(align=True)
        col.prop(s, "macro_amount")
        col.prop(s, "macro_scale")
        box = layout.box()
        box.label(text="全体の凹凸", icon=_icon('MOD_DISPLACE'))
        col = box.column(align=True)
        col.prop(s, "bump_strength")
        col.prop(s, "bump_distance")
        c = box.column()
        c.scale_y = 0.8
        c.label(text="重ねた最終のハイトから凹凸を作ります（焼き込みにも入る）")
        box = layout.box()
        box.label(text="ハイトで形を動かす", icon=_icon('MOD_DISPLACE'))
        has = obj.modifiers.get(DISP_MOD) is not None
        if has:
            col = box.column(align=True)
            col.prop(s, "disp_strength")
            col.prop(s, "disp_mid")
            box.operator("cmx.displace_remove", icon=_icon('X'))
        else:
            box.operator("cmx.displace_add", icon=_icon('MOD_DISPLACE'))
            c = box.column()
            c.scale_y = 0.8
            c.label(text="先に「焼き込み」でハイトを焼いておく")
            c.label(text="細かく分割した（サブディビジョン等）物向け")


class CMX_PT_bake(_Sub, bpy.types.Panel):
    bl_label = "焼き込み・書き出し"
    bl_idname = "CMX_PT_bake"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _mixer_obj(context)
        s = obj.cmx
        if s.baked_material is not None:
            r = layout.row()
            r.scale_y = 1.2
            r.prop(s, "use_baked", toggle=True,
                   icon=_icon('IMAGE_DATA' if s.use_baked else 'NODE_MATERIAL'),
                   text="焼き込み表示中（押すとミキサーに戻る）" if s.use_baked else "焼き込んだ軽いマテリアルで表示")
        col = layout.column(align=True)
        col.prop(s, "tex_res")
        col.prop(s, "bake_uv")
        flow = layout.grid_flow(row_major=True, columns=3, even_columns=True, align=True)
        for k, _l, _s in BAKE_CHANNELS:
            flow.prop(s, "bake_ch_" + k.lower(), toggle=True)
        layout.prop(s, "export_save")
        if s.export_save:
            layout.prop(s, "export_dir", text="")
            c = layout.column(align=True)
            c.scale_y = 0.8
            c.label(text="保存先: " + export_dir(obj), icon=_icon('FILE_FOLDER'))
            if not s.export_dir.strip() and not bpy.data.filepath:
                c.label(text="（blend未保存のためドキュメントに保存）")
        r = layout.row()
        r.scale_y = 1.4
        r.operator("cmx.bake_textures", text="焼き込む（Cycles）", icon=_icon('RENDER_STILL'))
        if s.last_export:
            layout.operator("cmx.open_export", icon=_icon('FILE_FOLDER'))


class CMX_PT_smart(_Sub, bpy.types.Panel):
    bl_label = "スマートマテリアル"
    bl_idname = "CMX_PT_smart"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        r = layout.row(align=True)
        r.prop(wm, "cmx_smart", text="")
        r.operator("cmx.smart_delete", text="", icon=_icon('TRASH'))
        layout.prop(wm, "cmx_smart_keep_base")
        n = len(_smart_targets(context))
        r = layout.row()
        r.scale_y = 1.4
        r.operator("cmx.smart_apply", text="選んだ物に適用（%d個）" % n, icon=_icon('CHECKMARK'))
        if _mixer_obj(context) is not None:
            layout.operator("cmx.smart_save", text="今の構成を保存…", icon=_icon('FILE_TICK'))
        c = layout.column(align=True)
        c.scale_y = 0.8
        c.label(text="塗り（足す・消す）は引き継ぎません", icon=_icon('INFO'))
        layout.operator("cmx.smart_open", icon=_icon('FILE_FOLDER'))


class CMX_PT_settings(_Sub, bpy.types.Panel):
    bl_label = "全体の設定・軽さ"
    bl_idname = "CMX_PT_settings"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _mixer_obj(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _mixer_obj(context)
        s = obj.cmx
        if s.tile_mode:
            box = layout.box()
            box.label(text="タイル素材", icon=_icon('TEXTURE'))
            box.prop(s, "tile_size_m")
            box.prop(s, "tile_preview")
        col = layout.column(align=True)
        col.prop(s, "height_strength")
        col.prop(s, "noise_scale")
        layout.prop(s, "resolution")
        box = layout.box()
        box.label(text="軽くする", icon=_icon('MOD_SUBSURF'))
        box.prop(s, "light_paint")
        box.prop(s, "paint_lower_subdiv")
        r = box.row()
        r.enabled = s.paint_lower_subdiv
        r.prop(s, "paint_subdiv_level")
        layout.operator("cmx.rebuild", icon=_icon('FILE_REFRESH'))
        layout.operator("cmx.disable", text="解除（元のマテリアルに戻す）", icon=_icon('CANCEL'))


# ------------------------------------------------------------------ handlers
@persistent
def _cmx_save_pre(*args):
    for img in bpy.data.images:
        if img.name.startswith("CMX_") and img.is_dirty:
            try:
                img.pack()
            except Exception as e:
                print("CMX: pack failed", img.name, e)


@persistent
def _cmx_load_post(*args):
    _watch.clear()
    objs = getattr(bpy.data, "objects", None)
    if objs is None:      # アドオン有効化の最中は bpy.data が使えない
        return
    for obj in objs:
        try:
            if obj.get("cmx_subdiv_backup") is not None or (obj.cmx.enabled and _is_light(obj)):
                _watch.add(obj.name)
        except Exception:
            pass
    if _watch and not bpy.app.timers.is_registered(_poll_restore):
        bpy.app.timers.register(_poll_restore, first_interval=1.0)


_HANDLERS = ((bpy.app.handlers.save_pre, _cmx_save_pre), (bpy.app.handlers.load_post, _cmx_load_post))

classes = (CMX_Mask, CMX_Layer, CMX_MatRef, CMX_Settings, CMX_UL_layers, CMX_UL_masks, CMX_MT_add_layer,
           CMX_MT_add_mask, CMX_OT_enable, CMX_OT_disable, CMX_OT_layer_add, CMX_OT_layer_remove,
           CMX_OT_layer_move, CMX_OT_layer_duplicate, CMX_OT_mask_add, CMX_OT_mask_remove, CMX_OT_mask_move,
           CMX_OT_paint_layer, CMX_OT_paint_exit, CMX_OT_paint_mode, CMX_OT_fill, CMX_OT_rebuild,
           CMX_OT_mask_bake, CMX_OT_bake_textures, CMX_OT_open_export, CMX_OT_smart_save, CMX_OT_smart_apply,
           CMX_OT_smart_delete, CMX_OT_smart_open, CMX_OT_tile_create, CMX_OT_import_ground_paint,
           CMX_OT_import_textures, CMX_FH_textures, CMX_OT_displace_add, CMX_OT_displace_remove,
           CMX_PT_main, CMX_PT_layer, CMX_PT_place, CMX_PT_relief, CMX_PT_antitile, CMX_PT_masks, CMX_PT_paint,
           CMX_PT_maskdata, CMX_PT_global, CMX_PT_bake, CMX_PT_smart, CMX_PT_settings)


def _remove_handler(lst, fn):
    for h in list(lst):
        if getattr(h, "__name__", "") == fn.__name__ and getattr(h, "__module__", "") == fn.__module__:
            lst.remove(h)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.cmx = PointerProperty(type=CMX_Settings)
    bpy.types.WindowManager.cmx_smart = EnumProperty(name="スマートマテリアル", items=_smart_items,
                                                     description="保存したスマートマテリアル")
    bpy.types.WindowManager.cmx_smart_keep_base = BoolProperty(
        name="ベース（一番下）は今のまま", default=False,
        description="オンにすると、適用先の一番下のレイヤー（元のマテリアル）を残して、その上だけを置き換える")
    for lst, fn in _HANDLERS:
        _remove_handler(lst, fn)
        lst.append(fn)
    if not bpy.app.timers.is_registered(_deferred_load):
        bpy.app.timers.register(_deferred_load, first_interval=0.5)


def _deferred_load():
    try:
        _cmx_load_post()
    except Exception as e:
        print("CMX: load_post failed", e)
    return None


def unregister():
    if bpy.app.timers.is_registered(_deferred_load):
        bpy.app.timers.unregister(_deferred_load)
    if bpy.app.timers.is_registered(_poll_restore):
        bpy.app.timers.unregister(_poll_restore)
    for lst, fn in _HANDLERS:
        _remove_handler(lst, fn)
    del bpy.types.WindowManager.cmx_smart_keep_base
    del bpy.types.WindowManager.cmx_smart
    del bpy.types.Object.cmx
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
