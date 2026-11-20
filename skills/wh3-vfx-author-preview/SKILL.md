---
name: wh3-vfx-author-preview
description: 制作、修改或审片《全面战争：战锤 3》Route-A 战斗特效时使用。从标准 IR 编译独立沙箱包，回读生成 L2 视频、诊断图和签收记录；不写目标 MOD 包。
metadata:
  short-description: "战锤3特效制作、沙箱预览与审片"
---

# 从沙箱回读用户要审的特效

把“效果怎么做”和“写进哪个 MOD”分开。本阶段只在独立工作目录生成 IR、沙箱包和预览；游戏目录和目标 MOD 保持只读，不启动 Terry、游戏或管理器。特效风格由需求决定。

## 做出可审的预览

1. 先定触发宿主、挂点、尺度、持续时间和主要观察机位。按用途选择天柱/漩涡、弹道、爆炸/冲击波、循环光环或流体/破碎原型。用 Houdini 或程序生成 IR；字段查 baker 的 `manifest.schema.json`，采样、坐标、材质和原版依据查同目录 `WH3_VFX_AUTHORING_SPEC.md`。
2. 下例可在任意工作目录运行；安装脚本会自动设置用户级环境变量 `WH3_VFX_BAKER`（如未设置，指向仓库内的 `baker` 目录）。首次使用或环境变化先体检，再编译到独立的输出目录：

```powershell
$baker = if ($env:WH3_VFX_BAKER) { $env:WH3_VFX_BAKER } else { throw '请先运行仓库根目录 install.ps1，或将 WH3_VFX_BAKER 指向 baker 目录' }
$py = powershell -NoProfile -ExecutionPolicy Bypass -File "$baker\resolve_python.ps1"
& $py "$baker\doctor.py"
if ($LASTEXITCODE) { throw '先修复 doctor 报出的必需项' }
& $py "$baker\preview_ir.py" --ir $ir --out-dir $preview --keep-sandbox-pack --env daylight --cam-mode auto --supersample 2 --fps 24
```

3. 审片依据必须来自同一份沙箱的回读：L1-A 验 IR，L1-B 验包；L2 产出 MP4、双视角时相图、Raw/Bloom/合成诊断图及 `*_preview_signoff.json`。打开成片和关键帧，检查出场、峰值、收尾与镜头遮挡；循环效果还要看首尾衔接。若有遗漏，以签收 JSON 的 `l2_preview_status` 和 `omissions` 为准。

## 针对缺陷选检查

| 观察到的问题 | 先查什么 |
|---|---|
| 流光遮罩失效、流向反了 | `wh_vfx_mesh_vertex_anim` 的全局遮罩取 **R**；三层纹理分别按通道权重采样。检查 UV.y、scroll 方向和线性 coverage。 |
| 软粒子发黑、贴花有框或翻页不动 | 区分粒子 shader 的显式 alpha 转换与网格纹理 alpha；检查预乘方式、压缩后 mip 边缘。静态选格是 `vs_cell_texture`，动画翻页是 `vs_animated_texture`。 |
| 主形拉远被裁、贴地层悬空或火星拉成针 | 查量化后 bbox、pivot、CullDistances 与拉伸 cap；用 `--slope-deg 18` 看贴合，分别试 `ground` 和 `beam` 机位。 |
| 白爆或跨环境观感不稳 | 拆看 Raw、Bloom、最终图；用 `--compare-env snow_prologue chaos_arbaal dark_forge middenheim` 在同一包、时相和机位对照。`daylight/dusk` 是工作室曝光。 |
| VAT 主体未出现在最终画面 | 分清三张二维 float4 VAT 与三维 half4 流场。核对 `*_vat_geometry.png` 首/中/末帧、bbox 和 UV；几何通过不能替代延迟材质受光验收。 |

已有宿主 CSC 时，可在 wrapper 声明 `host_csc` 并用 `--host-pack` 指定只读基线。需要人工 L3 测试包时才加 `--l3-sandbox`；`--l3-only` 会跳过 L2 视频。

交付可打开的 MP4、时相图、诊断图、签收 JSON 及对应的 `*_sandbox_preview.pack`。L1 有错先修 IR；L2 为 `PARTIAL` 时，指出哪个效果仍需实机确认。用户认可预览并明确指定写入目标后，再交给 `$wh3-vfx-bake-pack`。
