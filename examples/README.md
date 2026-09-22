# examples/

Phase 0 的示例插件，不做任何真实功能，只用来证明插件运行时的机制本身能跑通（见 [../docs/ROADMAP.md](../docs/ROADMAP.md) Phase 0 验收标准）。用真实的插件管理器 CLI 试一下：

```bash
python -m core.cli --plugins-dir examples --state-file /tmp/rag-redo-demo-state.json scan
python -m core.cli --plugins-dir examples --state-file /tmp/rag-redo-demo-state.json enable example-hello
python -m core.cli --plugins-dir examples --state-file /tmp/rag-redo-demo-state.json enable example-hello-conflict
python -m core.cli --plugins-dir examples --state-file /tmp/rag-redo-demo-state.json enable example-broken
python -m core.cli --plugins-dir examples --state-file /tmp/rag-redo-demo-state.json status
```

- [hello-plugin/](hello-plugin/) —— 正常的完整生命周期（加载/启用/禁用/卸载都成功）
- [hello-plugin-conflict/](hello-plugin-conflict/) —— 和 hello-plugin 声明同一个单例扩展点 `demo_singleton`，两个都启用后，`status` 会显式报冲突
- [broken-plugin/](broken-plugin/) —— `on_enable` 故意抛异常，用来验证核心把它隔离成 `failed` 状态而不崩溃、不牵连其他插件

这些不是"官方插件集"的一部分（那些在 [../plugins/official/](../plugins/official/)，Phase 1 起落地），纯粹是开发/演示用的最小示例。
