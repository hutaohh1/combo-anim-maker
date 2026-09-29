# -*- coding: utf-8 -*-
"""
test_pipeline.py —— 管线金标准回归测试（零外部依赖，纯 Pillow）

跑法:
  python scripts/tests/test_pipeline.py

内容:
  1. 现场生成五类武器测试角色（长枪/斧/弓/徒手/红棍）
  2. 断言武器检测类别（spear/axe/bow/None/dagger）
  3. 端到端跑一遍 cutout_animator（1 段 · low 密度），断言帧数 / idle / timing.json / QA 通过
  4. 跑 hit_reactions（whole 模式），断言 4 套输出齐全

任何一项失败 -> 退出码 1。改管线后必跑，防回归。
"""
import glob, json, math, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)          # scripts/
sys.path.insert(0, SCRIPTS)

FAILS = []

def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)

def make_chars(tmp):
    from PIL import Image, ImageDraw
    def base(d):
        d.ellipse([190, 58, 250, 132], fill=(40, 40, 50))
        d.polygon([(200, 125), (240, 125), (250, 260), (190, 260)], fill=(40, 40, 50))
        d.rectangle([205, 258, 218, 420], fill=(40, 40, 50))
        d.rectangle([226, 258, 239, 420], fill=(40, 40, 50))
        d.line([205, 150, 150, 200], fill=(40, 40, 50), width=14)
        d.line([238, 150, 290, 180], fill=(40, 40, 50), width=14)
    def spear(d):
        d.line([30, 340, 500, 40], fill=(180, 40, 30), width=9)
        d.polygon([(500, 40), (462, 42), (478, 72)], fill=(120, 120, 135))
    def axe(d):
        d.line([290, 180, 430, 120], fill=(120, 80, 40), width=9)
        d.ellipse([405, 95, 470, 155], fill=(150, 150, 160))
    def bow(d):
        d.arc([300, 60, 460, 260], start=-70, end=70, fill=(120, 80, 40), width=8)
        d.line([365, 85, 365, 235], fill=(200, 200, 210), width=3)
    def rod(d):     # 离手红棍（独立细长件 -> dagger/sword 档）
        d.line([300, 65, 375, 200], fill=(200, 30, 40), width=8)
    for name, fn in (("spear", spear), ("axe", axe), ("bow", bow), ("none", lambda d: None), ("rod", rod)):
        im = Image.new("RGB", (512, 512), (250, 250, 248))
        d = ImageDraw.Draw(im)
        base(d)
        fn(d)
        im.save(os.path.join(tmp, f"char-{name}.png"))

def main():
    import cutout_animator as ca

    tmp = tempfile.mkdtemp(prefix="combo-qa-")
    make_chars(tmp)
    print(f"测试角色已生成 -> {tmp}\n")

    # ---- 1. 武器检测金标准 ----
    expect = {"spear": "spear", "axe": "axe", "bow": "bow", "none": None, "rod": "dagger"}
    dets = {}
    for name in ("spear", "axe", "bow", "none", "rod"):
        rgba, ok = ca.load_character_rgba(__import__("PIL.Image", fromlist=["Image"]).open(os.path.join(tmp, f"char-{name}.png")))
        char = rgba.crop(rgba.getbbox())
        det = ca.detect_weapon(char)
        dets[name] = det["class"] if det else None
        check(f"检测 {name}", dets[name] == expect[name], f"期望 {expect[name]}，实得 {dets[name]}")

    # ---- 2. 端到端出帧（红棍 · 1 段 · low） ----
    out1 = os.path.join(tmp, "build-rod")
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cutout_animator.py"),
                        "--image", os.path.join(tmp, "char-rod.png"), "--out", out1,
                        "--segments", "1", "--density", "low"],
                       capture_output=True, text=True)
    check("cutout_animator 退出码", r.returncode == 0, r.stderr[-300:] if r.returncode else "")
    n_seg1 = len(glob.glob(os.path.join(out1, "frames", "seg1", "f*.png")))
    check("seg1 帧数 = 6+7+6 = 19（low 密度 + 段节奏）", n_seg1 == 19, f"实得 {n_seg1}")
    n_idle = len(glob.glob(os.path.join(out1, "frames", "idle", "f*.png")))
    check("idle 48 帧呼吸待机", n_idle == 48, f"实得 {n_idle}")
    timing_path = os.path.join(out1, "timing.json")
    ok_t = os.path.exists(timing_path)
    check("timing.json 导出", ok_t)
    if ok_t:
        t = json.load(open(timing_path, encoding="utf-8"))
        check("activeFrames 在 hit 相 30%~65% 内",
              19 and all(7 <= i <= 13 for i in t["segments"]["1"]["activeFrames"]),
              str(t["segments"]["1"]["activeFrames"]))
        check("逐帧时长不等（时序生效）",
              len(set(t["segments"]["1"]["durations"])) > 2,
              str(sorted(set(t["segments"]["1"]["durations"]))))
    qap = os.path.join(out1, "qa-report.json")
    ok_q = os.path.exists(qap) and json.load(open(qap, encoding="utf-8"))["ok"]
    check("QA 质检通过（脱手/越界/空帧）", ok_q)
    check("GIF 预览生成", os.path.exists(os.path.join(out1, "seg1.gif"))
          and os.path.exists(os.path.join(out1, "idle.gif")))

    # ---- 3. hit_reactions whole 模式 4 套 ----
    out2 = os.path.join(tmp, "build-hit")
    r2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "hit_reactions.py"),
                         "--image", os.path.join(tmp, "char-none.png"), "--out", out2,
                         "--mode", "whole", "--hit-by", "thrust"],
                        capture_output=True, text=True)
    check("hit_reactions 退出码", r2.returncode == 0, r2.stderr[-300:] if r2.returncode else "")
    for tag in ("light-front", "light-back", "heavy-front", "heavy-back"):
        check(f"受击 {tag}.gif", os.path.exists(os.path.join(out2, "hit-reactions", f"{tag}.gif")))

    print("\n" + "=" * 40)
    if FAILS:
        print(f"回归失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("回归测试全部通过")

if __name__ == "__main__":
    main()
