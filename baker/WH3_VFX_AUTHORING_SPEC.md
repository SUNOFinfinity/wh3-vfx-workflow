# 战锤 3 Route-A 特效制作规范
基准：Windows / WH3 **9.x**，IR 接口版本 `1.0.0`。本规范约束游戏能读取的资产和交付证据，不规定配色、造型或美术风格。

## 从源工程到可签收的包
1. DCC 导出 `manifest.json`、网格 NPZ 和纹理源；可选原生刚体根轨。用 [Schema](manifest.schema.json) 对齐字段，用 `spec_validator.py --ir` 验证资产语义。IR 的米、秒、角度、UV 方向不可混用。
2. 在 baker 目录用具有 NumPy、Pillow 的 Python 运行 `doctor.py`。工具位置依次取环境变量、邻近 `wh3_vfx_config.json`、自动探测、本机回退。配置文件也可由 `WH3_VFX_CONFIG` 指定；可设 `WH3_GAME_DIR / WH3_DATA_DIR / TEXCONV_PATH / FFMPEG_PATH / ANGLE_DLL_DIR / ZSTD_DLL_PATH / HOUDINI_BIN_DIR / PYTHON_EXE`。SQLite 只缓存偏移，不存游戏资源，目录可由 `WH3_VFX_CACHE_DIR` 指定。
3. `preview_ir.py --ir <IR> --out-dir <preview> --keep-sandbox-pack`：L1-A → 编译独立沙箱 → L1-B 回读 → L2 双视角 MP4、时相图、诊断图和签收 JSON。L1 ERROR 先修复，WARNING 看其影响；`omissions` 非空时为 PARTIAL，不用空白物件代替通过。L2 PASS 仅表示当前实现顺利绘制声明的层。
4. 用户审片并明确授权目标后，`bake_from_ir.py --ir <IR> --pack <目标> --baseline-pack <干净基线> --preview-pack <保留的沙箱> --work-dir <输出>`。同一相机、取景、采样、环境和坡度才可作视觉对比。覆盖前比较沙箱资产清单与内容；新候选包逐条 SHA-256 回读后原子替换。**L1-B / L2 在替换后运行**；若失败，停止交付并从不可变基线恢复。游戏目录只读，不启动游戏或 Terry，不写 shader manifest。L3 仅指用户在真实战场/Terry 中观察到的结果。

## 网格、材质与贴图：先确定采样者
- **几何**：Y 向上，单位米，局部原点为锚点；由法线和三角形叉积检查绕序。默认编译 v7 单组、32 B 顶点、16 位索引。受支持的静态 donor 为 `cylinder`（默认）、`sphere`、`plane`；不把全部 v7/v8 格式当成同一布局。双面由反面几何生成。组头相对偏移 +24/+36 写量化后 bbox 的最小/最大 XYZ，+628 写零 pivot。半精度最大有限值 65504；大坐标先做局部化，按所需细节评估量化误差；对竖直大模型优先源几何预缩放，控制 XML 比例。v8 的存在不意味着可以对 v7 文件就地改为 float32。
- **流光网格** `wh_vfx_mesh_vertex_anim`：每层 `uv_i=(uv0+t*scroll_i)*tile_i`；源 DCC 若是先 tile 再加速度，传入速度需除以 tile。用各自四通道权重与样本做点积，再乘 brightness。三层相乘，再乘**红通道** global mask、深度淡出、Fresnel、总透明度、顶点 alpha² 和启用的发射器透明度。线性 coverage 输入由编译器作 sqrt 编码。Fresnel 两端是 `(1−|N·V|)^p` 与 `|N·V|^p`，按 `normal_fade_in_out` 插值。Overlay 为 `rgb * lerp(1,strength,a)`；输出 RGB 乘未截顶的非负 alpha、顶点 RGB、glow 与发射器 RGB；目标 alpha 截在 [0,1]，混合为 **ONE / INV_SRC_ALPHA**。顶点位移有专用纹理蓝通道和 UV 衰减；原生振荡由时间、空间位置与材质参数共同决定。滚动方向看实际 UV；不是固定改一个符号。
- **颜色空间**：DXGI 99 是 BC7 sRGB，98 是 BC7 UNORM。sRGB 采样只线性化 RGB；网格样本的 A 保持线性。粒子库 `ps_texture` 另有显式的转换，不能推断成“所有格式/所有 pass 的 alpha 都做 EOTF”。非平铺光斑、贴花留透明护边，压缩后验 mip 0，同时查看较小 mip 的漏边记录。缩放颜色源用独立浮点通道。图集另外给单元格留边，不能只看整张图的外框。
- **粒子/地面**：`FULLRES_DEFERRED_ALPHA`、`HALF_RES_DEFERRED_ALPHA` 用于粒子，`DEFERRED_DECAL` 与 `DEFERRED_DECAL_EMISSIVE` 分别校对非发光/发光投影，`DEFERRED_DYNAMIC_LIGHT` 是点光。贴花、光斑在本工程的常规制作底线为 256 px。选模板时同时核对预乘开关与 `ps_lighting`，不得通过重复乘 alpha 来修黑边。原生 `vs_cell_texture` 是定格选格；动画使用 `vs_animated_texture` 的 `no_cells_xy / no_loops / time_mode`。速度拉伸必须给出合理 cap；工程生成曲线每轴以 28 个点为预算。原版控制点可乱序、可带 Bezier 手柄；L2 排序采样不等于复刻所有插值。
- **折射与后处理**：BC3/DXT5nm 的 A/G 为有符号偏移的 X/Y，中心保持中性。屏幕合成在采样的无归一化分支中是 `UV+0.02*XY*(1−A)`，另有按 B 归一的质量分支。亮度权重为 (0.2126,0.7152,0.0722)；Bloom 门限权重为 `saturate(L−threshold)`，场景、水面、emissive 和粒子在原版取不同增益。HDR 到屏幕是按亮度缩放的扩展 Reinhard，而不是 ACES。预览采用测试地形和有限的后处理分支，不因读到了环境参数就声称具备游戏的全部 GBuffer、天气 LUT 或自动曝光。
- **VAT 与流场分开**：新 VAT 材质选 `wh3_rigid_baked_fluid_simulation`，提供 `vat_pos_tex / vat_nrm_tex / vat_uv_tex / vat_fps / vat_frame_count`。使用原版三槽材质及 legacy FourCC 116 二维 float4；顶点 UV0 寻址，`frame=floor(t*fps)`，坐标是 `floor((uv0+(0,frame/N))*(W,H))`，读取绝对 XYZ 与动画 UV。不要为这一变体额外乘 bounds 或插帧；材质声明法线槽不等于该 VS 重建法线。老的两槽 IR 保留兼容输出并发出警告。L2 提供首/中/末帧几何线图、bbox 和 UV 回读；**没有 VAT 最终延迟光照画面**。粒子 VectorField 是 legacy FourCC 113 的 3D half4，不是 VAT。

