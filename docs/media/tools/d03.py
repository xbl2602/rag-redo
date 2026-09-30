import sys; sys.path.insert(0,'tools')
from lib import *
A,B,M,C=90,350,220,610
r=[30,144,258,372,486,600,714]
comps=[
 comp('ask','external','你 / AI 助手提问','一句话，按意思搜',M,r[0]),
 comp('fresh','backend','先看笔记变了没','变了就自动更新（AI 助手搜索时）',M,r[1]),
 comp('vec','backend','按意思找','向量 · 大范围候选',A,r[2]),
 comp('kw','backend','按字词找','关键词 · 中文分词',B,r[2]),
 comp('fuse','backend','合并两路排名','RRF 融合',M,r[3]),
 comp('rerank','backend','精挑重排','更准的打分模型',M,r[4]),
 comp('deliver','backend','整理结果','同节合并 · 同文件封顶 · 补足条数',M,r[5]),
 comp('answer','frontend','返回结果','+ 至多 2 条搜索建议',M,r[6]),
 comp('sync','cloud','后台增量更新','更新期间旧索引照常可用',C,r[1]),
 comp('hyde','cloud','假设答案再查一遍','分数太低时 · 默认关闭',C,r[4]),
 comp('visual','cloud','看图找 PDF 页面','独立的第二套系统',C,r[2]),
 comp('vres','database','页面图 + 页码','不与文字分数混排',C,r[3]),
]
conns=[
 conn('c1','ask','fresh','提问',variant='emphasis',fs='bottom',ts='top',at=[305,119]),
 conn('c2','fresh','vec','意思',variant='emphasis',fs='bottom',ts='top'),
 conn('c3','fresh','kw','字词',variant='emphasis',fs='bottom',ts='top'),
 conn('c4','vec','fuse',variant='emphasis',fs='bottom',ts='left'),
 conn('c5','kw','fuse',variant='emphasis',fs='bottom',ts='right'),
 conn('c6','fuse','rerank',variant='emphasis',fs='bottom',ts='top'),
 conn('c7','rerank','deliver',variant='emphasis',fs='bottom',ts='top'),
 conn('c8','deliver','answer',variant='emphasis',fs='bottom',ts='top'),
 conn('c9','fresh','sync','有变化',variant='dashed',fs='right',ts='left'),
 conn('c10','rerank','hyde','分数低',variant='dashed',fs='right',ts='left'),
 conn('c11','hyde','deliver','更高才换',variant='dashed',fs='bottom',ts='right'),
 conn('c12','visual','vres','页面',variant='emphasis',fs='bottom',ts='top',at=[695,347]),
]
bounds=[{"kind":"region","label":"第二套系统：看图找页面（独立）","wraps":["visual","vres"],"pad":18}]
write('03-search', doc('一次搜索：从提问到拿到结果',[810,810],comps,conns,bounds))
