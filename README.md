# Paper Harvester - Sci-Hub Edition

面向学术元分析的论文全文批量获取工具。从期刊列表出发，通过Sci-Hub镜像批量下载PDF全文，记录来源、生成可复现清单，支持大规模学术元分析研究。

## 核心功能

- **期刊驱动批量下载**：从Excel期刊列表读取ISSN，通过Crossref发现DOI，批量从Sci-Hub获取全文
- **多镜像自动切换**：内置多个Sci-Hub镜像，自动检测可用性，失败时无缝切换
- **智能断点续传**：中断后可恢复，自动跳过已下载的PDF
- **元数据保存**：记录DOI、标题、作者、期刊、下载时间、文件哈希
- **元分析就绪**：输出结构化清单，支持后续文本分析

## 数据来源

### DOI发现
- **Crossref API**：通过ISSN + 年份范围查询期刊论文DOI和元数据

### 全文下载
- **Sci-Hub Mirrors**：批量下载PDF全文

## 输入文件

期刊列表位于：

```text
期刊列表分组/期刊列表分组/
```

当前分组包括（文件名后缀为各表期刊条数；**同一出版社在本目录下满 2 本才单独建表**，否则收入 `other_journals_*.xlsx`；**工作论文**单独 `working_papers_*.xlsx`）：

- `aea_journals_7.xlsx` - 美国经济学会期刊
- `aom_journals_2.xlsx` - Academy of Management（AMJ / AMR）
- `cambridge_journals_9.xlsx` - Cambridge University Press
- `degruyter_journals_2.xlsx` - De Gruyter（BE 系列等）
- `informs_journals_6.xlsx` - INFORMS 管理科学期刊
- `mit_press_journals_2.xlsx` - MIT Press
- `other_journals_14.xlsx` - 其余「单刊」出版社及杂项（含原 now / CUNY 单刊）
- `oup_journals_23.xlsx` - Oxford University Press
- `sage_journals_20.xlsx` - SAGE 出版
- `sciencedirect_journals_45.xlsx` - Elsevier / ScienceDirect
- `springer_journals_19.xlsx` - Springer Nature
- `tandfonline_journals_15.xlsx` - Taylor & Francis
- `uchicago_journals_9.xlsx` - University of Chicago Press
- `uwpress_journals_2.xlsx` - University of Wisconsin Press（Land Economics、JHR）
- `wiley_journals_50.xlsx` - Wiley 出版
- `working_papers_1.xlsx` - 工作论文（如 NBER Working Paper）

代码实现时应支持列名容错，例如 `journal`, `title`, `期刊名`, `ISSN`, `eISSN`, `publisher`, `platform`, `discipline` 等常见列名。

## 目录结构

```text
data/
  state/
    papers.sqlite          # 核心状态数据库
  metadata/                # 从Crossref获取的元数据JSON
    {journal_id}/
      {year}.json
  fulltext/
    pdf/                   # 下载的PDF全文
      {期刊全名}/
        {年份}/
          {issue}/
            {CODE}_{主标题}.pdf
  manifests/               # 下载清单和统计报告
    download_report.csv
    summary.md
  logs/                    # 运行日志
    download.log
    errors.log
```

约定：

- 原始全文和数据库不提交到 Git
- PDF 按期刊、年份、期次分层保存，文件夹结构为 `{期刊全名}/{年份}/{issue三位}`，例如 `Journal of Finance/2024/001`
- PDF 文件名使用 `{期刊ID三位}{volume四位}{issue三位}_{主标题}.pdf`，例如 `0750000000_The Effect of Unions on Employment.pdf`
- 期刊 ID 来自合并后的 `期刊列表_260511_zpc.xlsx`，例如第 1 本为 `001`、第 10 本为 `010`
- 如果 Crossref 元数据缺 volume 或 issue，分别用 `0000` / `000` 占位；超出固定位数时保留完整数字，不截断
- `Book Review`、`Correction`、`Introduction`、`Front Matter`、`Index` 等非论文主标题会在下载前跳过并记录为 `skipped`
- 下载记录保存 `doi`, `title`, `journal`, `year`, `mirror`, `url`, `sha256`, `retrieved_at`, `file_path`
- 日志记录每次请求的 `timestamp`, `doi`, `mirror`, `status_code`, `response_time`

