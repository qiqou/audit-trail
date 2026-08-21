# v1.3 运行时兼容矩阵

本表锁定的是发布与 CI 的工具链，而不是对用户电脑上 Node/Python 的运行时要求；审迹正式运行包不依赖用户预装 Node。

| 项目 | 锁定版本 | macOS 构建 | Windows 构建 | CI | 验证边界 |
|---|---:|---|---|---|---|
| Python | 3.14.6（标准 GIL） | macOS 14 / arm64 | Windows 10+ / x64 | Ubuntu + Windows 2022 | 安装哈希锁、Ruff、pytest、打包与 HTTP 冒烟 |
| Node.js | 24.19.0 | 仅前端构建 | 仅前端构建 | Ubuntu + Windows 2022 | pnpm 安装、类型、静态检查、纯逻辑测试、Vite 构建 |
| pnpm | 11.5.0 | 前端依赖安装 | 前端依赖安装 | Ubuntu + Windows 2022 | 必须使用 `--frozen-lockfile` |

## 约束

- 不启用 Python free-threaded 或 JIT 实验构建；SQLite 单项目单写模型并不从中获益。
- 用户实际使用的是打包后的离线程序；Node 仅参与研发和发布构建。
- 每次修改版本声明，必须同时通过 `scripts/check_runtime_matrix.py`；该检查覆盖版本文件、前端 package、CI 及双端构建脚本。
- `scripts/build_macos.sh` 默认只接受 macOS 14 / Node 24.19.0，生成正式发布包。显式设置 `AUDIT_TRAIL_BUILD_MODE=candidate` 后，可在更高版本 macOS/Node 生成文件名带 `candidate` 的内部候选包，并记录 `dist/build-provenance.txt`；该候选包不能替代 macOS 14 / Node 24.19.0 或 Windows x64 的发布包验收。
