<p align="center">
  <img src="../assets/logo.svg" alt="keepr Logo" width="160">
</p>

<p align="center">
  <strong>一个隐私优先、本地优先的文档 RAG 助手——你的文档、你的设备、你的答案。</strong>
</p>

<p align="center">
  <a href="../README.md">English</a> •
  <a href="README.es.md">Spanish</a> •
  <a href="README.zh.md">简体中文</a> •
  <a href="README.ru.md">Русский</a> •
  <a href="README.fr.md">Français</a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/stars/pironc/keepr?style=social" alt="GitHub stars" data-canonical-src="https://img.shields.io/github/stars/pironc/keepr?style=social" style="max-width: 100%;"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/badge/100%25-Local-FF6600" alt="100% Local"></a>
  <a href="https://github.com/pironc/keepr"><img src="https://img.shields.io/github/issues/pironc/keepr?style=flat-square&color=blue" alt="Issues"></a>
</p>

<p align="center">
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-aarch64.dmg"><img src="https://img.shields.io/badge/OSX_ARM-FF6600?logo=apple&logoColor=white" alt="macOS Apple Silicon"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-mac-x86_64.dmg"><img src="https://img.shields.io/badge/OSX_x86-FF6600?logo=apple&logoColor=white" alt="macOS Intel"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-linux-x86_64.AppImage"><img src="https://img.shields.io/badge/Linux-FF6600?logo=linux&logoColor=white" alt="Linux"></a>
  <a href="https://github.com/pironc/keepr/releases/latest/download/keepr-windows-x86_64-setup.exe"><img src="https://img.shields.io/badge/Windows-FF6600?logo=data:image/svg%2Bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4OCA4OCIgZmlsbD0iI2ZmZmZmZiI+CiAgPHBhdGggZD0iTTAgMTIuNDAybDM1LjY4Ny00Ljg2LjAxNiAzNC40MjMtMzUuNjcuMjAzeiIvPgogIDxwYXRoIGQ9Ik0zNS42NyA0NS45MzFsLjAyOCAzNC40NTNMLjAyOCA3NS40OC4wMjYgNDUuN3oiLz4KICA8cGF0aCBkPSJNNDAuMDAyIDcuMzc3TDg3LjMxNCAwdjQxLjUyN2wtNDcuMzE4LjM3NnoiLz4KICA8cGF0aCBkPSJNODcuMzMxIDQ1LjkwNmwtLjAxMSA0MS4zNC00Ny4zMTgtNi42NzgtLjA2Ni0zNC43Mzl6Ii8+Cjwvc3ZnPgo=" alt="Windows"></a>
</p>

# keepr

**keepr** 是一个完全在你自己的设备上运行的检索增强生成（RAG）聊天应用。拖入 PDF 或文本文件，提出问题，获取基于——且仅基于——你上传内容的答案，并附带指向确切原文段落的内联引用。无需云 API，数据不会离开你的设备，模型下载完成后即可在完全断网状态下正常工作。

<p align="center">
  <img src="../assets/keepr.jpg" alt="keepr — 隐私优先的文档 RAG 助手" width="820">
</p>

## 目录

