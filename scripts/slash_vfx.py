# -*- coding: utf-8 -*-
"""
slash_vfx.py —— 程序化刀光三层贴图生成器（底色层 / 亮纹层 / 腐蚀层）

用法:
  python slash_vfx.py --out build/vfx [--color ff3040] [--arc 120]

输出（512x512，RGBA，引擎里叠加使用）:
  slash-base.png     底色层：主体色块弧带（沿弧从根部到尾部渐隐）
  slash-streaks.png  亮纹层：高速亮线
  slash-erosion.png  腐蚀层：溶解遮罩（白=被溶解的洞，越靠尾部越大越密）
  slash-combined.png 三层叠加预览（棋盘格底，诚实显示透明区）

依赖: Pillow
"""
import argparse, math, os, random
from PIL import Image, ImageDraw, ImageFilter

W, H = 512, 512
ARC_C = (60, 480)          # 弧心（左下角外）：弧带从正上方扫到右侧，全程在画布内
R_OUT, R_IN = 460, 250     # 弧带外/内半径

def arc_points(r, a0, a1, steps=48):
    """角度用数学方向（a=90° 指向画布上方）。"""
    pts = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append((ARC_C[0] + r * math.cos(a), ARC_C[1] - r * math.sin(a)))
    return pts

def in_band(x, y, a0, a1, slack=10):
    dx, dy = ARC_C[0] - x, ARC_C[1] - y
    r = math.hypot(dx, dy)
    if not (R_IN - slack < r < R_OUT + slack):
        return None
    a = math.degrees(math.atan2(dy, dx))
    if not (a0 - slack <= a <= a1 + slack):
        return None
    # 返回归一化弧长位置：0=根部(a1) 1=尾部(a0)
    return (a1 - a) / (a1 - a0)

def base_layer(color, a0, a1):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    outer = arc_points(R_OUT, a0, a1)
    inner = arc_points(R_IN, a0, a1)
    d.polygon(outer + inner[::-1], fill=color + (235,))
    img = img.filter(ImageFilter.GaussianBlur(6))
    # 渐隐沿弧向：根部实、尾部虚；内外缘各留一点柔边
    px = img.load()
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if not a:
                continue
            t = in_band(x, y, a0, a1, slack=40)
            fade = max(0.06, 1.0 - (t if t is not None else 0.0)) ** 1.2
            dx, dy = x - ARC_C[0], y - ARC_C[1]
            rr = math.hypot(dx, dy)
            edge = 1.0
            if rr < R_IN + 18:
                edge = max(0.0, (rr - R_IN) / 18)
            elif rr > R_OUT - 18:
                edge = max(0.0, (R_OUT - rr) / 18)
            px[x, y] = (r, g, b, int(a * (0.25 + 0.75 * fade) * max(0.15, edge)))
    return img

def streak_layer(a0, a1):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    random.seed(7)
    for k in range(7):
        r = R_IN + 20 + random.random() * (R_OUT - R_IN - 40)
        wdt = 2 if k % 2 else 3
        # 亮线不到根部——从 15% 弧长起，像被拖出来的
        s0 = a1 - (a1 - a0) * random.uniform(0.10, 0.28)
        pts = arc_points(r, a0, s0)
        d.line(pts, fill=(255, 255, 245, 200), width=wdt, joint="curve")
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    return img

def erosion_layer(a0, a1):
    """溶解遮罩：白色块 = 被腐蚀掉的洞。越靠尾部洞越大越密，模拟拖尾消散。"""
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    random.seed(11)
    for _ in range(320):
        t = random.random() ** 0.6                     # 偏向尾部
        a = a1 - (a1 - a0) * t
        r = R_IN + random.random() * (R_OUT - R_IN)
        x = ARC_C[0] + r * math.cos(math.radians(a))
        y = ARC_C[1] - r * math.sin(math.radians(a))
        rad = 2 + 16 * (t ** 1.4) * random.uniform(0.5, 1.3)
        strength = max(0.0, (t - 0.25) / 0.75)      # 根部 25% 不开洞，向尾部线性增强
        alpha = int(255 * strength)
        if alpha <= 0:
            continue
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=alpha)
    mask = mask.filter(ImageFilter.GaussianBlur(3))
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    img.putalpha(mask)
    return img

def checker_bg(size, cell=16):
    bg = Image.new("RGBA", size)
    d = ImageDraw.Draw(bg)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            c = (120, 124, 134, 255) if (x // cell + y // cell) % 2 else (96, 100, 110, 255)
            d.rectangle([x, y, x + cell, y + cell], fill=c)
    return bg

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--color", default="ff3040", help="刀光主色 RRGGBB")
    ap.add_argument("--arc", type=int, default=118, help="弧带角度跨度")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    color = tuple(int(args.color[i:i+2], 16) for i in (0, 2, 4))
    a1 = 88.0                       # 根部角度
    a0 = a1 - args.arc              # 尾部角度

    base = base_layer(color, a0, a1)
    streaks = streak_layer(a0, a1)
    erosion = erosion_layer(a0, a1)

    base.save(os.path.join(args.out, "slash-base.png"))
    streaks.save(os.path.join(args.out, "slash-streaks.png"))
    erosion.save(os.path.join(args.out, "slash-erosion.png"))

    # 叠加预览：base + streaks，再按腐蚀遮罩挖洞，放棋盘格上看清透明区
    combined = base.copy()
    combined.alpha_composite(streaks)
    er = erosion.getchannel("A").point(lambda v: min(255, v * 2))   # 遮罩对比增强
    cut_alpha = Image.composite(
        combined.getchannel("A").point(lambda v: int(v * 0.15)),    # 被腐蚀处只剩 15% 残影
        combined.getchannel("A"),
        er.point(lambda v: 255 if v > 110 else 0),
    )
    cut = combined.copy()
    cut.putalpha(cut_alpha)
    preview = checker_bg((W, H))
    preview.alpha_composite(cut)
    preview.convert("RGB").save(os.path.join(args.out, "slash-combined.png"))
    print(f"刀光贴图已生成 -> {args.out}（颜色 #{args.color}，弧跨 {args.arc}°）")

if __name__ == "__main__":
    main()
