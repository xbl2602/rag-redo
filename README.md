<h1 align="center">RAG NAVIGATION</h1>

<p align="center">
  给 AI 助手一张你笔记的地图。<br />
  不用翻遍整台电脑，问一句话，就知道资料在哪个文件的哪一节。
</p>

<p align="center">
  <img alt="MCP 18 个工具" src="https://img.shields.io/badge/MCP-18%20tools-8A2BE2" />
  <img alt="本地运行" src="https://img.shields.io/badge/runs-100%25%20local-brightgreen" />
  <img alt="平台 Windows" src="https://img.shields.io/badge/platform-Windows-lightgrey" />
  <img alt="许可证 MIT" src="https://img.shields.io/badge/license-MIT-blue" />
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/01-overview-dark.gif" />
    <img alt="整体全景：笔记库经过建索引进入索引库，你或 AI 助手通过三个入口来搜索" src="docs/media/01-overview-light.gif" width="860" />
  </picture>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#ai-拿到的是什么">AI 拿到什么</a> ·
  <a href="#它是怎么工作的">工作原理</a> ·
  <a href="#常见问题">常见问题</a> ·
  <a href="docs/DIAGRAMS.md">全部流程图</a>
</p>

---

> **没有它**：AI 为了找一条资料，把整个文件夹翻一遍，还得靠文件名去猜里面写了什么。
> **有了它**：AI 问一句话，按内容匹配，拿到“哪个文件、哪一节”，只读那一小段。文件叫什么都不要紧。
>
> 所有处理都在你自己的电脑上完成，笔记不出本机。

## 它解决什么问题

让 AI 助手用上你的笔记，常见做法有两种：让它自己 grep、翻文件夹，或者把资料整包贴给它、传到云端。

前者有个绕不开的问题：**文件名说明不了内容。** “笔记 3.md”“扫描件_0912.pdf”里到底写了什么，光看名字没法知道，只能一个个打开。grep 又只认字面（你写的是“会议纪要”，它找不到写成“周会记录”的那篇），PDF 和扫描件里的内容搜不到，文件一多又慢又占上下文。后者麻烦，还有隐私顾虑。

RAG NAVIGATION 换了个思路：先在你的电脑上把笔记的**内容**读一遍，建成一张可以按意思查询的地图，匹配的是内容，不看文件叫什么。AI 之后每次只需要问一句话，地图告诉它去哪找，它再只读那一小段。**它不替 AI 回答问题，它负责让 AI 知道去哪找。**

## AI 拿到的是什么

| AI 想知道 | 用哪个工具 | 拿到什么 |
|---|---|---|
| 有哪些库，哪个值得查 | `list_libraries` | 每个库的名字、路径、内容量、最近索引时间，以及一段简介 |
| 资料在哪个文件的哪一节（按内容找，不看文件名） | `search_knowledge` | 库、文件路径、所在小节、置信度；可以只要清单不要正文，先看一眼再决定读哪个 |
| 想看细节 | `read_document` | 那个文件的全文 |
| 哪个 PDF 的第几页 | `navigate_knowledge` | PDF 路径和页码，适合图表、扫描页这类没法靠文字定位的内容 |
| 这份地图靠不靠谱 | `index_status`、`index_failures` | 索引进度，以及哪些文件没收进来、为什么 |

一共 18 个工具，另外还能看笔记的出链入链、找重复笔记、改库的范围和简介、导出导入。地图不会过期：AI 每次搜索前会先检查笔记有没有改动，有的话先同步。AI 也有权限边界：默认只能看到纯文本类文件，PDF、Word 要你先授权；想改库的范围或简介，得先提案、再确认，一句话改不掉。

你自己也能用：桌面窗口里可以搜索、管理库，还有一张全库总览星图——每个库是一根绕中心转的光环，内容相近的文件挨在一起、同类内容同一种颜色（这一页刚换新，还在真机验收中；它只在打开时占用少量显存，离开就全部交还）。PDF、Word 有没有转成文字、页库（按页面找的那套）有没有建好，也能一眼看到：库卡片上一行数字，缺了标黄，点进去能看到每个文件缺在哪、为什么、下一步怎么办，还能打开存缓存的文件夹，里面有一份看得懂的目录（这部分也刚做完，还在真机验收中）。桌面窗口、命令行和 AI 助手用的是同一个大脑。

