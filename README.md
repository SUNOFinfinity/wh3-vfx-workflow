# wh3-vfx-workflow

面向 **《全面战争：战锤 3》**（Total War: WARHAMMER III）的 **Route-A 原生特效制作、沙箱 L2 审片、动作同屏对齐与安全烘焙工作流**，开箱适配 **Codex Skills**。

- **安全原则**：全程只读游戏目录，不改 `fxc/shaders.manifest`，不自动启动游戏、Terry 或 Mod Manager；所有特效先在独立沙箱包渲染 L2 视频与多环境诊断图，经你签收后再基于独立基线哈希校验落盘。
- **内置 4 个 Codex Skills**：
  1. `$wh3-vfx-author-preview`：从标准 IR 编译独立沙箱包，回读产出 L1-A/B 校验、双视角 L2 MP4、多环境诊断图与签收 JSON。
  2. `$wh3-action-vfx-from-pack`：从 MOD Pack 提取单位动作、骨骼轨迹与蒙皮模型，在 Houdini 中对齐挂骨/偏移并生成**动作+特效同屏 L2 预览视频**，签收后安全写回 `.anm.meta` 事件。
  3. `$wh3-vfx-bake-pack`：核对已签收沙箱与镜头参数，逐条 SHA-256 回读写入目标 MOD Pack 并复验成品包。
  4. `$vfx-art-direction`：特效美术指导技能，把“简陋、贴纸感、像灯管、缺立体感”等观感问题转成可验证的造型、材质、运动与节奏修改。

---

## 两条核心工作流

```text
工作流一（常规法术 / 弹道 / 光环 / 爆炸 / VAT 特效）：
  [$vfx-art-direction] + [$wh3-vfx-author-preview]  ──▶  审 L2 MP4 & 诊断图  ──▶  [$wh3-vfx-bake-pack] 写回 MOD Pack

工作流二（绑定单位动作的枪口焰 / 刀光 / 砸地 / 挂骨特效）：
  [$vfx-art-direction] + [$wh3-action-vfx-from-pack] ──▶  审动作同屏 L2 MP4  ──▶  [$wh3-vfx-bake-pack] 入包 + 写回 .anm.meta 事件
```

---

## 3 步傻瓜式安装（Windows 10/11 + Codex）

### 前置条件
- 已安装正版 **Total War: WARHAMMER III**（支持 Steam 自动发现；非 Steam 或自定义位置可在配置里指定）
- 已安装 **Python 3.10+**（安装时勾选 `Add python.exe to PATH`）与 **Codex**
- （可选）**Houdini**（使用 Houdini 制作场景或动作对齐时需要）
- （可选）**Node.js 18+**（仅在显式校验 `host_csc` 时需要；ESF/CSC 编解码器已内置于 `baker/csc_codec/`）

### 第 1 步：克隆仓库
```powershell
git clone https://github.com/SUNOFinfinity/wh3-vfx-workflow.git
cd wh3-vfx-workflow
```

### 第 2 步：一键安装（自动建环境、装依赖、部署 Codex Skills 并体检）

