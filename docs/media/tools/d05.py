import json
P=[
 {"id":"user","type":"external","label":"你","sublabel":"提问 / 点按钮"},
 {"id":"search","type":"backend","label":"搜索模块","sublabel":"嵌入 + 重排"},
 {"id":"arb","type":"security","label":"显卡裁判","sublabel":"谁先谁后"},
 {"id":"ocr","type":"cloud","label":"本机 OCR","sublabel":"看图服务同理"},
]
M=[
 ("ocr","arb",190,"申请显卡","emphasis"),
 ("arb","ocr",232,"批准，开始占用","return"),
 ("user","search",312,"搜索","emphasis"),
 ("search","arb",354,"申请显卡（检索优先）","emphasis"),
 ("arb","ocr",396,"请让出：卸载模型","security"),
 ("ocr","arb",438,"已卸载，进程还在","return"),
 ("arb","search",480,"批准","return"),
 ("search","user",522,"返回结果","return"),
 ("search","arb",606,"空闲一阵：自动归还","dashed"),
 ("user","search",648,"点「释放显存」","emphasis"),
 ("search","arb",690,"卸载模型、归还名额","emphasis"),
 ("arb","ocr",732,"让子进程也卸载","dashed"),
]
d={"schema_version":1,"diagram_type":"sequence",
 "meta":{"title":"显卡与后台进程：排队，不打架","locale":"zh-CN","animation":"trace","quality_profile":"standard","column_fit":"spread","viewBox":[740,840],"legend":{"mode":"hidden"}},
 "participants":P,
 "segments":[{"from":160,"to":252,"label":"① 后台正在扫描 PDF"},{"from":286,"to":544,"label":"② 你来搜索：检索优先"},{"from":580,"to":764,"label":"③ 自动或手动释放"}],
 "messages":[{"id":f"m{i+1}","from":a,"to":b,"y":y,"label":l,"variant":v} for i,(a,b,y,l,v) in enumerate(M)],
 "activations":[
   {"participant":"ocr","from":184,"to":240,"type":"cloud"},
   {"participant":"arb","from":184,"to":240,"type":"security"},
   {"participant":"search","from":306,"to":528,"type":"backend"},
   {"participant":"arb","from":348,"to":486,"type":"security"},
   {"participant":"ocr","from":390,"to":444,"type":"cloud"},
   {"participant":"search","from":642,"to":696,"type":"backend"},
   {"participant":"arb","from":684,"to":738,"type":"security"},
 ]}
json.dump(d,open('src/05-gpu.sequence.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