## 和常见做法有什么不一样

| | 常见做法（翻文件夹、关键词搜索、传云端） | RAG NAVIGATION |
|---|---|---|
| 给 AI 的答案 | 整个文件，或一堆全文 | 哪个文件的哪一节，可以先只要清单 |
| 怎么找到 | 靠文件名，或只认字面关键词 | 按内容匹配：意思和字词两路同时找，合并后再精挑；文件叫什么都不要紧 |
| PDF 和扫描件 | 读不进去，或只收有文字的页 | 可接 OCR，另有按页面找的独立系统 |
| 笔记去向 | 常常要上传 | 全在本机（云端 OCR 是默认关闭的例外） |
| AI 的权限 | 有什么读什么 | 默认只读纯文本，改动要确认 |

## 快速开始

需要 Windows 10 或 11（Linux 也能跑，主要用于开发）。第一次使用要联网下载模型，建议预留 10GB 以上磁盘。显卡不是必需的，有 NVIDIA 显卡会快很多。

### 1. 装好

把仓库拉下来，自己装，目前只有这一种装法。需要先装好 [Python 3.14](https://www.python.org/downloads/)（开发和测试用的版本，其他版本没有验证）和 [Git](https://git-scm.com/)。

```powershell
git clone https://github.com/xbl2602/rag-redo.git
cd rag-redo
python -m venv .venv
.venv\Scripts\python.exe -m pip install jieba chromadb pymupdf4llm python-docx pywebview mcp datasketch
.venv\Scripts\python.exe -m pip install torch sentence-transformers
.venv\Scripts\python.exe gui_main.py
```

有 NVIDIA 显卡的话，倒数第二行换成 `... install torch sentence-transformers --extra-index-url https://download.pytorch.org/whl/cu128`，更快。Linux 下把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

**更新**：进到仓库文件夹，执行 `git pull` 拉最新代码，再把上面两行 `pip install` 重新跑一遍（依赖没变的话很快就结束）。你的索引和设置都在 `data` 文件夹里，不会被覆盖。

### 2. 建一个库

仓库里带了一个很小的 `demo-vault`，用它试一遍：

1. 进入“库”页，点“添加库”，“文件夹路径”填 `demo-vault` 的完整路径。
2. 进入“索引”页，点“增量重建”。第一次要下载模型，要等几分钟；之后每次只处理有变动的文件。
3. 进入“检索”页，搜一句话试试，比如“为什么两种分数不能直接相加”。

### 3. 让 AI 用上它

它是标准的 MCP 服务，Claude Code、opencode 等支持 MCP 的工具都能接。把下面这段放进 AI 工具的 MCP 配置（路径换成你自己的）：

```json
{
  "mcpServers": {
    "rag-navigation": {
      "command": "C:\\path\\to\\rag-redo\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\rag-redo\\mcp_stdio.py"]
    }
  }
}
```

Claude Code 也可以一行搞定：`claude mcp add rag-navigation -- C:\path\to\rag-redo\.venv\Scripts\python.exe C:\path\to\rag-redo\mcp_stdio.py`。

接好后，直接对 AI 说“去我的笔记里查一下……”就行。

## 它是怎么工作的

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/02-indexing-dark.gif" />
    <img alt="建索引流程：挑文件、读内容、切块、向量化和关键词、一次性切换新版索引" src="docs/media/02-indexing-light.gif" width="760" />
  </picture>
</p>

*先建地图：把笔记读一遍，做成按意思和按字词两份目录。没改动的文件跳过；带图的 PDF 整本一起处理，不会只收文字页；全部做完才一次性换成新版，中途崩了也不会留下半成品。*

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/03-search-dark.gif" />
    <img alt="一次搜索的流程：先看笔记有没有变，两路查找、合并、重排、整理、返回，另有独立的看图找页面" src="docs/media/03-search-light.gif" width="760" />
  </picture>
</p>

*再按地图找：两路同时找，合并名次，重排精挑，同一个文件不会刷屏。“按页面找 PDF”是独立的第二套系统，不和文字结果混着排名。*

## 像搭积木一样的零件

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/04-plugins-dark.gif" />
    <img alt="插件装配：核心只有插件运行时和数据流管理器两样，其余都是可换的零件" src="docs/media/04-plugins-light.gif" width="760" />
  </picture>
</p>

*核心只有两样，插件运行时管零件本身，数据流管理器管数据怎么流。其余全是插件，目前 21 个官方插件；较重的部分（看图、本机 OCR）各自跑在独立的进程里。*

目前每个环节官方只带一套默认实现，想换模型得自己写插件，最小的例子在 `examples/hello-plugin`。零件之间的沙箱隔离还在规划中，现在的插件是“受信任的”，装什么就信什么。

## 常见问题

**需要装 Obsidian 吗？** 不需要，它只读文件夹里的文件。用 Obsidian 的话，`[[双链]]` 会被识别出来。

**我的笔记会被上传吗？** 默认不会，读文件、算向量、建索引、搜索都在本机。有三点例外：第一次使用时模型要从网上下载；把扫描件识别切到“MinerU 云端”，对应的文件会上传给这项服务（默认关闭，选本机识别则不出内网）；给 HyDE 或库简介配置了语言模型，请求会发到你填的那个地址。

**没有显卡能用吗？** 能。文字检索用 CPU 就能跑，只是第一次建索引会慢。看图导航建议有 NVIDIA 显卡（约需 5.5GB 空闲显存），没有的话在设置里关掉，不影响文字检索。

**PDF 是扫描件，搜不到？** 扫描件识别默认关闭，这类文件会被标为“扫描件，这次没收”，在“诊断”页能看到。到设置的“PDF 与云端 OCR”里打开后，下一轮会自动补上。云端识别需要 MinerU 的 API Key；本机识别需要先装 `mineru` 环境（`uv tool install --python 3.12 -U "mineru[all]"`）。

**PDF 很多，第一次转文字特别慢、CPU 一直满载？** 有文字层的 PDF 默认按“自动”方式转：超过 200 页的大文件用快速的规则式转换，其余逐页做 AI 版面分析（结构最准，但每页约 0.3 秒、会占满 CPU）。在设置的“PDF 与云端 OCR”里可以改成全部精细或全部快速，只影响之后新转换的 PDF，已经转好的不会重转（这项刚做完，还在真机验收中）。

**数据放在哪？怎么卸载？** 在仓库下的 `data` 文件夹。模型默认放在程序目录的 `models` 文件夹，可在设置里改到别的盘。它不改系统的 Python 和环境变量，卸载就是删掉程序文件夹和数据文件夹。

## 项目现状

能用的：文字检索的完整链路、21 个官方插件、桌面窗口 / 命令行 / MCP 三个入口、看图导航、扫描件 OCR、库简介、重复笔记检测、导出导入。核心功能都带自动测试，测试全程用假模型，不用下载模型就能跑。

还在打磨的：桌面界面的部分细节仍在逐项对齐；本机 OCR 要自己先装 `mineru`。

## 更多

设计文档：[架构总览](docs/ARCHITECTURE.md)、[数据流规则](docs/DATA_FLOW.md)、[插件规范](docs/PLUGIN_SPEC.md)、[路线图](docs/ROADMAP.md)；另有 7 张[流程图](docs/DIAGRAMS.md)。跑测试：`.venv\Scripts\python.exe tests\run.py`。

这个项目的原则是：已经对用户承诺的行为不随意改变，每一处都登记在 [behavior_contract.json](docs/behavior_contract.json) 里。参与维护或新增功能之前，请先看 [AGENTS.md](AGENTS.md)。它由 [obsidian-rag](https://github.com/xbl2602/obsidian-rag) 重构而来，旧项目已归档，不再维护。

许可证：[MIT](LICENSE) © 2026 xbl2602
