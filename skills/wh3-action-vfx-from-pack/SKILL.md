---
name: wh3-action-vfx-from-pack
description: 从《全面战争：战锤3》MOD Pack 提取单位动作、骨骼轨迹和匹配的加权角色/武器模型，在 Houdini 中对齐并从沙箱生成动作同屏 L2 预览视频，逐帧审看枪口焰、刀光、砸地或挂骨特效；签收后安全写回动作事件。不处理纯数值、图标或角色重绑定。
---

# 用原模型检查特效是否挂在动作上

美术判断交给 `vfx-art-direction`；这里负责动作、挂点、模型与事件。默认在 Houdini 制作，通用 IR 编译/审片用 `wh3-vfx-author-preview`，目标入包用 `wh3-vfx-bake-pack`。始终走 Route A：游戏目录只读，不改 `fxc/shaders.manifest`，不部署、不启动游戏、Terry 或管理器。

## 1 · 先确定动作和模型，再选挂点

下文 `$source` 为只读输入 Pack，`$unit` 为单位键，`$work` 为独立工作目录。PowerShell 7.3+ 可设置 `$ErrorActionPreference='Stop'; $PSNativeCommandUseErrorActionPreference=$true`，任一命令非零即停。不要把原包设为沙箱输出。

```powershell
$b = if ($env:WH3_VFX_BAKER) { $env:WH3_VFX_BAKER } else { throw '请先运行仓库根目录 install.ps1，或将 WH3_VFX_BAKER 指向 baker 目录' }
$py = powershell -NoProfile -ExecutionPolicy Bypass -File "$b\resolve_python.ps1"
$s = @((Join-Path $HOME '.codex\skills\wh3-action-vfx-from-pack\scripts'), (Join-Path (Split-Path -Parent $b) 'skills\wh3-action-vfx-from-pack\scripts')) | Where-Object { Test-Path $_ } | Select-Object -First 1
& $py "$b\doctor.py" --profile action
if ($LASTEXITCODE) { throw '先修复 doctor --profile action 报出的必需项' }
& $py "$s\inspect_pack_actions.py" --pack $source --unit $unit --out "$work\actions.json"
& $py "$s\extract_action_motion.py" --pack $source --unit $unit --anim $slot --weapon-bones $bones --unit-scale $scale --scale-source $scaleEvidence --out-dir "$work\motion"
$motion = "$work\motion\action_motion.npz"
& $py "$s\export_action_model.py" --pack $source --unit $unit --motion $motion --out-dir "$work\actor"
$model = "$work\actor\actor_model.npz"
```

从清单选槽名/编号或精确虚拟路径；多候选用 `--table`、精确动作、`--variant` 或明确的 `--wsmodel`。缩放查实际单位→变体；`$bones` 是以逗号分隔的真实骨名。没有匹配模型时说明原因，可单独交付骨架；不把替身当成原模型。独立动作也可查，但只有实际 fragment 绑定可用于回写。详见[动作及双容器](references/action-pack-chain.md)。

## 2 · 在 Houdini 用动作制作 IR

```powershell
$hy = powershell -NoProfile -ExecutionPolicy Bypass -File "$b\resolve_hython.ps1"
& $hy "$s\build_action_scene.py" --motion $motion --model $model --out "$work\action.hip"
```

打开场景拖时间轴：`action_model` 是蒙皮模型，`action_guides` 是可单独显示的骨架和武器轨迹。以实际接触/开火事件与轨迹核对发力时刻；将骨局部偏移和旋转记录下来，别用武器骨原点代替枪口。以引导点/曲线做发射源，将资产和 manifest 写入标准 `$ir`，按 baker 的 authoring spec 导出。[坐标、蒙皮及四项发射器检查](references/houdini-motion-bridge.md)。

## 3 · 生成并交付动作与特效同屏的 L2 预览视频

每次完成动作特效制作或修改后，主动执行本节，在写回目标 Pack 之前把本次生成的 MP4 交给用户审片；用户无需单独提出预览请求。仅查询或提取动作时按用户指定的范围执行。

从本次 IR 编译特效沙箱，生成特效单独预览，再用同一个沙箱与第 1 节提取的动作、模型生成同屏视频。每轮使用新的工作目录。首次使用或环境变化后先运行 baker 的 doctor.py。循环效果给动作渲染器传 --looping 和明确的 --tail 观察时长。

