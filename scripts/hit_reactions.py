# -*- coding: utf-8 -*-
"""
hit_reactions.py —— 程序化敌人受击动画 4 套（正面/背面 × 轻/重）v3

用法:
  python hit_reactions.py --image enemy.png --out build/ [--rig rig.json]
      [--hit-by slash|thrust|smash|bow|gun|fist] [--fps 30]

输出:
  <out>/hit-reactions/{light-front,light-back,heavy-front,heavy-back}.gif
  <out>/hit-reactions/sheets/xxx-sheet.png
  <out>/hit-reactions/rig.json            实际使用的部件配置（可微调后 --rig 重跑）

规则（见 references/vfx-notes.md）:
  轻受击原地硬直小动作（12 帧）；重受击大后仰 + 踉跄（24 帧）；
  --hit-by 按主角色攻击方式给出对应的反应：挥砍甩头狠、突刺击退深、重砸下压、
  弓/枪械后坐小、徒手拧身；前 4 帧白闪强化命中感；
  出血方向不烘进动画——由 combo-spec.json 的 bloodDirections 参数化。

依赖: Pillow（与 cutout_animator.py 同目录使用）
"""
import argparse, json, os
from PIL import Image

from cutout_animator import remove_bg, main_subject_crop, DEFAULT_RIG, WHOLE_RIG, render_frame

# 受击动作曲线：(rot 度, dx, dy @512 宽基准)，负 dx = 向后仰/击退（角色面向右）
REACTIONS = {
    "light-front": [
        {"t": 0.0, "head": (-10, -6, -2), "torso": (-7, -4, 0), "arm_front": (10, -4, 0), "arm_back": (-10, -4, 0)},
        {"t": 0.35, "head": (-16, -12, -2), "torso": (-11, -9, 0), "arm_front": (18, -7, 0), "arm_back": (-16, -7, 0)},
        {"t": 0.7, "head": (-6, -5, -1), "torso": (-4, -3, 0), "arm_front": (6, -2, 0), "arm_back": (-6, -2, 0)},
        {"t": 1.0, "head": (0, 0, 0), "torso": (0, 0, 0), "arm_front": (0, 0, 0), "arm_back": (0, 0, 0)},
    ],
    "heavy-front": [
        {"t": 0.0,  "head": (-16, -14, -4), "torso": (-22, -18, -2), "arm_front": (26, -10, 0), "arm_back": (-22, -10, 0)},
        {"t": 0.22, "head": (-24, -32, -6), "torso": (-30, -40, -4), "arm_front": (36, -20, 0), "arm_back": (-30, -20, 0)},
        {"t": 0.42, "head": (-13, -44, -2), "torso": (-15, -52, 0),  "arm_front": (20, -24, 0), "arm_back": (-15, -24, 0)},
        {"t": 0.58, "head": (-19, -48, -3), "torso": (-21, -56, -1), "arm_front": (25, -28, 0), "arm_back": (-21, -28, 0)},
        {"t": 0.78, "head": (-8, -30, 0),   "torso": (-8, -36, 0),   "arm_front": (10, -18, 0), "arm_back": (-8, -18, 0)},
        {"t": 0.9,  "head": (-3, -12, 0),   "torso": (-3, -14, 0),   "arm_front": (4, -7, 0),   "arm_back": (-3, -7, 0)},
        {"t": 1.0,  "head": (0, 0, 0),      "torso": (0, 0, 0),      "arm_front": (0, 0, 0),    "arm_back": (0, 0, 0)},
    ],
}
FLASH_FRAMES = 4   # 开头白闪帧数

# 受击反应随攻击方式对应（kb=水平击退倍率, rot=拧身甩头倍率, drop=下压强度）
HIT_BY = {
    "default": {"kb": 1.0,  "rot": 1.0,  "drop": 0.0},
    "slash":   {"kb": 1.0,  "rot": 1.3,  "drop": 0.0},
    "thrust":  {"kb": 1.35, "rot": 0.8,  "drop": 0.0},
    "smash":   {"kb": 0.9,  "rot": 1.0,  "drop": 1.0},
    "bow":     {"kb": 0.85, "rot": 0.9,  "drop": 0.0},
    "gun":     {"kb": 0.8,  "rot": 0.7,  "drop": 0.0},
    "fist":    {"kb": 0.9,  "rot": 1.1,  "drop": 0.0},
}

def sample_curve(keys, t):
    """在关键帧曲线间平滑插值，返回 {part: (rot, dx, dy)}。"""
    t = max(0.0, min(1.0, t))
    a, b = keys[0], keys[-1]
    for i in range(len(keys) - 1):
        if keys[i]["t"] <= t <= keys[i + 1]["t"]:
            a, b = keys[i], keys[i + 1]
            span = b["t"] - a["t"]
            t = (t - a["t"]) / span if span > 0 else 1.0
            break
    e = t * t * (3 - 2 * t)  # smoothstep
    out = {}
    for p in a:
        if p == "t":
            continue
        ra, da, dya = a.get(p, (0, 0, 0))
        rb, db, dyb = b.get(p, (0, 0, 0))
        out[p] = (ra + (rb - ra) * e, da + (db - da) * e, dya + (dyb - dya) * e)
    return out

def apply_hit_by(pose, hb):
    """按攻击方式缩放反应：击退/拧身/下压。"""
    return {k: (r * hb["rot"], dx * hb["kb"], dy * hb["kb"] + hb["drop"] * 8)
            for k, (r, dx, dy) in pose.items()}

