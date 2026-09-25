"""BM25 + jieba 中文分词的词法检索。

不引入 rank_bm25 这类第三方包——BM25 算法本身只有几十行标准公式，没必要
为此多背一个依赖。jieba 是唯一真正需要的第三方依赖，用来正确切分中文：
纯空白分词对中文没有词间空格，召回会很差——这是旧 obsidian-rag 项目自己
踩过、写进 requirements.txt 注释里的原话教训："缺失时 retriever 会降级
为纯2-gram，召回略降"。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from core.atomic import atomic_write_text

import jieba

INDEXER_VERSION = "0.2.0"

_ENGLISH_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]{1,}")
_ZH_SEG_RE = re.compile(r"[一-鿿]+")

# 中文停用词（检索噪声：虚词/泛动词/无区分度词），配合 jieba 分词过滤——
# 逐字对齐 obsidian-rag/retriever.py::_CH_STOP（保留"配置/能力"这类实义词）。
_CH_STOP = frozenset(
    "的了是在有和就不太人这那也还我一个要会去与或于及之而但并其们"
    "对于为了以及根据相关包括如何什么怎么哪些为什么要需要可以应该"
    "我们你们他们这个那个这样那样时候地方情况问题办法方式途径通过"
    "进行使用用到做了着过吧吗呢哦啊呀呢因为所以然后接着接下来"
)


def tokenize(text: str) -> list[str]:
    """BM25 分词——逐字对齐 obsidian-rag/retriever.py::tokenize（2026-08-13
    升级）：jieba 精确分词（滤停用词）+ 中文 2-gram 双通道 + 英文/数字 token。

    jieba 负责词级语义（"火箭发动机"→一个词，IDF 更准）；2-gram 兜底召回
    （jieba 对专名/未登录词切错时 bigram 仍能命中）；英文/数字走原 token 路。
    此前只做 jieba 词级单通道，双语/专名场景的召回回退（2026-09-25 终审）。"""
    lowered = text.lower()
    tokens: list[str] = []
    for m in _ENGLISH_TOKEN_RE.finditer(lowered):
        tokens.append(m.group(0))
    for seg in _ZH_SEG_RE.findall(lowered):
        for w in jieba.lcut(seg):
            if w.strip() and w not in _CH_STOP:
                tokens.append(w)
        tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


@dataclass
class _Posting:
    doc_id: str
    term_freq: int


@dataclass
class BM25Index:
    """标准 Okapi BM25，k1=1.5、b=0.75——和 Elasticsearch/Lucene 的默认值
    一致，没有特殊理由偏离业界常用默认。"""

    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, list[_Posting]] = field(default_factory=lambda: defaultdict(list))
    _doc_lengths: dict[str, int] = field(default_factory=dict)
    _doc_tokens_cache: dict[str, Counter] = field(default_factory=dict)

    @property
    def doc_count(self) -> int:
        return len(self._doc_lengths)

    @property
    def _avg_doc_length(self) -> float:
        if not self._doc_lengths:
            return 0.0
        return sum(self._doc_lengths.values()) / len(self._doc_lengths)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._doc_lengths:
            self.remove(doc_id)
        tokens = tokenize(text)
        counts = Counter(tokens)
        self._doc_lengths[doc_id] = len(tokens)
        self._doc_tokens_cache[doc_id] = counts
        for term, freq in counts.items():
            self._postings[term].append(_Posting(doc_id=doc_id, term_freq=freq))

    def remove(self, doc_id: str) -> None:
        if doc_id not in self._doc_lengths:
            return
        counts = self._doc_tokens_cache.pop(doc_id)
        del self._doc_lengths[doc_id]
        for term in counts:
            self._postings[term] = [p for p in self._postings[term] if p.doc_id != doc_id]
            if not self._postings[term]:
                del self._postings[term]

    def _idf(self, term: str) -> float:
        n_docs_with_term = len(self._postings.get(term, []))
        if n_docs_with_term == 0:
            return 0.0
        # 标准 BM25 idf 公式（带 +1 平滑，避免出现在超过半数文档里的词
        # 算出负分）。
        return math.log(1 + (self.doc_count - n_docs_with_term + 0.5) / (n_docs_with_term + 0.5))

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self.doc_count == 0:
            return []
        query_terms = tokenize(query)
        scores: dict[str, float] = defaultdict(float)
        avg_len = self._avg_doc_length
        for term in query_terms:
            idf = self._idf(term)
            if idf == 0.0:
                continue
            for posting in self._postings.get(term, []):
                doc_len = self._doc_lengths[posting.doc_id]
                length_norm = (1 - self.b + self.b * doc_len / avg_len) if avg_len else 1.0
                denom = posting.term_freq + self.k1 * length_norm
                scores[posting.doc_id] += idf * (posting.term_freq * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def to_dict(self) -> dict:
        """只存 doc_lengths + doc_tokens_cache，_postings 是它俩的倒排
        索引视图，重建时重新推导即可，不用重复存一份容易和原数据对不上
        的冗余状态。返回值是纯 JSON 兼容类型（字符串/数字的字典），供
        save() 落盘，也供 official-import-export 插件直接拿去打包进
        导出归档，不需要先写一个临时文件再读回来。"""
        return {
            "k1": self.k1,
            "b": self.b,
            "doc_lengths": dict(self._doc_lengths),
            "doc_tokens_cache": {doc_id: dict(counts) for doc_id, counts in self._doc_tokens_cache.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Index":
        idx = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        idx._doc_lengths = dict(data.get("doc_lengths", {}))
        idx._doc_tokens_cache = {
            doc_id: Counter(counts) for doc_id, counts in data.get("doc_tokens_cache", {}).items()
        }
        for doc_id, counts in idx._doc_tokens_cache.items():
            for term, freq in counts.items():
                idx._postings[term].append(_Posting(doc_id=doc_id, term_freq=freq))
        return idx

    def save(self, path: Path) -> None:
        """用 JSON 不用 pickle——这份数据完全是简单类型，JSON 够用且没有
        反序列化任意代码执行的隐患，没有理由为了省几行代码换一个有安全
        面的格式。"""
        # 原子写（core/atomic.py）：BM25 索引半截 = 词法检索路整体失效
        atomic_write_text(path, json.dumps(self.to_dict(), ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        """文件不存在或损坏都安全降级成空索引，不崩溃——同
        AGENTS.md"失败折叠成诚实终态"纪律；空索引意味着这个库下一次
        reindex_knowledge 之前搜不到东西，比进程直接崩溃或读到一份
        损坏数据继续跑安全得多。"""
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)