- [为什么选择 keepr？](#为什么选择-keepr)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
  - [Docker](#docker)
  - [本地开发](#本地开发)
  - [环境变量](#环境变量)
  - [使用真实本地模型运行](#使用真实本地模型运行)
  - [原生桌面应用（macOS）](#原生桌面应用macos)
- [开发](#开发)
  - [项目结构](#项目结构)
  - [命令](#命令)
  - [测试](#测试)
  - [故障排除](#故障排除)
- [许可证](#许可证)

每个技术选择的理由、完整的系统设计、数据传输格式以及扩展方案，见 **[ARCHITECTURE.md](../ARCHITECTURE.md)**（英文）。

## 为什么选择 keepr？

大多数 RAG 工具要么将你的文档发送到云 API（隐私风险），要么封装一个黑盒本地服务器如 Ollama（内部不透明）。keepr 走了第三条路：**每一层都是手写、有文档记录、由你完全拥有**——从向量索引数学到 LLM 分词流。核心理念是：个人规模的 RAG 系统不需要分布式基础设施；它需要的是清晰、正确、你可以端到端阅读和理解的代码。

- **你的数据永远不会离开你的设备。** 无遥测，无云 API 密钥，推理期间无网络调用。
- **每个技术选择都有文档记录的、具体的理由**——不是"因为它流行"。完整决策理由见 [ARCHITECTURE.md](../ARCHITECTURE.md)。
- **反幻觉机制是确定性的**（针对相似度阈值的浮点数比较），而不是让一个 7-8B 模型自我审查的提示词。
- **设计为从笔记本扩展到专用服务器而无需重写**——每一层都位于命名接口之后，目前各有一个具体实现，需要时随时可添加第二个。

## 功能特性

### 🔒 隐私与离线运行
- **默认完全本地。** `LLM_DRIVER=mock` / `EMBEDDER=mock`（默认值）在零模型下载的情况下运行完整的摄入→检索→引用管线——可在毫秒内完成测试。
- **真实的本地模型**通过 `llama-cpp-python`（GGUF 格式）在 Metal（Apple Silicon）、CUDA 或 CPU 上运行。
- **设计上即支持离线。** 前端是手写原生 HTML/CSS/JS，零外部依赖——无 CDN script 标签，无 npm 构建步骤。字体以本地 `.woff2` 文件形式内置；图标为手写/本地内置为 `.svg` 文件（无图标 CDN）。
- **离线能力已验证。** 整个测试套件在 `pytest-socket` 阻止所有真实网络访问的情况下运行，包含一个正向控制测试来证明该阻止确实生效。

### 📄 基于聊天的 RAG 与确定性反幻觉
- 每个答案都由该对话中文档检索到的文本块支撑——不是全局索引，不是模型的预训练数据。
- **LLM 调用前拒绝：** 如果没有检索到文本块的余弦相似度超过阈值，keepr 会在调用 LLM *之前*就拒绝——这是一个确定性的 `float` 比较，而不是交给模型的主观判断。
- **生成后引用验证：** LLM 流式输出答案后，每个 `[chunk_N]` 引用标签都会与该轮实际检索到的文本块 ID 集合进行比对。模型无法伪造一个从未出现在它面前的文档引用——这是通过集合成员关系强制执行的，而不是靠信任模型的输出。
- **逐块独立阈值判定：** 每个检索到的文本块都单独与相似度门槛比较，而不仅仅取最佳结果——避免一个强匹配将几个勉强相关的文本块拖入上下文。

### 🗂️ 按对话的检索范围
- 每个对话拥有独立的检索范围，如同 Claude Project 或 NotebookLM 笔记本。
- 侧边栏允许你在对话之间切换、按标题搜索、置顶收藏，以及通过右键上下文菜单进行内联重命名或删除。
- 文档通过内容哈希（SHA-256）去重——重复添加同一文件是无操作，不会静默地使每个文本块翻倍。

### ⚡ 实时摄入管线
- 文件在拖入时暂存，在你按下发送的瞬间开始处理：**提取→分块→嵌入→索引**。
- 每个文件的状态动画（`等待中→提取中→分块中→嵌入中→已索引`）由真实后端 SSE 事件驱动，而非虚假的定时器。
- 每种文件类型都经过同一条管线——`Ingestor` 协议使得日后支持音频/视频时无需修改下游任何代码。
- 作为独立的后台任务（`IngestionWorker`）运行，独立于你的 HTTP 连接——关闭标签页或在上传过程中断网都不会暂停或丢失正在进行的提取→分块→嵌入→索引工作；重新打开该对话会实时重新连接到它。

### 🔄 持久化、独立于连接的生成
- 从你按下发送的那一刻起，消息生成就作为**后台任务**（`GenerationWorker`）运行，独立于你的 HTTP 连接，拥有与摄入任务分离的独立队列——嵌入操作只会等待前一个嵌入操作，绝不会等待无关的 LLM 生成。
- 在回答生成过程中刷新页面——回答会继续生成并最终写入数据库。重新加载后可以实时重新连接到它，无论它进行到了哪一步。
- 崩溃恢复：启动时，任何因上次进程异常终止而卡在处理中途的消息或文档都会被标记为错误状态，并保留其已生成的部分内容，而不是永远空转等待。

### ⚙️ 内置模型管理
- 设置菜单会列出 `models/` 中的每个 `.gguf` 文件，仅根据其自身元数据（一个池化层标记）而非文件名，将其分类为 LLM 模型或嵌入模型——无需维护一份硬编码的模型名称列表来应对不断出现的新架构。
- 直接从设置中下载模型（Hugging Face Hub，带实时进度），无需先运行单独的命令行步骤。
- 切换当前使用的 LLM 或嵌入模型会保存该选择并重启应用，以便新模型真正被加载；删除模型文件也以同样的方式提供。

### 🧱 可插拔架构
每一层都位于协议/ABC 之后——无需重构即可替换实现：

| 层 | 接口 | 实现 |
|---|---|---|
| **LLM 驱动** | `LLMDriver` | `MockLLMDriver`（确定性，零下载），`LlamaCppDriver`（真实 GGUF 推理） |
| **嵌入器** | `Embedder` | `MockEmbedder`（哈希技巧词袋），`LlamaCppEmbedder`（nomic-embed-text-v2-moe，多语言） |
| **向量索引** | `VectorIndex` | `NumpyFlatIndex`（float32，精确），`QuantizedNumpyFlatIndex`（int8 标量量化，约 4 倍内存节省） |
| **摄入器** | `Ingestor` | `PdfIngestor`、`TextIngestor`、`AudioVideoIngestor`（干净的占位桩——抛出 `UnsupportedSourceError`） |
| **存储** | `Repository` | 通过 `aiosqlite` 的 SQLite（唯一使用 SQL 的模块——切换到 Postgres 只需修改一个文件） |

### 🖥️ 原生桌面应用
- 一个 [Tauri](https://tauri.app/) v2 封装器将 Python 后端（通过 PyInstaller 编译）打包为原生 macOS `.app` 包和 `.dmg` 安装程序。
- 目标机器上无需安装 Python——后端是嵌入在应用内的独立可执行文件。
- 同一代码库既可以作为 Web 应用运行（`make run` 用于开发时的热重载），也可以作为原生桌面应用运行（`make tauri-build` 用于分发）。

---

## 快速开始

### Docker

获取运行实例的最快方式——无需配置 Python 环境：

**前置条件：** [Docker](https://docs.docker.com/get-docker/) 及 Compose v2。

```bash
git clone https://github.com/pironc/keepr.git
cd keepr
docker compose up --build
```

打开 [http://localhost:8000](http://localhost:8000)。将 PDF 或 `.txt` 文件拖入聊天并提问。Compose 文件默认使用 `LLM_DRIVER=mock` / `EMBEDDER=mock`——整个管线在零模型下载的情况下运行。

如需真实的本地推理，挂载你的模型并切换驱动：

```bash
# 首先，下载 GGUF 模型到 models/（一次性操作）：
python scripts/download_models.py

# 然后覆盖 Compose 环境变量：
LLM_DRIVER=llama_cpp EMBEDDER=llama_cpp docker compose up --build
```

### 本地开发

原生运行并支持热重载——无需 Docker：

**前置条件：** Python 3.12+。

```bash
git clone https://github.com/pironc/keepr.git
cd keepr

python3 -m venv .venv && source .venv/bin/activate
make install
make run
```

打开 [http://localhost:8000](http://localhost:8000)。默认值（`LLM_DRIVER=mock`、`EMBEDDER=mock`）无需任何下载——整个摄入→检索→引用管线立即可测试。

### 环境变量

将 `.env.example` 复制为 `.env` 来自定义配置。关键变量：

| 变量 | 用途 |
|---|---|
| `LLM_DRIVER` | `mock` 或 `llama_cpp`。留空则自动检测：一旦真的存在 GGUF 文件（例如在设置中下载/选择过）就使用 `llama_cpp`，否则使用 `mock`——全新安装时零下载，一旦模型存在就无需设置该变量。 |
| `LLM_MODEL_PATH` | GGUF 文件路径。仅当 `LLM_DRIVER=llama_cpp` 时使用。留空则使用“设置”菜单中选定的模型。 |
| `LLM_CONTEXT_WINDOW` | 上下文窗口大小（默认根据 `MEMORY_TIER` 自动检测，通常为 `8192`）。 |
| `LLM_GPU_LAYERS` | 卸载到 GPU 的层数（`-1` = 全部，默认）。 |
| `EMBEDDER` | `mock` 或 `llama_cpp`。与 `LLM_DRIVER` 相同的自动检测逻辑，二者独立判断。 |
| `EMBEDDING_MODEL_PATH` | GGUF 嵌入模型路径。仅当 `EMBEDDER=llama_cpp` 时使用。留空则使用“设置”菜单中选定的模型。 |
| `EMBEDDING_GPU_LAYERS` | 嵌入 GPU 层数（默认 `0`——嵌入在 CPU 上运行，以避免与 LLM 争抢 GPU）。 |
| `VECTOR_INDEX_BACKEND` | `flat`（float32，精确）或 `quantized`（int8 标量量化，约 4 倍内存节省）。 |
| `RETRIEVAL_TOP_K` | 每次查询检索的文本块数量（默认 `5`）。 |
| `RETRIEVAL_MIN_SIMILARITY` | 余弦相似度阈值，低于此值引擎拒绝回答（默认 `0.22`——针对 nomic-embed-text-v2-moe 校准）。 |
| `MODELS_DIR` | 扫描 `.gguf` 模型文件的目录（默认 `models`）。 |
| `DATABASE_PATH` | SQLite 数据库路径（默认 `data/keepr.db`）。 |
| `INDEX_DIR` | 按对话的向量索引目录（默认 `data/index`）。 |
| `UPLOAD_DIR` | 上传文件存储目录（默认 `data/uploads`）。 |
| `MEMORY_TIER` | 覆盖自动检测的内存预算（`minimal`、`standard`、`large`、`server`）。不设置则以自动检测为准。 |
| `CHUNK_SIZE` | 每个文本块的字符数（默认 `800`）。 |
| `CHUNK_OVERLAP` | 相邻文本块之间的重叠字符数（默认 `150`）。 |
| `LOG_LEVEL` | 日志级别（默认 `INFO`）。 |

### 使用真实本地模型运行

安装 `llama_cpp` 驱动——一个单独的、更重的可选依赖（从源码编译 `llama-cpp-python`；在 Apple Silicon 上会自动启用 Metal 加速，无需额外参数）：

```bash
pip install -e ".[llama]"
```

然后可以从应用的**设置**菜单获取 Qwen3-8B + nomic-embed-text-v2-moe GGUF 模型对（总计约 7.5 GB），也可以通过命令行：

```bash
python scripts/download_models.py
```

然后在 `.env` 中：

```env
LLM_DRIVER=llama_cpp
EMBEDDER=llama_cpp
```

重启 `make run`。第一条消息会较慢（模型加载到内存中）；之后的一切都在驻留的热堆栈上运行。

如果想使用不同的模型，将任何 llama.cpp 兼容的 GGUF 文件放入 `models/`，然后在**设置**菜单中选它——选择会被保存并在下次重启时生效。你也可以直接设置 `LLM_MODEL_PATH` / `EMBEDDING_MODEL_PATH` 来覆盖它。（嵌入模型不能随意更换：`RETRIEVAL_MIN_SIMILARITY` 是针对 nomic-embed-text-v2-moe 校准的，更换意味着需要重新校准该阈值。）

### 原生桌面应用（macOS）

```bash
# 开发模式——打开一个指向 http://localhost:8000 的原生窗口。
# Python 后端必须已在运行（在另一个终端中执行 `make run`）。
make tauri-dev

# 生产构建——将 Python 后端编译为独立可执行文件，
# 打包进 .app，并生成 .dmg 安装程序。
make tauri-build
```

编译后的 `.app` 在目标机器上无需安装 Python——后端是嵌入在包内的自包含 PyInstaller 可执行文件。

---

## 开发

### 项目结构

```
keepr/
├── src/
│   ├── models.py              # Pydantic v2 模型——单一真相来源
│   ├── config.py              # 硬件检测、内存层级、设置
│   ├── concurrency.py         # LockedEmbedder / LockedLLMDriver 封装器
│   ├── logger.py              # 结构化日志
│   ├── download.py            # 模型下载辅助（Hugging Face Hub，与 CLI 共用）
│   ├── gguf_meta.py           # 轻量级 GGUF 元数据分类器（pooling 检测）
│   ├── model_unavailable.py   # ModelUnavailableError —— 按角色区分的缺失/损坏 GGUF 文件
│   ├── api/
│   │   ├── app.py             # FastAPI 应用工厂 + 生命周期
│   │   ├── context.py         # AppContext + 依赖注入
│   │   ├── routes_conversations.py
│   │   ├── routes_messages.py # 多路复用 SSE 端点
│   │   ├── routes_models.py   # 模型状态/选择/下载端点
│   │   └── sse.py             # SSE 格式化辅助函数
│   ├── db/
│   │   ├── pool.py            # SQLiteConnectionPool
│   │   ├── repository.py      # 唯一使用 SQL 的模块
│   │   └── schema.py          # CREATE TABLE 语句
│   ├── embeddings/
│   │   ├── base.py            # Embedder 协议
│   │   ├── mock_embedder.py   # 确定性哈希技巧嵌入器
│   │   ├── llama_cpp_embedder.py  # 真实 GGUF 嵌入
│   │   └── factory.py
│   ├── ingestion/
│   │   ├── base.py            # Ingestor 协议
│   │   ├── pipeline.py        # 编排 提取→分块→嵌入→索引
│   │   ├── worker.py          # 后台任务——持久化、独立于连接
│   │   ├── chunker.py         # 带重叠的文本分块
│   │   ├── registry.py        # 为文件找到合适的摄入器
│   │   ├── pdf_ingestor.py
│   │   ├── text_ingestor.py
│   │   └── audio_video_ingestor.py  # 干净的占位桩——抛出 UnsupportedSourceError
│   ├── llm/
│   │   ├── base.py            # LLMDriver ABC（异步 token 流）
│   │   ├── mock_driver.py     # 用于测试的确定性模拟驱动
│   │   ├── llama_cpp_driver.py    # 真实 GGUF 推理，带思考内容剥离
│   │   └── factory.py
│   ├── rag/
│   │   ├── engine.py          # 核心 RAG：检索 + 阈值 + 生成 + 引用验证
│   │   ├── prompts.py         # 基于依据的系统提示词
│   │   ├── generation_worker.py   # 后台任务——持久化，独立于连接
│   │   ├── index_manager.py   # 按对话的向量索引缓存
│   │   ├── greeting.py        # 快速路径问候/告别检测
│   │   └── title.py           # LLM 生成对话标题
│   ├── vectorstore/
│   │   ├── base.py            # VectorIndex 协议
│   │   ├── flat_index.py      # NumpyFlatIndex（float32，精确）
│   │   ├── quantized_flat_index.py  # int8 标量量化
│   │   ├── similarity.py      # L2 归一化
│   │   └── factory.py
│   └── web/                   # 原生 HTML/CSS/JS——零外部依赖
│       ├── templates/index.html
│       └── static/
│           ├── app.js
│           ├── app.css
│           └── fonts/         # 内置 .woff2 文件（无 Google Fonts CDN）
├── src-tauri/                 # Tauri v2 原生桌面封装器（Rust）
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── src/lib.rs
│   └── binaries/              # PyInstaller 编译的后端可执行文件
├── tests/                     # pytest + pytest-asyncio + pytest-socket
├── models/                    # 已下载的 .gguf 模型权重（MODELS_DIR）
├── scripts/
│   └── download_models.py     # 一次性 GGUF 模型下载器（免测试的网络脚本）
├── assets/                    # Logo、截图
│   └── icons/                 # 下载按钮使用的白色品牌图标
├── ARCHITECTURE.md            # 完整技术设计与扩展方案
├── CLAUDE.md                  # AI 助手 / 贡献者指南
├── Makefile
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

### 命令

```bash
make install          # pip install -e ".[dev]"
make run              # uvicorn src.api.app:app --reload --port 8000
make test             # pytest
make lint             # ruff check .
make typecheck        # mypy --strict
make ci               # lint + typecheck + test——在认为完成之前应运行此命令
make clean            # 清除数据库/索引/上传文件（应用状态）——不包括模型权重
make clean-models     # 同时清除 models/
make wipe             # 完全重置到刚克隆时的状态（venv、缓存、
                       # 构建产物、src-tauri/target 等）——models/ 仍然保留不动
```

### 测试

```bash
make ci   # ruff check . && mypy && pytest
```

测试套件在全局禁用网络访问（`pytest-socket`）的情况下运行——任何打开真实网络套接字的测试都会导致整个套件失败。模拟驱动和嵌入器使测试快速且具有确定性（无需模型下载，无不可靠的 API 调用）。`asyncio_mode = "auto"` 意味着 `async def test_...` 直接可用。

关键测试文件：
- `tests/test_rag_engine.py`——基于阈值的拒绝、引用验证（包括一个刻意伪造引用的驱动）
- `tests/test_generation_worker.py`——独立于连接的生成、崩溃恢复、观察者重新连接
- `tests/test_ingestion_worker.py`——独立于连接的文档摄入、崩溃恢复、空闲卸载的时序
- `tests/test_ingestion_pipeline.py`——内容哈希去重、状态转换
- `tests/test_vectorstore.py`——量化内存比率及 top-1 一致性基准
- `tests/test_airgapped.py`——正向控制，证明套接字阻止确实生效
- `tests/test_llama_cpp_driver.py`——跨分割标签的思考内容剥离，无需真实模型

### 故障排除

**答案似乎与我的文档无关**

检索相似度可能低于阈值——检查服务器日志中的实际余弦相似度分数，并与 `RETRIEVAL_MIN_SIMILARITY` 对比。如果你使用的嵌入器不是 nomic-embed-text-v2-moe，请重新校准阈值（参见 `ARCHITECTURE.md`）。

**模拟驱动给出通用回复**

模拟驱动从系统提示词中解析 `[chunk_N]` 标签以生成确定性响应。如果没有检索到文本块（空索引），它会拒绝回答。请先添加文档。

**`make run` 因导入错误失败**

先运行 `make install`——该包需要以可编辑模式安装。

---

## 许可证

keepr 基于 [MIT 许可证](../LICENSE) 授权。

🤖 由 [Claude Code](https://claude.com/claude-code) 生成
