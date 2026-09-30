import sys; sys.path.insert(0,'tools')
from lib import *
A,B,C=90,350,610
rows=[40,170,300,430,560]
comps=[
 comp('pick','frontend','挑出要收录的文件','路径勾选 · 格式授权',A,rows[0]),
 comp('text','backend','读取内容','Markdown · Word · PDF 文字页',A,rows[1]),
 comp('chunk','backend','清洗并切成小段','按标题切块 · 保留双链',220,rows[2]),
 comp('embed','backend','向量化','BGE-M3 · 用显卡',A,rows[3]),
 comp('bm25','backend','关键词表','BM25 · 中文分词',B,rows[3]),
 comp('gen','database','新一版索引','写完才一次性切换',220,rows[4]),
 comp('ocr','cloud','整本走 OCR','有页面没文字时',B,rows[1]),
 comp('fail','security','失败记账','读不出 · 空文件 · 待 OCR',C,rows[1]),
 comp('skip','external','没变的直接跳过','旧内容原样保留',C,rows[0]),
]
conns=[
 conn('c1','pick','text','读取',fs='bottom',ts='top',variant='emphasis',at=[175,150]),
 conn('c2','text','chunk',variant='emphasis',fs='bottom',ts='top'),
 conn('c3','chunk','embed',variant='emphasis',fs='bottom',ts='top'),
 conn('c4','chunk','bm25',variant='emphasis',fs='bottom',ts='top'),
 conn('c5','embed','gen','写入',variant='emphasis',fs='bottom',ts='left',at=[175,507]),
 conn('c6','bm25','gen','写入',variant='emphasis',fs='bottom',ts='right',at=[435,507]),
 conn('c7','text','ocr','有页没字',variant='dashed',fs='right',ts='left'),
 conn('c8','ocr','chunk',variant='dashed',fs='bottom',ts='top'),
 conn('c9','ocr','fail','读不出',variant='security',fs='right',ts='left'),
 conn('c10','pick','skip','没改动',variant='dashed',fs='right',ts='left'),
]
write('02-indexing', doc('建索引：一篇笔记怎么变成能搜的样子',[810,660],comps,conns))
