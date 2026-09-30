import json, subprocess, sys
ARCHIFY = '/c/Users/xbl26/.claude/skills/archify'
def comp(id, t, label, sub, x, y, w=170, h=64, tag=None):
    d = {"id": id, "type": t, "label": label, "sublabel": sub, "pos": [x, y], "size": [w, h]}
    if tag: d["tag"] = tag
    return d
def conn(id, a, b, label=None, variant=None, fs=None, ts=None, at=None, **kw):
    d = {"id": id, "from": a, "to": b}
    if label: d["label"] = label
    if variant: d["variant"] = variant
    if fs: d["fromSide"] = fs
    if ts: d["toSide"] = ts
    if at: d["labelAt"] = at
    d.update(kw)
    return d
def doc(title, vb, comps, conns, bounds=None):
    d = {"schema_version": 1, "diagram_type": "architecture",
         "meta": {"title": title, "locale": "zh-CN", "animation": "trace", "quality_profile": "standard",
                  "viewBox": vb, "legend": {"mode": "hidden"}},
         "components": comps}
    if bounds: d["boundaries"] = bounds
    d["connections"] = conns
    return d
def write(name, d):
    p = f'src/{name}.architecture.json'
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    return p
