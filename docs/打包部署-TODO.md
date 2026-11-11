# Python 项目打包 / 交付 TODO

> 状态：**规划中，未实施**
> 记录日期：2026-11-11
> 背景：目前交付方式 = 容器镜像 + compose 挂载代码目录（见 `mcp-sidecar/docker-compose.yml`）。
> 后续可能需要把 `agentic-app` 这个 Python 项目打成"不依赖源码目录"的交付物。

---

## 1. 先明确三个前置问题（不回答就没法定方案）

| # | 问题 | 影响 |
|---|---|---|
| Q1 | 目标机器**有没有 Docker**？ | 没有就得走 venv/tar 或单文件可执行，不能依赖镜像 |
| Q2 | 目标机器**能不能联网**（pip / HuggingFace）？ | 离线就必须把所有 wheel 和模型权重一起带过去 |
| Q3 | 交付物里**要不要带真实密钥/地址**？ | 带配置=开箱即用但有泄密风险；不带=现场必须改 `.env` |

现状参考：代码里读 `OPENAI_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `INTENT_MODEL_PATH` /
`SCENE_MCP_URL` 等环境变量，密钥与地址都能外置，适合"包 + `.env`"分离交付。

---

## 2. 候选方案对比

| 方案 | 产物 | 优点 | 缺点 / 风险 |
|---|---|---|---|
| **A. 继续用镜像**（现状） | `*.tar` 镜像 | 已验证可用；环境与代码都固定；跨机器最稳 | 目标机要有 Docker；镜像大（2.6–4.4 GB） |
| **B. wheel + 私有源** | `*.whl` | 标准做法，`pip install` 后 `python -m app` 即可 | 现场要能装依赖；`requirements.txt` 版本钉得很死，得产物化 |
| **C. zipapp / `.pyz`** | 单文件 | 无需安装、`python app.pyz` 直接跑 | 二进制依赖（torch/sentence-transformers）无法塞进 pyz，只能把它们留给目标环境 |
| **D. PyInstaller / Nuitka** | 单可执行（目录） | 看起来最"产品化" | `torch` + `sentence-transformers` 体积大、hook 多，打包成功率低；建议先做 POC 再决定 |
| **E. 离线 tar（venv + 代码 + 模型）** | `agentic-app-offline.tar.gz` | 现场解压即跑，不需要 pip；不需要 Docker | 需要**同 OS / 同架构 / 同 Python 版本**（建议 python 3.11，与现镜像一致） |

**推荐顺序**：Q1 有 Docker → 保持 A（已跑通，成本最低）；Q1 无 Docker 但同构机器 → E；要求"单文件/免安装" → 先 POC D，不行退 C+E 混合。

---

## 3. 待办清单

### 3.1 决策
- [ ] 回答 Q1–Q3，选定方案（A/B/C/D/E）
- [ ] 明确目标 OS 与架构（Windows / Linux，x86_64 还是 arm64），以及 Python 版本

### 3.2 打包内容梳理
- [ ] 代码：`app.py`、`agent/`、`graph/`、`tools/`、`models/`、`mcp_client/`、`utils/`、`db/`、`memory/`、`web/`、`static/`
- [ ] 依赖：`requirements.txt` 落成可离线安装的形式（`pip download -r requirements.txt -d wheelhouse`）
- [ ] 意图模型：`agent/intent/models/BAAI_bge-small-zh-v1.5`（**92 MB**，是否随包需拍板）
- [ ] 配置：`.env` 模板（真实密钥不进包）+ `global_config.json` 模板
- [ ] 启动脚本：参照 `scripts/docker-start.sh`（校验目录 → 可选等依赖 → `exec uvicorn app:app`）
- [ ] 只读资源：`web/`、`static/`、`uploads/`、`reports/` 的路径要能在打包后定位（现在用 `BASE_DIR` 拼接）

### 3.3 打包脚本
- [ ] 写 `scripts/build-package.sh`（或 `.ps1`）：清理 → 收集 wheel → 打 tar/镜像 → 生成 SHA256
- [ ] 产物命名带版本号（如 `agentic-app-1.5.0-offline.tar.gz`）
- [ ] 在包内附 `README-install.md`：解压、改 `.env`、启动、验证四步

### 3.4 验证（每次打包都要跑）
- [ ] 空目录解压 → 按 README 启动 → `/docs` 可访问
- [ ] 意图分类可用（`INTENT_MODEL_PATH` 指向包内模型，且断网也能加载）
- [ ] 与 `mcp-sidecar` 连通（`/mcp/*` 全部返回正常，`scene` 通道可用）
- [ ] 一次性对话跑通（WS `/chat`），而不是只看服务起来

---

## 4. 已知坑（本仓库实测，打包时必须处理）

1. ~~**`pymysql` 缺失会直接崩**~~ —— **已规避（2026-11-11 定：统一用 `qingyanjiu/langchain:slim`）**。
   slim 自带 pymysql 1.4.6；`langchain:1.0.3` 缺它且不再使用。若将来换基础镜像，仍要验一遍：
   `memory/memory_persistor_mysql.py → db/database.py` 在 import 期就 `create_engine`，
   缺依赖会**启动即崩**（`python -c "import pymysql"` 一句话即可确认）。
2. **意图模型是运行必需项**：`INTENT_MODEL_PATH` 为空会退回 HuggingFace 下载，离线环境表现为
   `'NoneType' object has no attribute 'tokenize'` 启动崩溃，很难往"模型路径"上想。
3. **`global_config.json` 是相对路径读的**（`utils.get_config()` 默认 `./global_config.json`），
   所以启动的工作目录必须是项目根目录，打包后不能用别处当 CWD。
4. **重依赖体积**：`sentence-transformers` + `torch` 是包体积的主因；如果目标只需要意图分类，
   评估是否换成 `onnx` 版模型，能省一大截。
5. **`uploads/` 路径写死为 `/root/agentic-app/uploads`**（容器路径），本机跑会落到盘根，
   打包前应改成可配置项（`UPLOAD_FOLDER` 环境变量）。
6. **Windows 支持尚未验证**：路径、换行、`asyncio` 事件循环策略都需要一次专项验证。

---

## 5. 与其他文档的关系

- 容器化 / compose 的运行方式：`mcp-sidecar/docker-compose.yml` + `mcp-sidecar/deploy/README.md`
- Loop 相关后续项：`docs/loop-agent-TODO.md`
- 意图/MCP 功能进度：`docs/开发计划.md`
