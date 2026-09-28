/* ============================================================
   mock.js · 契约假实现（演示模式）
   - 与 contracts.md v1 逐字对齐：实现全部方法
   - window.pywebview 不存在时自注入，body.mock 显示演示角标
   - 1 秒 snapshot 推送、log/preview 推送，与真桥同构
   ============================================================ */
(function () {
  'use strict';
  window.__RAG_MOCK = window.__RAG_MOCK || { loaded: false };
  if (window.__RAG_MOCK.loaded) return;
  window.__RAG_MOCK.loaded = true;

  /* ---------- 工具 ---------- */
  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function jstr(v) { return JSON.stringify(v); }

  /* ---------- 基础假数据：库 ---------- */
  var LIBS = [
    { name: '技术笔记', path: 'D:\\Vault\\技术笔记', collection: 'tech_notes', state: 'ok', files: 1284, chunks: 5731,
      last_indexed: 1788000000.0, overrides: 'chunk_char_limit=600', issues: { scanned: 3 },
      summary: { text: '以 Obsidian RAG 项目自身的开发笔记为主，涵盖索引流水线、混合检索排序、'
        + 'GPU 显存仲裁与 MCP 工具设计等工程实现细节。', source: 'ai', updated_at: 1787950000.0,
        fingerprint: 'demo1', model: 'qwen2.5-3b-instruct' } },
    { name: '论文阅读', path: 'E:\\reading\\papers', collection: 'paper_reading', state: 'stale', files: 412, chunks: 1930,
      last_indexed: 1787900000.0, overrides: 'extensions=md,pdf', issues: { scanned: 5 },
      summary: { text: '', source: 'none', updated_at: null, fingerprint: null, model: null } },
    { name: '会议记录', path: 'C:\\Notes\\会议记录', collection: 'meeting_notes', state: 'ok', files: 198, chunks: 645,
      last_indexed: 1788050000.0, overrides: '', issues: {},
      summary: { text: '团队周会与项目评审的记录整理，按日期归档。', source: 'user',
        updated_at: 1787800000.0, fingerprint: 'demo3', model: null } }
  ];
  function agg() {
    var f = 0, c = 0;
    LIBS.forEach(function (l) { f += l.files; c += l.chunks; });
    return { files: f, chunks: c };
  }
  var VAULT_FILES_EXTRA = 8;

  /* ---------- 图谱：63 节点 ---------- */
  var G = (function () {
    var mds = [
      // 技术笔记 18
      { k: 'wemm',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/WEMM 设计.md',     theme: 'wemm',   big: true,  chunks: 12, upd: 1788300000, snip: '页级视觉导航整体设计：独立 collection、独立 meta、独立版本号，终态带 xsrc 签名，失败每轮真重试。' },
      { k: 'pvect',   lib: '技术笔记', rel: '20-Projects/Obsidian RAG/页向量检索.md',     theme: 'wemm',               chunks: 6,  upd: 1788200000, snip: '页向量写入独立 collection，查询零写副作用，先页后块两段式返回。' },
      { k: 'rerank',  lib: '技术笔记', rel: '20-Projects/Obsidian RAG/检索与重排.md',     theme: 'general', big: true, chunks: 9, upd: 1787900000, snip: '混合检索粗召回，交叉编码器精排，两段式返回页与块。' },
      { k: 'hybrid',  lib: '技术笔记', rel: '20-Projects/Obsidian RAG/混合检索笔记.md',   theme: 'general',            chunks: 5,  upd: 1785200000, snip: '稠密与稀疏通道加权融合，RRF 与线性打分的实测差异。' },
      { k: 'chroma',  lib: '技术笔记', rel: '20-Projects/Obsidian RAG/Chroma 实践.md',    theme: 'general',            chunks: 7,  upd: 1785600000, snip: '持久化配置、collection 规划与一致性自愈触发条件。' },
      { k: 'chunk',   lib: '技术笔记', rel: '20-Projects/Obsidian RAG/切块策略.md',       theme: 'chunk',              chunks: 6,  upd: 1784800000, snip: '标题感知切块与重叠窗口，切块逻辑变更必须递增 META_VERSION。' },
      { k: 'clean',   lib: '技术笔记', rel: '20-Projects/Obsidian RAG/清洗与折叠.md',     theme: 'chunk',              chunks: 4,  upd: 1785000000, snip: '代码块与超长表格折叠为摘要行，避免长 token 序列稀释语义。' },
      { k: 'embed',   lib: '技术笔记', rel: '20-Projects/Obsidian RAG/嵌入模型对比.md',   theme: 'general',            chunks: 6,  upd: 1785100000, snip: 'BGE-M3 与同规模模型在中文长文档上的召回对比记录。' },
      { k: 'mineru',  lib: '技术笔记', rel: '20-Projects/Obsidian RAG/MinerU 笔记.md',    theme: 'mineru',  big: true, chunks: 11, upd: 1788100000, snip: '云端 API 批量提交、滑动窗口限速与断点簿记机制。' },
      { k: 'ocr',     lib: '技术笔记', rel: '20-Projects/Obsidian RAG/OCR 路由策略.md',   theme: 'mineru',             chunks: 5,  upd: 1788000000, snip: '混合型 PDF 整本按扫描件路由，宁可诚实空缺也不拼接。' },
      { k: 'pdfp',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/PDF 提取踩坑.md',   theme: 'mineru',             chunks: 4,  upd: 1783800000, snip: '文字层缺失、竖排版式与加密文件在提取层的三类坑。' },
      { k: 'gate',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/Agent 门禁与隐私.md', theme: 'config', big: true, chunks: 8,  upd: 1787700000, snip: '未授权二进制文件冻结保留，批准一次长期有效可撤销。' },
      { k: 'conf',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/配置热加载.md',     theme: 'config',             chunks: 3,  upd: 1787500000, snip: '长驻 MCP 进程一律现读配置，不读 import 快照。' },
      { k: 'gui',     lib: '技术笔记', rel: '20-Projects/Obsidian RAG/Flet GUI 笔记.md',  theme: 'general',            chunks: 5,  upd: 1786700000, snip: 'GUI 是零侵入观察者：不加载模型、不直写 Chroma。' },
      { k: 'mcp',     lib: '技术笔记', rel: '20-Projects/Obsidian RAG/MCP server 设计.md', theme: 'general',           chunks: 6,  upd: 1786900000, snip: '工具面设计：search_knowledge 与 note_relations 双入口。' },
      { k: 'diag',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/诊断面板设计.md',   theme: 'general',            chunks: 4,  upd: 1788200000, snip: '失败溯源、页库状态与去重分析的四个面板布局。' },
      { k: 'tasklog', lib: '技术笔记', rel: '20-Projects/Obsidian RAG/TASK_LOG.md',       theme: 'config',  big: true, chunks: 21, upd: 1788400000, snip: '开发史叙事主线，问题 1 至 39 逐条记录设计取舍。' },
      { k: 'unif',    lib: '技术笔记', rel: '20-Projects/Obsidian RAG/统一终态设计.md',   theme: 'config',             chunks: 5,  upd: 1786800000, snip: '一切不产块的文件都落持久化终态，避免每轮误判死循环。' },
      // 论文阅读 7
      { k: 'gsurvey', lib: '论文阅读', rel: '论文笔记/图谱检索 Survey.md',        theme: 'wemm',     chunks: 7,  upd: 1788100000, snip: '把笔记双链当图遍历的检索路线，与页级视觉索引互相印证。' },
      { k: 'cerank',  lib: '论文阅读', rel: '论文笔记/Cross-Encoder 重排.md',     theme: 'general',  chunks: 5,  upd: 1783500000, snip: '查询与文档拼接过评分的重排范式，精度高但只能少量入围。' },
      { k: 'semchunk',lib: '论文阅读', rel: '论文笔记/语义分块研究.md',           theme: 'chunk',    chunks: 6,  upd: 1784900000, snip: '以句向量突变点切分文本的实验，与固定窗口对比。' },
      { k: 'bgem3',   lib: '论文阅读', rel: '论文笔记/BGE-M3 论文.md',            theme: 'general',  chunks: 8,  upd: 1784000000, snip: '多语种多粒度统一嵌入训练框架，稠密稀疏双通道输出。' },
      { k: 'colbert', lib: '论文阅读', rel: '论文笔记/ColBERT 稀疏向量.md',       theme: 'general',  chunks: 5,  upd: 1784100000, snip: '晚交互与词级稀疏表示，精确术语匹配的补充通道。' },
      { k: 'survey',  lib: '论文阅读', rel: '论文笔记/RAG 综述 2025.md',          theme: 'config',   chunks: 9,  upd: 1785900000, snip: '检索增强生成分型：naive、迭代、图增强与 agent 化路线。' },
      { k: 'minhash', lib: '论文阅读', rel: '论文笔记/MinHash 近似去重.md',       theme: 'config',   chunks: 4,  upd: 1786400000, snip: 'MinHash 加 LSH 的近似去重原理与分桶参数选择。' },
      // 会议记录 5
      { k: 'diary',   lib: '会议记录', rel: '会议与日记/2026-08-30 日记.md',  theme: 'daily', chunks: 2, upd: 1788000000, snip: '今天把页级导航原型跑通了，翻 PDF 像翻书一样，就是显存有点紧。' },
      { k: 'weekly',  lib: '会议记录', rel: '会议与日记/周会 2026-09-01.md',  theme: 'daily', chunks: 3, upd: 1788300000, snip: '演示视觉翻页检索，确认 DPI 与空闲卸载显存策略下周落地。' },
      { k: 'wemmmeet',lib: '会议记录', rel: '会议与日记/WEMM 评审会.md',      theme: 'daily', chunks: 3, upd: 1787600000, snip: '评审结论：页库独立 meta 与版本号，失败真重试，改档自动重渲染。' },
      { k: 'retro',   lib: '会议记录', rel: '会议与日记/检索质量复盘.md',     theme: 'daily', chunks: 4, upd: 1787300000, snip: '十个坏例归因：六个出在切块，三个出在重排缺席，一个纯 OCR。' },
      { k: 'perf',    lib: '会议记录', rel: '会议与日记/索引性能评审.md',     theme: 'daily', chunks: 3, upd: 1785800000, snip: '扫描段攒批与线程池并行的吞吐数据，限速闸门符合预期。' }
    ];
    // 11 PDF：6 全生效 / 2 缺 WEMM / 2 未识别 / 1 失败
    var pdfs = [
      { k: 'p1',  lib: '论文阅读', rel: 'E:\\reading\\papers\\ICLR2026_review.pdf',            mineru: 'done',   wemm: 'none',  pages: 0,  theme: 'general', upd: 1785600000, cache: { k: 'c1', chunks: 214 } },
      { k: 'p2',  lib: '论文阅读', rel: 'E:\\reading\\papers\\CS231n 课件 07.pdf',             mineru: 'done',   wemm: 'done',  pages: 28, theme: 'general', upd: 1784700000, cache: { k: 'c2', chunks: 168 } },
      { k: 'p3',  lib: '论文阅读', rel: 'E:\\reading\\papers\\扫描课件 注意力机制.pdf',        mineru: 'done',   wemm: 'done',  pages: 36, theme: 'general', upd: 1782600000, cache: { k: 'c3', chunks: 203 } },
      { k: 'p4',  lib: '论文阅读', rel: 'E:\\reading\\papers\\Transformer 原论文.pdf',         mineru: 'queued', wemm: 'none',  pages: 0,  theme: 'general', upd: 1781900000 },
      { k: 'p5',  lib: '论文阅读', rel: 'E:\\reading\\papers\\扩散模型综述.pdf',               mineru: 'done',   wemm: 'none',  pages: 0,  theme: 'config',  upd: 1785400000, cache: { k: 'c5', chunks: 141 } },
      { k: 'p6',  lib: '论文阅读', rel: 'E:\\reading\\papers\\RAG Survey 2025.pdf',            mineru: 'done',   wemm: 'done',  pages: 24, theme: 'general', upd: 1785700000, cache: { k: 'c6', chunks: 187 } },
      { k: 'p7',  lib: '论文阅读', rel: 'E:\\reading\\papers\\强化学习导论.pdf',               mineru: 'none',   wemm: 'none',  pages: 0,  theme: 'config',  upd: 1781000000 },
      { k: 'p8',  lib: '论文阅读', rel: 'E:\\reading\\papers\\视觉 Transformer 综述.pdf',      mineru: 'done',   wemm: 'done',  pages: 32, theme: 'wemm',    upd: 1787400000, cache: { k: 'c8', chunks: 176 } },
      { k: 'p9',  lib: '技术笔记', rel: 'D:\\work\\docs\\MinerU 使用手册.pdf',                 mineru: 'done',   wemm: 'done',  pages: 20, theme: 'mineru',  upd: 1787600000, cache: { k: 'c9', chunks: 158 } },
      { k: 'p10', lib: '会议记录', rel: 'E:\\reading\\papers\\季度汇报 扫描版.pdf',            mineru: 'done',   wemm: 'done',  pages: 18, theme: 'wemm',    upd: 1788300000, cache: { k: 'c10', chunks: 132 } },
      { k: 'p11', lib: '论文阅读', rel: 'E:\\reading\\papers\\API 限流与重试实践.pdf',         mineru: 'failed', wemm: 'none',  pages: 0,  theme: 'config',  upd: 1787600000, fail: '提交超时（429 限流）' }
    ];
    var pageDefs = [
      { c: 'c3', pgs: [3, 12, 21] }, { c: 'c2', pgs: [1, 9] }, { c: 'c10', pgs: [2, 5, 11] },
      { c: 'c6', pgs: [4, 15] }, { c: 'c8', pgs: [6, 19] }, { c: 'c9', pgs: [5, 13] }
    ];
    var links = [
      ['wemm', 'rerank'], ['wemm', 'mineru'], ['wemm', 'pvect'], ['wemm', 'tasklog'], ['ocr', 'wemm'],
      ['rerank', 'hybrid'], ['rerank', 'cerank'], ['rerank', 'chroma'],
      ['chunk', 'clean'], ['chunk', 'semchunk'],
      ['embed', 'bgem3'],
      ['mineru', 'pdfp'], ['mineru', 'ocr'], ['mineru', 'perf'],
      ['gate', 'conf'], ['gate', 'gui'],
      ['mcp', 'gui'], ['mcp', 'diag'],
      ['tasklog', 'unif'], ['tasklog', 'diag'],
      ['chroma', 'unif'],
      ['wemmmeet', 'weekly']
    ];
    var byK = {};
    mds.forEach(function (m) { byK[m.k] = m; });
    pdfs.forEach(function (p) { byK[p.k] = p; });

    function docId(lib, rel) { return lib + '|' + rel; }
    function baseName(rel) { return rel.split('\\').pop().split('/').pop(); }

    var nodes = [], edges = [], byId = {};
    mds.forEach(function (m) {
      var n = { id: docId(m.lib, m.rel), lib: m.lib, rel: m.rel, type: 'md', chunks: m.chunks,
        updated: m.upd, pipeline: { mineru: 'none', wemm: 'none' }, pages: null, fail_reason: null,
        theme: m.theme, big: !!m.big };
      n._k = m.k; n._snip = m.snip;
      nodes.push(n); byId[n.id] = n;
    });
    pdfs.forEach(function (p) {
      var n = { id: docId(p.lib, p.rel), lib: p.lib, rel: p.rel, type: 'pdf', chunks: 0,
        updated: p.upd, pipeline: { mineru: p.mineru, wemm: p.wemm }, pages: p.pages,
        fail_reason: p.fail || null, theme: p.theme, big: false };
      n._k = p.k;
      nodes.push(n); byId[n.id] = n;
      if (p.cache) {
        var crel = '提取缓存/' + p.lib + '/' + baseName(p.rel).replace(/\.pdf$/i, '.md');
        var c = { id: docId(p.lib, crel), lib: p.lib, rel: crel, type: 'cache', chunks: p.cache.chunks,
          updated: p.upd, pipeline: { mineru: p.mineru, wemm: 'none' }, pages: null, fail_reason: null,
          theme: p.theme, big: false, _owner: n.id };
        c._k = p.cache.k;
        nodes.push(c); byId[c.id] = c;
        edges.push({ a: c.id, b: n.id, kind: 'own' });
      }
    });
    var cacheByK = {};
    nodes.forEach(function (n) { if (n.type === 'cache') cacheByK[n._k] = n; });
    pageDefs.forEach(function (pd) {
      var cache = cacheByK[pd.c];
      if (!cache) return;
      pd.pgs.forEach(function (pg) {
        var n = { id: cache.lib + '|wemm|' + cache.rel.replace(/^提取缓存\/[^/]+\//, '') + '|p' + pg,
          lib: cache.lib, rel: cache.rel, type: 'page', chunks: 0, updated: cache.updated,
          pipeline: { mineru: 'done', wemm: 'done' }, page: pg, pages: null, fail_reason: null,
          theme: cache.theme, big: false, _owner: cache.id };
        nodes.push(n); byId[n.id] = n;
        edges.push({ a: n.id, b: cache.id, kind: 'page' });
      });
    });
    links.forEach(function (lk) {
      var a = byK[lk[0]], b = byK[lk[1]];
      if (a && b) edges.push({ a: docId(a.lib, a.rel), b: docId(b.lib, b.rel), kind: 'link' });
    });
    return { nodes: nodes, edges: edges, byId: byId, byK: byK, docId: docId, baseName: baseName };
  })();

  /* ---------- 双链关系（由 link 边推导） ---------- */
  function relMap() {
    var m = {};
    G.edges.forEach(function (e) {
      if (e.kind !== 'link') return;
      var a = G.byId[e.a], b = G.byId[e.b];
      if (!a || !b || a.type !== 'md' || b.type !== 'md') return;
      (m[a.rel] = m[a.rel] || { out: [], inn: [] }).out.push(b.rel);
      (m[b.rel] = m[b.rel] || { out: [], inn: [] }).inn.push(a.rel);
    });
    return m;
  }

  /* ---------- 库配置 ---------- */
  var GLOBAL_CFG = {
    extensions: ['md', 'pdf', 'docx'], exclude_dirs: ['.obsidian', '.trash'], exclude_files: ['~$*'],
    exclude_patterns: ['draft-*'], chunk_char_limit: 600, short_doc_char_limit: 200, collection: ''
  };
  var LIB_CFG = {
    '技术笔记': { chunk_char_limit: 600 },
    '论文阅读': { extensions: ['md', 'pdf'] },
    '会议记录': {}
  };

  /* ---------- 设置：12 分组 ---------- */
  var SETTINGS = { groups: [{"title": "知识库（全局默认）", "level": "basic", "desc": "本项目是多库架构：真正的库列表与每库配置在工具栏「📚 库管理」（data/libraries.json）。以下两项是单库时代的全局默认，改动不影响任何已注册库。", "fields": [{"key": "vault", "label": "知识库路径（全局默认）", "kind": "str", "hint": "仅作首库自动迁移源与未注册场景兜底；已注册库的路径请用工具栏「📚 库管理」，改这里不影响任何已注册库", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "D:\_STOREROOM\lol\Obsidian Vault"}, {"key": "collection_name", "label": "向量库名（全局默认）", "kind": "str", "hint": "每库 collection 由库名派生或按库覆盖（库管理）；本键只作用于旧单库路径与首库迁移", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "obsidian_kb"}]}, {"title": "模型", "level": "basic", "desc": "本机在用两个模型：bge-m3（嵌入）+ bge-reranker-v2-m3（精排）。候选芯片只是推荐清单，点选后首次使用才会从 HuggingFace 自动下载。", "fields": [{"key": "model_name", "label": "嵌入模型", "kind": "str", "hint": "HuggingFace 模型标识；候选芯片仅为推荐（点选后首次使用自动下载，非本机已装）；换模型 = 换向量空间", "rebuild": true, "secret": false, "choices": [], "suggest": [["BAAI/bge-m3", "推荐 · 中英多语"], ["BAAI/bge-large-zh-v1.5", "中文 · 效果优先"], ["BAAI/bge-small-zh-v1.5", "中文 · 低配轻量"], ["sentence-transformers/all-MiniLM-L6-v2", "英文 · 轻量"]], "value": "BAAI/bge-m3"}, {"key": "rerank_enabled", "label": "两阶段重排开关", "kind": "bool", "hint": "检索两步走：向量+关键词融合先粗筛候选，重排模型再对「查询-块」逐对精排取 top_k；关闭 = 只用粗筛排序", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "true"}, {"key": "rerank_model", "label": "重排模型", "kind": "str", "hint": "做第二步精排的 cross-encoder；本机在用的是默认这个，换别的首次使用下载约 1.1GB", "rebuild": false, "secret": false, "choices": [], "suggest": [["BAAI/bge-reranker-v2-m3", "默认 · 多语"], ["BAAI/bge-reranker-base", "轻量快速"], ["BAAI/bge-reranker-large", "效果更强更慢"]], "value": "BAAI/bge-reranker-v2-m3"}]}, {"title": "PDF 与云端 OCR", "level": "basic", "desc": "扫描件 OCR 与文字层 PDF 的提取后端；切到 mineru-cloud 会上传原始文件。", "fields": [{"key": "pdf_scan_backend", "label": "扫描件 OCR 后端", "kind": "str", "hint": "无文字层 PDF 的处理方式；切换后下轮索引自动重试存量扫描件", "rebuild": false, "secret": false, "choices": [["none", "不做 OCR，扫描件跳过（默认）"], ["mineru-cloud", "MinerU 云端 OCR（上传原始文件）"]], "suggest": [], "value": "mineru-cloud"}, {"key": "pdf_text_backend", "label": "文字层 PDF 后端", "kind": "str", "hint": "有文字层 PDF 的提取方式；云端版面/表格识别更准（is_ocr=False 不重复计费）", "rebuild": false, "secret": false, "choices": [["local", "本地直提（默认，免费快速）"], ["mineru-cloud", "MinerU 结构识别（上传原始文件）"], ["mineru-local", "本地部署模型（占位，尚未实现，自动退化为本地直提）"]], "suggest": [], "value": "mineru-cloud"}, {"key": "mineru_model_version", "label": "MinerU 云端模型版本", "kind": "str", "hint": "仅影响送 MinerU 云端时用哪个模型解析；本地直提不受影响", "rebuild": false, "secret": false, "choices": [["vlm", "视觉语言模型（默认，官方推荐，精度更高）"], ["pipeline", "传统流水线（更快更省配额，精度稍低）"]], "suggest": [], "value": "vlm"}, {"key": "mineru_api_key", "label": "MinerU API Key", "kind": "str", "hint": "mineru.net → 个人中心 → API Token；敏感信息，不进任何日志", "rebuild": false, "secret": true, "choices": [], "suggest": [], "value": ""}, {"key": "mineru_timeout_seconds", "label": "MinerU 超时（秒）", "kind": "int", "hint": "单个扫描件「提交+轮询+下载」的总时间预算", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "600"}, {"key": "mineru_concurrency", "label": "云端并行数", "kind": "int", "hint": "0=最大吞吐（限速闸门自动节流，完成一个补一个）；1=串行；≥2=固定并发。默认保守 3，实测无 429 再调高", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0"}, {"key": "mineru_rate_per_minute", "label": "每分钟提交上限", "kind": "int", "hint": "滑动窗口限速；官方三个提交接口共用 50 个文件/分钟，默认 45 留余量，0=不限", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "45"}]}, {"title": "检索输出", "level": "basic", "desc": "返回内容的形状与低置信护栏，全部实时生效。", "fields": [{"key": "return_chunk_limit", "label": "单块返回字符上限", "kind": "int", "hint": "超出截断并附标记；直接影响回答注入的 token 量", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "2000"}, {"key": "max_chunks_per_file", "label": "同文件最多块数", "kind": "int", "hint": "防单文件霸屏 top_k；想看更多可临时调到 3–5", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "3"}, {"key": "truncate_mark", "label": "截断标记", "kind": "str", "hint": "块被截断时附在末尾的提示文案", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "… [本块已截断，完整内容见源文件]"}, {"key": "default_top_k", "label": "默认返回条数", "kind": "int", "hint": "search_knowledge 未显式传参时的 top_k", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "5"}, {"key": "default_libraries", "label": "默认检索库", "kind": "list", "hint": "逗号分隔库名（须与注册表一致）；留空 = 全部注册库", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "Obsidian Vault"}, {"key": "confidence_warn_threshold", "label": "低置信标注阈值", "kind": "float", "hint": "命中置信度低于此值 → 来源标注「仅供参考」（0~1）", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0.55"}, {"key": "confidence_drop_threshold", "label": "低置信丢弃阈值", "kind": "float", "hint": "低于此值直接不输出该来源，宁缺毋滥（0~1）", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0.4"}]}, {"title": "视觉导航（WEMM）", "level": "basic", "desc": "把 PDF 每页渲染成图，用本机 GPU 的 WeMM 模型做成「每页一向量」导航库，告诉 AI 内容在哪个 PDF 哪页。默认开：看图服务按需自动拉起、空闲自动卸显存并自退出，与 bge-m3 显存互斥（问题41），无需手动管理。", "fields": [{"key": "wemm_backend", "label": "视觉导航开关", "kind": "str", "hint": "WEMM 页级视觉导航后端；开着时导航/页索引会自动拉起看图服务，显存与 bge-m3 互斥自动错峰；建页库命令：python wemm_indexer.py --backend on", "rebuild": false, "secret": false, "choices": [["on", "开启（默认：服务按需自动拉起、用完自动退出）"], ["local", "开启（同 on，兼容旧取值）"], ["off", "关闭（不建视觉库不占显存）"]], "suggest": [], "value": "local"}, {"key": "wemm_url", "label": "看图服务地址", "kind": "str", "hint": "wemm_server.py 本机 HTTP 服务地址（全局 Python 启动，默认 127.0.0.1:9101）；空闲可手动停释放显存", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "http://127.0.0.1:9101"}, {"key": "wemm_python", "label": "全局 Python 路径", "kind": "str", "hint": "拉起 wemm_server.py 用的解释器（须已装 torch/transformers），别填 .venv——项目虚拟环境不装 torch", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "python"}, {"key": "wemm_model", "label": "WEMM 模型", "kind": "str", "hint": "传给看图服务的模型标识，默认 tencent/WeMM-Embedding-2B", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "tencent/WeMM-Embedding-2B"}, {"key": "wemm_dim", "label": "向量维度", "kind": "int", "hint": "matryoshka 截断维度；2B 支持 64/128/256/512/1024/2048，512 质量近满、显存/存储适中", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "512"}, {"key": "wemm_render_dpi", "label": "页图渲染清晰度", "kind": "int", "hint": "页图渲染 DPI；越高越清楚但编码耗时随 DPI 平方暴涨，数百页时 120 会建数小时。改后下一轮页索引自动重渲染，无需 --full", "rebuild": false, "secret": false, "choices": [["40", "40 DPI · 最快（约 0.5s/页，图较糊）"], ["60", "60 DPI · 均衡（默认，约 2.5s/页）"], ["90", "90 DPI · 更清楚（约 13s/页）"], ["120", "120 DPI · 最清楚（约 25s/页，建库很慢）"]], "suggest": [], "value": "60"}]}, {"title": "融合与排序调优", "level": "advanced", "desc": "BM25 / RRF 权重 / 候选池 / 重排预算。拿不准就保持默认。", "fields": [{"key": "bm25_k1", "label": "BM25 k1", "kind": "float", "hint": "词频饱和度，标准值 1.5 一般不动", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "1.5"}, {"key": "bm25_b", "label": "BM25 b", "kind": "float", "hint": "长度归一强度，标准值 0.75 一般不动", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0.75"}, {"key": "fusion_dense_weight", "label": "语义权重（dense）", "kind": "float", "hint": "调大偏语义检索；1.0/1.0 即经典等权 RRF", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "1.0"}, {"key": "fusion_bm25_weight", "label": "关键词权重（bm25）", "kind": "float", "hint": "调大偏关键词/专名检索；两项均实时生效", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "1.0"}, {"key": "dense_candidate_factor", "label": "候选池系数", "kind": "int", "hint": "候选池 = top_k × 此系数，越大越准越慢", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "10"}, {"key": "dense_min_candidates", "label": "候选池下限", "kind": "int", "hint": "候选池保底数量，保证小 top_k 时融合质量", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "200"}, {"key": "rerank_candidates", "label": "重排候选数", "kind": "int", "hint": "送重排的融合候选数，建议 30–80；越大越慢", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "50"}]}, {"title": "切块粒度", "level": "advanced", "desc": "全局默认值，可在「库管理 → 库配置」按库覆盖；改后需 --full 重建。", "fields": [{"key": "chunk_char_limit", "label": "单块最大字符", "kind": "int", "hint": "全局默认，可按库覆盖；建议 400–800（小块检索 + 父节回填路线）", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "600"}, {"key": "short_doc_char_limit", "label": "整篇收录阈值", "kind": "int", "hint": "全局默认，可按库覆盖；正文短于此字符数的笔记不切块、整篇一块", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "200"}, {"key": "small_to_big", "label": "父节回填（small-to-big）", "kind": "bool", "hint": "命中小块时回填父节全文补偿上下文；与小块切块配套，若改回 1200+ 大块应关掉", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "true"}]}, {"title": "排除规则", "level": "advanced", "desc": "全局默认的排除名单：未单独配置的库都继承这里，单个库可在「库管理 → 库配置」覆盖（那里留空 = 用这里的默认）；改后需 --full 重建。", "fields": [{"key": "exclude_dirs", "label": "排除目录", "kind": "list", "hint": "全局默认，可按库覆盖；目录树任一层同名目录整棵跳过，逗号分隔", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": ".obsidian,.smart-env,.trash,.git,TEMP,templates,.opencode,.council-state,clippings-source,90-Archive"}, {"key": "exclude_files", "label": "排除文件名", "kind": "list", "hint": "全局默认，可按库覆盖；精确文件名匹配（任何层级同名都不索引），逗号分隔", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "目录.md,AGENTS.md,LOG.md,README.md,Home.md"}, {"key": "exclude_patterns", "label": "排除文件前缀", "kind": "list", "hint": "全局默认，可按库覆盖；文件名以任一前缀开头即跳过，逗号分隔", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "session-,会话,.tmp,MOC-"}, {"key": "tbd_exclude_ratio", "label": "TBD 占位过滤", "kind": "float", "hint": "[TBD] 行占比 ≥ 此值的半成品文件跳过索引；0 = 关闭；仅全局生效（不可按库覆盖）", "rebuild": true, "secret": false, "choices": [], "suggest": [], "value": "0.1"}]}, {"title": "HyDE 查询增强", "level": "advanced", "desc": "提问用词与笔记差太远导致检索落空时，先让本地 LLM 按问题写一段「假设答案」，拿它去检索（术语更接近笔记原文）。默认关；触发才多花一跳。", "fields": [{"key": "hyde_enabled", "label": "启用 HyDE", "kind": "bool", "hint": "开启后才可能触发；触发时多跑一轮检索 + 一次本地 LLM 调用（不触发零开销）；需 LM Studio 类服务在运行", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "false"}, {"key": "hyde_llm_url", "label": "HyDE 服务地址", "kind": "str", "hint": "OpenAI 兼容接口（如 LM Studio 默认 localhost:1234）", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "http://localhost:1234/v1/chat/completions"}, {"key": "hyde_llm_model", "label": "HyDE 模型名", "kind": "str", "hint": "填本地服务里已加载的模型名（如 qwen2.5-3b-instruct）", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "qwen2.5-3b-instruct"}, {"key": "hyde_min_confidence", "label": "HyDE 触发阈值", "kind": "float", "hint": "首轮 top1 置信度低于此值才触发（0~1）；调大更爱触发", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0.5"}]}, {"title": "性能与硬件", "level": "advanced", "desc": "批次大小与 CUDA 冷却；运行时会自动按显存收紧。", "fields": [{"key": "embed_batch_size", "label": "索引嵌入批次", "kind": "int", "hint": "显存紧张调小；运行时自动收紧，此处为上限", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "8"}, {"key": "encode_batch_size", "label": "查询编码批次", "kind": "int", "hint": "单次编码显存占用，同样自动按显存收紧", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "32"}, {"key": "cuda_cooldown_seconds", "label": "CUDA 冷却秒数", "kind": "int", "hint": "GPU 失败（OOM 等）后的冷却期，避免反复崩", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "30"}]}, {"title": "锁与心跳", "level": "advanced", "desc": "多进程写保护与卡死判定阈值，单机单进程场景无需调整。", "fields": [{"key": "lock_timeout_seconds", "label": "写锁等待上限（秒）", "kind": "int", "hint": "超时报「锁繁忙」明确错误；经常多进程并发才需调大", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "60"}, {"key": "lock_poll_seconds", "label": "锁轮询间隔（秒）", "kind": "float", "hint": "获锁失败后的重试间隔", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "0.5"}, {"key": "heartbeat_interval", "label": "心跳间隔（秒）", "kind": "float", "hint": "索引进度写盘周期；以下两个判定按其倍数联动", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "5.0"}, {"key": "heartbeat_timeout", "label": "心跳停止判定（秒）", "kind": "float", "hint": "超过即判「疑似卡死」，一般保持 3× 心跳间隔", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "15.0"}, {"key": "stall_timeout", "label": "进度停滞判定（秒）", "kind": "float", "hint": "心跳正常但进度不动判卡死，一般保持 5× 心跳间隔；特定阶段（转换/模型加载/写库）有内置宽限", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "25.0"}]}, {"title": "导出 / 导入", "level": "advanced", "desc": "导出包保留策略与导入批量。", "fields": [{"key": "keep_exports", "label": "保留导出包数", "kind": "int", "hint": "data/export 自动清理多余的旧导出包", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "3"}, {"key": "import_upsert_batch", "label": "导入批量", "kind": "int", "hint": "每批 upsert 的块数；内存紧张调小", "rebuild": false, "secret": false, "choices": [], "suggest": [], "value": "500"}]}] };
  var SET_VALUES = {};
  SETTINGS.groups.forEach(function (g) { g.fields.forEach(function (f) { SET_VALUES[f.key] = f.value; }); });
  function settingsPayload() {
    return { groups: SETTINGS.groups, missing_keys: [] };
  }

  /* ---------- 运行时状态 ---------- */
  var prog = { running: false, phase: 'idle', files_done: 0, files_total: 0, chunks_done: 0, chunks_total: 0,
    pct: 0, elapsed: 0, library: '', busy: false, task: 'idle', heartbeat: 'idle', heartbeat_note: null };
  var lastElapsed = 80;
  var idxTimer = null, idxStart = 0, idxFull = false, idxLibs = '', stoppedByUser = false;
  var PHASES = [
    { name: 'scanning', ms: 3000, fw: 0.08 },
    { name: 'converting', ms: 6000, fw: 0.32 },
    { name: 'embedding', ms: 8000, fw: 0.75 },
    { name: 'writing', ms: 4000, fw: 0.95 },
    { name: 'wemm', ms: 3000, fw: 1.0 }
  ];
  function snapshotPayload() {
    var a = agg();
    return {
      libs: LIBS.map(function (l) { return { name: l.name, state: l.state, files: l.files, chunks: l.chunks, path: l.path }; }),
      agg_state: LIBS.some(function (l) { return l.state === 'stale'; }) ? 'stale' : 'ok',
      files: a.files, chunks: a.chunks, vault_files: a.files + VAULT_FILES_EXTRA,
      progress: JSON.parse(JSON.stringify(prog)),
      last_elapsed: lastElapsed,
      issues: [
        { lib: '论文阅读', reason: 'scanned', count: 3, label: '扫描件 PDF', advice: '如已在设置中启用云端 OCR，下轮索引将自动重试。' },
        { lib: '技术笔记', reason: 'unreadable', count: 1, label: '损坏文件', advice: '建议用 Office 重新导出后手动重建。' }
      ],
      wemm: { backend: SET_VALUES.wemm_backend, url: '127.0.0.1:9101' },
      wemm_live: { alive: true, loaded: false, gpu_mem_gb: null },
      gpu: { ok: true, mem_used_mb: 1200, mem_total_mb: 8151, util_pct: 5, power_w: 22 },
      cpu: 12,
      device: { model: SET_VALUES.embedding_model, rerank: SET_VALUES.rerank_model, cuda: true }
    };
  }
  function pushLog(line) { LOG.push(line); window.__push('log', jstr({ lines: [line], cursor: LOG.length })); }
  function pushLogBulk(lines) {
    lines.forEach(function (l) { LOG.push(l); });
    window.__push('log', jstr({ lines: lines, cursor: LOG.length }));
  }

  /* ---------- 日志 ---------- */
  var LOG = [
    '2026-09-06 09:12:01 INFO  gui server 启动 · 单实例守卫通过',
    '2026-09-06 09:12:03 INFO  配置热读完成 · META_VERSION=9 EXTRACT_VERSION=4',
    '2026-09-06 09:12:04 WARNING 库「论文阅读」处于 stale 状态 · 3 个文件有变动',
    '2026-09-06 09:12:05 INFO  wemm_server 已由 gpu_arbiter 拉起 · 127.0.0.1:9101',
    '2026-09-06 09:12:30 INFO  增量索引完成 · 46 文件 · 新增 320 块 · 耗时 80s',
    '2026-09-06 09:15:11 ERROR  E:\\reading\\papers\\API 限流与重试实践.pdf · MinerU 提交超时（429）· 已记失败簿记',
    '2026-09-06 09:15:12 INFO  扫描件 3 个已提交 MinerU · 滑动窗口限速 45/min',
    '2026-09-06 09:20:44 WARNING 显存仲裁 · WEMM 让位 bge-m3（检索优先）'
  ];

  /* ---------- 检索语料 ---------- */
  // 演示用极简 MD→HTML（与真桥 _md_to_html 同语义：转义后套白名单标签）
  function mockMd(md) {
    return String(md || '').split('\n').filter(Boolean).map(function (ln) {
      var s = ln.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      var m = s.match(/^(#{1,3})\s+(.*)$/);
      if (m) return '<h' + m[1].length + '>' + m[2] + '</h' + m[1].length + '>';
      if (/^\s*[-*+]\s+/.test(s)) return '<li>' + s.replace(/^\s*[-*+]\s+/, '') + '</li>';
      s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
      return '<p>' + s + '</p>';
    }).join('');
  }
  var WEMM_RESULTS = [
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/WEMM 设计.md', heading: 'WEMM 页级向量', chunk_idx: 1, chunk_total: 3, confidence: 0.87,
      body: '页级视觉导航把每一页渲染成图片后编码为页面向量，写入独立 collection <collection>.wemm。检索时先页后块两段式返回：页命中给出「第几页」，块命中给出正文。终态与成功条目带 xsrc=wemm:<模型>:<维度>:<DPI> 签名，失败每轮真重试、改档自动重渲染。' },
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/检索与重排.md', heading: '两段式返回', chunk_idx: 2, chunk_total: 4, confidence: 0.74,
      body: '页级召回作为第一阶段粗排，重排器只对入围页面内的块精排。混合检索先由 BM25 与向量两路召回，经 RRF 融合后送交叉编码器；页级维度上则按页相似度直接取 Top 页，再在页内做块级定位。' },
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/MinerU 笔记.md', heading: '按页对齐', chunk_idx: 3, chunk_total: 5, confidence: 0.61,
      body: '扫描件经 MinerU 云端产出完整 MD 后，按页分隔符对齐生成 WEMM 索引所需的页文本。整本按扫描件路由，绝不产出「文字页直提+图片页丢失」的半份拼接内容。' },
    { lib: '会议记录', rel: '会议与日记/2026-08-30 日记.md', heading: '', chunk_idx: 0, chunk_total: 1, confidence: 0.55,
      body: '今天把页级导航原型跑通了，翻 PDF 像翻书一样，就是显存有点紧。WEMM 空闲 5 分钟卸显存、30 分钟自退出，按需自动拉起，配合 GPU 仲裁不会和检索模型打架。' }
  ];
  var GENERIC_POOL = [
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/检索与重排.md', heading: '混合检索', chunk_idx: 1, chunk_total: 4, confidence: 0.78,
      body: '混合检索采用 BM25 关键词通道与向量语义通道并行召回，两路结果经 RRF 融合后再送交叉编码重排器精排。召回追求高 recall，精排用更强的模型把真正相关的块顶到前排。' },
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/切块策略.md', heading: '', chunk_idx: 2, chunk_total: 6, confidence: 0.66,
      body: '切块按标题层级优先、600 字目标长度兜底。清洗阶段折叠代码块与表格为摘要行，避免长 token 序列稀释语义。改切块逻辑必须递增 META_VERSION，让旧块自然过期。' },
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/Agent 门禁与隐私.md', heading: '', chunk_idx: 1, chunk_total: 3, confidence: 0.57,
      body: 'Agent 触发的索引默认只处理文本类格式与已批准的二进制格式；未授权文件被冻结：保留条目与已产块，但每轮索引跳过。批准一次长期有效，可随时撤销。' },
    { lib: '论文阅读', rel: '论文笔记/BGE-M3 论文.md', heading: '', chunk_idx: 1, chunk_total: 4, confidence: 0.49,
      body: 'BGE-M3 是多语种多粒度统一嵌入训练框架，稠密与稀疏双通道输出，中英混合场景召回稳定，是本系统的默认嵌入模型。' }
  ];
  var MINERU_RESULTS = [
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/OCR 路由策略.md', heading: '整本路由', chunk_idx: 1, chunk_total: 3, confidence: 0.83,
      body: '混合型 PDF（任一页无文字层）整本按扫描件路由：云端开则整本送 MinerU vlm 认字产出一份完整 MD；未开则整本落 scanned 终态待 xsrc 自愈，宁可诚实空缺。' },
    { lib: '技术笔记', rel: '20-Projects/Obsidian RAG/MinerU 笔记.md', heading: '并行批量', chunk_idx: 1, chunk_total: 5, confidence: 0.72,
      body: '扫描段 classify_extraction 分流攒批，线程池只并行网络 I/O（mineru_concurrency 默认 3，0=最大吞吐），结果回主线程单线程收口；滑动窗口限速 45 每分钟。' },
    { lib: '会议记录', rel: '会议与日记/索引性能评审.md', heading: '', chunk_idx: 1, chunk_total: 2, confidence: 0.58,
      body: '扫描段攒批与线程池并行的吞吐数据：限速 45/min 下批量提交稳定无 429，断点簿记保证中断任务下轮续接不重复提交。' }
  ];

  /* ---------- 试验台 ---------- */
  var preview = { running: false, done: false, start: 0, timer: null, result: null, path: '', backend: null };
  function previewEmit() { window.__push('preview', jstr({ running: preview.running, done: preview.done })); }
  function previewFinish() {
    preview.running = false; preview.done = true;
    var ext = preview.path.split('.').pop().toLowerCase();
    preview.result = {
      ok: true, error: null, reason: '',
      route: preview.backend === 'mineru-cloud' ? 'ocr:mineru-cloud' : 'local',
      cached: false, elapsed: 5,
      markdown: '# ' + preview.path.split('\\').pop().split('/').pop() + '\n\n> 试验台隔离缓存产物 · 未写入正式索引\n\n'
        + '## 一、页面结构\n\n本文件为 ' + ext.toUpperCase() + ' 格式，经 '
        + (preview.backend === 'mineru-cloud' ? 'MinerU 云端 vlm' : '本地直提') + ' 通道提取。\n\n'
        + '| 区块 | 说明 |\n|---|---|\n| 标题层级 | 按字号映射 h1-h4 |\n| 表格 | 还原为 Markdown 表 |\n| 公式 | 转写为 LaTeX |\n\n'
        + '## 二、正文示例\n\n扫描件页面经视觉模型认字后按阅读顺序重组，`代码片段` 与 **重点** 保留原语义。此结果仅用于评估提取质量。',
      chars: 0,
      rendered_html: '<h2>页面结构</h2><p>本文件经 <b>' + (preview.backend === 'mineru-cloud' ? 'MinerU 云端 vlm' : '本地直提')
        + '</b> 通道提取，标题按字号映射，表格还原为结构化行。</p><table><thead><tr><th>区块</th><th>说明</th></tr></thead><tbody>'
        + '<tr><td>标题层级</td><td>按字号映射 h1-h4</td></tr><tr><td>表格</td><td>还原为 Markdown 表</td></tr>'
        + '<tr><td>公式</td><td>转写为 LaTeX</td></tr></tbody></table><p>扫描件页面经视觉模型认字后按阅读顺序重组，<code>代码片段</code> 与 <b>重点</b> 保留原语义。此结果仅用于评估提取质量。</p>'
    };
    preview.result.chars = preview.result.markdown.length;
    pushLog('试验台提取完成 · ' + preview.path + ' · ' + (preview.backend || 'global') + ' · 隔离缓存');
    previewEmit();
  }

  /* ---------- 库简介批量刷新（问题60，演示逐库推进）---------- */
  var sumref = { running: false, total: 0, done: 0, current: null, results: {}, names: [], force: false, timer: null };
  function sumrefStep() {
    var name = sumref.names[sumref.done];
    var l = LIBS.find(function (x) { return x.name === name; });
    if (!l) {
      sumref.results[name] = { ok: false, error: '库不存在' };
    } else if (l.summary && l.summary.source === 'user' && !sumref.force) {
      sumref.results[name] = { ok: false, needs_confirm: true };
    } else {
      var text = '（演示生成）' + name + '库的内容概括：涵盖若干主题笔记，采样自现有索引块。';
      l.summary = { text: text, source: 'ai', updated_at: Date.now() / 1000,
        fingerprint: 'demo-' + Date.now(), model: 'qwen2.5-3b-instruct' };
      sumref.results[name] = { ok: true, text: text };
    }
    sumref.done++;
    if (sumref.done >= sumref.names.length) {
      sumref.running = false; sumref.current = null;
      pushLog('库简介刷新完成（演示）：' + sumref.names.join('、'));
      return;
    }
    sumref.current = sumref.names[sumref.done];
    sumref.timer = setTimeout(sumrefStep, 900);
  }

  /* ---------- 失败明细 ---------- */
  var FAILS = [
    { lib: '论文阅读', rel: 'E:\\reading\\papers\\API 限流与重试实践.pdf', reason: 'extract-failed', will_retry: true,
      detail: ['[t] MinerU 云端 OCR 失败：HTTP 429 限流，等待 Retry-After 后重试', '[t] 提取失败（extract-failed），记入终态待重试：API 限流与重试实践.pdf'] },
    { lib: '论文阅读', rel: 'E:\\reading\\papers\\Transformer 原论文.pdf', reason: 'scanned', will_retry: true,
      detail: ['[t] PDF 含图片页，当前未启用云端 OCR 后端，已整本跳过：Transformer 原论文.pdf'] },
    { lib: '论文阅读', rel: 'E:\\reading\\papers\\强化学习导论.pdf', reason: 'scanned', will_retry: true },
    { lib: '技术笔记', rel: 'D:\\work\\docs\\损坏的存档.docx', reason: 'unreadable', will_retry: false },
    { lib: '技术笔记', rel: 'Daily\\2026-05-03.md', reason: 'empty', will_retry: false },
    { lib: '会议记录', rel: '会议与日记\\草稿-空.md', reason: 'empty', will_retry: false }
  ];

  /* ---------- 去重 ---------- */
  var DUPS = [
    { a: '20-Projects/Obsidian RAG/切块策略.md', b: '90-Archive/切块笔记 v1.md', sim: 0.91, lib: '技术笔记' },
    { a: '论文笔记/RAG 综述 2025.md', b: '论文笔记/图谱检索 Survey.md', sim: 0.84, lib: '论文阅读' },
    { a: '会议与日记/周会 2026-09-01.md', b: '会议与日记/WEMM 评审会.md', sim: 0.81, lib: '会议记录' }
  ];

  /* ---------- 语义边 ---------- */
  function fakeSim(a, b) {
    var seedStr = a.id < b.id ? a.id + '~' + b.id : b.id + '~' + a.id;
    var h = 0;
    for (var i = 0; i < seedStr.length; i++) { h = ((h * 31 + seedStr.charCodeAt(i)) >>> 0); }
    var base = a.theme === b.theme ? 0.68 : 0.50;
    return Math.min(0.92, base + (h % 100) / 100 * 0.22);
  }

  /* ---------- 索引模拟 ---------- */
  function tickIndex() {
    var t = Date.now() - idxStart;
    var total = PHASES.reduce(function (s, p) { return s + p.ms; }, 0);
    var acc = 0, cur = null, curT = 0;
    for (var i = 0; i < PHASES.length; i++) {
      if (t < acc + PHASES[i].ms) { cur = PHASES[i]; curT = t - acc; break; }
      acc += PHASES[i].ms;
    }
    if (!cur) { // 完成
      prog.running = false; prog.phase = 'done'; prog.pct = 100; prog.task = 'idle';
      prog.files_done = prog.files_total; prog.chunks_done = prog.chunks_total;
      prog.heartbeat = 'done'; prog.heartbeat_note = null;
      lastElapsed = Math.round(total / 1000);
      clearInterval(idxTimer); idxTimer = null;
      pushLog('索引完成 · ' + (idxFull ? '全量' : '增量') + ' · ' + prog.files_total + ' 文件 · ' + prog.chunks_total + ' 块 · 耗时 ' + lastElapsed + 's');
      // 数秒后回到空闲心跳，避免长期停留在 done
      setTimeout(function () {
        if (!prog.running && prog.heartbeat === 'done') {
          prog.phase = 'idle'; prog.heartbeat = 'idle'; prog.heartbeat_note = null; prog.pct = 0;
        }
      }, 5000);
      return;
    }
    prog.phase = cur.name; prog.running = true;
    var frac = (acc + curT * cur.fw) / total; // 粗略权重推进
    prog.pct = Math.min(99, Math.round(frac * 100));
    prog.files_done = Math.round(prog.files_total * frac);
    prog.chunks_done = Math.round(prog.chunks_total * frac);
    prog.elapsed = Math.round(t / 1000);
    // 心跳：转换中段模拟一次 stalled + note
    if (cur.name === 'converting' && curT > 2500 && curT < 4500) {
      prog.heartbeat = 'stalled';
      prog.heartbeat_note = '正在转换大型 PDF（扫描件 OCR 排队中），心跳等待';
    } else {
      prog.heartbeat = 'running';
      prog.heartbeat_note = null;
    }
  }

  /* ---------- 契约实现 ---------- */
  var impl = {
    get_snapshot: function () { return Promise.resolve(snapshotPayload()); },

    list_libraries: function () {
      return Promise.resolve(LIBS.map(function (l) {
        return { name: l.name, path: l.path, collection: l.collection, blocks: l.chunks,
          last_indexed: l.last_indexed, overrides: l.overrides, state: l.state, issues: l.issues,
          summary: l.summary || { text: '', source: 'none', updated_at: null, fingerprint: null, model: null } };
      }));
    },

    set_library_summary: function (name, text) {
      var l = LIBS.find(function (x) { return x.name === name; });
      if (!l) return Promise.resolve({ ok: false, error: '库不存在：' + name });
      l.summary = { text: text, source: 'user', updated_at: Date.now() / 1000,
        fingerprint: l.summary && l.summary.fingerprint, model: null };
      pushLog('手动编辑库简介：' + name);
      return delay(120).then(function () { return { ok: true }; });
    },

    refresh_library_summaries_batch: function (names, force) {
      if (sumref.running) return Promise.resolve({ ok: false, error: '已有简介刷新任务在运行，请等它跑完或稍后再试' });
      var all = LIBS.map(function (l) { return l.name; });
      var targets = (names && names.length ? names : all).filter(function (n) { return all.indexOf(n) >= 0; });
      if (!targets.length) return Promise.resolve({ ok: false, error: '没有可刷新的库' });
      sumref.running = true; sumref.total = targets.length; sumref.done = 0;
      sumref.current = targets[0]; sumref.results = {}; sumref.names = targets; sumref.force = !!force;
      pushLog('库简介刷新已启动（演示）：' + targets.join('、') + (force ? '（强制覆盖手写）' : ''));
      sumref.timer = setTimeout(sumrefStep, 900);
      return Promise.resolve({ ok: true, total: targets.length });
    },

    refresh_library_summaries_poll: function () {
      return Promise.resolve({ running: sumref.running, total: sumref.total, done: sumref.done,
        current: sumref.current, results: JSON.parse(JSON.stringify(sumref.results)) });
    },

    get_library_config: function (name) {
      var ov = LIB_CFG[name] || {};
      var eff = {};
      Object.keys(GLOBAL_CFG).forEach(function (k) { eff[k] = ov[k] !== undefined ? ov[k] : GLOBAL_CFG[k]; });
      return delay(150).then(function () {
        return { effective: eff, overrides: JSON.parse(JSON.stringify(ov)),
          all_keys: ['extensions', 'agent_formats', 'exclude_dirs', 'exclude_files', 'exclude_patterns',
                     'chunk_char_limit', 'short_doc_char_limit', 'collection'] };
      });
    },

    add_library: function (path, name) {
      if (!path || !String(path).trim()) return Promise.resolve({ ok: false, error: '路径不能为空' });
      if (!name || !String(name).trim()) return Promise.resolve({ ok: false, error: '库名不能为空' });
      if (LIBS.some(function (l) { return l.name === name; })) return Promise.resolve({ ok: false, error: '同名库已存在' });
      LIBS.push({ name: name, path: path, collection: '', state: 'none', files: 0, chunks: 0,
        last_indexed: null, overrides: '', issues: {} });
      LIB_CFG[name] = {};
      pushLog('已注册库「' + name + '」· ' + path + ' · 下轮索引生效');
      return Promise.resolve({ ok: true });
    },

    remove_library: function (name, drop) {
      LIBS = LIBS.filter(function (l) { return l.name !== name; });
      delete LIB_CFG[name];
      pushLog((drop ? '已注销并删除库' : '已注销库') + '「' + name + '」');
      return Promise.resolve({ ok: true });
    },

    set_library_config: function (name, updates) {
      var ov = LIB_CFG[name] = LIB_CFG[name] || {};
      var errors = {};
      Object.keys(updates || {}).forEach(function (k) {
        var v = updates[k];
        if (typeof v === 'string') {
          if (v === '') { delete ov[k]; return; }
          if (k === 'chunk_char_limit' || k === 'short_doc_char_limit') {
            var n = parseInt(v, 10);
            if (isNaN(n) || n <= 0) { errors[k] = '需为正整数'; return; }
            ov[k] = n; return;
          }
          if (k === 'extensions' || k === 'agent_formats' || k === 'exclude_dirs' || k === 'exclude_files' || k === 'exclude_patterns') {
            ov[k] = v.split(',').map(function (s) { return s.trim(); }).filter(Boolean); return;
          }
          ov[k] = v;
        } else if (v === null) { delete ov[k]; }
        else { ov[k] = v; }
      });
      var l = LIBS.find(function (x) { return x.name === name; });
      if (l) l.overrides = Object.keys(LIB_CFG[name]).map(function (k) { return k + '=' + [].concat(LIB_CFG[name][k]).join('/'); }).join(';');
      return delay(150).then(function () { return { ok: true, errors: errors, cloud_confirm: false }; });
    },

    unset_library_config: function (name, keys) {
      var ov = LIB_CFG[name];
      if (ov) (keys || []).forEach(function (k) { delete ov[k]; });
      return Promise.resolve({ ok: true });
    },

    start_index: function (full, libraries) {
      if (prog.running || prog.busy) return Promise.resolve({ ok: false, already_running: true });
      stoppedByUser = false;
      idxFull = !!full; idxLibs = libraries || '';
      var targets = !idxLibs ? LIBS : LIBS.filter(function (l) { return idxLibs.split(',').indexOf(l.name) >= 0; });
      if (!targets.length) return Promise.resolve({ ok: false, already_running: false });
      var a = { files: 0, chunks: 0 };
      targets.forEach(function (l) { a.files += l.files; a.chunks += l.chunks; });
      var scale = idxFull ? 1 : 0.02;
      prog = { running: true, phase: 'scanning', files_done: 0,
        files_total: Math.max(3, Math.round(a.files * scale) || 3),
        chunks_done: 0, chunks_total: Math.max(20, Math.round(a.chunks * scale) || 20),
        pct: 0, elapsed: 0, library: targets.map(function (l) { return l.name; }).join('、'),
        busy: false, task: 'ours', heartbeat: 'running', heartbeat_note: null };
      idxStart = Date.now();
      if (idxTimer) clearInterval(idxTimer);
      idxTimer = setInterval(tickIndex, 300);
      pushLog('索引启动 · ' + (idxFull ? '全量' : '增量') + ' · 目标：' + prog.library);
      return Promise.resolve({ ok: true });
    },

    stop_index: function () {
      if (!prog.running) return Promise.resolve({ ok: false, stopped: false, reason: '当前没有正在进行的索引任务' });
      clearInterval(idxTimer); idxTimer = null;
      prog.running = false; prog.phase = 'idle'; prog.heartbeat = 'idle'; prog.heartbeat_note = null; prog.pct = 0;
      pushLog('索引已被用户停止 · 已完成部分保留，下轮增量续接');
      stoppedByUser = true;
      return Promise.resolve({ ok: true, stopped: true });
    },

    search: function (query, top_k, libraries, include_body) {
      var q = String(query || '');
      var ms = q.indexOf('慢速') >= 0 ? 35000 : 800;
      return delay(ms).then(function () {
        var pool;
        var ql = q.toLowerCase();
        if (ql.indexOf('wemm') >= 0 || q.indexOf('页级') >= 0 || q.indexOf('视觉') >= 0) pool = WEMM_RESULTS;
        else if (ql.indexOf('mineru') >= 0 || q.indexOf('扫描') >= 0 || q.indexOf('OCR') >= 0) pool = MINERU_RESULTS;
        else pool = GENERIC_POOL;
      var libs = libraries ? libraries.split(',').filter(Boolean) : [];
      var k = top_k || 5;
      // 同文件封顶：对齐真检索语义（max_chunks_per_file，演示数据固定 3）
      var cap = 3, perFile = {}, results = [];
      for (var i = 0; i < pool.length && results.length < k; i++) {
        var r = pool[i];
        if (libs.length && libs.indexOf(r.lib) < 0) continue;
        var fk = r.lib + '|' + r.rel;
        perFile[fk] = (perFile[fk] || 0) + 1;
        if (perFile[fk] > cap) continue;
        var cp = JSON.parse(JSON.stringify(r));
        cp.rendered_html = mockMd(cp.body);   // 对齐真桥：命中默认看渲染视图
        results.push(cp);
      }
      return { results: results, elapsed: ms / 1000 + Math.random() * 0.4, error: null };
      });
    },

    read_document: function (lib, rel) {
      return delay(400).then(function () {
        var md = '# ' + rel.split('/').pop() + '\n\n> 演示模式假正文 · 未读取真实文件\n\n'
          + '这是「' + lib + '/' + rel + '」的演示全文，**渲染视图**默认展示。\n\n'
          + '- 命中片段只给一块，点「查看正文」读整篇\n- 不离开本窗口\n\n`行内代码` 示例。';
        return { ok: true, markdown: md, rendered_html: mockMd(md),
                 chars: md.length, truncated: false, route: '源文件', error: null };
      });
    },

    note_relations: function (lib, rel) {
      var m = relMap()[rel] || { out: [], inn: [] };
      return delay(400).then(function () {
        return { resolved: !!relMap()[rel], file: G.baseName(rel), outlinks: m.out, inlinks: m.inn };
      });
    },

    open_source: function () { return Promise.resolve({ ok: true }); },
    open_path: function () { return Promise.resolve({ ok: true }); },

    pick_path: function (mode) {
      return delay(300).then(function () {
        return { path: mode === 'file' ? 'D:\\Vault\\论文\\课件.pdf' : 'C:\\Users\\you\\Notes',
                 error: null };
      });
    },

    selection_tree: function (lib, sub) {
      return delay(250).then(function () {
        sub = sub || '';
        var mk = function (name, dir) {
          var rel = sub ? sub + '/' + name : name;
          var ext = dir ? '' : name.split('.').pop().toLowerCase();
          var inFmt = ['md', 'pdf', 'docx'].indexOf(ext) >= 0 || dir;
          return { name: name, dir: dir, path: rel, explicit: null,
                   state: inFmt ? 'auto_in' : 'auto_out',
                   state_text: inFmt ? '入库（跟随格式）' : '排除（跟随格式）',
                   ext: ext, n_children: dir ? 3 : undefined };
        };
        var dirs = sub ? [] : [
          mk('20-Projects', true), mk('10-Areas', true), mk('私人', true)
        ];
        var files = sub === '私人'
          ? [Object.assign(mk('账单.pdf', false), { explicit: 'out', state: 'out', state_text: '已排除（显式取消）' }),
             mk('日记.md', false)]
          : sub ? [mk('青苹果菜单.pdf', false), mk('锅包肉配方.pdf', false), mk('小明账单.pdf', false)]
          : [mk('主页.md', false), mk('读书笔记.md', false)];
        var folders = [
          { path: '', depth: 0, name: lib, explicit: null, state: 'root', state_text: '库根' },
          { path: '20-Projects', depth: 1, name: '20-Projects', explicit: null, state: 'auto_in', state_text: '入库（跟随子内容）' },
          { path: '10-Areas', depth: 1, name: '10-Areas', explicit: null, state: 'auto_in', state_text: '入库（跟随子内容）' },
          { path: '私人', depth: 1, name: '私人', explicit: null, state: 'auto_in', state_text: '入库（跟随子内容）' }
        ];
        return { lib: lib, sub: sub, root: 'C:\\demo\\' + lib, dirs: dirs, files: files, folders: folders,
                 selection_in: ['20-Projects/课件/青苹果菜单.pdf'], selection_out: ['私人/账单.pdf'],
                 extensions: ['md', 'pdf', 'docx'], default: 'follow',
                 error: null };
      });
    },

    selection_update: function (lib, changes) {
      return delay(300).then(function () {
        pushLog('勾选范围更新（' + lib + '）· ' + (changes || []).length + ' 项');
        return { ok: true, error: null, selection_in: [], selection_out: [] };
      });
    },

    selection_format_bulk: function (lib, ext, include) {
      return delay(300).then(function () {
        pushLog('勾选格式批量（' + lib + '）· ' + ext + ' → ' + (include ? '纳入' : '排除'));
        return { ok: true, changed: 2, error: null };
      });
    },

    selection_resolve_conflict: function (lib, path) {
      return delay(300).then(function () {
        pushLog('勾选矛盾已解决（' + lib + '）· 仅本库移除排除 ' + path);
        return { ok: true, error: null, selection_in: [path], selection_out: [] };
      });
    },

    get_settings: function () { return Promise.resolve(settingsPayload()); },

    save_settings: function (updates) {
      var errors = {};
      Object.keys(updates || {}).forEach(function (k) {
        var v = String(updates[k]);
        var def = null;
        SETTINGS.groups.forEach(function (g) { g.fields.forEach(function (f) { if (f.key === k) def = f; }); });
        if (!def) { errors[k] = '未知配置项'; return; }
        if ((def.kind === 'int' || def.kind === 'float') && v !== '' && isNaN(parseFloat(v))) { errors[k] = '需为数字'; return; }
        SET_VALUES[k] = v;
      });
      return delay(200).then(function () {
        if (!Object.keys(errors).length) pushLog('配置保存并热读生效 · ' + Object.keys(updates || {}).join(', '));
        return { errors: errors };
      });
    },

    graph: function () {
      return delay(250).then(function () {
        return {
          nodes: G.nodes.map(function (n) {
            return { id: n.id, lib: n.lib, rel: n.rel, type: n.type, chunks: n.chunks, updated: n.updated,
              pipeline: n.pipeline, page: n.page, pages: n.pages, fail_reason: n.fail_reason,
              theme: n.theme, big: n.big };
          }),
          edges: G.edges.map(function (e) { return { a: e.a, b: e.b, kind: e.kind }; }),
          libs: LIBS.map(function (l) { return l.name; }),
          stats: { nodes: G.nodes.length, edges: G.edges.length }
        };
      });
    },

    semantic_edges: function (libraries, threshold) {
      return delay(1400).then(function () {
        var libs = libraries ? libraries.split(',').filter(Boolean) : [];
        var docs = G.nodes.filter(function (n) { return n.type === 'md' && (!libs.length || libs.indexOf(n.lib) >= 0); });
        var edges = [];
        for (var i = 0; i < docs.length; i++) {
          for (var j = i + 1; j < docs.length; j++) {
            var s = fakeSim(docs[i], docs[j]);
            if (s >= (threshold || 0.62)) edges.push({ a: docs[i].id, b: docs[j].id, sim: Math.round(s * 100) / 100 });
          }
        }
        edges.sort(function (x, y) { return y.sim - x.sim; });
        pushLog('语义边计算完成 · ' + edges.length + ' 条 · 阈值 ' + (threshold || 0.62));
        return { edges: edges.slice(0, 40) };
      });
    },

    dedup_run: function (threshold) {
      return delay(2600).then(function () {
        var th = threshold || 0.8;
        var clusters = DUPS.filter(function (d) { return d.sim >= th; });
        return { clusters: clusters, stats: { files: agg().files, clusters: clusters.length,
          seconds: 12.4, threshold: th } };
      });
    },

    failures: function (lib) {
      var rows = FAILS.filter(function (f) { return !lib || f.lib === lib; })
        .map(function (f) { return { lib: f.lib, rel: f.rel, reason: f.reason,
          will_retry: !!f.will_retry, detail: f.detail || [] }; });
      return Promise.resolve({ total: rows.length, healthy: 0, multi: !lib,
        rows: rows, error: null });
    },

    wemm_status: function (lib) {
      var rows = [];
      var pdfsOf = G.nodes.filter(function (n) { return n.type === 'pdf' && (!lib || n.lib === lib); });
      pdfsOf.forEach(function (p) {
        if (p.pipeline.wemm === 'done') rows.push({ lib: p.lib, rel: p.rel, pages: p.pages, failed: false, reason: null });
        else if (p.pipeline.mineru === 'failed') rows.push({ lib: p.lib, rel: p.rel, pages: null, failed: true, reason: p.fail_reason || 'OCR 失败' });
      });
      return delay(200).then(function () {
        var tp = 0; rows.forEach(function (r) { tp += r.pages || 0; });
        return { exists: rows.length > 0, total_pages: tp, rows: rows, error: null };
      });
    },

    wemm_probe: function () {
      return delay(900).then(function () {
        if (SET_VALUES.wemm_backend === 'off') return { alive: false, detail: 'wemm_server 未运行（页级视觉索引已关闭）' };
        return { alive: true, detail: 'wemm_server 运行中 · 127.0.0.1:9101 · bge-visualized · 显存 2.1GB · 页向量 41,377 条' };
      });
    },

    preview_start: function (path, backend) {
      if (!path || !String(path).trim()) return Promise.resolve({ ok: false, error: '请先填写文件路径' });
      if (preview.running) return Promise.resolve({ ok: false, error: '已有提取任务在运行' });
      preview.path = String(path).trim(); preview.backend = backend || null;
      preview.running = true; preview.done = false; preview.result = null; preview.start = Date.now();
      if (preview.timer) clearTimeout(preview.timer);
      preview.timer = setTimeout(previewFinish, 5000);
      pushLog('试验台提取启动 · ' + preview.path + ' · 后端覆盖：' + (backend || '跟随全局'));
      previewEmit();
      return Promise.resolve({ ok: true });
    },

    preview_poll: function () {
      if (preview.running && !preview.done) {
        return Promise.resolve({ running: true, done: false, result: null });
      }
      return Promise.resolve({ running: false, done: preview.done, result: preview.result });
    },

    preview_cancel: function () {
      if (preview.timer) { clearTimeout(preview.timer); preview.timer = null; }
      if (preview.running) {
        preview.running = false; preview.done = false; preview.result = null;
        pushLog('试验台提取已取消 · ' + preview.path);
        previewEmit();
      }
      return Promise.resolve({ ok: true });
    },

    log_tail: function (cursor) {
      var c = (cursor === null || cursor === undefined) ? 0 : cursor;
      var lines = LOG.slice(c);
      return Promise.resolve({ lines: lines, cursor: LOG.length });
    },

    export_run: function () {
      pushLogBulk(['导出子进程启动 · python export.py', '导出 · 打包文本向量库 …', '导出 · 打包页向量库 …']);
      setTimeout(function () { pushLog('导出完成 · exports/rag-export-20260906.zip · 校验通过'); }, 2400);
      return Promise.resolve({ ok: true });
    },

    import_run: function (confirm_text) {
      if (confirm_text !== '我确认导入') return Promise.resolve({ ok: false, error: '确认文字不匹配' });
      pushLogBulk(['导入子进程启动 · python import.py', '导入 · 结构校验通过', '导入 · 写回向量库 …']);
      setTimeout(function () { pushLog('导入完成 · 8,306 块 · 一致性校验通过'); }, 2400);
      return Promise.resolve({ ok: true });
    },

    get_static_path: function (name) {
      return Promise.resolve({ path: 'C:\\Users\\xbl26\\projects\\obsidian-rag\\data\\' + (name || 'gui_index.log') });
    }
  };

  /* ---------- 未实现方法守卫：缺方法抛错 + 控制台警告 ---------- */
  var KNOWN = Object.keys(impl);
  var apiProxy = new Proxy(impl, {
    get: function (t, m) {
      if (m in t) return t[m];
      return function () {
        console.warn('[mock] 未实现的契约方法：' + String(m));
        return Promise.reject(new Error('mock 未实现：' + String(m)));
      };
    }
  });

  /* ---------- 注入（带真桥让位：pywebview 注入是异步的，必须等宽限期）----------
     真 App 里 pywebview 在文档脚本执行前/后不久才注入 window.pywebview；
     mock 若无条件抢先安装，真桥会被覆盖或与之每秒互殴（表现为快照数字闪跳、
     图谱是假数据、右下角常驻演示角标）。因此：真桥存在 → 永不激活；
     1.5s 宽限期内真桥就绪 → 永不激活。 */
  function realBridge() {
    var a = window.pywebview && window.pywebview.api;
    return !!(a && a.__isMock !== true);
  }
  function activate() {
    if (window.__RAG_MOCK.active || realBridge()) return;
    window.__RAG_MOCK.active = true;
    apiProxy.__isMock = true;
    window.__push = function (type, payloadJson) {
      window.dispatchEvent(new CustomEvent(type, { detail: payloadJson }));
    };
    window.pywebview = { api: apiProxy };
    if (document.body) document.body.classList.add('mock');
    else document.addEventListener('DOMContentLoaded', function () { document.body.classList.add('mock'); });
    // 顶部横幅：演示模式必须一眼可辨，避免与真实库混淆
    document.addEventListener('DOMContentLoaded', function () {
      var b = document.createElement('div');
      b.className = 'mock-banner';
      b.textContent = '演示模式：当前页面为假数据，与真实知识库无关 · 真实检索请用桌面 App（Obsidian RAG 快捷方式）';
      document.body.appendChild(b);
    });
    setTimeout(function () { window.dispatchEvent(new Event('pywebviewready')); }, 30);
    setInterval(function () { window.__push('snapshot', jstr(snapshotPayload())); }, 1000);
    console.info('[mock] 演示模式已启用：实现契约方法 ' + KNOWN.length + ' 个。检索框输入含「慢速」可模拟 35 秒慢返回。');
  }
  if (!realBridge()) {
    var graceStart = Date.now();
    var graceTimer = setInterval(function () {
      if (realBridge()) { clearInterval(graceTimer); return; }
      if (Date.now() - graceStart >= 1500) { clearInterval(graceTimer); activate(); }
    }, 100);
  }
})();
