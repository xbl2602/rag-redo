# installer/

RAG Redo 的 Windows 便携版打包脚本。

## 构建

在 Windows 开发机执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\python.exe installer\build_windows.py
```

脚本会生成：

- `dist/rag-redo-portable/start-gui.cmd`：双击启动 GUI。
- `dist/rag-redo-portable/start-mcp.cmd`：MCP stdio 启动器。
- `dist/rag-redo-portable/runtime/python/`：随包携带的独立 Python。
- `dist/rag-redo-portable/app/`：核心代码、插件和资源。
- `dist/rag-redo-portable.zip`：可直接复制给其他 Windows 用户。

用户不需要安装 Python、CUDA、Node 或 Inno Setup。首次使用模型功能仍可能需要联网下载模型；WEMM 首次启用会在插件目录建立自己的独立环境。

## 数据位置

便携版启动器设置 `RAG_REDO_DATA_ROOT=%LOCALAPPDATA%\\RAG-Redo\\data`。删除便携版文件夹不会删除索引、配置和插件状态；迁移机器时如需保留数据，请同时复制这个目录。

## MCP 配置

把 MCP 客户端的 `command` 指向解压后的 `start-mcp.cmd` 完整路径，`cwd` 指向同一便携版目录即可。stdio 输出会原样转发，不要把启动器改成 GUI 模式。

## 验证

构建后至少检查：

```powershell
Test-Path .\dist\rag-redo-portable\runtime\python\python.exe
Test-Path .\dist\rag-redo-portable\app\plugins\official-gui-shell\plugin.toml
Test-Path .\dist\rag-redo-portable.zip
```

本目录不再生成 PyInstaller exe，也不再需要 Inno Setup。