def to_whole_curve(keys):
    """把多部件受击曲线聚合成整图曲线（rot 取均值×0.8，位移取均值）。"""
    out = []
    for k in keys:
        vals = [v for name, v in k.items() if name != "t"]
        rot = sum(v[0] for v in vals) / len(vals) * 0.8
        dx = sum(v[1] for v in vals) / len(vals)
        dy = sum(v[2] for v in vals) / len(vals)
        out.append({"t": k["t"], "body": (rot, dx, dy)})
    return out

def mirror_pose(pose):
    """背面受击 = 动画镜像（击退/后仰朝另一边）。单张参考图近似背身，诚实标注。"""
    return {k: (-deg, -dx, dy) for k, (deg, dx, dy) in pose.items()}

def white_flash(frame, alpha):
    """命中白闪：按角色 alpha 覆盖一层白色。alpha 0~255。"""
    white = Image.new("RGBA", frame.size, (255, 255, 255, 0))
    white.putalpha(frame.getchannel("A").point(lambda v: min(255, int(v * alpha / 255))))
    out = frame.copy()
    out.alpha_composite(white)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="敌人角色图（纯色底最佳）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--rig", help="部件比例 JSON（cutout_animator 输出的 rig.json 可直接复用）")
    ap.add_argument("--mode", choices=["cutout", "whole"], default="cutout",
                    help="cutout=切部件（默认）；whole=整张角色后仰/踉跄，与主角色动画模式保持一致")
    ap.add_argument("--hit-by", choices=list(HIT_BY.keys()), default="default",
                    help="按主角色的攻击方式给对应的受击反应（如被突刺打中击退更深）")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "hit-reactions", "sheets"), exist_ok=True)
    src = Image.open(args.image)
    rgba, bg_ok = remove_bg(src)
    rgba, cropped = main_subject_crop(rgba)
    if cropped:
        bg_ok = True
    if not bg_ok:
        print("WARN: 背景可能不干净——建议先换纯色底或手动抠图")
    char = rgba.crop(rgba.getbbox())
    cw, ch = char.size

    if args.mode == "whole":
        rig = json.loads(json.dumps(WHOLE_RIG))
        part_names = ["body"]
    else:
        rig = json.load(open(args.rig, encoding="utf-8")) if args.rig else json.loads(json.dumps(DEFAULT_RIG))
        if rig.get("no_weapon"):
            rig["parts"].pop("weapon", None)
            rig["zorder"] = [z for z in rig["zorder"] if z != "weapon"]
        part_names = None
    with open(os.path.join(args.out, "hit-reactions", "rig.json"), "w", encoding="utf-8") as f:
        json.dump(rig, f, ensure_ascii=False, indent=2)

    parts = {}
    for name, cfg in rig["parts"].items():
        x0, y0, x1, y1 = cfg["box"]
        crop = char.crop((max(0, int(x0 * cw)), max(0, int(y0 * ch)),
                          min(cw, int(x1 * cw)), min(ch, int(y1 * ch))))
        parts[name] = {"img": crop, "bw": cw, "bh": ch,
                       "pivot": (cfg["pivot"][0] * crop.width, cfg["pivot"][1] * crop.height)}

    ss = 2
    parts_ss = {n: {"img": p["img"].resize((p["img"].width * ss, p["img"].height * ss), Image.LANCZOS),
                    "bw": p["bw"], "bh": p["bh"],
                    "pivot": (p["pivot"][0] * ss, p["pivot"][1] * ss)} for n, p in parts.items()}

    pad = int(max(cw, ch) * 0.45)   # 重受击位移大，余量放宽
    W, H = cw + pad * 2, ch + pad * 2
    origin = (pad, pad)
    hb = HIT_BY.get(args.hit_by, HIT_BY["default"])
    if args.hit_by != "default":
        print(f"受击反应按攻击方式「{args.hit_by}」调整：击退 x{hb['kb']} 拧身 x{hb['rot']}"
              + (f" 下压 +{hb['drop']}" if hb["drop"] else ""))

    for name, keys in REACTIONS.items():
        if args.mode == "whole":
            keys = to_whole_curve(keys)
        for side in ("front", "back"):
            tag = name.replace("front", side)       # light-front / light-back / ...
            n = 12 if tag.startswith("light") else 24
            frames = []
            for i in range(n):
                t = i / (n - 1)
                pose = sample_curve(keys, t)
                pose = apply_hit_by(pose, hb)
                if args.mode == "whole":
                    pose = {"body": pose["body"]}
                if side == "back":
                    pose = mirror_pose(pose)
                frame = render_frame(parts_ss, rig, (W * ss, H * ss), (origin[0] * ss, origin[1] * ss),
                                     pose, ss=ss, shadow=True, phase="recovery", t=1.0)
                if i < FLASH_FRAMES:                 # 白闪渐弱
                    frame = white_flash(frame, 220 - i * 55)
                frames.append(frame)
            gif = os.path.join(args.out, "hit-reactions", f"{tag}.gif")
            frames[0].save(gif, save_all=True, append_images=frames[1:],
                           duration=int(1000 / args.fps), loop=0)
            sheet = Image.new("RGBA", (W * n, H), (0, 0, 0, 0))
            for i, fr in enumerate(frames):
                sheet.alpha_composite(fr, (i * W, 0))
            sheet.save(os.path.join(args.out, "hit-reactions", "sheets", f"{tag}-sheet.png"))
            print(f"{tag}: {n} 帧 -> {gif}")

    print("完成：受击 4 套（正面/背面 × 轻/重），12/24 帧 · 30fps · 2x 超采样")
    print("提示：背身版为动画镜像近似；部件切歪了改 hit-reactions/rig.json 后 --rig 重跑")
    print("出血方向不烘进动画——写进 combo-spec.json 的 bloodDirections")

if __name__ == "__main__":
    main()
