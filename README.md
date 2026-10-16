# Aerial Manipulation VLA 第一阶段代理整合

本项目将已有的 Embodiment Understanding、Task Generation、Asset & Scene Agent 放在同一个 Python 项目中，并提供 `aerial_vla_pipeline` 串联入口。三个原始项目及机器人模型均未修改。

## 当前范围

1. Embodiment 读取 USD 模型，输出 `RobotProfile.json`、`ModelValidationReport.json` 和 `EmbodimentResult.json`。
2. Task Generation 读取上述产物，输出任务意图、任务/成功条件及审计记录。
3. Asset & Scene 接收任务结果和机器人描述，在 Isaac Sim 中构建、重新打开并检查场景。
4. `PipelineResult.json` 保存各阶段状态与产物的路径和 SHA-256。

Task Generation 的 `PRODUCED` 只表示任务意图已生成；Asset & Scene 的 `PASSED` 只表示其场景检查通过。现有模型的 Embodiment 状态为 `NEEDS_INFORMATION`，其中最大总推力和末端额定负载仍未知。项目尚不包含 Gate A、轨迹、控制或 VLA episode。

Asset & Scene 当前仅实现 `press` 任务中 `button` / `push button` 且无非空场景硬约束的适配。其他模板明确返回失败，不应视作已支持。按钮网格是视觉资产；关节、弹簧、质量和摩擦由仿真配置构建，不是网格原有的物理参数。场景验证也不证明机器人接触、飞行或任务可行性。

## 文件结构

```text
embodiment_agent/       原 Embodiment 实现
task_generation/        原 Task Generation 实现及七类模板
asset_scene/            原 Asset & Scene 实现，含 Isaac Sim runner
aerial_vla_pipeline/    新增串联入口
assets/cache/           已校验的 Kenney Factory Kit 资源包和来源记录
tests/                  原测试集及接口集成测试
examples/               本机请求与离线提案示例
example_run/            两阶段离线串联结果
```

## 安装

使用 Python 3.13，在项目根目录运行：

```powershell
python -m pip install -e .
```

依赖为 `usd-core==26.8` 和 `openai>=1,<3`。`usd-core` 用于从原始 USD 模型重新运行 Embodiment；复用已生成的 Embodiment 产物时无需加载它。场景构建另需本机安装 Isaac Sim，示例为 `D:\IsaacSim`。不要把 API 密钥写入请求或项目文件。

## 从原始 USD 模型运行

创建一份符合 `PipelineRequest` 的 JSON。必填字段为 `job_id`、`request_id`、`model_directory`、`output_directory`；其余字段可参考 `examples/reuse_existing_request.json`，但删除 `embodiment_directory`。随后运行：

```powershell
$env:OPENAI_API_KEY = '<your-key>'
$env:OPENAI_MODEL = '<your-model-id>'
python -m aerial_vla_pipeline path\to\request.json
```

串联入口先执行 Embodiment，保存其结果，再将这次新产物的引用交给 Task Generation。请求包含 `isaac_root` 时，再将任务结果交给 Asset & Scene。Embodiment 为 `ERROR` 时不会调用后续模块；Task Generation 未产出时不会构建场景。Embodiment 为 `NEEDS_INFORMATION` 时仍可生成**候选任务意图**，并把缺项传入任务报告；这不是 Gate A 通过。

## 复用现有产物进行离线验证

下面的命令不调用 USD 解析器或外部模型。它从用户现有的 Embodiment 输出目录复制产物，先校验其哈希，再用固定提案测试接口交接：

```powershell
python -m aerial_vla_pipeline examples\reuse_existing_request.json --proposal-file examples\press_proposal.json
```

`examples/press_proposal.json` 是离线测试输入；`example_run/` 是对应的运行结果。它不是新的 GPT 调用。若把项目移动到另一台机器，需更新示例请求中的绝对路径。

要继续构建按钮场景，在项目根目录运行：

```powershell
python -m aerial_vla_pipeline examples\reuse_with_scene_request.json --proposal-file examples\press_proposal.json
```

该请求使用本项目 `assets/cache/` 下的资源包及已记录的来源信息，先验证哈希与授权文本，无须再次下载。若省略 `cached_asset_archive`，Asset & Scene 会执行实时资源发现与下载，需要网络。场景位于 `runs/scene_demo/asset_scene/scenes/<task_id>/`，移动场景时应保留其中的 `assets/` 和 `dependencies/`。`SCENE_BUILT` 仅表示 Asset & Scene 的局部检查通过，`gate_a` 仍为 `NOT_EVALUATED`。

## 测试

```powershell
python -m unittest discover -s tests -p 'test_task_generation.py' -v
python -m unittest discover -s tests -p 'test_pipeline.py' -v
python -m unittest discover -s tests -p 'test_asset_scene.py' -v
python -m unittest discover -s tests -p 'test_embodiment.py' -v
```

最后一组测试需要已安装的 `usd-core`。本次整合已通过 Task Generation、Asset & Scene 和串联接口测试，并使用现有 Embodiment 产物、固定任务提案、已校验资源包在 Isaac Sim 6.0.1 中成功构建并重新打开 `press` 场景。由于当前环境无法安装 OpenUSD 依赖，本次没有重新从原始 USD 运行 Embodiment；其原项目保存了 4 项通过的测试记录。离线提案不代表新进行了一次模型调用。
