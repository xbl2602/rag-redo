# 插件规范

> 定义一个插件长什么样、怎么被发现、怎么声明依赖。这是 [ARCHITECTURE.md](ARCHITECTURE.md) 第2.1节的实现细节展开，具体字段是首版草案，Phase 0 写插件加载器代码时可能微调，但改动要回来同步这份文件——类型定义的权威来源最终是代码（Phase 0 后是 `core/` 下的 manifest schema 实现），这份文档是给人看的说明，两边不一致时以代码为准。

## 1. 目录结构

```
plugins/
  <plugin_id>/
    plugin.toml       # 清单，见第2节
    README.md         # 面向人的说明（做什么、需要什么、已知限制）
    <入口代码>          # in_process: 一个Python包；subprocess_service: 任意语言的独立项目
```

`<plugin_id>` 是插件的唯一标识，建议反向域名或简单短横线命名（如 `official-embedder-bge-m3`），官方默认插件集统一 `official-` 前缀。

## 2. plugin.toml 字段（草案）

```toml
id = "official-embedder-bge-m3"
name = "BGE-M3 向量化"
version = "0.1.0"
api_version = ">=0.1,<0.2"   # 兼容的核心插件API版本范围

[provides]
# 键=扩展点id，值="singleton"（同一时刻只能有一个生效实现）或"multi"（可以多个同时生效）。
# 核心不维护一份写死的扩展点名单——是不是单例，由声明它的插件自己说；如果两个插件
# 对同一个点的基数声明不一致，核心保守按 singleton 处理（宁可多报冲突不可漏报）。
embedder = "singleton"

[requires]
# 依赖其他插件或核心能力
core = ">=0.1"
# plugin_id = "版本范围"（依赖别的插件时）

[runtime]
kind = "in_process"          # 或 "subprocess_service"
entry = "official_embedder_bge_m3.plugin:Embedder"   # module:class——subprocess_service 也需要这个字段
# subprocess_service 额外字段（entry 这时指向一个本地的薄转发类：on_enable
# 用 core/subprocess_service.py 的 SubprocessServiceHandle 拉起下面这条
# command、on_disable 收掉它，方法调用转发成对子进程的本机HTTP请求——真正
# 跑重依赖/模型的是子进程，entry 指向的这个类本身很薄，不需要装任何重
# 依赖。参考实现：plugins/official-ocr-mineru-local）：
# command = ["{python}", "server.py", "--port", "{port}"]   # {port} 由核心自动分配空闲端口替换
# health_check = "http://127.0.0.1:{port}/health"
# env_bootstrap = "setup.sh"   # 首次启用时跑一次，负责拉起独立环境/下载权重——尚未被核心真正执行，见 docs/ROADMAP.md Phase 2 状态

[permissions]
# 声明式，v1不做沙箱强制执行，只做展示——个人本机工具的信任模型是"你选择装什么"，不是"防着插件作恶"
filesystem = ["vault_read"]   # 例：只读用户库文件；写权限需要显式声明 "data_write" 等
network = false
gpu = false
```

## 3. 生命周期

```
发现(scan) → 校验(validate) → [invalid: 停在这一步，原因可见]
                              ↓ ok
                          load → on_load(ctx)
                              ↓ 用户启用
                          enable → on_enable(ctx)   # subprocess_service在这一步拉起子进程
                              ↓ 用户禁用
                          disable → on_disable(ctx) # 释放资源、停子进程
                              ↓
                          unload → on_unload(ctx)
```

`ctx`（PluginContext）是核心传给插件的唯一接口，暴露：日志、配置读写（仅限该插件自己的配置命名空间）、DataStore（数据流管理器提供，按扩展点类型收窄权限——一个 `extractor` 插件拿不到 `vector_store` 的写接口）、资源仲裁器的租约 API。**插件不能绕过 ctx 直接 import 核心内部模块**，这是插件和"核心内部子模块"的本质区别。

## 4. 发现与"热插拔"的真实语义

- 新增插件：把文件夹放进 `plugins/`，在插件管理器（GUI 或 CLI）里点"重新扫描"，插件出现在列表（状态=已发现未启用）。**不做持续文件系统监听**，避免复制中途的半成品文件夹被提前扫到。
- 启用/禁用已发现的插件：**即时生效，不需要重启核心**。
- 移除插件：先禁用（触发 `on_disable`），再删除文件夹，再"重新扫描"让它从列表消失。删除正在启用的插件文件夹而不走禁用流程属于用户误操作，核心下次扫描时会明确报"插件文件缺失"而不是崩溃。

## 5. 版本兼容

`api_version` 是插件 against 核心插件 API 的兼容范围（不是插件自己的版本）。核心插件 API 版本变更规则：新增能力→minor；破坏性改动（移除/改变 ctx 接口）→major，并要求所有官方插件同步升级。插件间 `requires` 同理，用标准的语义化版本范围表达式。

## 6. 官方插件集（Phase 1 默认打包哪些）

见 [FEATURE_TRIAGE.md](FEATURE_TRIAGE.md) 第2列标"Phase 1"的条目——安装包/便携版默认包含这些，其余插件 Phase 2+ 以后作为可选下载。

## 7. 依赖装在哪：不碰系统环境

- `in_process` 插件在 `plugin.toml` 里声明的 Python 依赖，由插件运行时统一装进**核心自己的隔离虚拟环境**（比如 `.venv/` 或等价机制），不使用、不修改用户机器上任何已存在的系统 Python 或全局 site-packages。
- `subprocess_service` 插件的 `env_bootstrap` 负责建一个**只属于这个插件自己**的隔离环境（独立 venv/uv 环境），既不进核心 venv，也不和其他 `subprocess_service` 插件共享——避免旧项目里"MinerU 本机 OCR 需要独立 py3.12 环境"这种重依赖污染或冲突主环境的问题重演。
- 任何插件都不允许在 `on_load`/`on_enable` 里执行会修改系统级状态的操作（写系统 PATH、装到全局 site-packages、写注册表非安装器管辖的部分）——这属于 [AGENTS.md](../AGENTS.md) 架构红线7的范围，校验阶段如果侦测到明显违反（比如声明的 `permissions` 之外的可疑行为），应该在插件管理器里警示而不是静默放行。