## 配置

从 `.env` 读取配置：

```env
# 必需配置
CROSSREF_MAILTO=your-email@example.com
USER_AGENT=paper-harvester/1.0 (mailto:your-email@example.com)

# Sci-Hub镜像配置（逗号分隔，按优先级排序）
SCIHUB_MIRRORS=https://sci-hub.al,https://sci-hub.se,https://sci-hub.st,https://sci-hub.ru,https://sci-hub.wf,https://sci-hub.ren

# 可选配置
SCIHUB_TIMEOUT=30                    # 单次请求超时（秒）
SCIHUB_RETRY=3                       # 单镜像失败后的重试次数
SCIHUB_MIRROR_COOLDOWN=300           # 镜像失效后的冷却时间（秒）

# 系统配置
DATA_DIR=data                        # 数据根目录
REQUESTS_PER_MINUTE=10               # 每分钟请求数限制
CONCURRENT_DOWNLOADS=3               # 预留配置；当前实现为顺序下载
MAX_RETRIES_PER_DOI=5                # 单个DOI的最大尝试次数（跨所有镜像）

# 代理配置（可选）
HTTP_PROXY=
HTTPS_PROXY=
```

最小可运行配置只需 `CROSSREF_MAILTO` 和至少一个可用的Sci-Hub镜像。

## 启动 Python 环境

本项目需要 Python 3.10+。推荐每位使用者在自己的机器上创建独立虚拟环境，然后安装依赖和命令行入口。

### 方式一：使用 venv（推荐通用方式）

Windows PowerShell：

```powershell
cd "你的项目目录"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python run.py --help
paper-harvester --help
```

macOS / Linux：

```bash
cd "你的项目目录"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python run.py --help
paper-harvester --help
```

其中：

- `pip install -r requirements.txt` 安装依赖。
- `pip install -e .` 以可编辑模式安装本项目，并注册 `paper-harvester` 命令。
- 如果只想临时运行，也可以使用 `python run.py ...`，不一定要使用命令行入口。

### 方式二：使用 conda

如果习惯用 conda，可以自己创建环境，环境名不必固定为 `paper-harvester`：

```powershell
conda create -n paper-harvester python=3.10
conda activate paper-harvester
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python run.py --help
```

如果 `conda activate` 提示不可用，需要先根据本机 conda 安装路径初始化 shell，或参考 conda 官方文档执行 `conda init`。不要直接复制他人机器上的 conda 路径。

### 配置运行参数

首次运行前，建议复制配置模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，至少填写：

- `CROSSREF_MAILTO`
- `USER_AGENT`
- 如需下载全文，配置可用的下载源和代理

### 本地开发辅助脚本（可选）

仓库中的 `scripts/activate_paper_harvester.ps1` 和 `.vscode/` 配置是本地开发辅助文件，用于在特定 Windows + conda 环境里自动激活终端。其他使用者不需要依赖这些文件；如果本机环境名或 conda 安装路径不同，请按上面的通用步骤手动创建环境。

## CLI 规格

命令入口：

```powershell
# 安装包后使用
paper-harvester --help

# 或在源码目录直接使用
python run.py --help
```

以下命令为当前代码已经实现的功能。

### 导入期刊清单

```powershell
# 导入目录下所有 Excel 文件
paper-harvester journals import --input "期刊列表分组/期刊列表分组"

# 只导入某一个 Excel 文件（.xlsx / .xls）
paper-harvester journals import --input "期刊列表分组/期刊列表分组/aea_journals_7.xlsx"

# 查看已导入期刊
paper-harvester journals list
paper-harvester journals list --platform elsevier
paper-harvester journals list --discipline economics
```

### 发现 DOI 和元数据

```powershell
# 从指定期刊发现论文（从Crossref获取元数据）
paper-harvester discover --journal-id journal_of_financial_economics --from-year 2020 --until-year 2024

# 按平台批量发现
paper-harvester discover --platform wiley --from-year 2020 --until-year 2024

# 发现所有期刊
paper-harvester discover --all --from-year 2000 --until-year 2024

# 预演模式（只显示将要发现的DOI数量，不写入数据库）
paper-harvester discover --journal-id journal_of_finance --from-year 2020 --until-year 2024 --dry-run
```

