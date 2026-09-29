# -*- coding: utf-8 -*-
"""
build_preview.py —— 生成自包含试玩 HTML（离线双击即玩，J 键攻击，状态机接段）

用法:
  python build_preview.py --dir build/ [--out build/preview.html] [--fps 30]

输入: <dir>/frames/seg{n}/f###.png 与 <dir>/frames/idle/（cutout_animator.py 的输出）
      <dir>/timing.json（可选：各段前摇/攻击/后摇帧数 + 逐帧时长，缺失时按三等分与等间隔兜底）
      <dir>/combo-spec.json（可选，提供位移参数）
输出: <dir>/preview.html —— 单文件，帧图以 base64 内嵌，无任何外部请求

试玩页内置：状态机（与 simulator 同一套规则）、idle 呼吸待机、逐帧时长播放
（极值停顿/apex 掠过）、hit-stop 顿帧、屏幕震动、火花粒子、HUD 状态显示。
"""
import argparse, base64, glob, io, json, os
from PIL import Image

# 模板用 __TOKEN__ 替换而不是 str.format——JS 代码里有大量字面花括号
HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>连招试玩 · 双击即玩</title>
<style>
 body{margin:0;background:#171b24;color:#cfd8ea;font-family:"Microsoft YaHei",sans-serif;
      display:flex;flex-direction:column;align-items:center;user-select:none}
 #stage{position:relative;margin-top:24px;border:1px solid #2c3444;border-radius:10px;
      overflow:hidden;background:linear-gradient(#5a6b8c,#3a455c)}
 canvas{display:block}
 #hud{position:absolute;left:10px;top:10px;font-size:13px;line-height:1.7}
 #tip{margin:14px 0;font-size:13px;color:#8b97ab}
 b{color:#ffd97a}
</style></head><body>
<div id="stage"><canvas id="cv" width="__W__" height="__H__"></canvas>
<div id="hud">状态: <b id="st">IDLE</b>　段: <b id="sg">-</b><br>命中帧提示: <b id="win">-</b></div></div>
<div id="tip">按 <b>J</b> 或 <b>点击画面</b> 出招 —— 攻击进度过半再按可续接 · 最后一段后摇中再按直接开新连招 · 窗口外连点会被丢弃</div>
<script>
const FRAMES = __FRAMES_JSON__;
const N = __N__;
const WINDOW = __WINDOW__;
const HITLEN = __HITLEN__;      // 每段攻击帧数
const WINDLEN = __WINDLEN__;    // 每段前摇帧数
const RECOVLEN = __RECOVLEN__;  // 每段后摇帧数
const DISPLACEMENT = __DISPLACEMENT__;  // 每段追击位移 px
const DUR = __DUR__;            // {"seg1": [逐帧时长 ms], "idle": [...]}
const OFF = __OFF__;            // {"seg1": {"WINDUP":0,"HIT":w,"RECOVERY":w+h}}

let state="IDLE", seg=1, fi=0, buf=false, hitstop=0, shake=0, acc=0, last=0;
let particles=[];
const cv=document.getElementById("cv"), ctx=cv.getContext("2d");

function press(){
  if(state==="IDLE"){ seg=1; state="WINDUP"; fi=0; acc=0; }
  else if(state==="WINDUP"){ /* 前摇硬直，丢弃 */ }
  else if(state==="HIT"){
    const prog=fi/HITLEN[seg-1];
    if(prog>=WINDOW && seg<N) buf=true;
  } else if(state==="RECOVERY"){
    if(seg<N){ seg++; } else { seg=1; }
    state="WINDUP"; fi=0; acc=0;
  }
}
addEventListener("keydown",e=>{ if(e.key==="j"||e.key==="J") press(); });
cv.addEventListener("mousedown",press);

function durOf(){
  if(state==="IDLE"){
    const a=DUR.idle;
    return (a && a[fi % a.length]) || 33;
  }
  const arr=DUR["seg"+seg];
  const off=(OFF["seg"+seg]||{})[state];
  if(arr && off!==undefined){ const d=arr[off+fi]; if(d) return d; }
  return 33;
}
function advance(){   // 推进一帧 + 状态转移
  fi++;
  if(state==="WINDUP" && fi>=WINDLEN[seg-1]){
    state="HIT"; fi=0; spawnSparks(); shake=6; hitstop=2;
  }
  else if(state==="HIT"){
    if(fi>=HITLEN[seg-1]){
      if(buf && seg<N){ seg++; state="WINDUP"; fi=0; buf=false; }
      else { state="RECOVERY"; fi=0; }
    }
  }
  else if(state==="RECOVERY" && fi>=RECOVLEN[seg-1]){ state="IDLE"; seg=1; fi=0; }
  if(state==="IDLE"){ const a=FRAMES.idle; if(a) fi = fi % a.length; }
}
function step(now){
  const dt = Math.min(120, now - (last || now)); last = now;
  if(hitstop>0){ hitstop--; render(); requestAnimationFrame(step); return; }
  acc += dt;
  let guard = 0;
  while(acc >= durOf() && guard < 8){ acc -= durOf(); advance(); guard++; }
  stepParticles();
  render();
  requestAnimationFrame(step);
}

function spawnSparks(){
  for(let i=0;i<14;i++) particles.push({x:cv.width*0.62,y:cv.height*0.45,
    vx:(Math.random()*2-1)*7, vy:-Math.random()*6-1, life:14+Math.random()*8});
}
function stepParticles(){
  particles=particles.filter(p=>p.life>0);
  for(const p of particles){p.x+=p.vx; p.y+=p.vy; p.vy+=0.4; p.life--;}
}

function draw(){
  let ox=0, oy=0;
  if(shake>0){ ox=(Math.random()*2-1)*shake; oy=(Math.random()*2-1)*shake; shake*=0.8; if(shake<0.5)shake=0; }
  ctx.save(); ctx.translate(ox,oy);
  ctx.fillStyle="#4a5878"; ctx.fillRect(-20,-20,cv.width+40,cv.height+40);
  ctx.strokeStyle="#333d52"; ctx.beginPath(); ctx.moveTo(0,cv.height*0.82); ctx.lineTo(cv.width,cv.height*0.82); ctx.stroke();
  const key = state==="IDLE" ? "idle" : "seg"+seg+"-"+state;
  const arr=FRAMES[key];
  if(arr){ const im=arr[Math.min(fi,arr.length-1)];
    // spec 位移：攻击段随进度前移，后摇保持贴进位置，回待机归零
    const disp=(DISPLACEMENT[seg]||0) * (state==="HIT"? Math.min(1,fi/Math.max(1,HITLEN[seg-1])) : (state==="RECOVERY"?1:0));
    ctx.drawImage(im, cv.width*0.18+disp, cv.height*0.82-im.height*0.9, im.width*0.9, im.height*0.9); }
  ctx.fillStyle="#ffd97a";
  for(const p of particles) ctx.fillRect(p.x,p.y,3,3);
  ctx.restore();
}
function render(){
  document.getElementById("st").textContent=state;
  document.getElementById("sg").textContent=state==="IDLE" ? "-" : seg;
  const win=document.getElementById("win");
  if(state==="HIT"){ const prog=fi/HITLEN[seg-1]; win.textContent = prog>=WINDOW? "可续接!":"-"; }
  else win.textContent="-";
  draw();
}
requestAnimationFrame(step);
</script></body></html>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="cutout_animator 的输出目录（含 frames/）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fps", type=int, default=None, help="覆盖 timing.json 的帧率")
    args = ap.parse_args()
    out = args.out or os.path.join(args.dir, "preview.html")

    timing = None
    tp = os.path.join(args.dir, "timing.json")
    if os.path.exists(tp):
        timing = json.load(open(tp, encoding="utf-8"))
    fps = args.fps or (timing or {}).get("fps") or 24

    frames_json = {}
    dur_map, off_map = {}, {}
    segs = sorted(int(os.path.basename(p)[3:]) for p in glob.glob(os.path.join(args.dir, "frames", "seg*")))
    wind, hit, rec = [], [], []
    base_ms = 1000.0 / fps
    for s in segs:
        files = sorted(glob.glob(os.path.join(args.dir, "frames", f"seg{s}", "f*.png")))
        n = len(files)
        ts = (timing or {}).get("segments", {}).get(str(s))
        if ts:
            w = min(ts["windupFrames"], n)
            h = min(ts["hitFrames"], max(1, n - w))
            r = max(1, n - w - h)
            durs = ts.get("durations") or [int(base_ms)] * n
        else:
            w = max(1, n // 3); h = max(1, n // 3); r = max(1, n - 2 * w)
            durs = [int(base_ms)] * n
        wind.append(w); hit.append(h); rec.append(r)
        dur_map[f"seg{s}"] = durs[:n]
        off_map[f"seg{s}"] = {"WINDUP": 0, "HIT": w, "RECOVERY": w + h}
        for i, fp in enumerate(files):
            im = Image.open(fp).convert("RGBA")
            im.thumbnail((420, 420))
            buf = io.BytesIO(); im.save(buf, "PNG", optimize=True)
            key = f"seg{s}-" + ("WINDUP" if i < w else "HIT" if i < w + h else "RECOVERY")
            frames_json.setdefault(key, []).append(base64.b64encode(buf.getvalue()).decode())

    idle_files = sorted(glob.glob(os.path.join(args.dir, "frames", "idle", "f*.png")))
    if idle_files:
        idle_durs = ((timing or {}).get("idle") or {}).get("durations") or [int(base_ms)] * len(idle_files)
        dur_map["idle"] = idle_durs
        for fp in idle_files:
            im = Image.open(fp).convert("RGBA")
            im.thumbnail((420, 420))
            buf = io.BytesIO(); im.save(buf, "PNG", optimize=True)
            frames_json.setdefault("idle", []).append(base64.b64encode(buf.getvalue()).decode())

    spec_path = os.path.join(args.dir, "combo-spec.json")
    disp = {}
    if os.path.exists(spec_path):
        spec = json.load(open(spec_path, encoding="utf-8"))
        for d in spec.get("displacement", []):
            disp[d["segment"]] = sum(p["px"] for p in d.get("splits", []))
    n_seg = len(segs)

    html = HTML
    for token, val in [
        ("__W__", 560), ("__H__", 560),
        ("__FRAMES_JSON__", json.dumps(frames_json)),
        ("__N__", n_seg), ("__WINDOW__", 0.5),
        ("__HITLEN__", json.dumps([max(1, h) for h in hit])),
        ("__WINDLEN__", json.dumps([max(1, w) for w in wind])),
        ("__RECOVLEN__", json.dumps([max(1, r) for r in rec])),
        ("__DISPLACEMENT__", json.dumps(disp)),
        ("__DUR__", json.dumps(dur_map)),
        ("__OFF__", json.dumps(off_map)),
    ]:
        html = html.replace(token, str(val))

    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    idle_tag = " · 含 idle 待机" if idle_files else ""
    size = os.path.getsize(out) // 1024
    print(f"试玩页已生成: {out}（{size} KB，{n_seg} 段{idle_tag}，{fps}fps 逐帧时长）—— 双击打开，按 J 出招")

if __name__ == "__main__":
    main()
