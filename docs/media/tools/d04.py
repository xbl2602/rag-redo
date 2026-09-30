import sys; sys.path.insert(0,'tools')
from lib import *
A,B,C,D=20,270,480,690
r=[30,170,340,480]
comps=[
 comp('ui','frontend','窗口和 AI 接口','GUI 界面 · MCP 工具',C,r[0]),
 comp('runtime','backend','插件运行时','发现 · 校验 · 启停 · 显卡裁判',B,r[1]),
 comp('flow','backend','数据流管理器','规定顺序 · 唯一存储入口',C,r[1]),
 comp('sub','security','独立进程的重活','看图检索 · 本机 OCR',A,r[1]),
 comp('read','cloud','读文件零件','Markdown · Word · PDF · OCR',B,r[2]),
 comp('store','cloud','加工与存储零件','切块 · 向量化 · 关键词 · 向量库',C,r[2]),
 comp('more','cloud','搜索与增强零件','合并 · 重排 · 建议 · 摘要 · 去重',D,r[2]),
 comp('swap','external','换一个零件','换模型 / 存储：改设置立即生效',C,r[3]),
 comp('plan','external','零件之间互相隔离','规划中：现在靠约定避免冲突',D,r[3],tag='规划中'),
]
conns=[
 conn('c1','ui','flow','只调公共接口',variant='emphasis',fs='bottom',ts='top',at=[565,120]),
 conn('c2','runtime','read','装配启停',variant='emphasis',fs='bottom',ts='top',at=[355,300]),
 conn('c3','flow','store','按顺序调用',variant='emphasis',fs='bottom',ts='top',at=[565,300]),
 conn('c4','flow','more','按顺序调用',variant='emphasis',fs='right',ts='top'),
 conn('c5','runtime','sub','拉起',variant='security',fs='left',ts='right',at=[218,202]),
 conn('c6','store','swap','可替换',variant='dashed',fs='bottom',ts='top',at=[565,445]),
 conn('c7','more','plan','规划中',variant='dashed',fs='bottom',ts='top',at=[775,445]),
]
bounds=[{"kind":"region","label":"核心（只有这两样）","wraps":["runtime","flow"],"pad":24}]
write('04-plugins', doc('插件怎么装配：小核心 + 可换的零件',[890,570],comps,conns,bounds))
