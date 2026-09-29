# -*- coding: utf-8 -*-
"""
cutout_animator.py —— 程序化剪纸动画 v3：抠图切部件 + 武器识别 + 分类动作库 + 逐帧合成

v3 相对 v2 的四项修复:
  1. 抠图: 全局颜色替换 → 边界洪水填充（连通判定不误吃角色相近色、渐变底可续走）、
     收边去色晕 + 羽化、噪点清理、多主体自动取主裁剪（双人图不再手工裁）
  2. 武器: 写死的比例框 → 自动检测（连通域 + 细长比/端部胖细/空心比/颜色差异），
     类别（sword/dagger/spear/staff/axe/hammer/bow）+ 握点 + 朝向角写进 rig.json，
     并把武器规范化到该类别的 rest 姿态；识别不准可用 --weapon 指定或改 rig.json 重跑
  3. 动作: 单一挥砍曲线 → 六类武器动作库（斩/刺/砸/弓/枪/拳），每类三段是三种不同招式，
     且各段节奏自动错开（第一段快、第三段重）
  4. 帧数: 前6/攻8/后6(20帧) → std 8/12/8(28帧)，--density high 10/16/10(36帧)；
     多关键帧插值 + 攻击段双残影 + 2x 超采样渲染（亚像素抖动消除）

用法:
  python cutout_animator.py --image char.png --out build/ [--mode cutout|whole]
      [--segments 3] [--density std|low|high] [--fps 30] [--weapon auto|none|sword|...]
      [--rig rig.json]

输出:
  <out>/rig.json                    实际使用的部件配置（可微调后用 --rig 重跑）
  <out>/rig-debug.png               切件+武器检测调试叠层（box/pivot/轴线画在角色图上）
  <out>/frames/seg{n}/f###.png      每段帧序列
  <out>/seg{n}.gif                  每段动图预览
  <out>/sheets/seg{n}-sheet.png     每段 sprite sheet

依赖: Pillow（可选 numpy：有则边缘去色晕更精细）
"""
import argparse, json, math, os
from collections import deque
from PIL import Image, ImageChops, ImageDraw, ImageFilter

try:
    import numpy as _np
except Exception:
    _np = None

# ---------------- 抠图（v3：边界洪水填充 + 羽化去晕） ----------------
def _border_clusters(px, w, h):
    """取四边 2px 环带颜色做聚类，返回背景候选色列表。"""
    ring = []
    for x in range(w):
        for y in (0, 1, h - 2, h - 1):
            ring.append(px[x, y][:3])
    for y in range(h):
        for x in (0, 1, w - 2, w - 1):
            ring.append(px[x, y][:3])
    clusters = []  # [r和, g和, b和, 个数]
    for c in ring:
        for cl in clusters:
            m = (cl[0] / cl[3], cl[1] / cl[3], cl[2] / cl[3])
            if abs(c[0] - m[0]) + abs(c[1] - m[1]) + abs(c[2] - m[2]) <= 72:
                cl[0] += c[0]; cl[1] += c[1]; cl[2] += c[2]; cl[3] += 1
                break
        else:
            clusters.append([c[0], c[1], c[2], 1])
    clusters = [cl for cl in clusters if cl[3] >= len(ring) * 0.05]
    return [(cl[0] / cl[3], cl[1] / cl[3], cl[2] / cl[3], cl[3] / len(ring)) for cl in clusters]

def _flood(sp, aw, ah, clusters, tol, allow_grad):
    """从边界向内漫水。bg_like 像素直接收；allow_grad 时邻域色差≤14 的渐变也续走。"""
    def bg_like(c):
        return any(abs(c[0] - b[0]) <= tol and abs(c[1] - b[1]) <= tol
                   and abs(c[2] - b[2]) <= tol for b in clusters)
    bg = bytearray(aw * ah)
    dq = deque()
    for x in range(aw):
        for y in (0, ah - 1):
            if bg_like(sp[x, y][:3]):
                bg[y * aw + x] = 1; dq.append((x, y))
    for y in range(ah):
        for x in (0, aw - 1):
            if not bg[y * aw + x] and bg_like(sp[x, y][:3]):
                bg[y * aw + x] = 1; dq.append((x, y))
    walked = 0
    limit = aw * ah * 0.3
    while dq:
        x, y = dq.popleft()
        c0 = sp[x, y][:3]
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < aw and 0 <= ny < ah and not bg[ny * aw + nx]:
                nc = sp[nx, ny][:3]
                if bg_like(nc):
                    bg[ny * aw + nx] = 1; dq.append((nx, ny))
                elif allow_grad and abs(nc[0] - c0[0]) <= 14 and abs(nc[1] - c0[1]) <= 14 and abs(nc[2] - c0[2]) <= 14:
                    bg[ny * aw + nx] = 1; dq.append((nx, ny))
                    walked += 1
                    if walked > limit:      # 渐变续走失控（前后景颜色接近）
                        return None
    return bg

def _components(flags, w, h):
    """8-连通域标记。flags: bytearray(1=前景点)。返回 [(面积, [像素索引...]), ...] 按面积降序。"""
    seen = bytearray(w * h)
    comps = []
    for start in range(w * h):
        if flags[start] and not seen[start]:
            pixs = []
            seen[start] = 1
            dq = deque([start])
            while dq:
                i = dq.popleft()
                pixs.append(i)
                x, y = i % w, i // w
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if 0 <= nx < w and 0 <= ny < h:
                            j = ny * w + nx
                            if flags[j] and not seen[j]:
                                seen[j] = 1
                                dq.append(j)
            comps.append((len(pixs), pixs))
    comps.sort(key=lambda c: -c[0])
    return comps

def remove_bg(img, tol=46):
    """v3 抠图：边界洪水填充 + 收边羽化。连通判定不会误删角色身上的相近色。

    返回 (RGBA, 是否可靠)。复杂背景（边带颜色杂乱/几乎没抠动）时可靠=False 并 WARN，
    但仍会尽力抠。"""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    if min(c[3] for c in corners) < 10:
        return rgba, True   # 已带透明背景
    scale = min(1.0, 512.0 / max(w, h))
    aw, ah = max(2, int(w * scale)), max(2, int(h * scale))
    small = rgba.resize((aw, ah), Image.BILINEAR)
    sp = small.load()
    clusters = _border_clusters(sp, aw, ah)
    if not clusters:
        print("WARN: 四边颜色杂乱（复杂背景），抠图可能不干净——建议换纯色底或手动抠图")
        return rgba, False
    bg = _flood(sp, aw, ah, clusters, tol, allow_grad=True)
    if bg is None:              # 渐变续走失控 → 退回严格模式重跑
        bg = _flood(sp, aw, ah, clusters, tol, allow_grad=False)
    removed = sum(bg) / (aw * ah)
    if removed < 0.03:
        print("WARN: 背景几乎没抠动（复杂背景/渐变怪底）——建议换纯色底或手动抠图")
        return rgba, False
    # 噪点清理：极小的前景孤岛当背景处理
    fg = bytearray(1 - b for b in bg)
    for area, pixs in _components(fg, aw, ah):
        if area < max(24, 0.002 * aw * ah * 0.01):
            for i in pixs:
                bg[i] = 1
    # 掩膜上采样回全尺寸：双线性放大自带抗锯齿，再收 1px 去贴边色晕、微羽化
    mask = Image.new("L", (aw, ah))
    mask.putdata([255 if bg[i] else 0 for i in range(aw * ah)])
    alpha = mask.resize((w, h), Image.BILINEAR)
    alpha = alpha.point(lambda v: 255 - v)
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    if _np is not None:
        # 边缘去色晕（defringe）：半透明像素按 alpha 反解出前景色
        arr = _np.asarray(rgba, dtype=_np.float32)
        a = _np.asarray(alpha, dtype=_np.float32)
        band = (a > 8) & (a < 248)
        if band.any():
            af = (a[band] / 255.0)[:, None]
            cols = arr[:, :, :3][band]
            cs = _np.array([c[:3] for c in clusters], dtype=_np.float32)
            d = _np.abs(cols[:, None, :] - cs[None, :, :]).sum(axis=2)
            base = cs[d.argmin(axis=1)]
            arr[:, :, :3][band] = _np.clip((cols - base * (1 - af)) / _np.maximum(af, 0.05), 0, 255)
            arr[:, :, 3][band] = a[band]
        arr[:, :, 3] = a
        rgba = Image.fromarray(_np.clip(arr, 0, 255).astype(_np.uint8), "RGBA")
    else:
        rgba.putalpha(alpha)
    return rgba, removed > 0.08