`discover` 阶段只写入 Crossref 元数据，不下载全文。Crossref 分页请求会重试；如果连续失败，命令会显式报错，避免只保存部分页却显示成功。

### 下载全文

```powershell
# 下载指定期刊和年份范围的PDF
paper-harvester download --journal-id journal_of_financial_economics --from-year 2020 --until-year 2024

# 限制下载数量（测试用）
paper-harvester download --journal-id journal_of_financial_economics --from-year 2020 --until-year 2020 --limit 20

# 按平台批量下载
paper-harvester download --platform elsevier --from-year 2020 --until-year 2024

# 下载单个DOI
paper-harvester download --doi 10.1016/j.jfineco.2020.01.001

# 从文件批量下载（每行一个DOI）
paper-harvester download --file data/doi_list.txt

# 强制重新下载（覆盖已有文件）
paper-harvester download --journal-id ... --force

# 指定特定镜像
paper-harvester download --journal-id ... --mirror https://sci-hub.se
```

下载特性：
- 自动断点续传：跳过已存在且哈希校验通过的PDF
- 多镜像自动切换：当前镜像失败时自动尝试列表中的下一个
- 智能限速：可配置的请求频率限制
- 完整性校验：SHA256哈希验证

当前下载后端为 Sci-Hub 镜像解析与 PDF 直链下载；后续如果更换下载方式，建议保留 `download_papers` 的输入输出契约，即 DOI 列表、输出目录、成功/失败统计和 `downloads` 表记录。

工作论文类来源会优先尝试官方 PDF。目前期刊清单中识别到的工作论文来源为 `NBER Working Paper`；其 DOI 形如 `10.3386/w15630`，下载时先尝试 `https://www.nber.org/papers/w15630.pdf`，成功记录 `mirror=nber-official`，失败后再回落到 Sci-Hub 镜像。

如果需要批量稳定测试 NBER 官方源，可以使用 `--official-only` 禁止回落到 Sci-Hub：

```powershell
paper-harvester download --journal-id nber_working_paper --from-year 2010 --until-year 2010 --official-only
paper-harvester download --doi 10.3386/w15630 --official-only --force
```

官方源下载支持 `.env` 中的 `OFFICIAL_USER_AGENT` 和 `NBER_COOKIE`。如果本机直接请求 NBER 返回 403，可先在浏览器或机构网络中打开 NBER，再把必要 cookie 填入 `NBER_COOKIE`，或配置 `HTTP_PROXY` / `HTTPS_PROXY` 后重试。`--official-only` 模式下官方源失败会写入失败记录，不会混入 Sci-Hub 成功结果。

当前 PDF 下载代码主要作为练习版后端：

1. `SciHubClient.download()` 负责从 DOI 找到 PDF 直链；工作论文 DOI 会先尝试官方源。
2. `SciHubClient._download_pdf()` 负责把 PDF 流式写入 `.part` 临时文件，校验大小和 `%PDF` 文件头后再保存为最终文件。
3. `paper_harvester.paths.build_pdf_path()` 负责生成 `{期刊全名}/{年份}/{issue三位}/{CODE}_{主标题}.pdf` 保存路径。
4. `download_papers()` 负责批量调度、写入 `downloads` 表；只有当前 DOI 的成功记录与本地文件哈希/大小匹配时才会跳过，单纯同名文件存在不会被当作该 DOI 成功；非论文主标题会直接跳过。

后续替换为其他下载方式时，优先替换 `SciHubClient.download()` 内部逻辑，尽量保留 `download_papers()`、`build_pdf_path()`、SHA256 校验和数据库记录格式，这样 `status`、`queue`、`report`、`verify`、`extract-text` 可以继续复用。

### 查看状态和队列

```powershell
# 查看下载状态统计
paper-harvester status
paper-harvester status --journal-id journal_of_financial_economics

# 查看待下载队列
paper-harvester queue --journal-id journal_of_financial_economics --limit 50

# 查看失败列表
paper-harvester queue --status failed --limit 50

# 查看指定DOI的详细信息
paper-harvester show 10.1016/j.jfineco.2020.01.001
```

### 镜像管理

