# plugins/

真实运行时扫描的插件目录（`PluginRuntime` 默认 `plugins_dir`）。每个插件是这个目录下**直接的**一层子文件夹（`plugins/<plugin_id>/`，不嵌套子目录），结构见 [../docs/PLUGIN_SPEC.md](../docs/PLUGIN_SPEC.md) 第1节——这是 `core/runtime.py` 的 `scan()` 已经实现并测试过的扫描方式（单层 `iterdir()`，不递归）。

官方维护的插件用 `official-` 前缀命名（如 `official-extractor-text`），对应 [../docs/FEATURE_TRIAGE.md](../docs/FEATURE_TRIAGE.md) 的映射表；第三方/自定义插件同样直接放在这层，用自己选的 id，不需要额外的命名空间子目录——`official-` 前缀本身就是区分官方/第三方的标记，不需要再用文件夹层级重复表达一遍。

开发/演示用的最小示例插件（不是官方插件集的一部分）在 [../examples/](../examples/)，不放在这里。
