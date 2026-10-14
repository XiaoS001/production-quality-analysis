# 生产质量数据分析工具

> **数据为合成，非真实工业图像。** 本项目的缺陷图像数据由固定种子的 Pillow/numpy
> 生成器现场合成，页面指标和评测报告只用于验证标注、复核、传统 CV 检测与 IoU
> 评测流程，不代表真实工业缺陷检测能力。

## 项目定位

XM02 包含生产质量数据导入、良率看板和异常闭环，并在 P2-7 中补充一条完整链路：

`合成缺陷数据 -> 标注 -> 复核 -> 传统 CV 检测 -> IoU 评测`

这个模块用于说明数据处理和算法流程已经跑通，不是可上产线的工业缺陷检测系统。

## 环境安装

项目使用现有 `venv`，新增依赖仅为 Pillow 和 numpy：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

当前验证环境为 Python 3.14、Pillow 12.3.0、numpy 2.5.3。

## 生成合成数据

默认只生成文件，不写数据库：

```powershell
.\venv\Scripts\python.exe synth_defects.py `
  --count 80 `
  --seed 20260926 `
  --out data\defect_synth
```

输出结构：

- `data/defect_synth/images/*.png`：80 张 320x240 合成图。
- `data/defect_synth/labels/*.png.json`：每张图的合成真值框。
- `data/defect_synth/manifest.json`：固定种子、尺寸和图片清单。
- `data/defect_synth/quality_synth.db`：可选的隔离演示数据库。

所有清单、标签和图片表记录都带有 `is_synthetic: true`。相同命令和种子具有字节级
可复现性。

如需登记到隔离演示库，显式追加数据库参数；不要为验证任务写入 `quality.db`：

```powershell
.\venv\Scripts\python.exe synth_defects.py `
  --count 80 `
  --seed 20260926 `
  --out data\defect_synth `
  --database data\defect_synth\quality_synth.db
```

## 启动标注页

先设置隔离数据库和合成图片目录，再启动 Flask：

```powershell
$env:QUALITY_DATABASE_PATH=(Resolve-Path 'data\defect_synth\quality_synth.db').Path
$env:SYNTH_DATASET_DIR=(Resolve-Path 'data\defect_synth').Path
.\venv\Scripts\python.exe -m flask --app app run --no-debugger --no-reload -p 5010
```

访问：

- 标注页：`http://127.0.0.1:5010/annotation`
- 原质量看板：`http://127.0.0.1:5010/dashboard`

标注状态流为 `未标注 -> 已标注 -> 已复核`。已复核记录不可直接覆盖或重复复核，
接口返回 HTTP 409。

## 传统 CV 检测与评测

```powershell
.\venv\Scripts\python.exe evaluate.py `
  --dataset data\defect_synth `
  --output data\defect_synth\evaluation_report.md `
  --iou-threshold 0.5
```

检测器使用灰度化、局部高斯背景估计、残差阈值、形态学闭运算、8 邻域连通域和
可解释几何分类，不包含深度学习或模型训练。当前样例合成集结果为：

- TP=65，FP=1，FN=1
- Precision=0.9848，Recall=0.9848，F1=0.9848

这些数字只描述当前合成生成器上的流程结果。

## 角色权限与 AI 助手（P2-8）

在原有质量数据链路之上补充「**角色权限 + AI 助手**」，把系统从"能看数"推进到"能问数"。

### 三种角色

| 角色 | 账号（演示） | 能力 |
|---|---|---|
| 管理员 | `admin` / `admin123` | 查看 · 录入与修改 · 复核 · **用户管理** |
| 质检员 | `inspector` / `inspector123` | 查看 · 录入与修改 · 复核 |
| 只读 | `viewer` / `viewer123` | 仅查看（含向 AI 助手提问） |

> 登录后签发 **token**（itsdangerous 签名，8 小时有效），业务接口通过
> `Authorization: Bearer <token>` 鉴权；令牌失效、账号停用都会立即返回 401。
> 演示账号在首次启动时自动创建；**生产环境请立即改密并设置 `XM02_SECRET_KEY`**。

### 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 登录，返回 token / 角色 / 权限清单 |
| GET | `/api/auth/me` | 登录 | 当前身份 |
| GET | `/api/auth/users` | `user:manage` | 账号清单（仅管理员） |
| POST | `/api/assistant/ask` | `data:view` | **AI 助手问答**（查真实数据后作答） |
| POST | `/api/issue` · `POST /api/issue/<id>/advance` | `data:write` | 异常录入与推进 |
| POST | `/api/annotation/image/<id>` | `data:write` | 保存标注 |
| POST | `/api/annotation/image/<id>/review` | `data:review` | 复核 |

### AI 助手怎么工作

LangGraph 编排「选工具 → 查真实数据库 → 生成回答」的循环，工具集见
`assistant_tools.py`（整体良率、批次/产品良率、不良分布、趋势、异常清单、
标注进度、检验明细），**全部复用 `metrics.py` 的口径** ——
助手说的数字与看板上的数字来自同一套统计逻辑，不编造数据。

三种运行模式：

| 模式 | 触发条件 | 行为 |
|---|---|---|
| `llm` | 配置了 `DEEPSEEK_API_KEY` | 大模型 function calling 选工具 + 生成回答 |
| `offline` | 未配置密钥 | 本地关键词路由 + 数据摘要（**零 API 消耗**） |
| `offline_fallback` | 模型超时/报错 | 自动降级为数据直查，**接口仍返回 200** |

### 启动（带角色权限与助手）

```powershell
$env:XM02_SECRET_KEY="请替换为你自己的随机串"          # 未设置则使用开发默认值
$env:QUALITY_DATABASE_PATH=(Resolve-Path 'quality.db').Path
.\venv\Scripts\python.exe -m flask --app app run --no-debugger --no-reload -p 5010
```

访问：

- 登录页：`http://127.0.0.1:5010/login`
- AI 助手：`http://127.0.0.1:5010/assistant`
- 质量看板：`http://127.0.0.1:5010/dashboard`
- 图像标注：`http://127.0.0.1:5010/annotation`

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest tests -p no:warnings      # 45 passed
.\venv\Scripts\python.exe smoke_assistant.py                  # 真实启动冒烟 15/15
```

`smoke_assistant.py` 会复制一份数据库副本、真实启动服务并打接口，校验
「未登录 401 / 只读越权 403 / 质检员可写 / 助手返回真实数字 / 真实库哈希不变」。

测试使用临时数据库，不读写 `quality.db`；合成器、检测与助手用例全部离线执行，
助手的大模型调用在测试中被替换为替身，**不消耗真实 API 额度**。

## 主要局限

合成缺陷具有规则形状、均匀背景和受控噪声。真实工业图像还会出现复杂划痕、材质
纹理、反光、光照变化、遮挡、样本不均衡和域差异。因此本模块不能证明产线缺陷
检测准确率；真实场景需要现场数据治理、标注一致性、误报漏报成本评估和长期验证。
