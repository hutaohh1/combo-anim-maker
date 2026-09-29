# 连招状态机完整规范

把"一整串动画"变成"能玩的连招"的全部规则都在这里。来源：像素小宝实战方法（BV1ZJeU6pESc）。

## 为什么需要状态机

直接播整串连招只能用于**敌人 AI**（它不需要响应玩家输入）。玩家角色必须满足：

- 按一下打一下
- 不按就停下来
- 连段过程中可以续接、可以被打断

这就是"游戏感"的来源，也是本 skill 与"直接生成视频"的本质区别。

## 状态定义

| 状态 | 含义 | 关键属性 |
|------|------|----------|
| 待机 Idle | 不按就停在这里 | 再按 → 从第一段的前摇开始 |
| 前摇 Wind-up | 每段攻击的起手 | 输入后必播，硬直不可取消 |
| 攻击 Hit | 当前段的有效判定 | 后半段 = 可续接窗口 |
| 后摇 Recovery | 收招动作 | 可被下一次输入打断 |
| 回到待机 | 整套连招结束 | 等待下一次输入 |

每一段动作素材都按「前摇 → 攻击 → 后摇」三段式生成，段与段在状态机里拼接。

## 输入判定规则（核心算法）

以 N 段连击（示例 5 段）为例：

1. 每次输入 → 从**当前段的前摇**开始播放
2. 攻击播放进度 **< 50%**：不接受下一次输入（前摇+攻击前半段是硬直）
3. 攻击进度 **≥ 50%** 时再按 → 本段打完**跳过后摇**，直接进下一段
4. **后摇期间**再按 → 中断收招，立即接下一段
5. 无输入 → 播完后摇 → 回到待机；待机后再按 → 从第一段重来

最终手感 = **首次前摇 → 连续攻击 ×N → 最后收招**。

## 参数表（写入 combo-spec.json）

| 参数 | 默认 | 说明 |
|------|------|------|
| `segments` | 3 | 连击段数；宁少勿多，5 为上限 |
| `continuationWindow` | 0.5 | 攻击进度多少以后接受续接输入 |
| `interruptRecovery` | true | 后摇是否可被输入打断 |
| `restartFromIdle` | true | 回待机后再按是否从第一段开始 |
| `mode` | "player" | player = 可交互状态机；enemy = 整串直播 |

## 状态转移表（伪代码）

```
state = IDLE
on (attack pressed):
    if state == IDLE:            -> WINDUP(segment=1)
    if state == WINDUP:          ignore (硬直)
    if state == HIT:
        if progress >= continuationWindow:
            buffered = true      -> 段末跳过后摇进下一段
    if state == RECOVERY:
        if interruptRecovery:    -> 下一段（跳过剩余后摇）

on (segment animation end):
    if buffered and segment < segments: -> WINDUP/HIT(segment+1)
    elif segment < segments and buffered: loop
    else:                        -> RECOVERY -> IDLE
```

## 位移（追击能力）

AI 生成的动作都是原地的。不做"边攻击边移动"，而是**给每个动画分段加位移**：

- 用位移调整器思路：把单个动作按帧拆 2~3 段，各段加不同位移
- 实测示例（单手剑连击第五击，共 49 帧）：拆 1-9 帧 / 10-38 帧 / 39-49 帧，分别前移 **+20 / +230 / +10 px**；跳起转身向前砍那段位移最大，承担追击
- 全部数值写入 `combo-spec.json` 的 `displacement`，不写死在代码里

## 敌人模式

`mode: "enemy"` 时**不需要交互状态机**：出招直接整串播放（视频原话：敌人的出招这样播没问题）。省工作量，打击感靠受击反馈补。

## 引擎落地提示

- **Godot**：AnimationPlayer + AnimationStateMachine（或自制状态机节点）；`continuationWindow` 映射为动画播放位置判定；buffered input 用一个"输入缓冲"变量实现
- **Unity**：Animator 子状态机；每段一个 Sub-State；续接窗口用 `Animation.normalizedTime >= 0.5` 判定；后摇中断 = 在 Recovery 状态加 exit transition（条件：攻击键按下）
- 位移建议用根运动或代码位移（按 spec 的分段帧区间插值），不要烘进每一帧
