"""MCP 工具定义：把 Pipeline / library-manager 的能力包装成 MCP tool。

只定义"给定 server/pipeline/lib_mgr，注册哪些工具"，不关心 stdio/http
传输——那是仓库根目录 mcp_stdio.py（真正的可执行入口）的事。这个模块可以
在不启动任何 stdio 服务的情况下被直接测试（见 ../tests/test_tools.py）。
"""
from __future__ import annotations

import base64
from typing import Any

from core.pipeline import DEFAULT_CONFIDENCE_WARN_THRESHOLD, Pipeline, confidence_tier


def register_tools(server, pipeline: Pipeline, lib_mgr) -> None:
    @server.tool()
    def search_knowledge(
        query: str,
        top_k: int = 5,
        libraries: str = "",
        exclude: str = "",
        folder: str = "",
        include_body: bool = True,
    ) -> dict[str, Any]:
        """语义搜索知识库（混合检索：向量语义 + 关键词 + 重排），返回最相关的片段。

        库选择（先调 list_libraries 查看可用库名，对齐 obsidian-rag/
        retriever.py::hybrid_search 的选库语法）：libraries 为空="全部已
        注册库"；"all"=全部库；"A,B"=多库并查（跨库统一重排，不是简单
        拼接——见 core/pipeline.py::search 的说明）；exclude="B"=全部库
        排除B（反选）；最终范围=(libraries非空?libraries:全部库)−exclude，
        未知库名会报错并列出可用库。folder 可按库内子目录过滤（须是完整
        目录名，比如"docs"能匹配"docs/x.md"但不匹配"docs2/x.md"）。

        置信度是重排器给出的、跨查询可比的校准概率（0~1，0.5=无法判断，
        <0.30弱相关/0.30~0.75中相关/≥0.75高相关）——不是"这一批结果内部
        排出来的相对名次"，同一次查询内分数越高越相关，但不同查询之间的
        分数不能直接比较优劣。

        include_body=False 时只返回来源清单（路径/标题/库id，无正文），
        用于两阶段检索：先低成本枚举全量候选，再对命中少数用 read_document
        精读——省去把大段无关正文传回来的token开销。

        实测过：MCP SDK（本项目锁定版本 2.2.0）的工具函数里裸抛异常
        （比如库名打错触发的 ValueError）不会被自动折叠成一个干净的
        is_error 结果——会原样往上炸。AI agent 调用这类工具时打错参数
        是完全正常会发生的事，不能让它变成服务端异常，所以这里显式
        try/except，把"库不存在"这类可预期的失败折叠成 {"ok": False,
        "error": ...} 返回，绝不裸抛（同 official-gui-shell 的 Api 类
        用的是一模一样的防御模式，两边都是"给同一个 Pipeline 包一层协议
        外壳"，错误处理纪律也该一致）。

        Args:
            query: 查询文本
            top_k: 最多返回几条结果
            libraries: 库范围，逗号分隔的库id列表，空="全部库"，"all"=全部库
            exclude: 要排除的库id，逗号分隔
            folder: 按库内子目录过滤，留空=不过滤
            include_body: False 时只返回来源清单不含正文
        """
        try:
            results = pipeline.search(libraries, query, top_k=top_k, exclude=exclude, folder=folder)
        except Exception as exc:  # noqa: BLE001 - 见上方 docstring
            return {"ok": False, "error": str(exc)}
        warn_threshold = pipeline.runtime.settings.get(
            "confidence_warn_threshold", DEFAULT_CONFIDENCE_WARN_THRESHOLD
        )
        return {
            "ok": True,
            "results": [
                {
                    "library_id": r.library_id,
                    "path": r.path,
                    "heading": r.heading_breadcrumb,
                    **({"text": r.text} if include_body else {}),
                    "confidence": round(r.confidence, 3),
                    "confidence_tier": confidence_tier(r.confidence, warn_threshold),
                    **({"note": f"低置信度 {r.confidence:.2f}，仅供参考"} if r.confidence < warn_threshold else {}),
                }
                for r in results
            ],
        }

    @server.tool()
    def navigate_knowledge(query: str, library_id: str, top_k: int = 5) -> dict[str, Any]:
        """页级视觉导航——独立于 search_knowledge 的"第二套检索"，把 PDF
        每一页渲染成图直接"看图"匹配，不依赖文字提取/OCR，扫描件、图表、
        公式密集的页面也能按页定位。用法建议：先用 search_knowledge 找
        文字线索，遇到"要看图/表格/扫描页"的情况再调这个工具按页定位到
        具体 PDF+页码，自己去看 abs_path 指向的原文件（本工具不返回图片
        本身）。

        调查过旧项目 obsidian-rag 的 navigate_knowledge/wemm_retriever.py
        后确认：这条检索路径从来不与 search_knowledge 的 BM25+向量+RRF
        融合排序发生任何关系（不混向量空间、不混分数），所以这里的
        confidence/score 量纲和 search_knowledge 的 confidence 不可比，
        不要拿两边的分数互相排序。异常处理策略同 search_knowledge：绝不
        裸抛，折叠成 {"ok": False, "error": ...}。

        Args:
            query: 查询文本
            library_id: 要搜索的库的 id（用 list_libraries 查看有哪些库）
            top_k: 最多返回几条结果
        """
        try:
            hits = pipeline.navigate(library_id, query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "results": [
                {
                    "path": h.path,
                    "abs_path": h.abs_path,
                    "page": h.page_index + 1,  # 对外展示用1-based页码，符合人类阅读习惯
                    "score": h.score,
                }
                for h in hits
            ],
        }

    @server.tool()
    def wemm_status() -> dict[str, Any]:
        """WEMM 页级视觉导航状态诊断（对齐 obsidian-rag 的 `wemm_status`
        工具）：子进程是否存活、各库已建的页级索引规模（几个PDF、几页
        向量）——用这个一眼确认"WEMM 到底能不能用"，不用靠猜。只读，
        不会拉起子进程、不会加载模型。

        `official-visual-wemm` 插件未启用时 `providers` 为空字典，不是
        错误——同 `navigate_knowledge` 未装该插件时"空结果不是失败"的
        语义一致。
        """
        try:
            status = pipeline.visual_status()
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "providers": status}

    @server.tool()
    def read_document(library_id: str, path: str) -> dict[str, Any]:
        """读取某文档的完整正文（对齐 obsidian-rag 的 `read_document` 工具）：
        检索命中后想通读全文时用，不是 search_knowledge 的替代品——没有
        query，没有 confidence，正文不截断不封顶。

        `.md`/`.txt` 直接现读源文件；pdf/docx 等需要提取的格式读上一次
        `reindex_knowledge` 索引时留下的提取结果，不会现场重新提取（尤其
        本机OCR代价很高，精读一篇已索引文档不该悄悄触发一次重扫描）——
        如果这个文件还没被成功索引过，会明确报错提示先 reindex_knowledge。
        已被用户排除出检索范围的文件拒绝读取（那类文件对整个 RAG 流程
        "不存在"，同其他工具的排除语义一致）。

        Args:
            library_id: 文档所在库的 id（用 list_libraries 查看有哪些库）
            path: 库内相对路径（用 search_knowledge 的来源行/list_libraries 确认）
        """
        try:
            doc = pipeline.read_document(library_id, path)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": doc.path, "text": doc.text, "source": doc.source, "chars": len(doc.text)}

    @server.tool()
    def find_duplicates(library_id: str, threshold: float = 0.7) -> dict[str, Any]:
        """近似重复文档检测（只读建议，绝不删除/移动文件）——对齐
        obsidian-rag 的 `find_duplicates` 工具：找出库内"内容几乎相同"
        的重复文档（同一课件多份拷贝、同一文档转出的多个副本），返回
        重复组，由你决定是否清理/合并，避免检索反复命中同一段内容。

        比较的是提取出的文字内容（文本级 MinHash+LSH，不是语义相似度），
        只统计已经被成功索引过的文件——还没索引过的文件不参与比较，
        先 reindex_knowledge 建好索引再调用本工具才有意义。

        Args:
            library_id: 要检测的库的 id
            threshold: 相似度阈值（0~1，默认0.7），越高越严格，只有真正
                       "近乎逐字重复"的才会被分进同一组
        """
        try:
            groups_by_provider = pipeline.find_duplicates(library_id, threshold=threshold)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "groups": groups_by_provider}

    @server.tool()
    def note_relations(library_id: str, path: str) -> dict[str, Any]:
        """查询某篇笔记的双链关系（出链=本文链接到谁、入链=谁链接到本文），
        基于 Obsidian `[[wiki链接]]` 语法（对齐 obsidian-rag 的
        `note_relations` 工具）。这是独立于 search_knowledge 的关系查询，
        不参与语义检索排序，用于在搜到一篇笔记后"顺着链接找相关笔记"。

        `path`：笔记的库内相对路径（如 "20-Projects/机器.md"）或不含扩展名
        的标题（如 "机器"，Obsidian 双链引用同款写法）——先按路径精确匹配，
        找不到再按标题匹配，标题在库内重名时任取其一，与 Obsidian 自身
        处理同名笔记的方式一样存在歧义。库从没建过索引、或指定的笔记
        不存在/无法解析，都返回 `resolved=False`（不是错误）。

        Args:
            library_id: 笔记所在库的 id（用 list_libraries 查看有哪些库）
            path: 库内相对路径，或不含扩展名的标题
        """
        try:
            result = pipeline.note_relations(library_id, path)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}

    @server.tool()
    def list_libraries() -> list[dict]:
        """列出所有已注册的库及其基本信息，含每个库的简介（导航/澄清性质
        的一段话，帮你在真正检索/通读全文之前先判断"这个库值不值得往这
        查"——不是检索结果的替代品）。没有简介的库不代表没内容，只是还
        没生成过；如果用户明确要求，先调用 get_library_sample 采样，自己
        写一段，再用 propose_library_summary 提交。

        library_summary 是可选插件——关掉它之后 list_libraries 仍应正常
        工作，只是每条结果的 summary 字段一律是 None（同 AGENTS.md"关掉
        任意一个非必需插件，核心+MCP仍能正常工作"这条既有验收标准，不能
        因为新增了库摘要功能就让这条退化）。
        """
        summary_available = pipeline.runtime.registry.active_of("library_summary") is not None
        rows = []
        for cfg in lib_mgr.store.list_libraries():
            summary_text = pipeline.get_library_summary(cfg.library_id).text if summary_available else ""
            rows.append(
                {
                    "library_id": cfg.library_id,
                    "name": cfg.name,
                    "root_path": cfg.root_path,
                    "summary": summary_text or None,
                }
            )
        return rows

    @server.tool()
    def get_selection(library_id: str) -> dict[str, Any]:
        """查看某库的路径级勾选状态（只读，对齐 obsidian-rag 的
        `get_selection` 工具）：显式纳入/排除清单——用于提议变更前了解
        现状。未列出的文件是"中性"，按格式开关与库默认策略
        （`new_file_default`）判定，不在这两份清单里。

        Args:
            library_id: 要查看的库的 id
        """
        try:
            result = lib_mgr.get_selection(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}

    @server.tool()
    def propose_selection_changes(library_id: str, changes: list[dict]) -> dict[str, Any]:
        """提议库内路径级勾选变更（对齐 obsidian-rag 的
        `propose_selection_changes` 工具）：`changes` 是一批
        `{"path": "库内相对路径", "action": "in"|"out"|"neutral"}`——
        `in`=显式纳入，`out`=显式排除，`neutral`=撤回显式规则、恢复跟随
        库的格式开关/默认策略判定。被排除的文件会从整个检索流程中消失
        （不提取、不嵌入、不建页级导航）。

        ⚠ 硬性确认门禁：本工具绝不直接生效，只生成一份待确认提案并返回
        变更清单、提案号与6位确认码；你必须把变更清单完整展示给用户、
        得到用户明确同意后，才能携带 proposal_id 与确认码调用
        apply_selection_changes。未经用户同意就调用 apply 是严重违规。
        提案10分钟后过期。

        Args:
            library_id: 要变更的库的 id
            changes: 变更列表，每项 {"path": ..., "action": "in"|"out"|"neutral"}
        """
        try:
            result = lib_mgr.propose_selection_changes(library_id, changes)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return result

    @server.tool()
    def apply_selection_changes(library_id: str, proposal_id: str, confirmation_code: str) -> dict[str, Any]:
        """应用已获用户确认的路径级勾选变更提案（对齐 obsidian-rag 的
        `apply_selection_changes` 工具）。只有 propose_selection_changes
        返回的提案号 + 用户看到的确认码二者匹配、且未过期（10分钟）时才
        会生效——硬编码门禁，无任何配置可绕过。生效后下一轮
        reindex_knowledge 自动应用新的勾选状态。

        Args:
            library_id: 目标库的 id
            proposal_id: propose_selection_changes 返回的提案号
            confirmation_code: 用户确认后提供的6位数字确认码
        """
        try:
            result = lib_mgr.apply_selection_changes(library_id, proposal_id, confirmation_code)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return result

    @server.tool()
    def reindex_knowledge(library_id: str) -> dict[str, Any]:
        """后台重建索引，立即返回（对齐 obsidian-rag 的 `reindex_knowledge`
        "后台执行+立即返回"语义，2026-09-23 全面功能审计发现的缺口——
        此前是完全同步阻塞，大库重建索引会让这次工具调用一直卡到全部
        跑完才返回，AI 客户端也可能在这期间等到协议超时）。

        真正想知道跑得怎么样——是不是还在跑、跑到第几个文件、有没有
        卡死、最终成功/失败了几个文件——调 `index_status(library_id)`
        轮询，本工具的返回值里没有这些信息（这是设计使然，不是遗漏：
        任务这时候大概率还没跑完）。

        同一个库同一时刻只允许一个后台索引任务：重复调用会返回
        `started=False`，不是错误，也不会打断正在跑的那个任务——不同
        库互不影响，可以同时各自跑一个。

        注：MCP SDK 对裸 `dict` 返回类型标注推不出结构化输出 schema
        （实测 `structured_content` 会是 None，只能从 content[0].text
        里解析JSON），必须写成 `dict[str, Any]` 这种带参数的形式——这是
        真实踩过的坑，不是随手加的类型标注。异常处理策略同
        search_knowledge，见其 docstring。

        Phase 1 现状是全量重跑（不做"内容没变就跳过"的增量判断——虽然
        ExtractedDocument 已经带 content_hash，真正的增量跳过逻辑要等
        docs/LESSONS.md 第3条的版本号机制落地后再接，这里先如实说明，
        不假装已经支持增量）。

        Args:
            library_id: 要重建索引的库的 id
        """
        try:
            started, message = pipeline.start_index_library(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "started": started, "message": message}

    @server.tool()
    def index_status(library_id: str) -> dict[str, Any]:
        """查询后台索引进度（对齐 obsidian-rag 的 `index_status` 工具，
        适配成按库查询——本项目的 reindex_knowledge 本来就是单库粒度，
        不像 obsidian-rag 那样一次调用可能触及多个库）。配合
        reindex_knowledge 轮询用：`status.stage` 是 running/done/failed，
        `status.health` 是心跳/进度健康判定（healthy/stalled_no_heartbeat/
        stalled_no_progress），从没跑过（本进程视角的）后台索引时
        `status` 为 `None`（不是错误——调用方自己决定"从没跑过"要怎么
        展示，同一般"查询不存在的资源返回空而不是报错"的约定）。

        Args:
            library_id: 要查询的库的 id
        """
        try:
            status = pipeline.index_status(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "status": status}

    @server.tool()
    def index_failures(library_id: str) -> dict[str, Any]:
        """索引失败溯源（只读诊断，对齐 obsidian-rag 的 `index_failures`
        工具）：列出最近一次 reindex_knowledge 跑完后，库内"没转成/没
        索引上"的文件及原因——解决"什么文件转不到、为什么"，你看一眼就
        知道哪份文档没进库、卡在哪。只读，绝不触发重新索引或模型加载。

        简化说明：rag-redo 每次都是全量重跑（不做增量索引），本工具因此
        不判断"下一轮会不会自动重试"——反正下一轮本来就会重新试一遍全部
        文件，这个问题在 rag-redo 里不成立，见 core/index_failures.py
        模块 docstring。库存在但从没索引过时 `failures` 为 `None`
        （不是错误）；`failures` 为空列表则表示上次全部成功。

        Args:
            library_id: 要查询的库的 id
        """
        try:
            result = pipeline.index_failures(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}

    @server.tool()
    def export_library(library_id: str) -> dict[str, Any]:
        """导出一个库的完整已建索引数据（配置+向量+BM25状态）为可移植归档，
        base64 编码后返回——调用方把它存成一个 .zip 文件，就能把这个库搬到
        另一台机器，用 import_library 恢复，不需要重新跑一遍索引。

        异常处理策略同 search_knowledge/reindex_knowledge：绝不裸抛，折叠
        成 {"ok": False, "error": ...}。

        Args:
            library_id: 要导出的库的 id
        """
        try:
            archive_bytes = pipeline.export_library(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "library_id": library_id,
            "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
        }

    @server.tool()
    def import_library(archive_base64: str, root_path: str, library_id: str = "") -> dict[str, Any]:
        """从 export_library 产出的归档恢复一个库，不重新索引。

        root_path 必填——归档里不带原始机器上的路径（那个路径在新机器上
        通常没有意义），必须显式告诉这台机器"这些笔记文件现在在哪"，见
        core/pipeline.py 的 import_library 说明。library_id 留空（默认值
        ""）则沿用归档里记录的原始 library_id；如果目标 id 已经存在，会
        报错而不是覆盖——需要覆盖的话，先手动删除旧库。

        Args:
            archive_base64: export_library 返回的 archive_base64 字段内容
            root_path: 这些笔记文件在这台机器上的真实目录路径
            library_id: 恢复出的库用哪个 id；留空则沿用归档里的原始 id
        """
        try:
            archive_bytes = base64.b64decode(archive_base64)
            new_id = pipeline.import_library(archive_bytes, root_path=root_path, library_id=library_id or None)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "library_id": new_id}

    # -----------------------------------------------------------------
    # 库简介（Phase 3）：导航/澄清性质的一段话，帮你在真正检索/通读全文
    # 之前先判断"这个库值不值得往这查"。生成绝不自动触发——只能由用户在
    # GUI 点"刷新简介"，或在对话里向你明确提出请求后，你才调用下面这两个
    # 写入工具。覆盖保护复用核心 write_gate 两段式确认（数据面在
    # official-library-summary 插件）：库简介若是用户手写的，你想改写
    # 必须先 propose 拿到确认码、讲给用户听、得到明确同意后才能 apply；
    # 库简介若从没被人手写过，propose 会直接生效。
    # -----------------------------------------------------------------

    @server.tool()
    def get_library_sample(library_id: str, k: int = 20) -> dict[str, Any]:
        """只读：从某库已建索引的内容里采样出一批有代表性的片段（最远点
        采样，覆盖库内语义空间的分散区域），供你自己组织语言写一段库简介。

        ⚠ 仅在用户明确要求你生成/更新某库的简介时才调用本工具——不要在
        检索问答过程中顺手调用，那不是本工具的用途。

        看完采样后写简介必须遵守（这不是建议，是硬约束）——必须同时做到
        ①②两件事，只写主题范围而不给判断依据、或反过来，都不合格：
        ① 先一两句给总体定位（这库大致是什么性质/服务于什么），再概括
          库内主要覆盖哪几类主题或板块（口语化提及即可，不要用编号/项目
          符号罗列成清单）；
        ② 接着明确写清楚"适合来这库查什么类型的问题"、以及"大概率查不到
          什么"（正反两面都要有）——直接服务于"值不值得往这查"这个决策，
          而不是把主题范围甩给对方自己去猜；
        ③ 禁止逐字摘抄下面给的片段原文，必须用你自己的话概括转写；
        ④ 不点名任何一篇具体笔记的细节，只讲库整体范围；
        ⑤ 100~300 字，一段话，不用 markdown、不分点、不用标题；
        ⑥ 写好后调用 propose_library_summary(library_id, text) 提交，
          不要自己把文本回复给用户就结束——那样不会真正写入。

        Args:
            library_id: 要采样的库的 id
            k: 采样代表片段数
        """
        try:
            samples = pipeline.sample_library(library_id, k=k)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        if not samples:
            return {"ok": False, "error": f"库「{library_id}」尚未建索引或索引为空，无法采样。先调用 reindex_knowledge 建好索引再重试。"}
        return {
            "ok": True,
            "samples": [{"path": s.path, "heading": s.heading, "text": s.text} for s in samples],
        }

    @server.tool()
    def propose_library_summary(library_id: str, text: str) -> dict[str, Any]:
        """提交一段库简介（100~300 字，导航/澄清性质，见 get_library_sample
        的写作约束）。

        行为分两种情况，你不用自己判断走哪条——本工具会自动处理：
        - 该库简介此前是空白或由 AI 生成的：直接写入生效，返回确认信息
          （applied=True）。
        - 该库简介是用户手写的：不会直接覆盖，而是生成一份待确认提案
          （applied=False，含 proposal_id + 6 位数字确认码），你必须把
          新简介完整展示给用户，得到用户明确同意后，携带提案号与确认码
          调用 apply_library_summary 才会真正生效。未经用户同意就调用
          apply 是严重违规。

        Args:
            library_id: 要更新简介的库的 id
            text: 新的简介文本
        """
        try:
            result = pipeline.propose_library_summary(library_id, text)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return result

    @server.tool()
    def apply_library_summary(library_id: str, proposal_id: str, confirmation_code: str) -> dict[str, Any]:
        """应用已获用户确认的库简介覆盖提案。只有 propose_library_summary
        返回的提案号 + 用户看到的确认码二者匹配、且未过期（10 分钟）时才
        会生效——这是硬编码门禁，无任何配置可绕过。

        Args:
            library_id: 目标库的 id
            proposal_id: propose_library_summary 返回的提案号
            confirmation_code: 用户确认后提供的 6 位数字确认码
        """
        try:
            result = pipeline.apply_library_summary(library_id, proposal_id, confirmation_code)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return result