- **仅使用工作流一（常规特效制作 + 烘焙）**：双击 `install.bat`，或运行：
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **同时使用工作流二（动作提取 + 动作同屏预览 + 事件写回）**：
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithActionTools
  ```
  > 如果你本机已经有 AssetEditor 或 RPFM，可直接指定路径免重复下载：
  > ```powershell
  > powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithActionTools -AssetEditorDir "D:\Tools\AssetEditor" -RpfmCli "D:\Tools\RPFM\rpfm_cli.exe"
  > ```

安装脚本会自动完成：
1. 创建仓库内隔离环境 `.venv` 并安装 `numpy`、`Pillow`、`zstandard`；
2. 自动探测或下载 `texconv.exe`、`ffmpeg.exe`（加 `-WithActionTools` 时同时处理 `rpfm_cli.exe`、`schema_wh3.ron` 与 `AssetEditor`）到被 `.gitignore` 忽略的 `tools/`；
3. 生成 `wh3_vfx_config.json`；
4. 将 4 个 Skill 安装到 `$HOME\.codex\skills\`，并注册用户级环境变量 `WH3_VFX_BAKER`；
5. 自动运行 `baker/doctor.py` 体检并输出报告。

### 第 3 步：重启 Codex，跑一次 30 秒冒烟测试（可选）
```powershell
.\.venv\Scripts\python.exe .\examples\generate_smoke_ir.py --out .\work\smoke_ir
.\.venv\Scripts\python.exe .\baker\preview_ir.py --ir .\work\smoke_ir --out-dir .\work\smoke_preview --keep-sandbox-pack --env daylight --cam-mode ground --fps 12
```
打开 `work\smoke_preview\smoke_golden_burst_l2_preview.mp4`，看到金色爆发动画即代表整条渲染与编译链路完全打通。

---

## 在 Codex 里怎么用（直接发自然语言）

重启 Codex（让新装的 Skills 与 `WH3_VFX_BAKER` 生效）后，在任意 MOD 工作目录直接对话即可：

- **工作流一 · 制作并审片**：
  > 用 `$vfx-art-direction` 和 `$wh3-vfx-author-preview` 做一个金色圣光冲击波特效，挂地半径 6 米，持续 2.5 秒，生成 L2 视频和多环境对照图给我审片，暂不写目标 MOD 包。
- **工作流一 · 签收并入包**：
  > 上一版 L2 视频通过了，请用 `$wh3-vfx-bake-pack` 把它写进 `D:\my_mod\test.pack`，宿主键是 `my_holy_wave`。
- **工作流二 · 基于单位动作做特效**：
  > 用 `$wh3-action-vfx-from-pack` 从 `D:\my_mod\my_unit.pack` 提取单位 `my_gunner` 的开火动作和模型，在枪口位置做一个火光与烟尘特效，生成动作同屏 L2 视频给我看。
- **工作流二 · 签收并写回动作事件**：
  > 动作同屏视频对齐没问题，请先烘焙特效再写回 `D:\my_mod\my_unit.pack` 的动作事件。

---

## 配置与排错

所有路径均由根目录 `wh3_vfx_config.json`（模板见 `wh3_vfx_config.example.json`）或同名环境变量覆盖，随时可复跑体检：

```powershell
# 工作流一体检
.\.venv\Scripts\python.exe .\baker\doctor.py --profile basic

# 工作流一 + 工作流二完整体检
.\.venv\Scripts\python.exe .\baker\doctor.py --profile action
```

常见问题：
- **PowerShell 报脚本执行策略错误**：始终通过 `install.bat` 或 `powershell -ExecutionPolicy Bypass -File ...` 运行。
- **找不到战锤 3 目录**：在 `wh3_vfx_config.json` 中手动填入 `"WH3_GAME_DIR": "你的Steam库/steamapps/common/Total War WARHAMMER III"`。
- **ANGLE GLES3 检测失败**：自动探测会优先检查游戏目录、系统 `PATH` 以及本机 Edge/Chrome 附带的 `libEGL.dll` + `libGLESv2.dll`；若未命中，将任意 64 位 ANGLE 的 `libEGL.dll` 与 `libGLESv2.dll` 放入 `tools/angle/`，或在 `wh3_vfx_config.json` 指定 `ANGLE_DLL_DIR`。
- **卸载 Skills**：运行 `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1`（不删本地仓库）。

---

## 仓库结构

```text
wh3-vfx-workflow/
├── install.bat / install.ps1 / uninstall.ps1   # 一键安装与卸载
├── requirements.txt                            # Python 依赖（numpy, Pillow, zstandard）
├── wh3_vfx_config.example.json                 # 配置模板
├── baker/                                      # Route-A IR 编译、L1/L2 校验与安全烘焙引擎
│   ├── csc_codec/                              # 内置 ESF/CSC 无损编解码器（MIT）
│   ├── manifest.schema.json                    # 标准 IR JSON Schema
│   └── WH3_VFX_AUTHORING_SPEC.md               # Route-A 制作与材质规范
├── skills/                                     # 4 个 Codex Skills 源码
│   ├── wh3-vfx-author-preview/
│   ├── wh3-vfx-bake-pack/
│   ├── wh3-action-vfx-from-pack/
│   └── vfx-art-direction/
└── examples/
    └── generate_smoke_ir.py                    # 最小自测 IR 生成器
```

## 开源协议与声明

本项目采用 **MIT License**（见 [LICENSE](LICENSE)）。第三方组件版权与许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库不包含任何《全面战争：战锤 3》官方游戏文件或第三方 MOD 资产。