## 发射器、宿主和地形
- 工程每库最多 40 个发射器，超出由 wrapper 引用多页；不是游戏硬上限。同步 0/1/2 三档质量、核对 CullDistances 的 z/w 与实际战斗视距。瞬发与循环分别验证衰减退出和无限引用生命周期。贴地展开用 `map_zero_y_to_terrain_and_align`，竖直主形用 `map_zero_y_to_terrain`；宿主 CSC 的挂点和朝向也必须匹配。
- 保持 wrapper → library → wsmodel → RMV2 / material → DDS 闭合。原生刚体黑色无辐射材质、网格折射透镜和 v7 单 building 根轨仅支持代码标出的模板子集；不将它们外推为任意 PBR、骨骼动画或镜头效果。
- 此版本 VFX 注册位于 **db.pack**，表的物理条目为 `db/particle_effects_tables/data__`。输出独立分片，不覆盖原版表。注册不等于触发。Phase 顺着 display/composite scene，投射物顺着 display/explosion/vortex，查到宿主实际引用的 XML key；可选 wrapper.`host_csc` 会解码 VFX_ELEMENT 核对原文键。只有显式声明宿主时才需要 Node 和 `CSC_CODEC_PATH`。技能玩法 DB 不会由烘焙器凭空生成。
- 天柱重点看双视角遮挡、竖直轴和落地；投射物分飞行与命中；爆炸分冲击前沿与尾部；光环检验长循环和多单位叠加；VAT 看几何诊断后，还需在 L3 校对受光与运动。机位 `auto / beam / ground / close`；`--slope-deg` 范围 −35 至 +35（解析平面）。`daylight / dusk` 是保留的工作室曝光。实包预设 `snow_prologue / chaos_arbaal / dark_forge / middenheim` 的来源分别为 data.pack 中 weather/battle 下的 `battle_prologue_02.environment`、`wh3_qb_arbaal_spooky_v04.environment`、`wh_forge_02.environment`、`middenheim_01.environment_group`（取首条）。用 `--compare-env snow_prologue dark_forge` 对同一沙箱、时相和机位附加输出对照图。

## 可选原生刚体通道：黑核、网格折射和单根动画

一般半透明网格仍使用 `wh_vfx_mesh_vertex_anim`。以下是从原版实例单独确认的特例，供 Route-A IR 显式选择：

| IR 材质 `shader` | 原版依据 | 支持边界 |
|---|---|---|
| `vfx_rigid_unlit_emissive_fresnel` | Garagrim ethereal axe 的 GBUFFER 实例、DXBC 与深度状态 | 只支持黑色、不抖动、零漫反射与零自发光的写深度材质。显式设置 `glow_colour=[0,0,0]`、`glow_strength=0`。不是通用 PBR。 |
| `wh3_vfx_distortion_mesh` | Howling Citadel 的 `continuous_rigid`、原模型材质和 DISTORTION DXBC | 继承原版遮罩和滚动纹理；可设置 `depth_fade_distance`、`distortion_tex_strength`、`panning_tex_direction` 与 `panning_texture_scale`。形状随几何法线和相机改变；同心拉伸可另用原版法线粒子。 |

需要随刚体移动时，网格指定 `rigid_root_bone:"building"`，发射器提供 IR 内的 `anim_source` 和沙箱内的 `anim_pack_path`。已支持 v7、单 building 动态位移/四元数轨。独发粒子的 `EmitterPosition` 只在出生时采样。保持出生锚点固定，把随时间的平移写入根轨；请在编译后按实际粒子缩放和根轨回读世界位置。

折射粒子可用 `donor_library` 指向 IR 内已取得的原版 XML 库，并用 `donor_id` 选择原发射器。`burst_count` 指同时存活的目标层数；`window_sec` 是发射窗口，应短于单粒子寿命。三个质量档均继承对应渲染通道。检查编译后实际活粒子数，不能以作者端字段代替。

如要求法线贴图在边缘严格零偏移，可在 BC3 DXT5nm 纹理上显式启用 `exact_zero_distortion_border`。原版粒子 PS 对 A/G 加 `0.5/255` 后再映射到有符号区间；顶级 mip 全 8px 的 A/G 回读为 `127/127` 才是精确零。R 不参与此通道。未启用时沿用原 BC3 策略。

预览与烘焙共用编译器。同一份 L2 沙箱用于正片、机位图、折射和几何读数；这些均不等同于 Terry/实机 L3 验收。
