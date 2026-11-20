---
name: wh3-vfx-bake-pack
description: 用户已经审片并明确授权写入指定工作区 MOD 包时使用。核对《全面战争：战锤 3》Route-A 特效的宿主、基线与签收沙箱，逐条回读 PFH5，从成品包重绘并交接 L3 终验。
metadata:
  short-description: "按已审沙箱烘焙战锤3特效并核验成品包"
---

# 把用户审过的特效写进指定包

## 写入前的三个判定

- **审的是哪一版？** 取同次预览的 IR、`*_preview_signoff.json` 和保留的 `*_sandbox_preview.pack`。核对 effect_id、wrapper 及两项 L1 状态。用户尚未审片、IR 已改或缺少对应沙箱时，先回 `$wh3-vfx-author-preview`。`SKIPPED_L3_ONLY` 没有 L2 视频；`PARTIAL` 遗漏的层要有对应 L3 证据，或由用户明确接受该项局限。
- **游戏沿哪条链触发？** 从实际技能阶段、投射物 display/explosion 或 vortex 找到特效键；有 CSC 的宿主核对 `VFX_ELEMENT` 与 `host_binding.wrappers[].vfx_key`。声明 `host_csc` 后由烘焙器从基线或原版解码检查。新增 `particle_effects_tables` 分片只完成注册，不会建立玩法引用。需要改 DB 或 CSC 时，仅在已授权挂接范围内编辑并回读。
- **从哪份包重建？** 确认用户指定的是工作区目标，基线与目标为不同文件。首次写入可从现有目标创建单独的基线备份，以后只读复用。如果目标有基线之外的新改动，先解决版本差异，不要覆盖其他人的工作。游戏目录保持只读，不启动游戏、Terry 或管理器。

## 从签收记录复用镜头，而不是重新猜参数

下例可在任意工作目录运行；安装脚本会自动设置用户级环境变量 `WH3_VFX_BAKER`（如未设置，指向仓库内的 `baker` 目录）。首次使用或换环境先体检。下例中的文件变量对应上面已核对的 IR、签收 JSON、沙箱、目标、基线及独立输出目录：

```powershell
$baker = if ($env:WH3_VFX_BAKER) { $env:WH3_VFX_BAKER } else { throw '请先运行仓库根目录 install.ps1，或将 WH3_VFX_BAKER 指向 baker 目录' }
$py = powershell -NoProfile -ExecutionPolicy Bypass -File "$baker\resolve_python.ps1"
& $py "$baker\doctor.py"
if ($LASTEXITCODE) { throw '先修复 doctor 报出的必需项' }
$r = Get-Content -LiteralPath $signoff -Raw -Encoding utf8 | ConvertFrom-Json
& $py "$baker\bake_from_ir.py" --ir $ir --pack $target --baseline-pack $baseline --preview-pack $sandbox --work-dir $build `
  --env $r.env_preset --cam-mode $r.camera --width $r.width --height $r.height --supersample $r.supersample --slope-deg $r.slope_deg
```

旧签收 JSON 若缺少镜头字段，从当次运行命令补齐；无法重建时先重新出预览。不要用 L3 别名测试包代替签收沙箱。

1. **替换前**：检查 L1-A、与签收沙箱的条目清单和内容比对，以及候选 PFH5 新写条目的 SHA-256 回读。任一不符就停在写入前，不跳过 `--preview-pack`。
2. **替换后**：确认从目标包跑出的 L1-B、L2；同参数比较两次的时相图、诊断图和 omissions。需要成品视频可加 `--render-video`。L1-B 或 L2 失败时目标可能已替换：停止分发，保留日志并核对恢复基线；有并发写入先协调，不能直接覆盖。
3. **交付**：给出目标、可恢复基线、实际宿主键、写入条目数与回读结果，以及成品包的视觉对照。只将 Terry/真实战场中的触发、天气受光、复杂地形或多单位并发检验记为 L3。
