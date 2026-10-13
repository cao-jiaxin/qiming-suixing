# 启明随行 · 源代码说明与原创声明

> 2026 年华北五省（市、自治区）及港澳台大学生计算机应用大赛 · 方向一
> 本文件是提交源码的**入口说明**，同时应答大赛关于「源代码须标明原创部分」的要求。

---

## 一、这是什么

「启明随行」是一套面向**视障用户**的室内出行辅助系统。用户举起手机拍一下眼前，
系统用语音回答：正前方有什么、离你还有几步、接下来怎么走。

- **形态**：H5 网页应用（无需安装，浏览器打开即可用）
- **后端**：Python 3 + FastAPI，单进程服务，13 条 API
- **前端**：单文件 `index.html`（HTML5 + CSS3 + 原生 JavaScript，无框架、无构建步骤）

---

## 二、目录结构

```
code/
├── backend/                    FastAPI 服务端
│   ├── main.py                 服务入口 · 13 条 API · 静态挂载 · 访问口令闸门
│   ├── requirements.txt        依赖清单（含"可选依赖缺失自动降级"说明）
│   ├── .env.example            ★ 配置模板（空 Key，照它建 .env 即可跑）
│   ├── smoke_test.py           166 项端到端接口断言（无需 API Key）
│   ├── check_model.py          模型链路自检（沿三级降级链逐级试）
│   ├── check_secret.py         提交前密钥扫描
│   ├── start_demo.cmd          双击启动（示例模式）
│   ├── start_lan.cmd           双击启动（局域网访问）
│   ├── allow_firewall.cmd      放行防火墙入站
│   └── core/
│       ├── config.py           配置单例（模型清单的唯一数据源）
│       ├── providers.py        视觉 / OCR / ASR 接入 + 三级降级重试
│       ├── pipeline.py         主编排 + build_speech() 播报治理
│       ├── prompts.py          ★ 提示词（算法资产）
│       ├── depth.py            ★ 距离双后端 + 时钟方位映射
│       ├── sop.py              ★ 场景流程状态机
│       └── helpdesk.py         呼叫人工
├── frontend/
│   ├── index.html              ★ 单文件 H5 主界面
│   ├── test_a11y.js            228 项无障碍交互回归（jsdom，无需浏览器）
│   ├── shot_probe.js           13 组取景质检回归
│   ├── wav_probe.js            8 组录音转 WAV 回归
│   ├── preview_speech.js       打印"用户实际听到的每一句"
│   └── voice_check.html        语音可用性自检页
├── contrast_audit.py           26 组配色 WCAG 对比度审计
└── make_tables.py              生成三张项目管理表（设计文档第三、四章数据源）
```

---

## 三、怎么跑起来

```bash
# 1. 装依赖（基础依赖装完即可在示例模式端到端跑通）
cd code/backend
pip install -r requirements.txt

# 2. 建配置
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
#   → 不填 Key 也能跑（示例模式）；填上 Key 并把 MOCK_MODE 改为 false 即为真实模型

# 3. 启动
python main.py                # 浏览器打开 http://127.0.0.1:8000

# 4. 自检（可选）
python smoke_test.py          # 应输出 166/166 通过
python check_model.py         # 检查模型链路是否真的通
python check_secret.py        # 提交前密钥扫描
```

---

## 四、所用的第三方大模型清单

| 序号 | 用途 | 模型名称 | 版本号 | 提供方 | 使用方式 |
|:--:|---|---|---|---|---|
| 1 | 视觉理解（主力） | GLM-4.6V-Flash | glm-4.6v-flash | 智谱 AI（BigModel 开放平台） | API 调用 |
| 2 | 视觉理解（一级降级） | GLM-4.1V-Thinking-Flash | glm-4.1v-thinking-flash | 智谱 AI | API 调用 |
| 3 | 视觉理解（二级降级/兜底） | GLM-4V-Flash | glm-4v-flash | 智谱 AI | API 调用 |
| 4 | 视觉理解（可选第四通道） | qwen3-vl-flash | 2026-03 | 阿里云百炼（另行配置 Key 时启用） | API 调用 |
| 5 | 文本推理·意图识别 | GLM-4.7-Flash | glm-4.7-flash-2026-06 | 智谱 AI | API 调用 |
| 6 | 语音识别（ASR） | paraformer-zh | FunASR v1.1 | 阿里 FunASR（开源） | 本地部署 |
| 7 | 语音合成（TTS） | CosyVoice2-0.5B | CosyVoice2 v2.0 | 阿里 CosyVoice（开源） | 本地部署 |
| 8 | 文字识别（OCR） | PP-OCRv5 | PaddleOCR 3.x | 百度飞桨（开源） | 本地部署 |
| 9 | 单目深度估计 | Depth-Anything-V2-Small | vits-2024 | Depth-Anything（开源） | 本地部署 |

> **数据源**：`code/backend/core/config.py` 是本清单的唯一数据源，由 `model_manifest()` 生成。
> 服务起来后访问 `GET /api/meta` 可**实时核对**本表，文档口径与代码严格一致。

---

## 五、原创声明

### 5.1 属于本团队原创设计与实现的成果