```powershell
# 检查所有配置的镜像可用性
paper-harvester check-mirrors

# 输出示例：
# Mirror                    Status    Response Time    Last Checked
# https://sci-hub.se        OK        1.23s            2024-01-15 10:30:00
# https://sci-hub.st        FAIL      Timeout          2024-01-15 10:30:05
# https://sci-hub.ru        OK        2.45s            2024-01-15 10:30:03
```

### 导入本地PDF

```powershell
# 导入已有PDF文件；输入文件名需使用 DOI 安全格式，例如 10.1016_j.jfineco.2020.01.001.pdf
paper-harvester import-files --input "D:\existing_papers" --match-by doi

# 只导入某个期刊的已知论文PDF
paper-harvester import-files --input "D:\publisher_package" --journal-id journal_of_financial_economics

# 覆盖已复制文件并新增下载记录
paper-harvester import-files --input "D:\existing_papers" --force
```

`import-files` 会根据数据库中已有论文的 DOI 匹配输入文件名，然后按统一规则复制到 `DATA_DIR/fulltext/pdf/{期刊全名}/{年份}/{issue}/{CODE}.pdf`，并在 `downloads` 表中写入 `local-file` 来源记录。无法匹配到已知 DOI 的 PDF 会被跳过。

### 生成报告

```powershell
# CSV格式详细报告
paper-harvester report --output data/manifests/download_report.csv

# Markdown汇总报告
paper-harvester report --format markdown --output data/manifests/summary.md

# 按平台统计
paper-harvester report --format markdown --output data/manifests/by_platform.md --by-platform

# 按年份统计
paper-harvester report --format markdown --output data/manifests/by_year.md --by-year

# 按期刊统计
paper-harvester report --format markdown --output data/manifests/by_journal.md --by-journal

# 导出失败下载记录
paper-harvester report --status failed --output data/manifests/failed_downloads.csv
```

报告内容：
- 总DOI数、成功下载数、失败数、待处理数
- Markdown 报告支持按平台、年份、期刊分组统计
- CSV 报告输出 DOI、题名、期刊、年份、状态、镜像、文件路径、SHA256 和错误信息

### 导出映射

```powershell
# 导出 DOI 到本地文件路径的 JSON 映射
paper-harvester export-map --output data/manifests/doi_to_file.json
```

### 维护命令

```powershell
# 清理损坏/不完整的下载文件
paper-harvester cleanup

# 验证所有文件的哈希完整性
paper-harvester verify

# 重试失败的下载
paper-harvester retry-failed --limit 50

# 预览历史路径迁移和碰撞修复，不改动文件或数据库
paper-harvester migrate-paths

# 执行迁移，输出 JSON manifest；碰撞误记的 DOI 会改为 failed，之后可 retry-failed
paper-harvester migrate-paths --apply

# 从PDF提取纯文本（用于NLP分析）
paper-harvester extract-text --input data/fulltext/pdf --output data/fulltext/txt

# 覆盖已有txt
paper-harvester extract-text --force
```

`extract-text` 使用 PyMuPDF 读取 PDF，并在输出目录中保留相对路径结构，把 `.pdf` 转为同名 `.txt`。

## 数据库核心表

使用SQLite存储，核心表结构：

### `journals`

| 字段 | 类型 | 说明 |
|------|------|------|
| journal_id | TEXT PK | 内部ID（小写期刊名，空格改下划线） |
| source_id | INTEGER | 合并期刊表中的编号，用于 PDF 文件名前三位 |
| title | TEXT | 期刊完整名称 |
| platform | TEXT | 平台（elsevier/wiley/springer等） |
| publisher | TEXT | 出版商 |
| issn | TEXT | ISSN |
| eissn | TEXT | eISSN |
| discipline | TEXT | 学科分类 |
| source_file | TEXT | 来源Excel文件名 |

### `papers`

| 字段 | 类型 | 说明 |
|------|------|------|
| doi | TEXT PK | DOI |
| title | TEXT | 论文标题 |
| journal_id | TEXT FK | 期刊ID |
| published_year | INTEGER | 发表年份 |
| published_date | TEXT | 完整日期（ISO格式） |
| authors | TEXT | 作者JSON数组 |
| volume | TEXT | 卷号 |
| issue | TEXT | 期号 |
| pages | TEXT | 页码 |
| abstract | TEXT | 摘要 |
| keywords | TEXT | 关键词（JSON数组） |
| crossref_raw | TEXT | 原始Crossref响应JSON |
| created_at | TIMESTAMP | 记录创建时间 |

