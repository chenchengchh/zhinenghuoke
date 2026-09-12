# 智揽人工智能模型（Huoke Smart Bot）

基于 **RAG + RPA** 的抖音自动化智能客服与获客系统。集成知识库检索增强（RAG）、意图识别、自动回复、消息监听与精准获客能力，并提供可视化 Web 管理台。

> 本仓库为项目核心代码提取版，包含完整可运行的源码、配置、启动脚本与文档。

## 界面预览

| 任务发布（获客主控） | 客户管理 |
|:---:|:---:|
| ![任务发布](docs/images/dashboard.png) | ![客户管理](docs/images/customer.png) |

| 成果展示（意向分析） | 模型训练（知识运营） |
|:---:|:---:|
| ![成果展示](docs/images/analytics.png) | ![模型训练](docs/images/knowledge.png) |

## 功能特性

- **自动登录**：支持扫码/手机号登录，自动保存会话状态
- **关键词搜索获客**：自动搜索视频、爬取评论区，精准定位意向客户
- **自动私信触达**：文本/微信卡片轮流发送，模拟真人操作间隔
- **防风控**：随机延时、反检测脚本、CloakBrowser 持久化上下文直启浏览器
- **RAG 智能客服**：向量检索 + BM25 混合召回 + 交叉编码器重排序
- **意图识别**：BERT 意图分类 + 层级意图 + LLM 意图识别，购买意向打分
- **消息监听自动回复**：实时监听抖音私信，7x24 自动应答
- **知识库管理**：文件上传、语义分块、向量索引、知识图谱、审核与学习闭环
- **企业级组件**：熔断器、消息总线、会话管理、数据分析与报表

详细功能说明见 [docs/功能介绍.md](docs/功能介绍.md)。

## 目录结构

```
GIThuoke/
├── src/                    # 核心源码
│   ├── config/             #   全局配置（settings.py）
│   ├── common/             #   公共业务层（客服、意图、知识库、企业组件等）
│   ├── douyin_bot/         #   抖音 RPA 自动化（浏览器、爬虫、私信、监听）
│   ├── rag/                #   RAG 检索增强（向量库、BM25、重排序、语义分块）
│   ├── agents/             #   智能体（爬取/监控 Agent）
│   ├── web/                #   FastAPI Web 层（API 路由 + 前端模板/静态资源）
│   ├── infrastructure/     #   基础设施（日志、缓存、安全、中间件、健康检查）
│   ├── licensing/          #   授权许可（机器指纹、签名校验）
│   └── remote_control/     #   远程策略控制
├── config/                 # YAML 配置（应用/系统/生产环境/RAG 权重/告警）
├── scripts/                # 维护脚本（向量库重建、质量评估、授权生成等）
├── docs/                   # 文档与截图
│   ├── 功能介绍.md
│   ├── 安装文档.md
│   └── images/
├── run_app.py              # 生产/独立启动入口（自动清理端口与缓存）
├── start_dev.py            # 开发模式启动（uvicorn 热重载 + 自动开浏览器）
├── start.bat               # Windows 一键启动（环境自检）
├── requirements.txt        # Python 依赖清单
├── pyproject.toml          # 项目元数据与工具链配置
├── .env.example            # 环境变量模板（复制为 .env 使用）
└── .gitignore
```

> 运行时自动生成的目录（不入库）：`data/`（业务数据）、`models/`（本地模型）、`logs/`。

## 技术栈

| 层次 | 技术 |
|---|---|
| Web 框架 | FastAPI + Uvicorn + Starlette |
| 前端 | Bootstrap + ECharts + 原生 JavaScript（Jinja2 模板） |
| 浏览器自动化 | CloakBrowser + Playwright Page API |
| RAG 检索 | ChromaDB + BGE Embedding + BGE Reranker + BM25 |
| NLP | Jieba 分词 + BERT 意图识别 + Sentence Transformers |
| LLM | Ollama（本地）/ 通义千问 / OpenAI 兼容接口 |
| 数据存储 | SQLite/JSON 文件数据库 + Redis 缓存 + ChromaDB 向量库 |
| 监控 | Prometheus + psutil + loguru |

## 快速开始

完整步骤（含模型下载、Ollama 安装、常见问题）见 [docs/安装文档.md](docs/安装文档.md)。

```bash
# 1. 创建虚拟环境（Python >= 3.11）
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # Linux/macOS

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
copy .env.example .env           # 按需修改

# 4. 启动
python start_dev.py              # 开发模式（热重载，自动打开浏览器）
# 或 python run_app.py           # 生产/独立模式
# 或双击 start.bat               # Windows 一键启动
```

启动后访问 <http://127.0.0.1:8023/>

## 使用流程

1. 点击右上角「启动抖音」按钮启动浏览器
2. 扫码登录抖音账号
3. 在「任务发布」页配置关键词，运行获客任务
4. 在「客户管理」页查看采集到的客户并批量导出
5. 在「成果展示」页分析高意向客户，私信触达
6. 在「模型训练」页维护知识库，提升自动回复质量

## 环境变量

常用变量（完整见 `.env.example`）：

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SERVER_HOST` | `127.0.0.1` | 服务监听地址 |
| `SERVER_PORT` | `8023` | 服务监听端口 |
| `LLM_PROVIDER` | `ollama` | LLM 提供商（ollama/dashscope/openai/deepseek） |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama 服务地址 |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-small-zh-v1.5` | 嵌入模型 |
| `RERANKER_MODEL_PATH` | 空 | 重排序模型路径（留空用默认 bge-reranker） |
| `HEADLESS` | `false` | 是否无头模式启动浏览器 |
| `CLOAKBROWSER_BINARY_PATH` | 空 | 指定本机 Chrome / CloakBrowser 路径 |

## 注意事项

- 请勿用于非法用途，遵守目标平台的服务条款
- 首次运行需手动扫码登录抖音
- 本地模型（embedding/reranker）需提前下载到 `models/` 目录，详见安装文档
- 如遇浏览器启动失败，请关闭已运行的 Chrome 进程后重试
- `data/`、`logs/`、`models/` 为运行时目录，首次启动自动创建
