# 挂到模型上的点，要从真实姿态求出来

## 同一个坐标系供 Houdini 和 L2 使用

CA 角色模型空间为右手、Y-up、单位米。保留资产的原 X/Z 符号，角色朝哪侧取决于 rig；不要硬把 +Z 当成每件武器的枪口方向。骨局部四元数顺序 xyzw，父子 FK 得到模型空间的骨位置和转向；未写入的通道回填绑定姿态。`root_motion` 是相对首帧的根位移。这里没有战场出生位置、实机地形高度或 actor 世界变换。不要重复应用 root 位移或变体缩放。

`action_motion.json` 给出动作、骨架、来源、单位缩放、原事件及各武器骨速度峰值/高度极值。`action_motion.npz` 包含 times、bone_pos、bone_quat、bone_vel、parents、bone_names、root_motion 与武器/脚索引。速度单位 m/s，帧索引从 0 开始；Houdini 第 1 帧对应 0 秒。最低脚点是接触候选，不是命中事件。速度峰值也未必是 FIRE/IMPACT 时刻。

`build_action_scene.py` 生成新的 .hip，不覆盖旧文件；与 motion、actor 工作目录一起保留。场景中 /obj/action_guides 给出实时点组 pose、整段 weapon_trails；点属性 P/v/orient/u/power_frame 可驱动发射和轨迹裁切。/obj/action_model 从同一时钟蒙皮，带 UV、原基础色贴图、法线和分部材质。可只显示模型、单开引导线，或在两者都显示的视图检查接触。场景生成后，先拖过触发前后帧，确认武器跟手，再把轨迹转为标准 Route-A IR。

**模型支持边界：**当前已实现 RMV2 v8、weighted4（36 B/顶点）、带 .wsmodel 逐部位材质的同骨架网格。用 mesh 的真实索引/权重与逆绑定矩阵，不向最近骨头硬贴点：
`P_pose = Σ w_i · (G_pose_i · inverse(G_bind_i)) · P_rest`。同一武器的多个骨可以独立运动。单位变体多个选项时指定实际变体，或用可重复的 `--wsmodel` 指定同 rig 组件；额外 socket 变换、独立骨架坐骑、其他顶点布局不在此快路。模型材质是基色诊断照明；不宣称复现原版皮肤、发光、布料或全部 PBR。

## 跟着骨动，还是把落点留在原地？

- **跟随**（`--tracking true`）：每帧取骨的完整模型空间变换。给定局部偏移与转角，组合为 骨 × 偏移 × wrapper。旋转输入度数 x,y,z，矩阵顺序 Ry × Rx × Rz；位置为未缩放骨局部米。
- **冻结**（选同一骨名，`--tracking false`）：在精确触发秒插值捕获骨的位置和朝向，此后不跟随。真正的模型空间原点才用 `--attach-bone world --tracking false`。这并不会把挂点自动吸附到地面。
- 已知模型空间接触点 P（例如已确认在 animroot 局部的 FIRE_POS 先转到模型空间），反求 `offset = R_boneᵀ · (P − P_bone) / unit_scale`。用武器网格的末端/刃口交叉检查。模型叠图中若闪光在手腕而非枪口，不要通过改动动作时间掩盖空间错误。
- 同屏渲染将**实际蒙皮网格先写入和地形共用的深度缓冲**，随后渲染签收 sandbox 的特效；可叠加同镜头骨线。峰值选择同帧「角色+特效」和「仅角色」的像素差，避免把角色移动误当作特效峰值。播完动作后若仍观察粒子尾部，保持最后姿态。EndTime 是联调及回写的数据，不是在 L2 精确重演的引擎截断。

## 在编译后的 XML、材质和模型上拦四类错误

| 条件 | 处理 |
|---|---|
| MaximumParticles=1 | 严格 EmLife < PLife。无载体旋转动画时 EmLife ≤ min(0.05 s, 0.4 × PLife)；有 EmitterOrientation 动画时 ≤ PLife − 0.15 s。取该质量档的最大发射器/最小粒子寿命。单发 wrapper 必须明确 infinite_life=false。循环用 `--looping --tail 秒数` 限定观察窗。 |
| 已生成的 continuous_rigid 要走复杂轨迹 | EmitterPosition 只在出生时采样；不得用多关键帧拖动同一刚体。首选真实骨 tracking；网格内相对运动用 AnimFile。多粒子在不同位置出生则是另一种用途。 |
| LOCAL_SPACE_POSITION_ROTATION + 偏心位置 + 载体旋转 | 会绕载体原点产生力臂。自转移到 ParticleOrientation/ParticleSpin；需要偏心几何时中心化 pivot 或动画驱动。 |
| 不透明遮罩与透明发光刚体叠层 | 对实际成对的层使用重复的 `--sort-pair 遮罩ID:发光ID`；校验有效枢轴重合且材质显式 sort_override 递增。单组已识别原版黑底可自动配对；复杂材质按设计显式声明。 |

只以角色/镜头能支持的结论签收；预览出现 omissions 或不可见时应查看 PARTIAL 原因。复杂分层动作、上下身混合移动射击、双人同步与事件结束必须在 L3 用真实单位/加载环境验证。