### `downloads`

| 字段 | 类型 | 说明 |
|------|------|------|
| download_id | INTEGER PK | 下载ID |
| doi | TEXT FK | DOI |
| file_path | TEXT | 本地文件相对路径 |
| file_size | INTEGER | 文件大小（字节） |
| sha256 | TEXT | 文件SHA256哈希 |
| mirror | TEXT | 使用的Sci-Hub镜像 |
| scihub_url | TEXT | 请求的Sci-Hub页面URL |
| pdf_url | TEXT | 实际PDF下载URL |
| status | TEXT | 状态：success/failed/pending |
| http_status | INTEGER | HTTP状态码 |
| error_message | TEXT | 错误信息（如失败） |
| attempts | INTEGER | 尝试次数 |
| started_at | TIMESTAMP | 开始下载时间 |
| completed_at | TIMESTAMP | 完成时间 |
| response_time_ms | INTEGER | 响应时间（毫秒） |

### `mirrors`

| 字段 | 类型 | 说明 |
|------|------|------|
| mirror_url | TEXT PK | 镜像URL |
| status | TEXT | 状态：active/inactive/cooldown |
| last_checked | TIMESTAMP | 最后检测时间 |
| response_time_ms | INTEGER | 上次响应时间 |
| fail_count | INTEGER | 连续失败次数 |
| success_count | INTEGER | 成功次数 |
| cooldown_until | TIMESTAMP | 冷却到期时间 |

### `logs`

| 字段 | 类型 | 说明 |
|------|------|------|
| log_id | INTEGER PK | 日志ID |
| timestamp | TIMESTAMP | 时间戳 |
| doi | TEXT | DOI（如适用） |
| mirror | TEXT | 镜像（如适用） |
| action | TEXT | 动作：discover/download/check/etc |
| status | TEXT | 状态：success/fail/retry |
| message | TEXT | 详细信息 |
| http_status | INTEGER | HTTP状态码（如适用） |
| response_time_ms | INTEGER | 响应时间 |

## Sci-Hub 下载流程

### 标准下载流程

```
1. 构造Sci-Hub查询URL
   → https://{mirror}/{doi}
   
2. 发送HTTP GET请求获取页面
   → 解析HTML查找PDF嵌入方式
   
3. 提取PDF直链
   → 方式A: <iframe src="...pdf">
   → 方式B: <embed id="pdf" src="...pdf">
   → 方式C: location.href 跳转
   
4. 发送HTTP GET请求下载PDF
   → 流式写入本地文件
   
5. 计算SHA256哈希
   → 写入downloads表
```

### 镜像切换策略

```
请求镜像A
  ├─→ 成功 → 继续
  ├─→ 超时/5xx → 标记冷却 → 尝试镜像B
  ├─→ 404 → 记录失败（DOI不存在）
  ├─→ 403/429 → 标记冷却 → 尝试镜像B
  └─→ Captcha/JS挑战 → 标记失效 → 尝试镜像B
```

### 错误处理

| 错误类型 | 检测方式 | 处理策略 |
|----------|----------|----------|
| 连接超时 | requests.Timeout | 指数退避重试，切换镜像 |
| 5xx服务器错误 | status_code >= 500 | 指数退避重试，切换镜像 |
| 404 Not Found | status_code == 404 | 标记为 `failed_final`（Sci-Hub无此论文） |
| 403 Forbidden | status_code == 403 | 标记镜像冷却，切换镜像 |
| 429 Too Many Requests | status_code == 429 | 增加限速延迟，切换镜像 |
| PDF解析失败 | iframe/embed未找到 | 切换镜像重试 |
| 空PDF文件 | file_size < 1KB | 删除文件，切换镜像重试 |
| 不完整下载 | 连接中断 | 删除临时文件，重试 |
| 哈希冲突 | SHA256已存在 | 保留已有文件，记录重复 |

## 论文状态流转

