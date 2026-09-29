# -*- coding: utf-8 -*-
"""
simulator.py —— 连招状态机输入序列自检（把 references/state-machine.md 的规则变成可执行断言）

用法:
  python simulator.py --selfcheck                 # 跑内置边界用例（两轮测试沉淀的规则）
  python simulator.py --spec combo-spec.json --inputs "....x...x..."   # 自定义输入序列（每字符=1帧, x=攻击）

退出码: 全部通过 = 0；有失败 = 1（可用于流水线）
"""
import argparse, json, math, sys

def make_fsm(spec):
    c = spec["combo"]
    return {
        "N": c.get("segments", 3),
        "window": c.get("continuationWindow", 0.5),
        "interrupt": c.get("interruptRecovery", True),
        "buffered_in": c.get("inputBuffer", False),
        "hit_frames": [s.get("hitFrames", 20) for s in c.get("segmentsDetail", [])] or [20] * c.get("segments", 3),
    }

def run(inputs, fsm, verbose=False):
    """inputs: 每帧是否按攻击的布尔列表。返回每帧 (state, segment) 轨迹。"""
    N = fsm["N"]
    state, seg, buf = "IDLE", 1, False
    prog_frame = 0
    trail = []
    hit_f = fsm["hit_frames"]
    def hit_len(s):
        return hit_f[s - 1] if s <= len(hit_f) else 20
    for f, pressed in enumerate(inputs):
        # selfcheck 传 "X"/"." 字符串，CLI 传 bool —— 统一成真 bool（非空字符串 "." 恒为真，是历史 bug）
        pressed = pressed is True or (isinstance(pressed, str) and pressed in ("x", "X"))
        if pressed:
            if state == "IDLE":
                state, seg, prog_frame = "WINDUP", 1, 0
                state = "HIT"  # 简化：本模拟器把前摇并入攻击计时（素材组装时前摇在段内）
            elif state == "WINDUP":
                pass  # 硬直丢弃（inputBuffer=true 时应缓冲——本模拟器仅记录）
            elif state == "HIT":
                prog = prog_frame / hit_len(seg)
                if prog >= fsm["window"]:
                    if seg < N:
                        buf = True
                    # 最后一段：丢弃
                elif fsm["buffered_in"] and seg < N:
                    buf = True  # inputBuffer：窗口外输入缓冲一次，到窗口自动生效
            elif state == "RECOVERY":
                if fsm["interrupt"]:
                    if seg < N:
                        seg += 1
                        state, prog_frame = "HIT", 0
                    else:
                        seg = 1
                        state, prog_frame = "WINDUP", 0
                        state = "HIT"
        # 推进
        if state == "HIT":
            prog_frame += 1
            prog = prog_frame / hit_len(seg)
            if prog >= 1.0:
                if buf and seg < N:
                    seg += 1
                    state, prog_frame, buf = "HIT", 0, False
                else:
                    state, prog_frame = "RECOVERY", 0
        elif state == "RECOVERY":
            prog_frame += 1
            if prog_frame >= max(4, hit_len(seg) // 2):
                state, seg, prog_frame = "IDLE", 1, 0
        trail.append((state, seg))
        if verbose:
            print(f"f{f:03d} {'X' if pressed else '.'} {state:8s} seg={seg} buf={buf}")
    return trail

DEFAULT_SPEC = {"combo": {"segments": 3, "continuationWindow": 0.5,
                          "interruptRecovery": True, "inputBuffer": False,
                          "segmentsDetail": [{"hitFrames": 20}, {"hitFrames": 22}, {"hitFrames": 24}]}}

def selfcheck(spec=None):
    """内置边界用例 = references/state-machine.md 边界情况速查表的可执行版。
    按键时机全部从 spec 的各段攻击帧数推算——任意合理的 spec 都应 7 例全 PASS，
    不过 = spec 参数与状态机规则矛盾，先修再交付。"""
    spec = spec or DEFAULT_SPEC
    buf_spec = json.loads(json.dumps(spec)); buf_spec["combo"]["inputBuffer"] = True
    no_buf = json.loads(json.dumps(spec)); no_buf["combo"]["inputBuffer"] = False
    no_int = json.loads(json.dumps(spec)); no_int["combo"]["interruptRecovery"] = False
    fsm = make_fsm(spec)
    L, N, w = fsm["hit_frames"], fsm["N"], fsm["window"]
    # 帧推进时序（与 run() 实测一致）：
    #   段1 由输入起手 S1=0，结束帧 E1=L1-1；链式接续段 k 起始于上一段结束帧，E_k=S_k+L_k
    #   段 k 内输入时的 pf = f - S_k - (0 if k==1 else 1)；RECOVERY 在 E_k 帧出现
    S, E = [0], [L[0] - 1]
    for k in range(1, N):
        S.append(E[k - 1])
        E.append(S[k] + L[k])
    win = lambda k: max(1, math.ceil(w * L[k - 1]))            # 窗口开启的段内 pf
    W = lambda k: S[k - 1] + win(k) + (0 if k == 1 else 1)     # 窗口内按键的绝对帧
    rec = lambda k: max(4, L[k - 1] // 2)                       # 后摇帧数（与 run() 一致）
    tail = E[N - 1] + rec(N) + L[0] + rec(1) + 12               # 播完 + 重播一整段仍回 IDLE
    def seq(presses):
        s = ["."] * tail
        for f in presses:
            s[f] = "X"
        return "".join(s)
    ends_idle = lambda t: t[-1][0] == "IDLE"
    max_seg = lambda t: max(g for _, g in t)
    ok = True

    burst = (S[1] + win(2) + 1) if N >= 2 else tail // 2       # 恰好停在段2窗口前一帧
    cases = [
        ("连点突发不自动连满：窗口外输入全部丢弃",
         "X" * burst + "." * tail,
         None,
         lambda t: max_seg(t) == min(2, N) and ends_idle(t)),
    ]
    if N >= 2:
        chain = [W(k) for k in range(1, N)]                     # 第 1..N-1 段各自窗口内续接
        cases += [
            ("攻击进度 ≥50% 再按：续接并跳过后摇",
             seq([0, W(1)]), None,
             lambda t: ("HIT", 2) in t and ("RECOVERY", 1) not in t),
            ("攻击进度 <50% 再按：输入丢弃（inputBuffer=false 时）",
             seq([0, W(1) - 1]), no_buf,
             lambda t: ("HIT", 2) not in t and ends_idle(t)),
            ("最后一段 ≥50% 再按：丢弃（没有下一段）并回待机",
             seq([0] + chain + [W(N)]), None,
             lambda t: max_seg(t) == N and ("RECOVERY", N) in t and ends_idle(t)),
            ("interruptRecovery=false：后摇中再按不再打断",
             seq([0, E[0] + 1 + max(1, rec(1) // 3)]), no_int,
             lambda t: ("RECOVERY", 1) in t and ("HIT", 2) not in t and ends_idle(t)),
            ("inputBuffer=true：窗口外输入缓冲一次自动续接",
             seq([0, W(1) - 1]), buf_spec,
             lambda t: ("HIT", 2) in t and ("RECOVERY", 1) not in t),
        ]
    for name, inputs, cspec, check in cases:
        t = run(list(inputs), make_fsm(cspec or spec))
        passed = bool(check(t))
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}")

    if N >= 2:
        # 最后一段后摇中再按：中断收招，从第 1 段重新开始
        inputs = list(seq([0] + chain + [E[N - 1] + 1 + max(1, rec(N) // 3)]))
        t = run(inputs, fsm)
        idx = next((i for i, (s, g) in enumerate(t) if s == "RECOVERY" and g == N), None)
        restarted = idx is not None and any(s == "HIT" and g == 1 for s, g in t[idx + 1:])
        p = restarted and ends_idle(t)
        ok &= p
        print(f"{'PASS' if p else 'FAIL'}  最后一段后摇中再按：从第 1 段重新开始")

    print("=" * 40)
    print("SELF-CHECK " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="combo-spec.json（缺省用内置默认 3 段）")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--inputs", help="自定义输入序列，如 '....x...x...'（每字符=1帧, x=攻击）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.spec:
        spec = json.load(open(args.spec, encoding="utf-8"))
    else:
        spec = {"combo": {"segments": 3, "continuationWindow": 0.5,
                          "interruptRecovery": True, "inputBuffer": False,
                          "segmentsDetail": [{"hitFrames": 20}, {"hitFrames": 22}, {"hitFrames": 24}]}}
    if args.selfcheck or not args.inputs:
        sys.exit(selfcheck(spec))
    inputs = [c == "x" or c == "X" for c in args.inputs]
    for i, (s, g) in enumerate(run(inputs, make_fsm(spec), args.verbose)):
        print(f"f{i:03d} {'X' if inputs[i] else '.'} {s:8s} seg={g}")

if __name__ == "__main__":
    main()
