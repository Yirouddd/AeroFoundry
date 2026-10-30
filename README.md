# Aerial Manipulation VLA 第一阶段代理整合

本项目将已有的 Embodiment Understanding、Task Generation、Asset & Scene Agent 放在同一个 Python 项目中，并提供 `aerial_vla_pipeline` 串联入口。三个原始项目及机器人模型均未修改。

## 当前范围

1. Embodiment 读取 USD 模型，输出 `RobotProfile.json`、`ModelValidationReport.json` 和 `EmbodimentResult.json`。
2. Task Generation 读取上述产物，输出任务意图、任务/成功条件及审计记录。
3. Asset & Scene 接收任务结果和机器人描述，通过 Isaac Lab launcher 在 Isaac Sim 中构建、重新打开并检查场景。
4. `PipelineResult.json` 保存各阶段状态与产物的路径和 SHA-256。

Task Generation 的 `PRODUCED` 只表示任务意图已生成；Asset & Scene 的 `PASSED` 只表示其场景检查通过。现有模型的 Embodiment 状态为 `NEEDS_INFORMATION`，其中最大总推力和末端额定负载仍未知。项目尚不包含 Gate A、轨迹、控制或 VLA episode。

Asset & Scene 当前仅实现 `press` 任务中 `button` / `push button` 且无非空场景硬约束的适配。其他模板明确返回失败，不应视作已支持。按钮网格是视觉资产；关节、弹簧、质量和摩擦由仿真配置构建，不是网格原有的物理参数。场景验证也不证明机器人接触、飞行或任务可行性。

## 文件结构

```text
embodiment_agent/       原 Embodiment 实现
task_generation/        原 Task Generation 实现及七类模板
asset_scene/            Asset & Scene 实现，含 Isaac Lab runner
aerial_vla_pipeline/    新增串联入口
assets/cache/           已校验的 Kenney Factory Kit 资源包和来源记录
tests/                  原测试集及接口集成测试
examples/               本机请求与离线提案示例
example_run/            两阶段离线串联结果
scripts/                Isaac Lab 后端兼容性试验
```

## 安装

Agent 层使用 Python 3.13，在项目根目录运行：

```bash
python3 -m pip install -e .
```

依赖为 `usd-core==26.8` 和 `openai>=1,<3`。`usd-core` 用于从原始 USD 模型重新运行 Embodiment；复用已生成的 Embodiment 产物时无需加载它。Isaac Lab 场景进程使用独立的 Isaac Python，不要在系统 Python 中安装 Isaac Sim。

当前 Linux 主机的兼容运行时为 Isaac Sim `5.1.0.0`、Isaac Lab `0.48.0`，解释器位于 `/home/windshape/conda-envs/a3-isaaclab5/bin/python`。Agent 层与 simulator 分别运行在 Python 3.13 和 Isaac 环境 Python 中。先配置本机模型、已有 Embodiment 产物及 Isaac Lab 解释器：

```bash
export AERIAL_VLA_ROBOT_MODEL_DIR=/absolute/path/to/robot/model
export AERIAL_VLA_EMBODIMENT_DIR=/absolute/path/to/embodiment/outputs
export AERIAL_VLA_ISAAC_PYTHON=/home/windshape/conda-envs/a3-isaaclab5/bin/python
```

不要把 API 密钥写入请求或项目文件。请求里的 `${...}` 路径会由 pipeline 展开。

## 从原始 USD 模型运行

创建一份符合 `PipelineRequest` 的 JSON。必填字段为 `job_id`、`request_id`、`model_directory`、`output_directory`；其余字段可参考 `examples/reuse_existing_request.json`，但删除 `embodiment_directory`。随后运行：

```bash
export OPENAI_API_KEY='<your-key>'
export OPENAI_MODEL='<your-model-id>'
python3 -m aerial_vla_pipeline path/to/request.json
```

串联入口先执行 Embodiment，保存其结果，再将这次新产物的引用交给 Task Generation。请求包含 `isaac_root`、`isaac_python` 或设置了 `AERIAL_VLA_ISAAC_PYTHON` 时，再将任务结果交给 Asset & Scene。Linux runner 使用 `isaaclab.app.AppLauncher` 和 `isaaclab.sim.SimulationContext`，不再调用 `python.bat`。Embodiment 为 `ERROR` 时不会调用后续模块；Task Generation 未产出时不会构建场景。Embodiment 为 `NEEDS_INFORMATION` 时仍可生成**候选任务意图**，并把缺项传入任务报告；这不是 Gate A 通过。

## 复用现有产物进行离线验证

下面的命令不调用 USD 解析器或外部模型。它从用户现有的 Embodiment 输出目录复制产物，先校验其哈希，再用固定提案测试接口交接：

```bash
python3 -m aerial_vla_pipeline examples/reuse_existing_request.json --proposal-file examples/press_proposal.json
```

`examples/press_proposal.json` 是离线测试输入；`example_run/` 是对应的运行结果。它不是新的 GPT 调用。若把项目移动到另一台机器，需更新示例请求中的绝对路径。

要继续构建按钮场景，在项目根目录运行：

```bash
python3 -m aerial_vla_pipeline examples/reuse_with_scene_request.json --proposal-file examples/press_proposal.json
```

该请求使用本项目 `assets/cache/` 下的资源包及已记录的来源信息，先验证哈希与授权文本，无须再次下载。若省略 `cached_asset_archive`，Asset & Scene 会执行实时资源发现与下载，需要网络。场景位于 `runs/scene_demo/asset_scene/scenes/<task_id>/`，移动场景时应保留其中的 `assets/` 和 `dependencies/`。`SCENE_BUILT` 仅表示 Asset & Scene 的局部检查通过，`gate_a` 仍为 `NOT_EVALUATED`。

## 测试

```bash
python3 -m unittest discover -s tests -p 'test_task_generation.py' -v
python3 -m unittest discover -s tests -p 'test_pipeline.py' -v
python3 -m unittest discover -s tests -p 'test_asset_scene.py' -v
python3 -m unittest discover -s tests -p 'test_embodiment.py' -v
```

最后一组测试需要已安装的 `usd-core`。`examples/reuse_*_request.json` 通过环境变量引用队友提供的 Swan Gamma 目录；该机器人资产当前不在此 Linux 主机，因此这些示例在设置有效本地路径前不能完整运行。P5 M500 与 Asset & Scene 后端 smoke 的命令、原始尝试及结果记录在 [`evidence/2026-10-30-isaaclab-p5-compatibility/`](evidence/2026-10-30-isaaclab-p5-compatibility/README.md)。最新 scene smoke 使用 PhysX `RigidObject` state view，合成 PressCap drive `12.232 mm`、probe-induced cap 位移 `17.700 mm`、return error `0.235 mm`；早期 0 位移失败保留。它验证场景机构/碰撞传播，不运行 HQP、不代表机械臂接触、飞行或任务验收。