```
┌─────────────┐     discover      ┌─────────────────┐
│  undiscovered │ ───────────────→ │  metadata_only  │
└─────────────┘                   └─────────────────┘
                                         ↓
                                    download
                                         ↓
                              ┌─────────────────────┐
                              ↓                     ↓
                    ┌───────────────┐     ┌───────────────┐
                    │ downloading   │     │   failed      │
                    └───────┬───────┘     │  (retryable) │
                            ↓              └───────┬───────┘
                    ┌───────────────┐              ↓
                    │  downloaded   │ ←──── retry ────┘
                    └───────────────┘
                           ↓
                    ┌───────────────┐
                    │ failed_final  │ (404或重试耗尽)
                    └───────────────┘
```

## 技术栈

- **语言**：Python 3.10+
- **CLI框架**：`click` 或 `typer`
- **HTTP请求**：`httpx`（支持异步）或 `requests`
- **HTML解析**：`beautifulsoup4`
- **Excel读取**：`pandas` + `openpyxl`
- **数据库**：`sqlite3`（标准库）
- **进度条**：`tqdm`
- **哈希计算**：`hashlib`（标准库）
- **配置读取**：`python-dotenv`
- **PDF文本提取**：`pymupdf`（fitz）

## 当前实现状态

### Phase 1: 基础架构
- [x] 项目结构搭建（目录创建）
- [x] 配置读取（.env + 命令行参数）
- [x] SQLite数据库初始化
- [x] 日志系统

### Phase 2: 期刊导入
- [x] Excel读取（多列名容错）
- [x] `journals`表写入
- [x] CLI命令：`journals import`, `journals list`

### Phase 3: DOI发现
- [x] Crossref API封装
- [x] 按ISSN+年份查询DOI
- [x] 元数据解析与`papers`表写入
- [x] CLI命令：`discover`

### Phase 4: Sci-Hub下载核心
- [x] 镜像可用性检测
- [x] PDF直链解析（iframe/embed模式）
- [x] 流式下载 + 临时文件保存
- [x] 哈希校验
- [x] 镜像自动切换逻辑

### Phase 5: CLI完善
- [x] `download`命令（支持所有参数）
- [x] `status`、`queue`、`show`命令
- [x] `check-mirrors`命令
- [x] `report`命令

### Phase 6: 维护功能
- [x] `cleanup`命令（清理损坏文件）
- [x] `verify`命令（哈希验证）
- [x] `retry-failed`命令
- [x] `extract-text`命令
- [x] `import-files`命令
- [x] `migrate-paths`命令（修复历史路径碰撞）

## 使用示例

```powershell
# 完整工作流示例

# 1. 导入期刊列表
paper-harvester journals import --input "期刊列表分组/期刊列表分组"

# 2. 检查镜像可用性（选择最快的）
paper-harvester check-mirrors

# 3. 发现Journal of Finance 2020-2024的所有DOI
paper-harvester discover --journal-id journal_of_finance --from-year 2020 --until-year 2024

# 查看发现结果统计
paper-harvester status --journal-id journal_of_finance

# 4. 下载前20篇测试
paper-harvester download --journal-id journal_of_finance --from-year 2020 --until-year 2020 --limit 20

# 5. 查看下载状态
paper-harvester status

# 6. 批量下载剩余论文
paper-harvester download --journal-id journal_of_finance --from-year 2020 --until-year 2024

# 7. 重试失败的下载
paper-harvester retry-failed

# 8. 生成最终报告
paper-harvester report --format markdown --output data/manifests/final_report.md

# 9. 导出DOI到文件映射（供后续分析）
paper-harvester export-map --output data/manifests/doi_map.json
```

## 注意事项

1. **网络环境**：Sci-Hub在某些地区可能无法直接访问，需配置代理
2. **镜像变动**：Sci-Hub镜像域名经常变化，需定期更新配置
3. **限速重要**：请合理设置 `REQUESTS_PER_MINUTE`，避免触发风控
4. **断点续传**：大规模下载建议使用 `--limit` 分批进行，便于中断和恢复
5. **存储空间**：经济学顶刊PDF平均2-5MB，1000篇约需2-5GB空间

## 参考项目

- [scihub.py](https://github.com/zaytoun/scihub.py) - Sci-Hub Python客户端基础实现
- [doi-hunter](https://pypi.org/project/doi-hunter/) - DOI批量下载CLI
- [scihub-cli](https://github.com/Oxidane-bot/scihub-cli) - 多源学术下载工具
