import json
S=[
 {"id":"m0","type":"start","label":"笔记新增或改动","sublabel":"进入收录范围","lane":"main","col":0,"step":"01"},
 {"id":"m1","type":"active","label":"被选中","sublabel":"路径与格式检查","lane":"main","col":1,"step":"02"},
 {"id":"m2","type":"active","label":"读取内容","sublabel":"提取文字 / OCR","lane":"main","col":2,"step":"03"},
 {"id":"m3","type":"active","label":"切块与向量化","sublabel":"写入新一版","lane":"main","col":3,"step":"04"},
 {"id":"m4","type":"success","label":"可被搜到","sublabel":"发布后生效","lane":"main","col":4,"step":"05"},
 {"id":"e0","type":"decision","label":"没读成","sublabel":"先分清原因","lane":"event","col":0,"width":104},
 {"id":"e1","type":"waiting","label":"稍后自动重试","sublabel":"服务暂时不可用","lane":"event","col":1,"width":104},
 {"id":"t0","type":"failure","label":"记账为终态","sublabel":"空文件 · 读取失败 · 扫描件","lane":"terminal","col":0,"width":150},
]
T=[
 {"id":"t1","from":"m0","to":"m1"},
 {"id":"t2","from":"m1","to":"m2"},
 {"id":"t3","from":"m2","to":"m3"},
 {"id":"t4","from":"m3","to":"m4"},
 {"id":"t5","from":"m2","to":"e0","label":"读失败","variant":"security","fromSide":"bottom","toSide":"top","route":"straight","labelAt":[402,233]},
 {"id":"t6","from":"e0","to":"e1","variant":"dashed","fromSide":"right","toSide":"left","route":"straight"},
 {"id":"t7","from":"e0","to":"t0","label":"永久读不出","variant":"security","fromSide":"bottom","toSide":"top","route":"straight","labelAt":[402,392]},
 {"id":"t8","from":"t0","to":"m1","label":"内容或设置变了","variant":"dashed","fromSide":"left","toSide":"bottom","labelAt":[248,400]},
 {"id":"t9","from":"m4","to":"m1","label":"笔记又改了","variant":"dashed","fromSide":"top","toSide":"top","route":"top-channel","channelY":66},
]
d={"schema_version":1,"diagram_type":"lifecycle",
 "meta":{"title":"一个文件的一生：状态怎么变","locale":"zh-CN","animation":"trace","quality_profile":"standard","viewBox":[830,660],"legend":{"mode":"hidden"}},
 "lanes":[{"id":"main","label":"正常的一生"},{"id":"event","label":"半路遇到问题"},{"id":"terminal","label":"最终落点：记账，不重复消耗"}],
 "states":S,"transitions":T}
json.dump(d,open('src/07-lifecycle.lifecycle.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
