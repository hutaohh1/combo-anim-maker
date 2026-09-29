# -*- coding: utf-8 -*-
"""
export_godot.py —— Godot SpriteFrames .tres 直出（纯文本生成，零依赖）

用法:
  python export_godot.py --dir build/ --name MyChar

输入: <dir>/frames/seg{n}/f###.png 与 <dir>/frames/idle/（cutout_animator.py 输出）
      <dir>/timing.json（可选，提供逐帧时长；缺省等间隔）
输出: <dir>/godot/<name>_spriteframes.tres
      Godot 4 用法：把 frames/ 拷进项目 res:// 下，新建 AnimatedSprite2D，
      Sprite 属性 -> Sprite Frames -> 载入本 .tres；动画名 idle / seg1..segN。
      idle 循环播放，segN 不循环（由你的状态机触发切换）。

依赖: 仅标准库
"""
import argparse, glob, json, os

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="cutout_animator 的输出目录（含 frames/）")
    ap.add_argument("--name", required=True, help="资源名（如 MyChar）")
    ap.add_argument("--fps", type=int, default=None, help="覆盖 timing.json 的帧率")
    args = ap.parse_args()

    timing_path = os.path.join(args.dir, "timing.json")
    fps, durs = 30, {}
    if os.path.exists(timing_path):
        t = json.load(open(timing_path, encoding="utf-8"))
        fps = args.fps or t.get("fps", 30)
        durs = {k: v.get("durations") for k, v in t.get("segments", {}).items()}
        if t.get("idle"):
            durs["idle"] = t["idle"]["durations"]
    else:
        fps = args.fps or 30
        print("提示: 未找到 timing.json，按等间隔时长生成")

    anims = []   # (名字, [png 路径相对 dir], [时长 ms])
    idle_files = sorted(glob.glob(os.path.join(args.dir, "frames", "idle", "f*.png")))
    if idle_files:
        rel = [os.path.relpath(p, args.dir).replace("\\", "/") for p in idle_files]
        anims.append(("idle", rel, durs.get("idle"), True))
    seg_dirs = sorted(glob.glob(os.path.join(args.dir, "frames", "seg*")),
                      key=lambda p: int(os.path.basename(p)[3:]))
    for sd in seg_dirs:
        s = os.path.basename(sd)[3:]
        files = sorted(glob.glob(os.path.join(sd, "f*.png")))
        if not files:
            continue
        rel = [os.path.relpath(p, args.dir).replace("\\", "/") for p in files]
        anims.append((f"seg{s}", rel, durs.get(s), False))

    base_ms = 1000.0 / fps
    ext_lines, anim_blocks = [], []
    for i, (name, files, dl, loop) in enumerate(anims):
        frames_js = []
        for j, rel in enumerate(files):
            rid = str(len(ext_lines) + 1)
            ext_lines.append(f'[ext_resource type="Texture2D" path="res://{rel}" id="{rid}"]')
            ms = dl[j] if dl and j < len(dl) else base_ms
            mult = round(ms / base_ms, 3)
            frames_js.append('{\n"duration": %.3f,\n"texture": ExtResource("%s")\n}' % (mult, rid))
        loop_s = "true" if loop else "false"
        anim_blocks.append('{\n"frames": [%s],\n"loop": %s,\n"name": &"%s",\n"speed": %s\n}'
                           % (", ".join(frames_js), loop_s, name, float(fps)))
    load_steps = len(ext_lines) + 1
    out_dir = os.path.join(args.dir, "godot")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{args.name}_spriteframes.tres")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f'[gd_resource type="SpriteFrames" load_steps={load_steps} format=3]\n\n')
        f.write("\n".join(ext_lines) + "\n\n[resource]\n")
        f.write("animations = [" + ", ".join(anim_blocks) + "]\n")
    with open(os.path.join(out_dir, "README-godot.txt"), "w", encoding="utf-8") as f:
        f.write(
            "Godot 4 导入说明\n"
            "===============\n"
            f"1. 把 build 目录里的 frames/ 整个拷进你的项目（如 res://assets/{args.name}/frames/）\n"
            f"2. 文本编辑器打开本 .tres，把里面的 res://frames/... 路径批量替换成你的实际路径\n"
            f"   （或保持目录结构一致即可零改动）\n"
            "3. 新建 AnimatedSprite2D 节点 -> Sprite Frames 属性 -> 快速载入本 .tres\n"
            "4. 动画名：idle（循环）/ seg1..segN（单次）——在你的状态机里 play(\"seg1\") 并\n"
            "   连接 animation_finished 信号接续段；命中判定帧区间见 timing.json 的 activeFrames\n")
    print(f"Godot SpriteFrames 已生成 -> {out}（{len(anims)} 个动画，{fps}fps）")

if __name__ == "__main__":
    main()