def main_subject_crop(rgba):
    """多主体自动取主：第二大连通域 ≥ 主体的 25%（双人图/贴纸组合）时去掉别的大主体，
    保留主体本体 + 主体附近的小件（离手武器、飘带等），裁到保留集合的包围盒。"""
    w, h = rgba.size
    scale = min(1.0, 384.0 / max(w, h))
    aw, ah = max(2, int(w * scale)), max(2, int(h * scale))
    a = rgba.resize((aw, ah), Image.BILINEAR).getchannel("A")
    raw = a.point(lambda v: 1 if v > 110 else 0).tobytes()
    fg = bytearray(raw)
    comps = _components(fg, aw, ah)
    if not comps:
        return rgba, False
    main_area = comps[0][0]
    others = [c for c in comps[1:] if c[0] >= main_area * 0.25]
    if not others:
        return rgba, False
    pixs = comps[0][1]
    mx0, mx1 = min(i % aw for i in pixs), max(i % aw for i in pixs) + 1
    my0, my1 = min(i // aw for i in pixs), max(i // aw for i in pixs) + 1
    keep = [comps[0]]
    for c in comps[1:]:
        if c in others:
            continue
        xs = [i % aw for i in c[1]]; ys = [i // aw for i in c[1]]
        if max(min(xs), mx0) < min(max(xs) + 1, mx1) and max(min(ys), my0) < min(max(ys) + 1, my1):
            keep.append(c)   # 小件与主体包围盒相交（武器/饰品），保留
    xs = [i % aw for _, pixs in keep for i in pixs]
    ys = [i // aw for _, pixs in keep for i in pixs]
    pad = 0.02
    box = (max(0, int((min(xs) / aw - pad) * w)), max(0, int((min(ys) / ah - pad) * h)),
           min(w, int((max(xs) + 1) / aw + pad * aw) ), min(h, int((max(ys) + 1) / ah + pad * ah)))
    print(f"检测到多主体（次要主体占主体 {others[0][0]/main_area:.0%}），已自动只保留最大主体及其附件。"
          f"如裁错了请先手动裁图再跑。")
    return rgba.crop(box), True

# ---------------- 武器检测（纯几何启发式） ----------------
def _shape_stats(pixs, aw, ah, ref_cx=None, ref_cy=None):
    """连通件的 PCA 主轴形状统计。"""
    n = len(pixs)
    sx = sy = 0
    for i in pixs:
        sx += i % aw; sy += i // aw
    cx, cy = sx / n, sy / n
    sxx = syy = sxy = 0
    for i in pixs:
        dx = i % aw - cx; dy = i // aw - cy
        sxx += dx * dx; syy += dy * dy; sxy += dx * dy
    sxx /= n; syy /= n; sxy /= n
    lam = math.hypot(sxx - syy, 2 * sxy)
    if abs(sxy) > 1e-9:
        vx, vy = (sxx + lam) / 2 - syy, sxy
    elif sxx >= syy:
        vx, vy = 1.0, 0.0
    else:
        vx, vy = 0.0, 1.0
    norm = math.hypot(vx, vy) or 1
    vx, vy = vx / norm, vy / norm
    tmin = tmax = None; tmin_i = tmax_i = None
    for i in pixs:
        t = (i % aw - cx) * vx + (i // aw - cy) * vy
        if tmin is None or t < tmin: tmin, tmin_i = t, i
        if tmax is None or t > tmax: tmax, tmax_i = t, i
    length = max(tmax - tmin, 0.8)
    # 沿轴分 10 档的宽度分布（端部是否膨大 / 是否空心）
    bins = [0] * 10
    span = tmax - tmin or 1.0
    for i in pixs:
        t = (i % aw - cx) * vx + (i // aw - cy) * vy
        bins[min(9, int((t - tmin) / span * 10))] += 1
    binlen = span / 10
    widths = [b / binlen for b in bins]
    tipw = sum(widths[8:]) / 2
    shaftw = sorted(widths[2:8])[3]
    fill = n / max(length * max(widths), 1.0)
    if ref_cx is not None:
        d0 = math.hypot(tmin_i % aw - ref_cx, tmin_i // aw - ref_cy)
        d1 = math.hypot(tmax_i % aw - ref_cx, tmax_i // aw - ref_cy)
        tip_i, grip_i = (tmax_i, tmin_i) if d1 > d0 else (tmin_i, tmax_i)
    else:
        tip_i, grip_i = tmax_i, tmin_i
    return {
        "n": n, "cx": cx, "cy": cy, "vx": vx, "vy": vy,
        "length": length, "width": n / length, "aspect": length / max(n / length, 0.8),
        "tipw": tipw, "shaftw": shaftw, "fill": fill,
        "tip": (tip_i % aw, tip_i // aw), "grip": (grip_i % aw, grip_i // aw),
        "bbox": (min(i % aw for i in pixs), min(i // aw for i in pixs),
                 max(i % aw for i in pixs) + 1, max(i // aw for i in pixs) + 1),
        "pixs": pixs,
    }

def _contour_trace(mask, aw, ah):
    """Moore 邻域轮廓跟踪（主组件最上左像素起步，屏幕坐标顺时针）。"""
    start = min((i for i in range(aw * ah) if mask[i]), key=lambda i: (i // aw, i % aw))
    dirs = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]  # E SE S SW W NW N NE
    def nb(i, d):
        x, y = i % aw + d[0], i // aw + d[1]
        return y * aw + x if 0 <= x < aw and 0 <= y < ah else None
    contour = [start]
    cur = start
    b = 4                      # 从 W 方向开始搜邻居
    for _ in range(8 * aw * ah):
        found = False
        for k in range(8):
            d = (b + k) % 8
            j = nb(cur, dirs[d])
            if j is not None and mask[j]:
                contour.append(j)
                b = (d + 5) % 8
                cur = j
                found = True
                break
        if not found:
            break
        if cur == start and len(contour) > 3:
            break
    return contour

def _straight_runs(pts, eps):
    """把有序轮廓点切成近似直线段。返回 [(i0, i1, 弦长, 弧长), ...]。"""
    runs = []
    n = len(pts)
    i = 0
    while i < n - 2:
        j = i + 1
        while j + 1 < n:
            ax, ay = pts[i]
            bx, by = pts[j + 1]
            L = math.hypot(bx - ax, by - ay)
            if L <= 0:
                j += 1
                continue
            ok = True
            for k in range(i, j + 2):
                px, py = pts[k]
                if abs((by - ay) * (px - ax) - (bx - ax) * (py - ay)) / L > eps:
                    ok = False
                    break
            if not ok:
                break
            j += 1
        chord = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
        if j - i >= 2 and chord >= 3:
            arc = sum(math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
                      for k in range(i, j))
            runs.append((i, j, chord, arc))
            i = j
        else:
            i += 1
    return runs

def _edge_candidates(mask, aw, ah, main_pixs, sx, sy, Hc):
    """贴手武器检测：武器杆会在剪影轮廓上留下长直线边缘，而身体轮廓是曲线。
    每条够长够直的轮廓边生成一个候选，收集直线带内的像素做形状统计。
    握点 = 体质心在杆线上的投影（横持棍取中部=双手握，单手持取靠身端=握手）。"""
    contour = _contour_trace(mask, aw, ah)
    pts = [(i % aw, i // aw) for i in contour]
    runs = _straight_runs(pts, max(2.0, 0.02 * Hc))
    cands = []
    for i0, i1, chord, arc in runs:
        if chord < 0.30 * Hc or arc / max(chord, 1) > 1.22:   # 斜线的阶梯轮廓会抬弧长，阈值放宽
            continue
        ax, ay = pts[i0]
        bx, by = pts[i1]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        dx, dy = dx / L, dy / L
        nx, ny = -dy, dx
        band = max(3.0, 0.055 * Hc)
        pixs = []
        for i in main_pixs:
            px, py = i % aw, i // aw
            t = (px - ax) * dx + (py - ay) * dy
            # 沿无限延长线收带内像素：杆是贯通的，轮廓边往往只覆盖半截
            if abs((px - ax) * nx + (py - ay) * ny) <= band:
                pixs.append(i)
        if len(pixs) < 30:
            continue
        # 中段细度守卫：武器杆中段必细；躯干/披风侧缘的条带中段宽 → 淘汰
        t_c0 = (sx - ax) * dx + (sy - ay) * dy
        mid = [i for i in pixs
               if 0.30 * L <= (i % aw - ax) * dx + (i // aw - ay) * dy <= 0.70 * L]
        if len(mid) < 10 or len(mid) / max(0.4 * L, 1) > 0.075 * Hc:
            continue
        st = _shape_stats(pixs, aw, ah, sx, sy)
        if st["aspect"] < 3.0 or st["width"] > 0.12 * Hc:
            continue
        # 握点：质心在杆线上的投影，收进杆段内
        t_c = max(0.08 * L, min(t_c0, 0.92 * L))
        grip = (ax + dx * t_c, ay + dy * t_c)
        if grip[1] > 0.85 * Hc:      # 握点不在手部高度带
            continue
        # 尖端：像素实际投影离质心投影最远的一端（枪头/斧头可以超出轮廓边端点）
        tmin = tmax = t_c
        for i in pixs:
            px, py = i % aw, i // aw
            t = (px - ax) * dx + (py - ay) * dy
            tmin = min(tmin, t); tmax = max(tmax, t)
        tip_t = tmax if (tmax - t_c) > (t_c - tmin) else tmin
        tip = (ax + dx * tip_t, ay + dy * tip_t)
        if tip[1] > 0.88 * Hc:       # 尖端垂到脚底 = 腿/垂摆，不是武器
            continue
        L_eff = max(st["length"], chord)
        conf = 1.0 + (0.5 if chord >= 0.6 * Hc else 0.0) + (0.3 if st["width"] <= 0.06 * Hc else 0.0)
        st = {**st, "length": L_eff, "width": len(pixs) / max(L_eff, 1)}
        cands.append((round(conf, 2), st, "attached", tip, grip))
    # 去重：同一根杆的左右两条轮廓边会生成重叠候选，保留置信最高者
    kept = []
    for c in sorted(cands, key=lambda c: -c[0]):
        b = c[1]["bbox"]
        dup = False
        for k in kept:
            b2 = k[1]["bbox"]
            ox = max(0, min(b[2], b2[2]) - max(b[0], b2[0]))
            oy = max(0, min(b[3], b2[3]) - max(b[1], b2[1]))
            inter = ox * oy
            amin = min((b[2] - b[0]) * (b[3] - b[1]), (b2[2] - b2[0]) * (b2[3] - b2[1]))
            if amin > 0 and inter / amin > 0.6:
                dup = True
                break
        if not dup:
            kept.append(c)
    return kept

def _classify(st, Hc):
    """直杆类武器的几何分类（弧形大件=弓 在调用处单独判）。"""
    L, W = st["length"], st["width"]
    if st["tipw"] >= 2.1 * max(st["shaftw"], 1.0) and st["shaftw"] <= 0.10 * Hc:
        return "axe"
    if L >= 1.30 * Hc:
        return "spear"
    if L >= 0.75 * Hc:
        return "sword"
    if L >= 0.22 * Hc:
        return "dagger"
    return None

def detect_weapon(rgba):
    """在透明底角色图上自动检测武器。返回 dict 或 None（含类别/包围盒/握点/朝向角/置信度）。

    坐标均为相对角色包围盒的 0~1 比例；angle0 为握点→尖端的角度（度，屏幕坐标顺时针为正）。
    检测双通道：①不接触主体的独立细长件（离手武器，直杆按长度分类、弧形大件=弓）；
    ②主体凸出扇区（贴手持武，横持长棍左右两端同时凸出会自动合并成一件）。"""
    w, h = rgba.size
    scale = min(1.0, 256.0 / max(w, h))
    aw, ah = max(4, int(w * scale)), max(4, int(h * scale))
    Hc = ah
    a = rgba.resize((aw, ah), Image.BILINEAR).getchannel("A")
    fg = bytearray(a.point(lambda v: 1 if v > 110 else 0).tobytes())
    comps = _components(fg, aw, ah)
    if not comps:
        return None
    main_area, main_pixs = comps[0]
    sx = sum(i % aw for i in main_pixs) / main_area
    sy = sum(i // aw for i in main_pixs) / main_area
    cands = []  # (置信度, 统计, 来源, tip, grip, 类别)
    # ① 不接触主体的独立细长件
    for area, pixs in comps[1:]:
        if area < max(40, 0.003 * main_area):
            continue
        st = _shape_stats(pixs, aw, ah, sx, sy)
        bw = st["bbox"][2] - st["bbox"][0]
        bh = st["bbox"][3] - st["bbox"][1]
        L = math.hypot(bw, bh)
        if L < 0.22 * Hc:
            continue
        # 像素到主轴的最大垂直距离：直杆贴轴，弓弧远离轴
        vx, vy = st["vx"], st["vy"]
        maxperp = 0.0
        for i in pixs:
            dx = i % aw - st["cx"]; dy = i // aw - st["cy"]
            maxperp = max(maxperp, abs(dx * vy - dy * vx))
        st2 = {**st, "length": L, "width": area / max(L, 1)}
        if maxperp > 0.12 * Hc and L >= 0.5 * Hc:
            cls = "bow"     # 弧形大件 = 弓
        else:
            cls = _classify(st2, Hc)
        if cls is None:
            continue
        conf = 2.0 if st["aspect"] >= 5 else 1.5
        cands.append((conf, st2, "separate", st["tip"], st["grip"], cls))
    # ② 贴手持武：剪影轮廓上的长直线边缘（武器杆），横持长棍的杆边横贯全身也能检出
    main_mask = bytearray(aw * ah)
    for i in main_pixs:
        main_mask[i] = 1
    for conf, st, src, tip, grip in _edge_candidates(main_mask, aw, ah, main_pixs, sx, sy, Hc):
        cands.append((conf, st, src, tip, grip, _classify(st, Hc)))
    cands = [c for c in cands if c[5] is not None]
    if not cands:
        return None
    conf, st, src, tip, grip, cls = max(cands, key=lambda c: c[0])
    if cls == "bow":   # 弓的握点在弓身中部而不是端点
        gx, gy = st["cx"], st["cy"]
    else:
        gx, gy = grip
    bx0, by0, bx1, by1 = st["bbox"]
    angle0 = math.degrees(math.atan2(tip[1] - gy, tip[0] - gx))
    return {
        "class": cls, "source": src, "confidence": round(conf, 2),
        "box": [bx0 / aw, by0 / ah, bx1 / aw, by1 / ah],
        "gripInBox": [(gx - bx0) / max(bx1 - bx0, 1), (gy - by0) / max(by1 - by0, 1)],
        "angle0": round(angle0, 1), "lengthRatio": round(st["length"] / Hc, 2),
    }

# ---------------- 部件与 rest 姿态 ----------------
DEFAULT_RIG = {
    "parts": {
        "head":      {"box": [0.34, 0.04, 0.66, 0.20], "pivot": [0.50, 0.90]},
        "torso":     {"box": [0.30, 0.18, 0.70, 0.56], "pivot": [0.50, 0.25]},
        "arm_back":  {"box": [0.14, 0.20, 0.38, 0.56], "pivot": [0.78, 0.28]},
        "arm_front": {"box": [0.62, 0.20, 0.90, 0.56], "pivot": [0.22, 0.28]},
        "leg_back":  {"box": [0.30, 0.54, 0.50, 0.99], "pivot": [0.50, 0.06]},
        "leg_front": {"box": [0.50, 0.54, 0.70, 0.99], "pivot": [0.50, 0.06]},
        "weapon":    {"box": [0.74, 0.00, 1.00, 0.30], "pivot": [0.10, 0.88]},
    },
    "zorder": ["arm_back", "leg_back", "torso", "head", "leg_front", "arm_front", "weapon"],
    "no_weapon": False,
}
WHOLE_RIG = {
    "parts": {"body": {"box": [0.0, 0.0, 1.0, 1.0], "pivot": [0.5, 0.9]}},
    "zorder": ["body"],
    "no_weapon": True,
}

# 各武器类别的 rest 姿态角（尖端指向；度，顺时针为正，0=正右，-60=右上）
REST_ANGLE_BY_CLASS = {
    "sword": -60, "dagger": -55, "spear": -20, "staff": -25,
    "axe": -65, "hammer": -65, "bow": -25, "gun": -8,
}
CLASS_TO_LIB = {
    "sword": "slash", "dagger": "slash", "spear": "thrust", "staff": "thrust",
    "axe": "smash", "hammer": "smash", "bow": "bow", "gun": "gun", "fist": "fist",
}
VALID_WEAPONS = ["auto", "none"] + list(REST_ANGLE_BY_CLASS.keys()) + ["fist"]

# ---------------- 动作库（v3：按武器类别分招式） ----------------
# 姿势约定：角色面向右；屏幕坐标 y 向下；rot 正 = 顺时针；位移 (dx, dy) 以 512px 画布为基准，负 dx = 向后。
# 武器 rot 相对 rest 姿态角（见 REST_ANGLE_BY_CLASS）；每段的招式互不相同，节奏由 SEG_RHYTHM 错开。
def K(t, **kw):
    d = {"t": t}
    d.update(kw)
    return d

MOTION_LIB = {
    "slash": {  # 剑/刀/短刃：横斩 → 反手上撩 → 蓄力重劈
        "1": {
            "windup": [
                K(0.0, arm_front=(10, 0, 0), arm_back=(-6, 0, 0), torso=(-4, -4, 0), weapon=(6, 0, 0), head=(-2, 0, 0)),
                K(1.0, arm_front=(-70, -16, 6), arm_back=(-40, -8, 0), torso=(-12, -12, 0), weapon=(-38, -20, 10), head=(-6, -2, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-70, -16, 6), arm_back=(-40, -8, 0), torso=(-12, -12, 0), weapon=(-38, -20, 10), head=(-6, -2, 0)),
                K(0.32, arm_front=(95, 30, -6), arm_back=(35, 12, 0), torso=(14, 20, 0), weapon=(122, 36, -10), head=(6, 4, 0)),
                K(0.7, arm_front=(80, 24, -4), arm_back=(28, 9, 0), torso=(10, 15, 0), weapon=(100, 28, -6), head=(5, 3, 0)),
                K(1.0, arm_front=(74, 22, -3), arm_back=(25, 8, 0), torso=(9, 13, 0), weapon=(92, 25, -5), head=(4, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(74, 22, -3), arm_back=(25, 8, 0), torso=(9, 13, 0), weapon=(92, 25, -5), head=(4, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 反手上撩：刀尖蓄到前下，撩到上前方
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-30, -10, 14), arm_back=(18, -4, 4), torso=(-8, -4, 3), weapon=(100, -8, 8), head=(-4, 0, 2)),
            ],
            "hit": [
                K(0.0, arm_front=(-30, -10, 14), arm_back=(18, -4, 4), torso=(-8, -4, 3), weapon=(100, -8, 8), head=(-4, 0, 2)),
                K(0.45, arm_front=(62, 22, -16), arm_back=(-14, 6, -4), torso=(10, 16, -3), weapon=(-8, 28, -16), head=(5, 2, -2)),
                K(1.0, arm_front=(50, 18, -10), arm_back=(-8, 4, -2), torso=(7, 12, -2), weapon=(-2, 22, -10), head=(3, 1, -1)),
            ],
            "recovery": [
                K(0.0, arm_front=(50, 18, -10), arm_back=(-8, 4, -2), torso=(7, 12, -2), weapon=(-2, 22, -10), head=(3, 1, -1)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 蓄力重劈：大幅举过头，劈到前下，重收
            "windup": [
                K(0.0, arm_front=(10, 0, 0), arm_back=(-6, 0, 0), torso=(-4, -4, 0), weapon=(6, 0, 0), head=(-2, 0, 0)),
                K(0.5, arm_front=(-40, -12, 4), arm_back=(-24, -6, 0), torso=(-9, -8, 0), weapon=(-30, -14, 6), head=(-4, -1, 0)),
                K(1.0, arm_front=(-88, -24, 8), arm_back=(-46, -12, 0), torso=(-16, -18, 0), weapon=(-70, -28, 12), head=(-8, -3, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-88, -24, 8), arm_back=(-46, -12, 0), torso=(-16, -18, 0), weapon=(-70, -28, 12), head=(-8, -3, 0)),
                K(0.30, arm_front=(105, 36, -8), arm_back=(40, 14, 0), torso=(18, 26, -2), weapon=(135, 42, -14), head=(8, 5, 0)),
                K(0.75, arm_front=(92, 30, -5), arm_back=(32, 11, 0), torso=(13, 20, -1), weapon=(116, 34, -8), head=(6, 3, 0)),
                K(1.0, arm_front=(84, 27, -4), arm_back=(29, 10, 0), torso=(11, 17, -1), weapon=(105, 30, -6), head=(5, 3, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(84, 27, -4), arm_back=(29, 10, 0), torso=(11, 17, -1), weapon=(105, 30, -6), head=(5, 3, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
    "thrust": {  # 枪/矛/杖：武器转角小、位移大——直线突刺才是枪的逻辑
        "1": {
            "windup": [
                K(0.0, arm_front=(4, 0, 0), arm_back=(-6, 0, 0), torso=(-3, -3, 0), weapon=(0, 0, 0), head=(-1, 0, 0)),
                K(1.0, arm_front=(-8, -16, 2), arm_back=(-20, -12, 0), torso=(-8, -14, 0), weapon=(-5, -18, 0), head=(-3, -1, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-8, -16, 2), arm_back=(-20, -12, 0), torso=(-8, -14, 0), weapon=(-5, -18, 0), head=(-3, -1, 0)),
                K(0.30, arm_front=(2, 50, -2), arm_back=(24, 20, 0), torso=(9, 26, 0), weapon=(7, 56, 0), head=(3, 3, 0)),
                K(0.65, arm_front=(0, 44, -1), arm_back=(20, 17, 0), torso=(7, 22, 0), weapon=(5, 48, 0), head=(2, 2, 0)),
                K(1.0, arm_front=(-2, 38, 0), arm_back=(16, 14, 0), torso=(5, 18, 0), weapon=(3, 41, 0), head=(2, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-2, 38, 0), arm_back=(16, 14, 0), torso=(5, 18, 0), weapon=(3, 41, 0), head=(2, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 疾步连刺：一收两送，两段快刺
            "windup": [
                K(0.0, arm_front=(-2, 30, 0), arm_back=(12, 10, 0), torso=(4, 14, 0), weapon=(2, 32, 0), head=(1, 1, 0)),
                K(1.0, arm_front=(-12, -22, 2), arm_back=(-24, -16, 0), torso=(-10, -18, 0), weapon=(-8, -24, 0), head=(-4, -1, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-12, -22, 2), arm_back=(-24, -16, 0), torso=(-10, -18, 0), weapon=(-8, -24, 0), head=(-4, -1, 0)),
                K(0.28, arm_front=(2, 46, -2), arm_back=(22, 18, 0), torso=(8, 24, 0), weapon=(6, 52, 0), head=(3, 2, 0)),
                K(0.45, arm_front=(-6, 30, 0), arm_back=(8, 8, 0), torso=(2, 12, 0), weapon=(-3, 34, 0), head=(1, 1, 0)),
                K(0.72, arm_front=(4, 54, -2), arm_back=(26, 22, 0), torso=(10, 28, 0), weapon=(8, 60, 0), head=(4, 3, 0)),
                K(1.0, arm_front=(-2, 40, 0), arm_back=(14, 12, 0), torso=(5, 18, 0), weapon=(3, 44, 0), head=(2, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-2, 40, 0), arm_back=(14, 12, 0), torso=(5, 18, 0), weapon=(3, 44, 0), head=(2, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 全力突刺：深蓄 + 大弓步 + 全身前压
            "windup": [
                K(0.0, arm_front=(4, 0, 0), arm_back=(-6, 0, 0), torso=(-3, -3, 0), weapon=(0, 0, 0), head=(-1, 0, 0)),
                K(0.45, arm_front=(-14, -20, 4), arm_back=(-26, -14, 0), torso=(-11, -18, 0), weapon=(-9, -22, 0), head=(-5, -2, 0)),
                K(1.0, arm_front=(-18, -26, 6), arm_back=(-30, -18, 0), torso=(-14, -24, 0), weapon=(-12, -28, 0), head=(-6, -3, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-18, -26, 6), arm_back=(-30, -18, 0), torso=(-14, -24, 0), weapon=(-12, -28, 0), head=(-6, -3, 0)),
                K(0.32, arm_front=(4, 62, -4), arm_back=(28, 26, 0), torso=(13, 34, -2), weapon=(9, 68, -2), head=(5, 5, 0)),
                K(0.7, arm_front=(0, 54, -2), arm_back=(22, 21, 0), torso=(10, 28, -1), weapon=(6, 58, -1), head=(3, 3, 0)),
                K(1.0, arm_front=(-4, 44, 0), arm_back=(17, 16, 0), torso=(7, 22, 0), weapon=(4, 48, 0), head=(2, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-4, 44, 0), arm_back=(17, 16, 0), torso=(7, 22, 0), weapon=(4, 48, 0), head=(2, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
    "smash": {  # 斧/锤：过顶劈 → 横扫 → 蓄力重砸，武器大转角 + 身体下压
        "1": {
            "windup": [
                K(0.0, arm_front=(10, 0, 0), arm_back=(-6, 0, 0), torso=(-4, -4, 0), weapon=(6, 0, 0), head=(-2, 0, 0)),
                K(1.0, arm_front=(-95, -24, 10), arm_back=(-44, -12, 0), torso=(-14, -14, 0), weapon=(-85, -26, 14), head=(-7, -2, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-95, -24, 10), arm_back=(-44, -12, 0), torso=(-14, -14, 0), weapon=(-85, -26, 14), head=(-7, -2, 0)),
                K(0.30, arm_front=(100, 32, -6), arm_back=(36, 13, 0), torso=(16, 22, -2), weapon=(125, 36, -8), head=(8, 4, 0)),
                K(0.72, arm_front=(86, 27, -4), arm_back=(29, 10, 0), torso=(12, 17, -1), weapon=(108, 30, -5), head=(6, 3, 0)),
                K(1.0, arm_front=(78, 24, -3), arm_back=(26, 9, 0), torso=(10, 14, 0), weapon=(98, 26, -4), head=(5, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(78, 24, -3), arm_back=(26, 9, 0), torso=(10, 14, 0), weapon=(98, 26, -4), head=(5, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 横扫：抡半圈扫前半平面
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-52, -16, 6), arm_back=(-30, -8, 0), torso=(-10, -10, 0), weapon=(-32, -18, 8), head=(-5, -1, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-52, -16, 6), arm_back=(-30, -8, 0), torso=(-10, -10, 0), weapon=(-32, -18, 8), head=(-5, -1, 0)),
                K(0.35, arm_front=(78, 30, 0), arm_back=(32, 12, 0), torso=(14, 18, 0), weapon=(118, 34, 2), head=(6, 3, 0)),
                K(1.0, arm_front=(62, 24, 0), arm_back=(24, 9, 0), torso=(9, 13, 0), weapon=(96, 27, 1), head=(4, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(62, 24, 0), arm_back=(24, 9, 0), torso=(9, 13, 0), weapon=(96, 27, 1), head=(4, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 蓄力重砸：高举过头深蓄，垂直砸地，全身下压
            "windup": [
                K(0.0, arm_front=(10, 0, 0), arm_back=(-6, 0, 0), torso=(-4, -4, 0), weapon=(6, 0, 0), head=(-2, 0, 0)),
                K(0.45, arm_front=(-55, -16, 6), arm_back=(-30, -10, 0), torso=(-10, -10, 0), weapon=(-45, -18, 8), head=(-5, -1, 0)),
                K(1.0, arm_front=(-108, -30, 14), arm_back=(-50, -16, 0), torso=(-17, -18, 0), weapon=(-100, -32, 18), head=(-9, -3, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-108, -30, 14), arm_back=(-50, -16, 0), torso=(-17, -18, 0), weapon=(-100, -32, 18), head=(-9, -3, 0)),
                K(0.28, arm_front=(112, 38, -6), arm_back=(42, 15, 0), torso=(20, 28, 4), weapon=(150, 44, -10), head=(9, 5, 2)),
                K(0.75, arm_front=(96, 32, -2), arm_back=(34, 12, 0), torso=(15, 22, 2), weapon=(128, 37, -6), head=(7, 4, 1)),
                K(1.0, arm_front=(88, 29, -1), arm_back=(30, 10, 0), torso=(12, 18, 1), weapon=(117, 33, -4), head=(6, 3, 1)),
            ],
            "recovery": [
                K(0.0, arm_front=(88, 29, -1), arm_back=(30, 10, 0), torso=(12, 18, 1), weapon=(117, 33, -4), head=(6, 3, 1)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
    "bow": {  # 弓：前手持弓基本不动，后手拉弦 → 松弦回弹；后坐由躯干小幅前顶表现
        "1": {  # 速射
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(3, 2, 0), arm_back=(-14, -12, 0), torso=(-3, -4, 0), weapon=(-2, 2, 0), head=(-2, 0, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(3, 2, 0), arm_back=(-14, -12, 0), torso=(-3, -4, 0), weapon=(-2, 2, 0), head=(-2, 0, 0)),
                K(0.25, arm_front=(-4, -3, 0), arm_back=(26, 8, 0), torso=(4, 4, 0), weapon=(3, -4, 0), head=(2, 1, 0)),
                K(1.0, arm_front=(-2, -1, 0), arm_back=(16, 4, 0), torso=(2, 2, 0), weapon=(1, -2, 0), head=(1, 0, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-2, -1, 0), arm_back=(16, 4, 0), torso=(2, 2, 0), weapon=(1, -2, 0), head=(1, 0, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 拉满瞄准：深拉 + 前探，松弦更狠
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(0.6, arm_front=(5, 3, 0), arm_back=(-26, -22, -2), torso=(-5, -8, 0), weapon=(-3, 2, 0), head=(-3, -1, 0)),
                K(1.0, arm_front=(7, 5, 0), arm_back=(-32, -28, -3), torso=(-7, -12, 0), weapon=(-4, 3, 0), head=(-4, -1, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(7, 5, 0), arm_back=(-32, -28, -3), torso=(-7, -12, 0), weapon=(-4, 3, 0), head=(-4, -1, 0)),
                K(0.25, arm_front=(-6, -5, 0), arm_back=(30, 10, 0), torso=(6, 7, 0), weapon=(4, -5, 0), head=(3, 2, 0)),
                K(1.0, arm_front=(-3, -2, 0), arm_back=(18, 5, 0), torso=(3, 3, 0), weapon=(2, -3, 0), head=(1, 1, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-3, -2, 0), arm_back=(18, 5, 0), torso=(3, 3, 0), weapon=(2, -3, 0), head=(1, 1, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 抛射：前手抬高、弓面仰起
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-14, 2, -10), arm_back=(-24, -20, -4), torso=(-6, -8, 0), weapon=(-16, 2, -6), head=(-6, -1, -2)),
            ],
            "hit": [
                K(0.0, arm_front=(-14, 2, -10), arm_back=(-24, -20, -4), torso=(-6, -8, 0), weapon=(-16, 2, -6), head=(-6, -1, -2)),
                K(0.25, arm_front=(-20, -3, -12), arm_back=(28, 9, 0), torso=(5, 6, 0), weapon=(-9, -5, -7), head=(2, 1, -1)),
                K(1.0, arm_front=(-12, -1, -7), arm_back=(17, 5, 0), torso=(2, 3, 0), weapon=(-5, -2, -4), head=(1, 0, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-12, -1, -7), arm_back=(17, 5, 0), torso=(2, 3, 0), weapon=(-5, -2, -4), head=(1, 0, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
    "gun": {  # 枪械：举枪 → 后坐上跳（枪口火花由 vfx 出）→ 收；点射段两次后坐
        "1": {
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-5, -3, -3), arm_back=(6, 4, 0), torso=(-2, -3, 0), weapon=(-4, -3, -2), head=(-2, 0, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-5, -3, -3), arm_back=(6, 4, 0), torso=(-2, -3, 0), weapon=(-4, -3, -2), head=(-2, 0, 0)),
                K(0.2, arm_front=(7, 2, -4), arm_back=(10, 6, 0), torso=(-5, -5, 0), weapon=(-11, 2, -5), head=(1, 1, 0)),
                K(0.6, arm_front=(3, 1, -2), arm_back=(7, 4, 0), torso=(-3, -3, 0), weapon=(-6, 1, -3), head=(1, 0, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(6, 3, 0), torso=(-1, -1, 0), weapon=(-3, 0, -1), head=(0, 0, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(6, 3, 0), torso=(-1, -1, 0), weapon=(-3, 0, -1), head=(0, 0, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 双发点射：两次后坐
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-5, -3, -3), arm_back=(6, 4, 0), torso=(-2, -3, 0), weapon=(-4, -3, -2), head=(-2, 0, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-5, -3, -3), arm_back=(6, 4, 0), torso=(-2, -3, 0), weapon=(-4, -3, -2), head=(-2, 0, 0)),
                K(0.2, arm_front=(7, 2, -4), arm_back=(10, 6, 0), torso=(-5, -5, 0), weapon=(-11, 2, -5), head=(1, 1, 0)),
                K(0.4, arm_front=(2, 1, -2), arm_back=(8, 5, 0), torso=(-3, -3, 0), weapon=(-5, 1, -3), head=(1, 0, 0)),
                K(0.6, arm_front=(7, 2, -4), arm_back=(10, 6, 0), torso=(-5, -5, 0), weapon=(-11, 2, -5), head=(1, 1, 0)),
                K(1.0, arm_front=(1, 0, -1), arm_back=(7, 4, 0), torso=(-2, -2, 0), weapon=(-4, 0, -2), head=(0, 0, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(1, 0, -1), arm_back=(7, 4, 0), torso=(-2, -2, 0), weapon=(-4, 0, -2), head=(0, 0, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 下蹲扫射：重心下压连续后坐
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-8, -4, -6), arm_back=(8, 5, 0), torso=(-4, -5, 2), weapon=(-6, -4, -5), head=(-3, -1, 1)),
            ],
            "hit": [
                K(0.0, arm_front=(-8, -4, -6), arm_back=(8, 5, 0), torso=(-4, -5, 2), weapon=(-6, -4, -5), head=(-3, -1, 1)),
                K(0.2, arm_front=(5, 2, -8), arm_back=(12, 7, 0), torso=(-7, -7, 3), weapon=(-12, 2, -8), head=(0, 1, 1)),
                K(0.45, arm_front=(0, 1, -6), arm_back=(9, 5, 0), torso=(-5, -5, 2), weapon=(-7, 1, -6), head=(0, 0, 1)),
                K(0.7, arm_front=(5, 2, -8), arm_back=(12, 7, 0), torso=(-7, -7, 3), weapon=(-12, 2, -8), head=(0, 1, 1)),
                K(1.0, arm_front=(-2, 0, -6), arm_back=(8, 4, 0), torso=(-4, -3, 2), weapon=(-5, 0, -5), head=(-1, 0, 1)),
            ],
            "recovery": [
                K(0.0, arm_front=(-2, 0, -6), arm_back=(8, 4, 0), torso=(-4, -3, 2), weapon=(-5, 0, -5), head=(-1, 0, 1)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), weapon=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
    "fist": {  # 徒手：直拳 → 上勾 → 冲拳（无武器）
        "1": {
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-14, -10, 2), arm_back=(8, -4, 0), torso=(-6, -6, 0), head=(-2, -1, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-14, -10, 2), arm_back=(8, -4, 0), torso=(-6, -6, 0), head=(-2, -1, 0)),
                K(0.30, arm_front=(6, 52, -2), arm_back=(-10, 6, 0), torso=(10, 16, 0), head=(3, 2, 0)),
                K(1.0, arm_front=(2, 40, 0), arm_back=(-6, 4, 0), torso=(7, 12, 0), head=(2, 1, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(2, 40, 0), arm_back=(-6, 4, 0), torso=(7, 12, 0), head=(2, 1, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "2": {  # 上勾拳：沉手蓄力，弧线上勾
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
                K(1.0, arm_front=(-48, -14, 10), arm_back=(14, -6, 4), torso=(-8, -8, 4), head=(-3, -1, 1)),
            ],
            "hit": [
                K(0.0, arm_front=(-48, -14, 10), arm_back=(14, -6, 4), torso=(-8, -8, 4), head=(-3, -1, 1)),
                K(0.35, arm_front=(46, 28, -14), arm_back=(-12, 6, -4), torso=(11, 18, -3), head=(4, 2, -2)),
                K(1.0, arm_front=(34, 20, -8), arm_back=(-7, 4, -2), torso=(7, 12, -2), head=(3, 1, -1)),
            ],
            "recovery": [
                K(0.0, arm_front=(34, 20, -8), arm_back=(-7, 4, -2), torso=(7, 12, -2), head=(3, 1, -1)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
        "3": {  # 冲拳：深蓄 + 大弓步全身前冲
            "windup": [
                K(0.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
                K(0.5, arm_front=(-20, -14, 4), arm_back=(10, -6, 0), torso=(-8, -10, 0), head=(-3, -1, 0)),
                K(1.0, arm_front=(-26, -20, 6), arm_back=(14, -8, 0), torso=(-11, -14, 0), head=(-4, -2, 0)),
            ],
            "hit": [
                K(0.0, arm_front=(-26, -20, 6), arm_back=(14, -8, 0), torso=(-11, -14, 0), head=(-4, -2, 0)),
                K(0.30, arm_front=(2, 70, -4), arm_back=(-16, 10, 0), torso=(15, 34, 0), head=(5, 4, 0)),
                K(0.7, arm_front=(0, 58, -2), arm_back=(-12, 8, 0), torso=(11, 26, 0), head=(4, 3, 0)),
                K(1.0, arm_front=(-3, 46, 0), arm_back=(-9, 6, 0), torso=(8, 20, 0), head=(3, 2, 0)),
            ],
            "recovery": [
                K(0.0, arm_front=(-3, 46, 0), arm_back=(-9, 6, 0), torso=(8, 20, 0), head=(3, 2, 0)),
                K(1.0, arm_front=(0, 0, 0), arm_back=(0, 0, 0), torso=(0, 0, 0), head=(0, 0, 0)),
            ],
        },
    },
}
LIB_NAMES = {"slash": "斩（剑/刀）", "thrust": "刺（枪/杖）", "smash": "砸（斧/锤）",
             "bow": "弓", "gun": "枪械", "fist": "徒手"}

# 各段节奏系数（前摇/攻击/后摇的帧数倍率）：第一段快、第三段重——多段连招节奏有别
SEG_RHYTHM = {1: (1.0, 0.85, 1.0), 2: (0.9, 0.9, 1.0), 3: (1.2, 1.05, 1.25)}
DENSITY = {"low": (6, 8, 6), "std": (8, 12, 8), "high": (10, 16, 10)}

# 整图模式：不切件，整张角色做前倾/后仰 + 位移（切件不适配时的体面降级，宁少勿多）
WHOLE_MOTION = {
    "windup": [K(0.0, body=(0, 0, 0)), K(1.0, body=(-11, -10, -2))],
    "hit": [K(0.0, body=(-11, -10, -2)), K(0.35, body=(17, 30, -4)), K(1.0, body=(11, 20, -2))],
    "recovery": [K(0.0, body=(11, 20, -2)), K(1.0, body=(0, 0, 0))],
}

def ease(t):
    return t * t * (3 - 2 * t)

def sample(motion, t, part_names):
    """任意数量关键帧之间的平滑插值（相邻关键帧内 smoothstep）。"""
    t = max(0.0, min(1.0, t))
    a, b = motion[0], motion[-1]
    for i in range(len(motion) - 1):
        if motion[i]["t"] <= t <= motion[i + 1]["t"]:
            a, b = motion[i], motion[i + 1]
            span = b["t"] - a["t"]
            t = (t - a["t"]) / span if span > 0 else 1.0
            break
    e = ease(t)
    out = {}
    for p in part_names:
        ra, da, dya = a.get(p, (0, 0, 0))
        rb, db, dyb = b.get(p, (0, 0, 0))
        out[p] = (ra + (rb - ra) * e, da + (db - da) * e, dya + (dyb - dya) * e)
    return out

def _rot(img, pivot, deg):
    """绕 pivot 旋转。返回 (旋转后的图, pivot 在新图中的坐标)。deg 顺时针为正。"""
    if abs(deg) < 0.01:
        return img, pivot
    out = img.rotate(-deg, expand=True, resample=Image.BICUBIC)  # PIL 逆时针为正，取负
    w0, h0 = img.size
    cx, cy = pivot
    ox, oy = cx - w0 / 2, cy - h0 / 2
    th = math.radians(deg)
    nx = ox * math.cos(th) - oy * math.sin(th)
    ny = ox * math.sin(th) + oy * math.cos(th)
    return out, (out.width / 2 + nx, out.height / 2 + ny)

def rotate_part(part, deg):
    return _rot(part["img"], part["pivot"], deg)

def save_rig_debug(char, rig, det, path):
    """切件调试叠层：部件 box/pivot + 武器检测框与主轴线画到角色图上。"""
    dbg = char.copy()
    d = ImageDraw.Draw(dbg)
    cw, ch = dbg.size
    palette = [(255, 80, 80), (80, 200, 255), (120, 255, 120), (255, 200, 80),
               (220, 120, 255), (255, 255, 120), (120, 255, 255)]
    for i, (name, cfg) in enumerate(rig["parts"].items()):
        col = palette[i % len(palette)]
        x0, y0, x1, y1 = cfg["box"]
        bx = [x0 * cw, y0 * ch, x1 * cw, y1 * ch]
        d.rectangle(bx, outline=col, width=3)
        px = bx[0] + cfg["pivot"][0] * (bx[2] - bx[0])
        py = bx[1] + cfg["pivot"][1] * (bx[3] - bx[1])
        d.ellipse([px - 5, py - 5, px + 5, py + 5], fill=col)
        d.text((bx[0] + 4, bx[1] + 4), name, fill=col)
    if det:
        x0, y0, x1, y1 = det["box"]
        d.rectangle([x0 * cw, y0 * ch, x1 * cw, y1 * ch], outline=(255, 120, 0), width=4)
        gx = x0 * cw + det["gripInBox"][0] * (x1 - x0) * cw
        gy = y0 * ch + det["gripInBox"][1] * (y1 - y0) * ch
        rad = math.radians(det["angle0"])
        L = ch * 0.5
        d.line([gx, gy, gx + math.cos(rad) * L, gy + math.sin(rad) * L], fill=(255, 120, 0), width=3)
        d.text((x0 * cw + 4, y0 * ch + 20),
               f"weapon: {det['class']} ({det['source']}, conf {det['confidence']})", fill=(255, 120, 0))
    dbg.save(path)

# 各动作库的武器遮挡：挥砍/重砸前摇武器收到身后（躯干之下），命中扫到身前——遮挡随时间变化带来纵深
WEAPON_Z = {
    "slash": {"windup": False, "hit": True, "recovery": True},
    "smash": {"windup": False, "hit": True, "recovery": True},
}
DEFAULT_WEAPON_Z = {"windup": True, "hit": True, "recovery": True}

def _part_scale(name, phase, t, cls):
    """伪 3D 深度缩放：命中相挥到一半朝镜头凸出 → 放大；前摇向后蓄力 → 微缩。"""
    if phase == "hit":
        b = math.sin(math.pi * min(1.0, max(0.0, t)))
        if name == "weapon":
            base = 0.10 if cls in ("slash", "smash") else 0.06
            return 1.0 + base * b
        if name in ("arm_front", "arm_back"):
            return 1.0 + 0.05 * b
        if name == "torso":
            return 1.0 + 0.03 * b
    if phase == "windup":
        b = math.sin(math.pi * min(1.0, max(0.0, t)))
        return 1.0 - 0.04 * b
    return 1.0

def render_frame(parts, rig, canvas_size, origin, pose, ghost=None, ss=1,
                 weapon_front=True, shadow=False, cls="fist", phase="recovery", t=1.0,
                 only_parts=None):
    """合成一帧。ss=超采样倍率（>1 时部件与画布须已是放大版，返回缩回 1x 的帧）。

    weapon_front=False 时武器画在躯干之下（前摇收到身后的遮挡关系）；
    shadow=True 画脚下椭圆软阴影（接地感）；pose 值第 4 位可选缩放系数。"""
    W, H = canvas_size
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # 地面软阴影：中心随躯干位移，落地在最低部件的底缘
    if shadow and parts:
        feet_y = origin[1] + max(p["bh"] * rig["parts"][n]["box"][3]
                                 for n, p in parts.items() if n in rig["parts"])
        tdx = pose.get("torso", pose.get("body", (0, 0, 0)))[1] * W / 512.0
        cw_sh = max(p["img"].width for p in parts.values()) * 0.42
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        cx = origin[0] + max(p["bw"] for p in parts.values()) * 0.5 + tdx * 0.7
        sd.ellipse([cx - cw_sh / 2, feet_y - 7 * ss, cx + cw_sh / 2, feet_y + 7 * ss],
                   fill=(10, 12, 20, 90))
        sh = sh.filter(ImageFilter.GaussianBlur(5 * ss))
        frame.alpha_composite(sh)
    for gimg, galpha in (ghost or []):
        g = gimg.copy()
        a = g.getchannel("A").point(lambda v: int(v * galpha))
        g.putalpha(a)
        frame.alpha_composite(g)
    body = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    zorder = list(rig["zorder"])
    if not weapon_front and "weapon" in zorder:
        zorder.remove("weapon")
        zorder.insert(zorder.index("torso"), "weapon")   # 沉到躯干之下
    for name in zorder:
        if name not in parts:
            continue
        if only_parts and name not in only_parts:
            continue
        cfg = rig["parts"][name]
        v = pose.get(name, (0, 0, 0))
        deg, dx, dy = v[0], v[1], v[2]
        sc = v[3] if len(v) > 3 else _part_scale(name, phase, t, cls)
        dx = dx * W / 512.0
        dy = dy * H / 512.0
        pimg, pf = parts[name]["img"], parts[name]["pivot"]
        if abs(sc - 1.0) > 0.005:
            ow, oh = pimg.width, pimg.height
            nw, nh = max(1, int(ow * sc)), max(1, int(oh * sc))
            pimg = pimg.resize((nw, nh), Image.BILINEAR)
            pf = (parts[name]["pivot"][0] * nw / ow, parts[name]["pivot"][1] * nh / oh)
        nat_x = origin[0] + cfg["box"][0] * parts[name]["bw"] + pf[0] + dx
        nat_y = origin[1] + cfg["box"][1] * parts[name]["bh"] + pf[1] + dy
        rot, piv = _rot(pimg, pf, deg)
        body.alpha_composite(rot, (int(nat_x - piv[0]), int(nat_y - piv[1])))
    if only_parts:   # 拖影子帧：裸部件，不加描边/光影
        frame.alpha_composite(body)
    else:
        # 渐变光影：顶部亮、底部暗，乘法混色只作用于角色像素
        grad = Image.new("L", (1, H))
        for y in range(H):
            grad.putpixel((0, y), int(150 + 55 * (1 - y / H)))   # 顶 205 → 底 150 的乘数
        grad = grad.resize((W, H))
        r, g, b, a = body.split()
        body = Image.merge("RGBA", (ImageChops.multiply(r, grad),
                                    ImageChops.multiply(g, grad),
                                    ImageChops.multiply(b, grad), a))
        # 描边（角色后面一圈深色轮廓）
        alpha = body.getchannel("A")
        outline = alpha.filter(ImageFilter.MaxFilter(5))
        edge = Image.new("RGBA", body.size, (0, 0, 0, 0))
        edge.putalpha(outline.point(lambda v: 255 if v > 10 else 0))
        dark = Image.new("RGBA", body.size, (24, 26, 34, 255))
        edge = Image.composite(dark, edge, edge.getchannel("A").point(lambda v: 255 if v > 10 else 0))
        frame.alpha_composite(edge)
        frame.alpha_composite(body)
        # 方向性 rim light：朝攻击方向的边缘光（角色面向右 → 右缘），命中相最亮、前摇微亮
        rim_i = 0.0
        if phase == "hit":
            rim_i = math.sin(math.pi * min(1.0, max(0.0, t)))
        elif phase == "windup":
            rim_i = 0.3 * math.sin(math.pi * min(1.0, max(0.0, t)))
        if rim_i > 0.06:
            a_ch = body.getchannel("A")
            shifted = Image.new("L", body.size, 0)
            shifted.paste(a_ch, (max(2, int(5 * ss)), 0))
            band = ImageChops.subtract(a_ch, shifted).point(lambda v: int(v * rim_i * 0.6))
            rim = Image.new("RGBA", body.size, (255, 240, 205, 0))
            rim.putalpha(band)
            frame.alpha_composite(rim)
    if ss > 1:
        return frame.resize((W // ss, H // ss), Image.LANCZOS)
    return frame

# ---------------- 帧时序（v4-②）：极值处停留、最快处跳过 ----------------
def _dur_mul(phase, t):
    if phase == "windup":
        return 1.0 + 0.5 * max(0.0, 1.0 - t / 0.45)          # 蓄力开头拖住
    if phase == "hit":
        return 1.0 - 0.45 * math.exp(-((t - 0.32) ** 2) / 0.02)   # apex 掠过
    if phase == "recovery":
        return 1.0 + 0.3 * max(0.0, (t - 0.7) / 0.3)          # 收尾定格
    return 1.0

# ---------------- 抠图统一入口（v4-⑨ rembg 可选档） ----------------
def _rembg_remove(src):
    try:
        from rembg import remove
        return remove(src).convert("RGBA"), True
    except ImportError:
        print('提示: pip install "rembg[cpu]" 可解锁复杂背景抠图（模型一次下载后离线使用）')
        return None, False
    except Exception as e:
        print(f"rembg 处理失败（{e}），回退到内置抠图")
        return None, False

def load_character_rgba(src, matting="auto"):
    """抠底 + rembg 可选升级 + 多主体取主的统一入口。返回 (RGBA, 是否可靠)。"""
    if matting == "rembg":
        out, ok = _rembg_remove(src)
        if out is not None:
            out, _ = main_subject_crop(out)
            return out, True
    rgba, ok = remove_bg(src)
    if not ok and matting == "auto":
        out, _ = _rembg_remove(src)
        if out is not None:
            out, _ = main_subject_crop(out)
            return out, True
    rgba, cropped = main_subject_crop(rgba)
    if cropped:
        ok = True
    return rgba, ok

# ---------------- 手动框选武器（v4-⑧） ----------------
def _weapon_from_box(char, box, cls):
    """用户手动框选武器区域：框内跑形状统计补握点/朝向；识别不出给默认握点。"""
    if cls is None:
        cls = "sword"
    w, h = char.size
    x0, y0, x1, y1 = [max(0.0, min(1.0, v)) for v in box]
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        print("WARN: --weapon-box 区域太小，忽略")
        return None
    region = char.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    a = region.getchannel("A")
    fg = bytearray(a.point(lambda v: 1 if v > 110 else 0).tobytes())
    comps = _components(fg, region.width, region.height)
    if comps and comps[0][0] > 30:
        st = _shape_stats(comps[0][1], region.width, region.height)
        grip, tip = st["grip"], st["tip"]
        if grip[1] < tip[1]:          # 握点默认取较低的一端
            grip, tip = tip, grip
        angle0 = round(math.degrees(math.atan2(tip[1] - grip[1], tip[0] - grip[0])), 1)
        grip_in = [grip[0] / region.width, grip[1] / region.height]
    else:
        angle0, grip_in = None, [0.12, 0.86]
    return {"class": cls, "source": "manual-box", "confidence": 3.0,
            "box": [x0, y0, x1, y1], "gripInBox": grip_in,
            "angle0": angle0, "lengthRatio": None}

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rig", help="部件比例 JSON（可用 --out 输出的 rig.json 微调后回传）")
    ap.add_argument("--mode", choices=["cutout", "whole"], default="cutout",
                    help="cutout=切部件（默认）；whole=整张角色做前倾/位移，切件不适配时用")
    ap.add_argument("--segments", type=int, default=3)
    ap.add_argument("--density", choices=list(DENSITY.keys()), default="high",
                    help="帧密度：low=6/8/6，std=8/12/8，high=10/16/10（默认，动画更流畅）")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--ss", type=int, default=2, choices=[1, 2],
                    help="超采样倍率：2（默认）抗锯齿更细；1 更快")
    ap.add_argument("--weapon", default="auto", choices=VALID_WEAPONS,
                    help="武器：auto=自动检测（默认）；none/fist=徒手；或指定类别覆盖检测结果")
    ap.add_argument("--weapon-box", default=None,
                    help='手动框选武器区域 "x0,y0,x1,y1"（相对角色包围盒 0~1），优先于自动检测')
    ap.add_argument("--matting", choices=["auto", "flood", "rembg"], default="auto",
                    help="抠图：auto=洪水填充失败时自动尝试 rembg（已安装才生效）；flood=只用内置；rembg=强制")
    ap.add_argument("--no-idle", action="store_true", help="跳过 idle 呼吸待机生成")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    src = Image.open(args.image)
    rgba, bg_ok = load_character_rgba(src, args.matting)
    if not bg_ok:
        print('WARN: 背景可能不干净——建议先换纯色底、手动抠图，或 pip install "rembg[cpu]" 启用高质档')
    bbox = rgba.getbbox()
    char = rgba.crop(bbox)
    cw, ch = char.size
    print(f"抠图完成：主体 {cw}x{ch}（{'自动裁剪过' if (cw, ch) != rgba.size else '整图'}）")

    if args.mode == "whole":
        rig = json.loads(json.dumps(WHOLE_RIG))
        motion_lib_name, det = "fist", None
        part_names = ["body"]
    else:
        if args.rig:
            rig = json.load(open(args.rig, encoding="utf-8"))
            det = rig.get("weaponDetection")
            motion_lib_name = rig.get("motionLib") or CLASS_TO_LIB.get(rig.get("weaponClass"), "slash")
        else:
            rig = json.loads(json.dumps(DEFAULT_RIG))
            # 武器识别（none/fist 除外；指定类别时仍检测取真实武器框，仅覆盖类别）
            if args.weapon_box:
                det = _weapon_from_box(char, [float(v) for v in args.weapon_box.split(",")],
                                       args.weapon if args.weapon not in ("auto", "none", "fist") else None)
            elif args.weapon in ("none", "fist"):
                det = None
            else:
                det = detect_weapon(char)
            if args.weapon in ("none", "fist"):
                cls = "fist"
            elif det is not None and det.get("source") == "manual-box":
                cls = det["class"]
            elif args.weapon != "auto":
                cls = args.weapon
                if det:
                    det["class"] = cls
                else:   # 没检测到 → 退回默认框
                    rig["parts"]["weapon"] = dict(DEFAULT_RIG["parts"]["weapon"])
                    det = {"class": cls, "box": DEFAULT_RIG["parts"]["weapon"]["box"],
                           "gripInBox": DEFAULT_RIG["parts"]["weapon"]["pivot"],
                           "angle0": None, "confidence": 0, "source": "manual"}
            else:
                cls = det["class"] if det else "fist"
            if det is not None and cls != "fist":
                # 把检测/手选的武器框真正赋给部件（v3 曾漏掉这一行：部件一直用默认比例框）
                rig["parts"]["weapon"] = {"box": det["box"], "pivot": det["gripInBox"]}
            if cls == "fist" or det is None:
                rig["parts"].pop("weapon", None)
                rig["zorder"] = [z for z in rig["zorder"] if z != "weapon"]
                cls = "fist"
            rig["no_weapon"] = cls == "fist"
            rig["weaponClass"] = cls
            rig["motionLib"] = CLASS_TO_LIB[cls]
            rig["weaponDetection"] = det
            motion_lib_name = CLASS_TO_LIB[cls]
        part_names = ["arm_front", "arm_back", "torso", "weapon", "head"]
        part_names = [p for p in part_names if p in rig["parts"]]
        save_rig_debug(char, rig, det, os.path.join(args.out, "rig-debug.png"))
        print(f"切件检查图 -> {os.path.join(args.out, 'rig-debug.png')}（部件/武器框歪了就改 rig.json 的 box/pivot，--rig 重跑；始终不齐整就换 --mode whole）")
    if det and motion_lib_name != "fist":
        print(f"武器检测：类别 {det['class']}（{det['source']}，置信 {det['confidence']}），"
              f"长度比 {det.get('lengthRatio')}，朝向 {det.get('angle0')}° -> 动作库「{LIB_NAMES[motion_lib_name]}」")
        if det.get("confidence", 0) < 1.0:
            print("  置信偏低：请看 rig-debug.png 确认武器框；不对就 --weapon 指定类别重跑")

    with open(os.path.join(args.out, "rig.json"), "w", encoding="utf-8") as f:
        rig["density"] = args.density
        rig["fps"] = args.fps
        json.dump(rig, f, ensure_ascii=False, indent=2)

    # 武器规范化：武器切件预旋转到该类别的战斗 rest 角（绕握点）；
    # 起手过渡向量在画布尺寸确定后计算（trans），第一段前摇内从源图姿态平滑过渡到战斗握姿
    det = rig.get("weaponDetection")
    canon = bool(det) and det.get("angle0") is not None and motion_lib_name != "fist" and "weapon" in rig["parts"]
    rest_angle = REST_ANGLE_BY_CLASS.get(rig.get("weaponClass"), -60)

    # 切件（cutout 模式）
    parts = {}
    for name, cfg in rig["parts"].items():
        x0, y0, x1, y1 = cfg["box"]
        crop = char.crop((max(0, int(x0 * cw)), max(0, int(y0 * ch)),
                          min(cw, int(x1 * cw)), min(ch, int(y1 * ch))))
        pivot = (cfg["pivot"][0] * crop.width, cfg["pivot"][1] * crop.height)
        if canon and name == "weapon":
            crop, pivot = _rot(crop, pivot, rest_angle - det["angle0"])
        parts[name] = {"img": crop, "bw": cw, "bh": ch, "pivot": pivot}

    ss = args.ss
    parts_ss = {n: {"img": p["img"].resize((p["img"].width * ss, p["img"].height * ss), Image.LANCZOS),
                    "bw": p["bw"], "bh": p["bh"],
                    "pivot": (p["pivot"][0] * ss, p["pivot"][1] * ss)} for n, p in parts.items()}

    pad = int(max(cw, ch) * 0.42)
    W, H = cw + pad * 2, ch + pad * 2
    origin = (pad, pad)

    # 武器运动学挂接预计算：手端相对肩 pivot 的自然偏移（1x 画布 px）。
    # 武器位移改为跟随手端的弧线位移（手臂绕肩旋转时武器不脱手），动作库里武器自带的线性位移在挂接时作废。
    hand_off = None
    if args.mode == "cutout" and "arm_front" in rig["parts"] and "weapon" in rig["parts"]:
        acfg = rig["parts"]["arm_front"]
        aw_ = max(1, int(acfg["box"][2] * cw) - int(acfg["box"][0] * cw))
        ah_ = max(1, int(acfg["box"][3] * ch) - int(acfg["box"][1] * ch))
        sh_x = origin[0] + acfg["box"][0] * cw + acfg["pivot"][0] * aw_
        sh_y = origin[1] + acfg["box"][1] * ch + acfg["pivot"][1] * ah_
        px_, py_ = acfg["pivot"]
        hx_c, hy_c = max([(0, 0), (1, 0), (0, 1), (1, 1)],
                         key=lambda c: (c[0] - px_) ** 2 + (c[1] - py_) ** 2)
        hx = origin[0] + acfg["box"][0] * cw + hx_c * aw_
        hy = origin[1] + acfg["box"][1] * ch + hy_c * ah_
        hand_off = (hx - sh_x, hy - sh_y)

    # 起手过渡向量：rot 从源图朝向衰减到 0（切件已预旋转到 rest，故 t=0 时叠加全额反向偏移即还原源图姿态）；
    # 位移从自然握点渐入到前臂手端（战斗握姿）
    trans = None
    if canon:
        wcfg = rig["parts"]["weapon"]
        wcrop_w = max(1, int(wcfg["box"][2] * cw) - int(wcfg["box"][0] * cw))
        wcrop_h = max(1, int(wcfg["box"][3] * ch) - int(wcfg["box"][1] * ch))
        gx = origin[0] + wcfg["box"][0] * cw + det["gripInBox"][0] * wcrop_w
        gy = origin[1] + wcfg["box"][1] * ch + det["gripInBox"][1] * wcrop_h
        if "arm_front" in rig["parts"]:
            aw_ = max(1, int(rig["parts"]["arm_front"]["box"][2] * cw) - int(rig["parts"]["arm_front"]["box"][0] * cw))
            ah_ = max(1, int(rig["parts"]["arm_front"]["box"][3] * ch) - int(rig["parts"]["arm_front"]["box"][1] * ch))
            hx = origin[0] + rig["parts"]["arm_front"]["box"][0] * cw + hx_c * aw_
            hy = origin[1] + rig["parts"]["arm_front"]["box"][1] * ch + hy_c * ah_
        else:
            hx, hy = gx, gy
        trans = {
            "rot": det["angle0"] - rest_angle,
            "dx": (hx - gx) * 512.0 / W,
            "dy": (hy - gy) * 512.0 / H,
        }
        rig["weaponTransition"] = {"fromAngle": det["angle0"], "toAngle": rest_angle,
                                   "gripDeltaChar": [round((hx - gx) / cw, 4), round((hy - gy) / ch, 4)]}
        print(f"武器起手过渡：源图朝向 {det['angle0']}° -> 战斗 rest {rest_angle}°"
              f"（握点挪距 {math.hypot(hx - gx, hy - gy) / max(cw, ch):.0%} 身位，第一段前摇内完成）")
        with open(os.path.join(args.out, "rig.json"), "w", encoding="utf-8") as f:
            json.dump(rig, f, ensure_ascii=False, indent=2)

    density = DENSITY[args.density]
    if args.mode == "whole":
        lib = {"1": WHOLE_MOTION, "2": WHOLE_MOTION, "3": WHOLE_MOTION}
    else:
        lib = MOTION_LIB[motion_lib_name]
        if hand_off is not None:   # 挂接手的弧线位移后，武器自带的线性位移作废，只保留自转
            for mv in lib.values():
                for keys in mv.values():
                    for k in keys:
                        if "weapon" in k:
                            k["weapon"] = (k["weapon"][0], 0, 0)
    print(f"动作库「{LIB_NAMES[motion_lib_name] if motion_lib_name in LIB_NAMES else '整图'}」· "
          f"密度 {args.density}（前{density[0]}/攻{density[1]}/后{density[2]}）· {args.fps}fps · {ss}x 超采样")

    base_ms = 1000.0 / args.fps
    seg_counts, durations, active, qa = {}, {}, {}, {}
    _qa_dbg = {"last": None}
    qa_thr = max(24.0, 0.16 * max(cw, ch))   # 武器脱手判定阈值（1x px）
    margin = 0.08 * max(cw, ch)              # 画布越界容差（1x px）

    def finish_pose(base_pose, tt):
        """武器挂接手的弧线位移 + 第一段前摇的起手过渡。"""
        p = dict(base_pose)
        if hand_off is not None and "weapon" in p and "arm_front" in p:
            th_ = math.radians(p["arm_front"][0])
            dhx = hand_off[0] * (math.cos(th_) - 1) - hand_off[1] * math.sin(th_)
            dhy = hand_off[0] * math.sin(th_) + hand_off[1] * (math.cos(th_) - 1)
            wr, wdx, wdy = p["weapon"]
            p["weapon"] = (wr, wdx + dhx * 512.0 / W, wdy + dhy * 512.0 / H)
        if trans is not None and "weapon" in p:
            if seg == 1 and phase == "windup":
                e = ease(tt)      # 起手：旋转偏移衰减、握点挪距渐入
                wr, wdx, wdy = p["weapon"]
                p["weapon"] = (wr + trans["rot"] * (1 - e),
                               wdx + trans["dx"] * e, wdy + trans["dy"] * e)
            else:
                # 挪距永久生效（含 seg2+ 与 idle）——武器从第二段起始终握在手里，段间不回弹
                wr, wdx, wdy = p["weapon"]
                p["weapon"] = (wr, wdx + trans["dx"], wdy + trans["dy"])
        return p

    def qa_measure(pose):
        """逐帧 QA（1x 坐标）：武器脱手距离 / 部件画布越界。"""
        d = None
        if hand_off is not None and "weapon" in pose and "arm_front" in pose:
            wpiv = parts["weapon"]["pivot"]
            gx = origin[0] + rig["parts"]["weapon"]["box"][0] * cw + wpiv[0] + pose["weapon"][1] * W / 512.0
            gy = origin[1] + rig["parts"]["weapon"]["box"][1] * ch + wpiv[1] + pose["weapon"][2] * H / 512.0
            th_ = math.radians(pose["arm_front"][0])
            hx_ = sh_x + hand_off[0] * math.cos(th_) - hand_off[1] * math.sin(th_)
            hy_ = sh_y + hand_off[0] * math.sin(th_) + hand_off[1] * math.cos(th_)
            d = math.hypot(gx - hx_, gy - hy_)
            _qa_dbg["last"] = (round(gx), round(gy), round(hx_), round(hy_))
        ov = None
        for name in pose:
            if name not in parts:
                continue
            cfg = rig["parts"][name]
            pf = parts[name]["pivot"]
            left = origin[0] + cfg["box"][0] * parts[name]["bw"] + pf[0] + pose[name][1] * W / 512.0 - pf[0]
            top = origin[1] + cfg["box"][1] * parts[name]["bh"] + pf[1] + pose[name][2] * H / 512.0 - pf[1]
            if left < -margin or top < -margin or \
               left + parts[name]["img"].width > cw + 2 * pad + margin or \
               top + parts[name]["img"].height > ch + 2 * pad + margin:
                ov = name
        return d, ov

    for seg in range(1, args.segments + 1):
        move = lib[str(min(seg, len(lib)))]          # 4 段以上末段复用（宁少勿多）
        rhythm = SEG_RHYTHM.get(seg, (1.1, 1.0, 1.1))
        fdir = os.path.join(args.out, "frames", f"seg{seg}")
        os.makedirs(fdir, exist_ok=True)
        frames = []
        prev = prev2 = None
        idx = 1
        counts = []
        for phase_i, phase in enumerate(("windup", "hit", "recovery")):
            n = max(4, round(density[phase_i] * rhythm[phase_i]))
            counts.append(n)
            for i in range(n):
                t = i / (n - 1) if n > 1 else 1.0
                pose = finish_pose(sample(move[phase], t, part_names), t)
                ghost = []
                if phase == "hit" and prev is not None:
                    ghost.append((prev, 0.30))
                    if t < 0.5 and prev2 is not None:
                        ghost.append((prev2, 0.15))
                wfront = WEAPON_Z.get(motion_lib_name, DEFAULT_WEAPON_Z)[phase]
                frame = render_frame(parts_ss, rig, (W * ss, H * ss), (origin[0] * ss, origin[1] * ss),
                                     pose, ghost, ss=ss, weapon_front=wfront, shadow=True,
                                     cls=motion_lib_name, phase=phase, t=t)
                # 帧内拖影：最快的一段把武器按子角度多重曝光，画出速度弧
                if phase == "hit" and 0.08 <= t <= 0.55 and "weapon" in pose:
                    step_t = 1.0 / max(n - 1, 1)
                    for back, salpha in ((0.4, 0.30), (0.8, 0.15)):
                        ts = t - back * step_t
                        if ts < 0:
                            continue
                        pose_s = finish_pose(sample(move[phase], ts, part_names), ts)
                        sm = render_frame(parts_ss, rig, (W * ss, H * ss), (origin[0] * ss, origin[1] * ss),
                                          pose_s, ss=ss, weapon_front=True, shadow=False,
                                          cls=motion_lib_name, phase=phase, t=ts, only_parts=("weapon",))
                        sm.putalpha(sm.getchannel("A").point(lambda v: int(v * salpha)))
                        frame.alpha_composite(sm)
                # ① 逐帧 QA
                det_, ov_ = qa_measure(pose)
                q = qa.setdefault(seg, {"detachMax": 0.0, "detachFrames": [], "overflow": [], "empty": []})
                if det_ is not None and not (seg == 1 and phase == "windup"):
                    q["detachMax"] = max(q["detachMax"], det_)
                    if det_ > qa_thr:
                        q["detachFrames"].append([idx, round(det_)])
                if ov_:
                    q["overflow"].append(idx)
                if frame.getchannel("A").getextrema()[1] == 0:
                    q["empty"].append(idx)
                # ② 帧时序：极值停留、apex 掠过
                durations.setdefault(seg, []).append(int(round(base_ms * _dur_mul(phase, t))))
                if phase == "hit" and 0.30 <= t <= 0.65:
                    active.setdefault(seg, []).append(idx)
                fp = os.path.join(fdir, f"f{idx:03d}.png")
                frame.save(fp)
                frames.append(frame)
                prev2, prev = prev, frame
                idx += 1
        seg_counts[seg] = tuple(counts)
        frames[0].save(os.path.join(args.out, f"seg{seg}.gif"), save_all=True,
                       append_images=frames[1:], duration=durations[seg], loop=0)
        sheet = Image.new("RGBA", (W * len(frames), H), (0, 0, 0, 0))
        for i, fr in enumerate(frames):
            sheet.alpha_composite(fr, (i * W, 0))
        os.makedirs(os.path.join(args.out, "sheets"), exist_ok=True)
        sheet.save(os.path.join(args.out, "sheets", f"seg{seg}-sheet.png"))
        print(f"seg{seg}: {len(frames)} 帧 -> {fdir}")

    # ⑤ idle 呼吸待机循环（48 帧 @30fps，正弦节拍；武器随前手轻摆）
    idle_durs = None
    if not args.no_idle:
        n_idle = 48
        fdir = os.path.join(args.out, "frames", "idle")
        os.makedirs(fdir, exist_ok=True)
        frames = []
        idle_durs = []
        for i in range(n_idle):
            ph = 2 * math.pi * i / n_idle
            if args.mode == "whole":
                pose = {"body": (0, 0, 1.6 * math.sin(ph), 1 + 0.012 * math.sin(ph))}
            else:
                pose = {"torso": (0, 0, 1.4 * math.sin(ph), 1 + 0.012 * math.sin(ph)),
                        "head": (0, 0, 0.9 * math.sin(ph - 0.5)),
                        "arm_front": (2.0 * math.sin(ph - 0.4), 0, 0.4 * math.sin(ph - 0.4)),
                        "arm_back": (-2.0 * math.sin(ph - 0.7), 0, 0.3 * math.sin(ph - 0.7)),
                        "weapon": (2.5 * math.sin(ph - 0.6), 0, 0)}
                pose = {k: v for k, v in pose.items() if k in part_names}
                if hand_off is not None and "weapon" in pose and "arm_front" in pose:
                    th_ = math.radians(pose["arm_front"][0])
                    dhx = hand_off[0] * (math.cos(th_) - 1) - hand_off[1] * math.sin(th_)
                    dhy = hand_off[0] * math.sin(th_) + hand_off[1] * (math.cos(th_) - 1)
                    wr, wdx, wdy = pose["weapon"]
                    pose["weapon"] = (wr, wdx + dhx * 512.0 / W, wdy + dhy * 512.0 / H)
                if trans is not None and "weapon" in pose:   # idle 里握点挪距同样保持
                    wr, wdx, wdy = pose["weapon"]
                    pose["weapon"] = (wr, wdx + trans["dx"], wdy + trans["dy"])
            frame = render_frame(parts_ss, rig, (W * ss, H * ss), (origin[0] * ss, origin[1] * ss),
                                 pose, ss=ss, weapon_front=True, shadow=True,
                                 cls="fist", phase="recovery", t=1.0)
            fp = os.path.join(fdir, f"f{i + 1:03d}.png")
            frame.save(fp)
            frames.append(frame)
            idle_durs.append(int(round(base_ms * (1.0 + 0.2 * math.sin(2 * ph)))))
        frames[0].save(os.path.join(args.out, "idle.gif"), save_all=True,
                       append_images=frames[1:], duration=idle_durs, loop=0)
        sheet = Image.new("RGBA", (W * len(frames), H), (0, 0, 0, 0))
        for i, fr in enumerate(frames):
            sheet.alpha_composite(fr, (i * W, 0))
        sheet.save(os.path.join(args.out, "sheets", "idle-sheet.png"))
        print(f"idle: {n_idle} 帧呼吸待机 -> {fdir}")

    # ① QA 报告 + ⑥ ⑥timing.json（逐帧时长 + activeFrames）
    qa_all_ok = True
    for s in sorted(qa):
        q = qa[s]
        bad = q["detachFrames"] or q["overflow"] or q["empty"]
        if bad:
            qa_all_ok = False
            print(f"QA WARN seg{s}: 脱手超阈 {len(q['detachFrames'])} 帧（最大 {q['detachMax']:.0f}px / 阈 {qa_thr:.0f}px）"
                  f"· 越界 {len(q['overflow'])} 帧 · 空帧 {len(q['empty'])}")
        else:
            print(f"QA PASS seg{s}: 最大脱手距离 {q['detachMax']:.0f}px（阈 {qa_thr:.0f}px），无越界无空帧")
    with open(os.path.join(args.out, "qa-report.json"), "w", encoding="utf-8") as f:
        json.dump({"thresholdPx": round(qa_thr, 1), "ok": qa_all_ok, "segments": qa,
                   "debugLast": _qa_dbg["last"],
                   "handOff": hand_off, "sh": [sh_x, sh_y] if hand_off else None},
                  f, ensure_ascii=False, indent=2)
    timing = {"fps": args.fps, "unit": "ms",
              "segments": {str(s): {"windupFrames": seg_counts[s][0], "hitFrames": seg_counts[s][1],
                                     "recoveryFrames": seg_counts[s][2],
                                     "durations": durations[s], "activeFrames": active.get(s, [])}
                           for s in seg_counts},
              "idle": {"durations": idle_durs} if idle_durs else None,
              "note": "activeFrames = hit 相内 30%~65% 进度的绝对帧号（1 起），引擎在此窗口开 hitbox；"
                       "durations 为逐帧时长（极值停顿），引擎按帧读取而非等间隔"}
    with open(os.path.join(args.out, "timing.json"), "w", encoding="utf-8") as f:
        json.dump(timing, f, ensure_ascii=False, indent=2)
    print(f"质检: {'全部通过' if qa_all_ok else '存在 WARN（见上）'} · timing.json / qa-report.json 已写出")

    print(f"完成：{args.segments} 段 · 密度 {args.density} · 输出目录 {args.out}")

if __name__ == "__main__":
    main()
