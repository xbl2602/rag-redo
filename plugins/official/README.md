# plugins/official/

官方维护的默认插件集所在地（Phase 1起逐个落地，对应 [../../docs/FEATURE_TRIAGE.md](../../docs/FEATURE_TRIAGE.md) 的映射表）。每个插件是这个目录下的一个独立文件夹，结构见 [../../docs/PLUGIN_SPEC.md](../../docs/PLUGIN_SPEC.md) 第1节。

第三方/自定义插件不放这里——运行时会扫描整个 `plugins/` 目录，`official/` 只是约定俗成的命名空间，不是硬编码路径。
