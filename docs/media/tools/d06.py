import sys; sys.path.insert(0,'tools')
from lib import *
L,M,R=30,280,530
r=[30,150,290,430,570]
comps=[
 comp('mcp','frontend','MCP 工具','AI 助手来调用',M,r[0]),
 comp('gate','security','写权限门禁','提案号 + 确认码 · 一次性',M,r[1]),
 comp('gui','frontend','GUI 窗口','点按钮、看进度',L,r[2]),
 comp('core','backend','同一个业务核心','排序、过滤、报错只在这里',M,r[2]),
 comp('cli','frontend','命令行','脚本、批处理',R,r[2]),
 comp('plugins','cloud','插件零件','读文件 · 切块 · 重排 等',M,r[3]),
 comp('store','database','索引库','数据只从唯一入口读写',M,r[4]),
]
conns=[
 conn('c1','gui','core','点按钮',variant='emphasis',fs='right',ts='left'),
 conn('c2','cli','core','命令',variant='emphasis',fs='left',ts='right'),
 conn('c3','mcp','gate','要改设置',variant='security',fs='bottom',ts='top',at=[365,122]),
 conn('c4','gate','core','确认后放行',variant='security',fs='bottom',ts='top',at=[365,252]),
 conn('c5','core','plugins','按顺序调用',variant='emphasis',fs='bottom',ts='top',at=[365,392]),
 conn('c6','plugins','store','唯一读写入口',variant='emphasis',fs='bottom',ts='top',at=[365,532]),
]
write('06-entrances', doc('三个入口，共用一个大脑',[740,680],comps,conns))