```powershell
$effect = (Get-Content -Raw -Encoding utf8 (Join-Path $ir 'manifest.json') | ConvertFrom-Json).meta.effect_id
& $py "$b\preview_ir.py" --ir $ir --out-dir "$work\sandbox" --keep-sandbox-pack --env daylight --cam-mode auto --supersample 2 --fps 24
if ($LASTEXITCODE) { throw '沙箱编译或特效 L2 渲染失败' }
$sandbox = "$work\sandbox\${effect}_sandbox_preview.pack"
$joint = @('--pack',$sandbox,'--wrapper',$wrapper,'--motion',$motion,'--attach-bone',$bone,'--tracking',$tracking,'--vfx-start',$start,'--vfx-end',$end,"--offset-pos=$offset","--offset-rot=$rotation")
if ($model) { $joint += @('--model',$model) }
& $py "$s\preview_action_vfx.py" @joint --fps 24 --env daylight --out-dir "$work\review"
if ($LASTEXITCODE) { throw '动作同屏 L2 渲染失败' }
```

`$effect` 为 IR 的 effect_id；`$wrapper` 为实际键。先看 L1-A/B、通用签收 JSON，再打开联调 MP4 和拼图。模型默认不盖骨线；加 `--guides true` 可叠加，省略 `--model` 是纯骨架。对比跟随与定点用同一骨名，分别传字符串 `true` / `false`。L2 的 PASS 只表示该镜头的触发和空间对位；事件 EndTime、动作混合、同步处决和实机材质只在 L3 终验。

交付前打开本轮 review/action_vfx.mp4，结合 action_vfx_sheet.png 核对触发前、触发、峰值和收尾，特别检查特效是否贴合运动中的挂点。同时查看 sandbox 下的 *_l2_preview.mp4 和诊断图。在回复中直接附上动作同屏 MP4 与特效 MP4，分别引用 action_review.json 的 status 和 *_preview_signoff.json 的 l2_preview_status。缺帧、播放失败或看不到应有特效时，定位问题并重渲；若为 PARTIAL，列明 omissions 和仍需确认的部分。用户看过本轮视频并明确同意写回后才继续第 4 节。

## 4 · 签收后，先入特效，再写动作事件

仅在用户审过对应版本并明确指定可写工作区目标后执行。下例 `$baseline` 是特效入包前基线；`$eventBase` 是入包后、写事件前的另一份基线。两者不可互换。

```powershell
$r = Get-Content "$work\sandbox\${effect}_preview_signoff.json" -Raw -Encoding utf8 | ConvertFrom-Json
& $py "$b\bake_from_ir.py" --ir $ir --pack $target --baseline-pack $baseline --preview-pack $sandbox --work-dir "$work\bake" --env $r.env_preset --cam-mode $r.camera --width $r.width --height $r.height --supersample $r.supersample --slope-deg $r.slope_deg
$event = @('--pack',$target,'--baseline-pack',$eventBase,'--anim-meta',$metaPath,'--vfx-key',$wrapper,'--start',$start,'--end',$end,'--bone-index',$index,"--offset-pos=$offset","--offset-rot=$rotation",'--tracking',$tracking,'--review',"$work\review\action_review.json")
& $py "$s\inject_anm_meta_vfx.py" @event
if ($LASTEXITCODE) { throw '事件计划未通过，停止写入' }
& $py "$s\inject_anm_meta_vfx.py" @event --apply
```

回读必须命中同一 EFFECT 的键、时间、骨、偏移与 tracking，以及每个松散/容器副本。交付视频、联调记录、两份基线与目标；双人处决和上下身混合明确标「混合状态仅 L3 实机可终验」。

| 症状 | 第一项检查 |
|---|---|
| 枪焰在手腕、特效在脚底或头顶 | 比较骨原点与模型实际枪口/接触面；检查骨索引、偏移所属坐标系及变体缩放。 |
| 刀光歪 90°、模型不随动作 | 用真实骨四元数叠线；核对骨顺序与逆绑定姿态，不靠交换轴或静态摆模型修正。 |
| 改了 META 却不触发 | 先读槽引用，再核对松散和每个 animpack 副本；注册 wrapper 本身不会创建触发。 |
| 切段截断、残留或偏心甩飞 | 分查事件结束、发射器/粒子寿命、infinite_life、空间和枢轴；动作切换留给实机。 |
