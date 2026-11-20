# 从单位键查到真正会触发的事件

1. 在目标 PFH5 的 `land_units_tables` 查单位的 `man_animation`；有 personality override 时同时查 junction 与 `battle_personalities_tables`。用 `battle_animations_table_tables` 对照骨架。DB 由 RPFM 按本机 WH3 schema 导出；显示的是本包行，不等于游戏中全部继承关系。单位 DB 只在依赖包时，给出具体表名或动作虚拟路径并注明证据。
2. 在 `animations/database/battle/bin` 查同表名的 v4 fragment：每个槽记录数值 ID、武器集合、选取权重以及各变体的 anim/meta/sound 三条路径。名称依赖枚举版本；脚本默认使用 AssetEditor 的 `Warhammer3AnimationSlots_dlc24.txt`，可用 `WH3_SLOT_FILE` 指向匹配版本。不要用相邻版本的名字反推编号。
3. 对每个虚拟名列出外层松散条目及全部 animpack 副本。animpack 是有序记录：小端 u32 数量、u16 字节长度 + UTF-8 名、u32 内容长度 + 内容。只在查找时忽略斜杠/大小写，不改成员原名和顺序。本机实包中，同一 fragment 在松散 `.bin` 与 `.animpack` 中各一份。文化、启用 MOD 与加载顺序会改变实机选中的来源；离线无法证明全局优先级。副本一致才继续，不一致先定位实际加载集合，不赌哪份覆盖。
4. clip 或 bind 不在目标包时，按 `--game-data` 或 baker 的只读目录查 `anim*.pack`，优先查已知散项再查文化容器。例如本机骨架来自 `anim2.pack::animations/skeletons/humanoid07c.anim`。检查 clip 骨名、父子关系和 fragment 骨架。没有 fragment 的独立 `.anim` 允许直接提取；同名 META 只是候选，可显式传 `--anim-meta`、`--sound-meta`，不能凭同名关系回写。
5. 外观走另一条链：单位 → `unit_variants` → `variants.variant_filename` → VMD 的 SLOT → `.wsmodel` → LOD / part 对应 RMV2 与材质 → DDS。以变体的实际 scale 或名义尺度导出，记录 scale_variation；马匹、随机装备、多候选部位不能全部叠在一起冒充一个单位。

## 用原标签读 META，按已知版本写

`meta_codec.ps1` 无界面加载已装 AssetEditor 的 `Shared.GameFormats.dll`。读取保留标签名、版本、可解码字段、起止秒数，并在有相应字段时关联骨索引；未知结构标为未解码。注意 VFX 是用途名：可写的原生事件实际叫 **EFFECT**，不是一个叫 VFX 的字符串。原版例证：`anim2.pack::animations/battle/ape01/attacks/ap01_attack_02.anm.meta`。不同的动作可以只有 FIRE_POS 等事件，不应伪造 IMPACT/SOUND。

- 新增使用 v2 META 中的 EFFECT v12；已有同名键只改唯一的 v11/v12 实例。两个 Start/End 时间组都写入；位置和四元数使用所选骨局部坐标。不删除其他事件，不碰 sound meta。有未知结构、多处同键、冲突副本、非字节一致的无操作往返时拒写。
- `--apply` 需要审过的联调记录及独立基线。先核对动作、骨架、所选模型来源、沙箱资产、时间和偏移，再做整个 PFH5 无改动重建（保留原时间戳）、容器和 META 的无改动往返。暂存候选逐项 SHA-256 回读；同时对无关外层 raw payload 及内部成员比对字节。原子替换前再查并发修改，替换后重开检查副本。重复相同事件返回 `unchanged`。
- 若目标不含可写的 META 或 fragment 没有指向它，先设计局部独立动作绑定；不要修改原版共享文件。Stage 2 的注册、动作事件挂接和玩家实机验收是三件不同的事。

## 环境

普通命令用 baker 的 `resolve_python.ps1`；`WH3_VFX_BAKER` 指向含 `pfh5.py`、`baker_config.py` 的目录。可覆盖 `ASSETEDITOR_DIR`、`PWSH_EXE`、`RPFM_CLI`、`RPFM_SCHEMA`、`WH3_SLOT_FILE`；Houdini 和 texconv 沿用 baker 配置。模型同屏需要 renderer 的可选 `draw_opaque_actor(VP)` 钩子（在地形写深度后、VFX 前）。
