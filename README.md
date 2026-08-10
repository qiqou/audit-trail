# 审迹 · Audit Trail

> 本地离线运行的审计工作台 —— 底稿编写 · 附件管理 · 复核流转 · 归档打包，为专项审计现场设计。

[![CI](https://github.com/qiqou/audit-trail/actions/workflows/ci.yml/badge.svg)](https://github.com/qiqou/audit-trail/actions/workflows/ci.yml)

**审迹（Audit Trail）** 是一款本地离线运行的审计工作台：按「审计项目 → 被审计单位 → 底稿/附件」三级结构组织审计数据，覆盖底稿编写、附件管理、复核流转、归档打包全流程。数据全部存在项目文件夹内，拷贝文件夹即完成项目转移，不依赖云端。

<img width="2704" height="1424" alt="image" src="https://github.com/user-attachments/assets/6ed3b5c1-b445-4507-a610-d73ceca93476" />


## 特性

| 模块 | 说明 |
|---|---|
| 📋 底稿管理 | 按单位组织底稿，未完成草稿清理、三档自动保存、历史版本回溯；底稿编号规则（前缀+序号+后缀）写入数据层，树/导出/打包全程一致 |
| 🔄 复核流转 | 草稿 → 编制完成 → 复核退回 → 已复核 → 已归档 五态状态机，复核意见留痕（审计日志） |
| 📎 附件管理 | 拖拽导入、自动关联底稿、资料库共享、批量重命名、文件夹实体（单文件规则） |
| 🗂️ 版块视图 | 一页三栏工作区，按单位/按版块双视图切换，分类列表带数量小计 |
| 📊 台账导出 | 问题汇总 Excel（含状态/版本数/证据提示），归档 ZIP 内置清单（路径+sha256） |
| 🩺 健康检查 | 附件完整性扫描（孤儿/缺失/哈希核对，异步进度可取消）、导入报告导出 |
| 🔀 合并导入 | 汇总多个备份/归档到当前项目，冲突自动处理并出具报告 |
| 🔐 数据安全 | SQLite 事务写入、底稿版本快照、项目自包含（`.auditproj` 目录伪装防误删）、强制使用人留痕 |
| 💻 跨平台 | macOS / Windows 双端打包（PyInstaller 统一 spec，GitHub Actions 自动构建） |

<img width="2718" height="1506" alt="image" src="https://github.com/user-attachments/assets/9bdc35ae-7548-4191-a3fd-9577b2e8cbd4" />

## 快速开始

### 方式一：打包版（推荐，免 Python 环境）

从 [Releases](https://github.com/qiqou/audit-trail/releases) 下载对应平台的安装包：

- **macOS**：解压后双击 `审迹.app`
- **Windows**：解压整个文件夹后双击 `审迹.exe`（onedir：exe 与 `_internal\` 目录须整体保留）

首次打开如提示"无法验证开发者"：右键 → 打开 → 确认。程序启动后自动打开浏览器进入工作界面。

### 方式二：源码运行（开发调试）

需要 Python 3.11+：

```bash
git clone https://github.com/qiqou/audit-trail.git
cd audit-trail

# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
# Windows
# python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

启动后会自动打开浏览器。程序默认由系统分配空闲本地端口，实际地址会显示在启动终端中；服务只监听 `127.0.0.1`，不会暴露到局域网。

### 基本流程

1. 启动 → 输入**使用人**（不输不进门，所有变更留痕）
2. 新建审计项目 → 添加被审计单位
3. 添加底稿 + 拖拽附件自动关联
4. 编制完成 → 提交复核 → 复核通过/退回 → 已复核 → 归档
5. 审计结束 → 导出台账 Excel / 一键打包归档 ZIP

## 数据与安全

- **项目自包含**：项目文件夹 = `audit.db` + `附件库/` + `输出/`，整体拷走即完整转移
- **项目目录防误删**：新建项目目录自动命名为 `项目名.auditproj` 并隐藏（默认文件管理器不可见），防止人员误入删改；删除项目只能在工具内"最近项目"列表操作（二次确认，仅限 `.auditproj` 目录）
- **输出不覆盖**：导出文件默认带时间戳后缀，绝不覆盖旧输出
- **版本快照**：可选输入停止后实时保存、每 5 分钟或每 20 分钟保存；仅内容变化时留版本，可回溯恢复
- **删除确认**：任何删除操作前需确认
- **强制使用人**：所有变更类操作写入 `audit_log`，日志随项目走
- **崩溃日志**：未捕获异常写入 `~/.shenji/logs/crash_*.log`

详细说明见 [用户说明.md](用户说明.md)，回退操作见 [回滚策略_T12.md](回滚策略_T12.md)。

## 项目结构

```
audit-trail/
├── main.py                    # FastAPI 应用入口 + API 路由 + 启动生命周期
├── 审迹.spec                  # PyInstaller 双端统一构建配置
├── backend/
│   ├── database.py            # 数据层（SQLite：项目/单位/底稿/附件/版本/日志）
│   ├── export.py              # 台账导出 / 导入合并 / 归档打包 / 备份恢复
│   ├── platform_adapter.py    # 平台抽象层（选目录/开浏览器/单实例锁/端口检测）
│   ├── limits.py              # 限额常量（解压上限等）
│   └── version.py             # 版本号单一来源
├── frontend-v3/               # Vue 3 + TypeScript + Vite 工作台（默认入口）
│   └── src/components/        # 三栏工作区、版本、证据、项目归档操作
├── scripts/
│   ├── gen_sample_project.py      # 生成试点样本项目（10单位/200底稿/500+附件）
│   ├── check_cross_platform.py    # 跨端一致性检查（六项指标比对）
│   └── release_manifest.py        # 发布产物清单（版本 + sha256）
├── tests/                     # pytest 全套（数据层/API/状态机/扫描/合并/打包）
└── .github/workflows/ci.yml   # lint + 测试 + macOS/Windows 双端打包
```

## 开发

### 前端构建

源码首次运行前先构建前端：

```bash
cd frontend-v3
pnpm install --frozen-lockfile
pnpm build
cd ..
.venv/bin/python main.py
```

启动日志会显示实际本地地址。前端构建产物缺失时，程序会明确提示先执行构建。

### 交付门禁（每个任务完成必须全绿）

```bash
# 1. 静态检查
ruff check .

# 2. 全量回归
python -m pytest tests/ -q

# 3. 界面回归（首次需下载 Chromium）
pnpm --dir frontend-v3 exec playwright install chromium
E2E_PYTHON=.venv/bin/python pnpm --dir frontend-v3 test:e2e

# 4. 验证配置
hermes verify --json   # ok: true
```

### 打包

**Windows 本地一键打包（推荐）**：双击项目根目录 `build-windows.bat`，自动完成 环境检查 → venv → 依赖 → 门禁(ruff+pytest) → PyInstaller → 发布清单 → 压缩安装包（版本+时间戳命名，不覆盖旧产物）→ 冒烟测试。

**手动打包（macOS / Windows 同一命令）**：

```bash
pnpm --dir frontend-v3 install --frozen-lockfile
pnpm --dir frontend-v3 build
pip install pyinstaller
pyinstaller --noconfirm 审迹.spec
# macOS → dist/审迹.app
# Windows → dist/审迹/（onedir：exe + _internal\ 整目录，分发时整体拷走）
python scripts/release_manifest.py dist   # 生成版本 + sha256 清单
```

CI（`.github/workflows/ci.yml`）在每次 push 后自动完成 lint、测试与双端打包，产物在 Actions 页下载。

## 文档索引

| 文档 | 内容 |
|---|---|
| [用户说明.md](用户说明.md) | 安装/升级/备份/回滚/FAQ（面向最终用户） |
| [回滚策略_T12.md](回滚策略_T12.md) | 程序回退与数据安全策略 |

## 版本记录

- **v1.0（正式版）**：前身「审计小助手」V3.2 功能冻结后正式发布，定名「审迹」。覆盖底稿编写、附件管理、复核流转、归档打包完整闭环，含项目目录伪装、问题分类、版本预览回溯、Excel 导入导出、健康检查、合并导入。
- 后续优先方向：模板管理、批量重命名增强、局域网协同。

## License

MIT