| 原创点 | 对应文件 | 说明 |
|---|---|---|
| 场景 SOP 状态机与流程数据结构 | `code/backend/core/sop.py` | 流程写死为数据，模型仅做节点达成判定，杜绝流程编造 |
| 导盲式播报提示词与三段式输出规范 | `code/backend/core/prompts.py` | 环境描述 / 障碍提示 / 行动建议的分段结构 |
| 深度→步数→时钟方位的结构化注入机制 | `core/prompts.py: build_distance_block()`、`core/pipeline.py` | 距离只能引用度量结果，抑制距离幻觉 |
| 三链路编排与并行/降级策略 | `code/backend/core/pipeline.py` | OCR 与深度并行、主备模型自动降级、填充语音消除等待感 |
| 语音优先的无障碍交互前端 | `code/frontend/index.html` | 大按钮 / 大字号 / 可打断播报 / 免视觉操作 |
| 播报额度分段保底算法 | `core/pipeline.py: build_speech()` | 优先给「建议」「前方情况」留额度，防长句吃光预算 |
| 盲用评测指标与评测集设计 | `eval/` | 方位正确率、距离步数误差、拒答正确率、幻觉率 |
| 实践验证方案与数据分析 | `docs/`、`results/` | 真实试用 / 蒙眼模拟（如实标注） |

### 5.2 属于第三方模型/工具生成的内容（本团队不主张原创）

- 场景画面的自然语言描述文本（由上述视觉理解模型生成）
- 语音识别文本、语音合成音频
- OCR 识别结果、深度估计结果
- 部分后端脚手架代码与文档初稿（由 AI 编程工具起草）

### 5.3 AI 未参与的部分

- 需求调研与用户访谈记录（团队实地走访、问卷原始记录）
- 真实 / 模拟测验的原始数据与计时记录
- 项目选题、方案架构、交互流程的**最终决策**
- 所有涉及安全性与伦理边界的判断

### 5.4 AI 编写代码所占比例

统计口径：**有效代码行**（非空且非纯注释行）为分母；**AI 起草且未经实质性改写**的行数为分子。
经人工重构或重写逻辑的代码**不计入**分子。

| 模块 | 总行数 | AI 起草保留占比 | 备注 |
|---|---:|---:|---|
| 后端 `code/backend` | 3,680 | 44% | 核心链路已人工重构 |
| 其中 `core/sop.py` | 183 | 10% | 状态机为人工设计，AI 仅辅助填充样板代码 |
| 其中 `core/prompts.py` | 127 | 0% | 提示词文本为人工撰写与迭代 |
| 其中 `core/pipeline.py` | 390 | 30% | 三链路编排与降级策略为人工设计 |
| 其中 `core/depth.py` | 274 | 25% | 距离双后端与时钟方位映射为人工设计 |
| 前端 `code/frontend` | 5,821 | 81% | 页面骨架与样板由 AI 起草，交互逻辑经人工调整 |
| 其中 `index.html` | 2,916 | 80% | 无障碍交互的可用性取舍由人工决策 |
| 其中 `test_a11y.js` | 2,099 | 85% | 用例模板由 AI 起草，断言逐条人工设计 |
| 审计与工具 `code/*.py` | 366 | 42% | 审计判据为人工设计 |
| 评测 `eval/` | 479 | 50% | 评测指标为人工设计 |
| **整体** | **9,867** | **约 65% ~ 75%** | 中心值 66.2% |

> 复算方式：`python tools/_ai_ratio.py`（各文件占比为逐文件人工判定，口径与
> 《AI 使用记录》§3.1 一致）。统计方法见该文件，逐文件明细可随时重算。

---

## 六、第三方依赖与开源许可

### 6.1 Python 包（见 `code/backend/requirements.txt`）

| 包 | 版本 | 许可 |
|---|---|---|
| fastapi | 0.115.6 | MIT |
| uvicorn | 0.34.0 | BSD-3-Clause |
| python-dotenv | 1.0.1 | BSD-3-Clause |
| requests | 2.32.3 | Apache-2.0 |
| Pillow | 11.0.0 | HPND |
| python-multipart | 0.0.20 | Apache-2.0 |
| numpy | ≥2.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |

### 6.2 可选依赖（缺失时对应模块自动降级，不影响服务启动）

| 包 | 用途 | 许可 |
|---|---|---|
| onnxruntime | 深度估计 ONNX 推理（F03 生产路径） | MIT |
| paddleocr / paddlepaddle | 中文小字 OCR（F06/F07） | Apache-2.0 |
| funasr / torch | 语音识别与合成（F05/F08） | MIT / BSD-3-Clause |

### 6.3 开源模型

FunASR、CosyVoice2、PaddleOCR、Depth-Anything-V2 均**遵循其原始许可证**使用。
相关许可见各项目仓库；本项目未对其源码做任何再分发。

### 6.4 前端

`code/frontend/index.html` **不引入任何第三方 JavaScript 库**，
所有交互逻辑与样式均为项目内实现，字体使用系统字体栈。

> 若使用任何第三方素材（图标 / 字体 / 图片），均已确认授权或替换为可商用素材。

---

## 七、提交包内未包含的内容及理由

| 未包含 | 理由 |
|---|---|
| `code/backend/.env` | 含真实 API Key —— 交出去即等于 Key 公开。**已在 `.env.example` 提供空模板** |
| `code/deploy_public/` | 与 `backend/` + `frontend/` 重复的部署副本，避免评委分不清哪份是源码 |
| `__pycache__/`、`*.pyc` | Python 编译产物 |
| `code/backend/models/*.onnx` | 深度模型权重体积大，且 `requirements.txt` 已说明获取方式；缺失时自动降级为几何启发式后端 |
| `cache/`、`results/*_demo.*` | 过程产物与合成演示数据 |

---

## 八、复现与验证

```bash
# 后端接口（166 项断言，无需 API Key）
cd code/backend && python smoke_test.py

# 前端无障碍交互回归（228 项，需 jsdom）
npm install jsdom
NODE_PATH=./node_modules node ../frontend/test_a11y.js

# 配色对比度审计（26 组配对色，不达标以非 0 退出码结束）
python ../contrast_audit.py
```
