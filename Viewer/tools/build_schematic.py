#!/usr/bin/env python3
"""KiCad schematic → compact JSON for Viewer/schematic.html.

Usage:  python3 Viewer/tools/build_schematic.py
Reads   PCB_Schematics/KiCad/Main/Mynd_Main.kicad_sch (+ its sub-sheets) and the netlist kicad-cli exports from it,
writes  Viewer/mynd_main.sch.json

Everything is pre-transformed into sheet coordinates (mm, y down): the page only draws lines, circles and text.
Connectivity (which wire belongs to which net) is worked out from geometry here, and every group of wires is named
after the netlist net of the pins it touches, so the names match the PCB and the 3D view.
"""
import json, math, os, re, subprocess, sys, tempfile
from annotations import annotate, net_meta
from simplify import merge_pins
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
KDIR = os.path.join(REPO, "PCB_Schematics/KiCad/Main")
ROOT_SCH = "Mynd_Main.kicad_sch"
OUT = os.path.join(REPO, "Viewer/mynd_main.sch.json")
KICAD_CLI = os.environ.get("KICAD_CLI", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")

# ---------------------------------------------------------------- s-expressions
TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')

class Q(str):
    """a quoted string (so the parser can tell `"hide"` from a bare hide)"""

def parse(text):
    stack, cur, pos = [], [], 0
    n = len(text)
    while pos < n:
        m = TOK.match(text, pos)
        if not m:
            break
        pos = m.end()
        if m.group(1):
            stack.append(cur); cur = []
        elif m.group(2):
            done = cur; cur = stack.pop(); cur.append(done)
        elif m.group(3) is not None:
            cur.append(Q(m.group(3).replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")))
        else:
            cur.append(m.group(4))
    return cur[0]

def kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]

def kid(node, name):
    for c in node:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None

def num(v):
    return float(v)

def at_of(node):
    a = kid(node, "at")
    if not a:
        return 0.0, 0.0, 0.0
    return num(a[1]), num(a[2]), (num(a[3]) if len(a) > 3 else 0.0)

def is_hidden(node):
    """(hide yes) or a bare `hide` atom, on the node or in its effects"""
    for c in node:
        if c == "hide" and not isinstance(c, Q):
            return True
        if isinstance(c, list) and c and c[0] == "hide":
            return len(c) < 2 or c[1] == "yes"
    eff = kid(node, "effects")
    return bool(eff) and is_hidden(eff)

def font_size(node, default=1.27):
    eff = kid(node, "effects")
    if eff:
        f = kid(eff, "font")
        if f:
            s = kid(f, "size")
            if s:
                return num(s[2]) if len(s) > 2 else num(s[1])
    return default

def justify(node):
    """→ (h, v): h -1 left / 0 centre / 1 right, v -1 top / 0 centre / 1 bottom"""
    h = v = 0
    eff = kid(node, "effects")
    j = kid(eff, "justify") if eff else None
    if j:
        for t in j[1:]:
            if t == "left": h = -1
            elif t == "right": h = 1
            elif t == "top": v = -1
            elif t == "bottom": v = 1
    return h, v

def stroke_of(node, default=0.0):
    s = kid(node, "stroke")
    w, dash = default, 0
    if s:
        ww = kid(s, "width")
        if ww and num(ww[1]) > 0:
            w = num(ww[1])
        t = kid(s, "type")
        if t and t[1] in ("dash", "dot", "dash_dot", "dash_dot_dot"):
            dash = 1
    return w, dash

def fill_of(node):
    f = kid(node, "fill")
    if not f:
        return 0
    t = kid(f, "type")
    t = t[1] if t else "none"
    return {"outline": 1, "background": 2, "color": 2}.get(t, 0)

R = lambda v: round(v, 4)

# ---------------------------------------------------------------- symbol transforms
# KiCad library coordinates are y-up; the sheet is y-down. T maps a library point to an offset on the sheet.
ROT = {0: (1, 0, 0, -1), 90: (0, -1, -1, 0), 180: (-1, 0, 0, 1), 270: (0, 1, 1, 0)}  # (a, b, c, d): x' = a x + b y, y' = c x + d y

def sym_matrix(rot, mirror):
    a, b, c, d = ROT[int(round(rot)) % 360]
    if mirror == "x":      # flip the library y first
        b, d = -b, -d
    elif mirror == "y":    # flip the library x first
        a, c = -a, -c
    return a, b, c, d

def apply(M, x, y):
    a, b, c, d = M
    return a * x + b * y, c * x + d * y

def screen_dir(M, dx, dy):
    """a direction given in sheet orientation (y down) inside the symbol → on the sheet"""
    # undo the y flip of the library frame, then apply M
    return apply(M, dx, -dy)

# ---------------------------------------------------------------- text helpers
def arc_points(s, m, e, steps=None):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [s, e]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (s, m, e))
    def norm(a):
        while a < 0: a += 2 * math.pi
        return a
    # go from a1 to a3 through a2
    ccw = norm(a2 - a1) < norm(a3 - a1)
    span = norm(a3 - a1) if ccw else -norm(a1 - a3)
    n = steps or max(6, int(abs(span) * r * 4) + 4)
    n = min(n, 48)
    return [(ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n)) for i in range(n + 1)]

def resolve(s, props, textvars):
    def sub(m):
        k = m.group(1)
        if k in props: return props[k]
        if k in textvars: return textvars[k]
        return m.group(0)
    for _ in range(3):
        s2 = re.sub(r"\$\{([^}]+)\}", sub, s)
        if s2 == s: break
        s = s2
    return s

# ---------------------------------------------------------------- load
def run_netlist():
    out = os.path.join(tempfile.mkdtemp(), "mynd.net")
    r = subprocess.run([KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, os.path.join(KDIR, ROOT_SCH)],
                       capture_output=True, text=True)
    if not os.path.exists(out):
        sys.exit("kicad-cli could not export the netlist:\n" + r.stderr)
    net = parse(open(out, encoding="utf-8").read())
    comps, pin_net, nets = {}, {}, {}
    for c in kids(kid(net, "components"), "comp"):
        ref = kid(c, "ref")[1]
        fields = {}
        fl = kid(c, "fields")
        if fl:
            for f in kids(fl, "field"):
                nm = kid(f, "name")[1]
                val = next((x for x in f[1:] if isinstance(x, Q)), "")
                if val: fields[nm] = str(val)
        comps[ref] = {"value": str(kid(c, "value")[1]) if kid(c, "value") else "",
                      "footprint": str(kid(c, "footprint")[1]) if kid(c, "footprint") else "",
                      "description": str(kid(c, "description")[1]) if kid(c, "description") else "",
                      "fields": fields}
    for n in kids(kid(net, "nets"), "net"):
        name = str(kid(n, "name")[1])
        nodes = []
        for nd in kids(n, "node"):
            ref, pin = str(kid(nd, "ref")[1]), str(kid(nd, "pin")[1])
            pf = kid(nd, "pinfunction")
            pin_net[(ref, pin)] = name
            nodes.append([ref, pin, str(pf[1]) if pf else ""])
        nets[name] = nodes
    return comps, pin_net, nets

def textvars_of():
    try:
        pro = json.load(open(os.path.join(KDIR, ROOT_SCH.replace(".kicad_sch", ".kicad_pro"))))
        return {k: v for k, v in pro.get("text_variables", {}).items()}
    except Exception:
        return {}

PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
         "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
         "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4)}

def paper_of(sch):
    p = kid(sch, "paper")
    if not p: return PAPER["A4"]
    if p[1] == "User": return num(p[2]), num(p[3])
    w, h = PAPER.get(str(p[1]), PAPER["A4"])
    if "portrait" in p[2:]: w, h = h, w
    return w, h

# ---------------------------------------------------------------- one library symbol → drawing in sheet coordinates
def lib_units(lib, unit, style=1):
    """the drawable sub-symbols of a library symbol for one unit (unit 0 and body style 0 are shared)"""
    out = []
    name = lib[1]
    base = name.split(":")[-1]
    for s in kids(lib, "symbol"):
        m = re.match(r"^(.*)_(\d+)_(\d+)$", s[1])
        if not m: continue
        u, st = int(m.group(2)), int(m.group(3))
        if u in (0, unit) and st in (0, style):
            out.append(s)
    return out

def unit_count(lib):
    us = set()
    for s in kids(lib, "symbol"):
        m = re.match(r"^(.*)_(\d+)_(\d+)$", s[1])
        if m and int(m.group(2)) > 0: us.add(int(m.group(2)))
    return max(len(us), 1)

PIN_NAME_OFFSET = 0.508

def draw_symbol(lib, inst, textvars, kind):
    """→ dict with graphics g, texts t, pins p, bbox b (all in sheet mm)"""
    X, Y, rot = at_of(inst)
    mir = kid(inst, "mirror")
    M = sym_matrix(rot, mir[1] if mir else None)
    unit = int(kid(inst, "unit")[1]) if kid(inst, "unit") else 1
    P = lambda x, y: (lambda dx, dy: (X + dx, Y + dy))(*apply(M, x, y))
    g, t, pins = [], [], []
    xs, ys = [], []
    def grow(pts):
        for x, y in pts: xs.append(x); ys.append(y)

    pn = kid(lib, "pin_names")
    name_off = PIN_NAME_OFFSET
    names_hidden = False
    if pn:
        o = kid(pn, "offset")
        if o: name_off = num(o[1])
        names_hidden = is_hidden(pn)
    pnum = kid(lib, "pin_numbers")
    nums_hidden = bool(pnum) and is_hidden(pnum)

    for sub in lib_units(lib, unit):
        for it in sub[1:]:
            if not isinstance(it, list): continue
            k = it[0]
            if k in ("polyline", "bezier"):
                pts = [P(num(p[1]), num(p[2])) for p in kids(kid(it, "pts"), "xy")]
                w, dash = stroke_of(it)
                g.append(["P", R(w), fill_of(it), [R(v) for xy in pts for v in xy]] + ([1] if dash else []))
                grow(pts)
            elif k == "rectangle":
                s, e = kid(it, "start"), kid(it, "end")
                x1, y1, x2, y2 = num(s[1]), num(s[2]), num(e[1]), num(e[2])
                pts = [P(x1, y1), P(x2, y1), P(x2, y2), P(x1, y2), P(x1, y1)]
                w, dash = stroke_of(it)
                g.append(["P", R(w), fill_of(it), [R(v) for xy in pts for v in xy]])
                grow(pts)
            elif k == "circle":
                c = kid(it, "center"); r = num(kid(it, "radius")[1])
                cx, cy = P(num(c[1]), num(c[2]))
                w, dash = stroke_of(it)
                g.append(["C", R(w), fill_of(it), R(cx), R(cy), R(r)])
                grow([(cx - r, cy - r), (cx + r, cy + r)])
            elif k == "arc":
                s, m, e = kid(it, "start"), kid(it, "mid"), kid(it, "end")
                if not m: continue
                pts = arc_points(P(num(s[1]), num(s[2])), P(num(m[1]), num(m[2])), P(num(e[1]), num(e[2])))
                w, dash = stroke_of(it)
                g.append(["P", R(w), fill_of(it), [R(v) for xy in pts for v in xy]])
                grow(pts)
            elif k == "text":
                if is_hidden(it): continue
                tx, ty, ta = at_of(it)
                x, y = P(tx, ty)
                vertical = (round(ta / 90) % 2 == 1) != (M[1] != 0)
                t.append([str(it[1]), R(x), R(y), R(font_size(it)), 90 if vertical else 0, 0, 0, "s"])
                grow([(x, y)])
            elif k == "pin":
                if is_hidden(it):
                    hidden = True
                else:
                    hidden = False
                px, py, pa = at_of(it)
                L = num(kid(it, "length")[1]) if kid(it, "length") else 2.54
                nm = kid(it, "name"); nb = kid(it, "number")
                pname = str(nm[1]) if nm else ""
                pnum_ = str(nb[1]) if nb else ""
                cx, cy = P(px, py)
                ex, ey = P(px + L * math.cos(math.radians(pa)), py + L * math.sin(math.radians(pa)))
                pins.append([R(cx), R(cy), pnum_, pname, hidden, it[1], R(ex), R(ey)])
                if hidden: continue
                grow([(cx, cy), (ex, ey)])
                dx, dy = ex - cx, ey - cy
                ln = math.hypot(dx, dy) or 1
                dx, dy = round(dx / ln), round(dy / ln)
                horizontal = dx != 0
                ns, nsz = pname, font_size(nm) if nm else 1.27
                show_name = ns not in ("", "~") and not names_hidden and not (nm and is_hidden(nm))
                show_num = False          # pin numbers live in the tooltip, as in Schematik
                mx, my = (cx + ex) / 2, (cy + ey) / 2
                if name_off > 0:
                    if show_name:
                        ax, ay = ex + dx * name_off, ey + dy * name_off
                        if horizontal: t.append([ns, R(ax), R(ay), R(nsz), 0, -1 if dx > 0 else 1, 0, "pn", pnum_])
                        else: t.append([ns, R(ax), R(ay), R(nsz), 90, -1 if dy < 0 else 1, 0, "pn", pnum_])
                    if show_num:
                        s = font_size(nb)
                        if horizontal: t.append([pnum_, R(mx), R(my - 0.25), R(s), 0, 0, 1, "pnum"])
                        else: t.append([pnum_, R(mx - 0.25), R(my), R(s), 90, 0, 1, "pnum"])
                else:
                    if show_name:
                        if horizontal: t.append([ns, R(mx), R(my - 0.25), R(nsz), 0, 0, 1, "pn", pnum_])
                        else: t.append([ns, R(mx - 0.25), R(my), R(nsz), 90, 0, 1, "pn", pnum_])
                    if show_num:
                        s = font_size(nb)
                        if horizontal: t.append([pnum_, R(mx), R(my + 0.25), R(s), 0, 0, -1, "pnum"])
                        else: t.append([pnum_, R(mx + 0.25), R(my), R(s), 90, 0, -1, "pnum"])

    # fields (Reference, Value, …) placed on the sheet; their stored position is already on the sheet
    props = {}
    for p in kids(inst, "property"):
        props[str(p[1])] = str(p[2])
    ref = ref_of(inst)
    props["Reference"] = ref
    b = [R(min(xs)), R(min(ys)), R(max(xs)), R(max(ys))] if xs else [X, Y, X, Y]
    return {"g": g, "t": t, "pins": pins, "b": b, "unit": unit, "props": props}

def ref_of(inst):
    inn = kid(inst, "instances")
    if inn:
        for proj in kids(inn, "project"):
            for path in kids(proj, "path"):
                r = kid(path, "reference")
                if r: return str(r[1])
    for p in kids(inst, "property"):
        if p[1] == "Reference": return str(p[2])
    return "?"

# ---------------------------------------------------------------- label shapes
def label_shape(shape, x, y, dx, dy, s):
    """outline of a hierarchical label / sheet pin starting at (x, y) and pointing along (dx, dy) into its text"""
    nx, ny = -dy, dx
    h = s / 2
    pt = lambda a, b: (R(x + dx * a + nx * b), R(y + dy * a + ny * b))
    if shape == "input":        # arrow points at the connection
        pts = [pt(0, 0), pt(h, -h), pt(s, -h), pt(s, h), pt(h, h), pt(0, 0)]
    elif shape == "output":     # arrow points away from it
        pts = [pt(0, -h), pt(h, -h), pt(s, 0), pt(h, h), pt(0, h), pt(0, -h)]
    elif shape in ("bidirectional", "tri_state"):
        pts = [pt(0, 0), pt(h, -h), pt(s, 0), pt(h, h), pt(0, 0)]
    else:
        pts = [pt(0, -h), pt(s, -h), pt(s, h), pt(0, h), pt(0, -h)]
    return [v for p in pts for v in p]

DIRS = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}

# ---------------------------------------------------------------- union-find
class UF:
    def __init__(self): self.p = {}
    def find(self, a):
        p = self.p
        p.setdefault(a, a)
        while p[a] != a:
            p[a] = p[p[a]]; a = p[a]
        return a
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb

def on_segment(px, py, x1, y1, x2, y2, eps=0.01):
    if min(x1, x2) - eps <= px <= max(x1, x2) + eps and min(y1, y2) - eps <= py <= max(y1, y2) + eps:
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        return abs(cross) <= eps * max(1.0, math.hypot(x2 - x1, y2 - y1))
    return False

# ---------------------------------------------------------------- main
def main():
    textvars = textvars_of()
    comps, pin_net, nets = run_netlist()
    print(f"netlist: {len(comps)} components, {len(nets)} nets")

    # walk the hierarchy: root first, then every sheet symbol (each sub-sheet is used once here)
    sheets, queue = [], [(ROOT_SCH, "/", "Root", None)]
    while queue:
        fn, path, name, parent = queue.pop(0)
        sch = parse(open(os.path.join(KDIR, fn), encoding="utf-8").read())
        idx = len(sheets)
        sheets.append({"file": fn, "path": path, "name": name, "sch": sch, "parent": parent})
        for sh in kids(sch, "sheet"):
            props = {str(p[1]): str(p[2]) for p in kids(sh, "property")}
            sname = props.get("Sheetname") or props.get("Sheet name")
            sfile = props.get("Sheetfile") or props.get("Sheet file")
            queue.append((sfile, path + sname + "/", sname, idx))

    uf = UF()
    key = lambda si, x, y: (si, int(round(x * 100)), int(round(y * 100)))
    out_sheets = []
    pin_points = []        # (key, ref, pin number, is_power, net_name_if_power)
    named = defaultdict(list)   # ("global"|"local:si"|"hier:si") + text → keys
    sheet_pins = []        # (child sheet index, name, key)
    hier_labels = []       # (sheet index, name, key)

    for si, sh in enumerate(sheets):
        sch = sh["sch"]
        libs = {str(l[1]): l for l in kids(kid(sch, "lib_symbols") or [], "symbol")}
        W, H = paper_of(sch)
        tb = kid(sch, "title_block")
        title = str(kid(tb, "title")[1]) if tb and kid(tb, "title") else sh["name"]
        S = {"name": sh["name"], "title": title, "file": sh["file"], "path": sh["path"], "paper": [W, H],
             "parts": [], "wires": [], "junctions": [], "nc": [], "labels": [], "sheets": [], "notes": {"g": [], "t": []}}
        points = []          # every connectable point on this sheet, to test against wires

        for inst in kids(sch, "symbol"):
            lib = libs.get(str(kid(inst, "lib_id")[1]))
            if not lib: continue
            is_power = kid(lib, "power") is not None
            d = draw_symbol(lib, inst, textvars, "pwr" if is_power else "part")
            ref = ref_of(inst)
            value = resolve(d["props"].get("Value", ""), d["props"], textvars)
            part = {"r": ref, "u": d["unit"], "b": d["b"], "g": d["g"], "t": d["t"], "p": []}
            if is_power:
                part["pwr"] = value
            for (x, y, pnum, pname, hidden, etype, ex, ey) in d["pins"]:
                k = key(si, x, y)
                points.append((x, y, k))
                uf.find(k)
                if is_power:
                    named["global" + value].append(k)
                else:
                    pin_points.append((k, ref, pnum))
                    if hidden and etype == "power_in" and pname:
                        named["global" + pname].append(k)     # hidden power pins join their net by name
                part["p"].append([x, y, pnum, pname, ex, ey, 1 if hidden else 0])
            S["parts"].append(part)

        for w in kids(sch, "wire") + kids(sch, "bus"):
            pts = [(num(p[1]), num(p[2])) for p in kids(kid(w, "pts"), "xy")]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                k1, k2 = key(si, x1, y1), key(si, x2, y2)
                uf.union(k1, k2)
                S["wires"].append([R(x1), R(y1), R(x2), R(y2), k1])
                points.append((x1, y1, k1)); points.append((x2, y2, k2))
        for j in kids(sch, "junction"):
            x, y, _ = at_of(j)
            k = key(si, x, y); uf.find(k); points.append((x, y, k))
            S["junctions"].append([R(x), R(y), k])
        for nc in kids(sch, "no_connect"):
            x, y, _ = at_of(nc)
            S["nc"].append([R(x), R(y)])

        for kind in ("label", "global_label", "hierarchical_label"):
            for lb in kids(sch, kind):
                x, y, a = at_of(lb)
                text = str(lb[1]); s = font_size(lb)
                k = key(si, x, y); uf.find(k); points.append((x, y, k))
                dx, dy = DIRS.get(int(round(a)) % 360, (1, 0))
                vertical = dy != 0
                L = {"s": text, "x": R(x), "y": R(y), "k": k, "z": R(s)}
                if kind == "label":
                    named[f"local{si}:" + text].append(k)
                    L.update(kind="l", a=90 if vertical else 0, h=-1 if (dx > 0 or dy < 0) else 1, v=1)
                else:
                    shp = kid(lb, "shape"); shp = str(shp[1]) if shp else "passive"
                    if kind == "global_label":
                        named["global" + text].append(k)
                    else:
                        hier_labels.append((si, text, k))
                    # the shape sits at the connection, the text after it, away from the wire
                    rdx, rdy = dx, dy
                    L.update(kind="h" if kind == "hierarchical_label" else "g", shape=label_shape(shp, x, y, rdx, rdy, s),
                             tx=R(x + rdx * s * 1.35), ty=R(y + rdy * s * 1.35), a=90 if vertical else 0,
                             h=-1 if (rdx > 0 or rdy < 0) else 1, v=0)
                S["labels"].append(L)

        for shs in kids(sch, "sheet"):
            x, y, _ = at_of(shs)
            sz = kid(shs, "size"); w, h = num(sz[1]), num(sz[2])
            props = {str(p[1]): p for p in kids(shs, "property")}
            sname = str((props.get("Sheetname") or props.get("Sheet name"))[2])
            sfile = str((props.get("Sheetfile") or props.get("Sheet file"))[2])
            target = next(i for i, s2 in enumerate(sheets) if s2["parent"] == si and s2["name"] == sname)
            entry = {"x": R(x), "y": R(y), "w": R(w), "h": R(h), "name": sname, "file": sfile, "target": target, "pins": [], "t": []}
            for key_, cls in (("Sheetname", "sn"), ("Sheetfile", "sf")):
                p = props.get(key_)
                if p is None or is_hidden(p): continue
                fx, fy, fa = at_of(p); hh, vv = justify(p)
                entry["t"].append([str(p[2]) if cls == "sn" else "File: " + str(p[2]), R(fx), R(fy), R(font_size(p)), 90 if round(fa / 90) % 2 else 0, hh, vv, cls])
            for sp in kids(shs, "pin"):
                px, py, pa = at_of(sp)
                text = str(sp[1]); shp = str(sp[2]) if len(sp) > 2 and not isinstance(sp[2], list) else "passive"
                s = font_size(sp)
                k = key(si, px, py); uf.find(k); points.append((px, py, k))
                sheet_pins.append((target, text, k))
                # the pin's angle names the edge it sits on; the text runs into the sheet
                ex, ey = DIRS.get(int(round(pa)) % 360, (1, 0))
                ix, iy = -ex, -ey
                entry["pins"].append({"s": text, "x": R(px), "y": R(py), "k": k, "z": R(s),
                                      "shape": label_shape(shp, px, py, ix, iy, s),
                                      "tx": R(px + ix * s * 1.35), "ty": R(py + iy * s * 1.35), "a": 90 if iy != 0 else 0,
                                      "h": -1 if (ix > 0 or iy < 0) else 1, "v": 0})
            S["sheets"].append(entry)

        # graphic notes
        for pl in kids(sch, "polyline"):
            pts = [(num(p[1]), num(p[2])) for p in kids(kid(pl, "pts"), "xy")]
            w, dash = stroke_of(pl)
            S["notes"]["g"].append(["P", R(w), 0, [R(v) for xy in pts for v in xy]] + ([1] if dash else []))
        for rc in kids(sch, "rectangle"):
            s, e = kid(rc, "start"), kid(rc, "end")
            x1, y1, x2, y2 = num(s[1]), num(s[2]), num(e[1]), num(e[2])
            w, dash = stroke_of(rc)
            S["notes"]["g"].append(["P", R(w), 0, [x1, y1, x2, y1, x2, y2, x1, y2, x1, y1]] + ([1] if dash else []))
        for ar in kids(sch, "arc"):
            s, m, e = kid(ar, "start"), kid(ar, "mid"), kid(ar, "end")
            pts = arc_points((num(s[1]), num(s[2])), (num(m[1]), num(m[2])), (num(e[1]), num(e[2])))
            w, dash = stroke_of(ar)
            S["notes"]["g"].append(["P", R(w), 0, [R(v) for xy in pts for v in xy]])
        for c in kids(sch, "circle"):
            cc = kid(c, "center"); w, dash = stroke_of(c)
            S["notes"]["g"].append(["C", R(w), 0, num(cc[1]), num(cc[2]), num(kid(c, "radius")[1])])
        for tx in kids(sch, "text"):
            if is_hidden(tx): continue
            x, y, a = at_of(tx); h, v = justify(tx)
            S["notes"]["t"].append([resolve(str(tx[1]), {}, textvars), R(x), R(y), R(font_size(tx)), 90 if round(a / 90) % 2 else 0, h, v, "note"])

        # connect everything that sits on a wire (pins, labels, junctions, other wire ends)
        segs = [(w[0], w[1], w[2], w[3], w[4]) for w in S["wires"]]
        for (px, py, k) in points:
            for (x1, y1, x2, y2, wk) in segs:
                if on_segment(px, py, x1, y1, x2, y2):
                    uf.union(k, wk)
        out_sheets.append(S)

    # names that join wires across the sheet: local labels within a sheet, globals and power everywhere
    for name, ks in named.items():
        for k in ks[1:]:
            uf.union(ks[0], k)
    # hierarchical labels meet the matching pin on their sheet symbol in the parent
    for (si, text, k) in hier_labels:
        for (target, ptext, pk) in sheet_pins:
            if target == si and ptext == text:
                uf.union(k, pk)

    # name every group after the netlist net of the pins in it
    group_net, conflicts = {}, 0
    for (k, ref, pnum) in pin_points:
        n = pin_net.get((ref, pnum))
        if not n: continue
        r = uf.find(k)
        if r in group_net and group_net[r] != n:
            conflicts += 1
        group_net.setdefault(r, n)
    for name, ks in named.items():
        r = uf.find(ks[0])
        if r not in group_net:
            group_net[r] = name.split(":", 1)[1] if name.startswith("local") else name[len("global"):]

    net_names = sorted(set(nets) | set(group_net.values()))
    net_index = {n: i for i, n in enumerate(net_names)}
    def nid(k):
        n = group_net.get(uf.find(k))
        return net_index[n] if n is not None else -1

    # replace point keys with net indices
    for S in out_sheets:
        for w in S["wires"]: w[4] = nid(w[4])
        for j in S["junctions"]: j[2] = nid(j[2])
        for L in S["labels"]: L["n"] = nid(L.pop("k"))
        for e in S["sheets"]:
            for p in e["pins"]: p["n"] = nid(p.pop("k"))
        for part in S["parts"]:
            for i, p in enumerate(part["p"]):
                n = pin_net.get((part["r"], p[2]))
                if n is None and "pwr" in part: n = part["pwr"]
                if n is None:
                    n = group_net.get(uf.find((out_sheets.index(S), int(round(p[0] * 100)), int(round(p[1] * 100)))))
                x, y, num_, name, ex, ey, hidden = p
                part["p"][i] = [x, y, num_, name, net_index[n] if n in net_index else -1, ex, ey, hidden]

    # check: every pin the netlist knows should sit in a group of the same name
    checked = bad = 0
    for (k, ref, pnum) in pin_points:
        n = pin_net.get((ref, pnum))
        if not n or len(nets.get(n, [])) < 2: continue
        checked += 1
        if group_net.get(uf.find(k)) != n: bad += 1
    wires_named = sum(1 for S in out_sheets for w in S["wires"] if w[4] >= 0)
    wires_all = sum(len(S["wires"]) for S in out_sheets)
    print(f"pins on a wire group with their netlist net: {checked - bad}/{checked}; groups with two netlist names: {conflicts}; "
          f"wires named: {wires_named}/{wires_all}")

    merge_pins(out_sheets)
    annotate(out_sheets, comps, net_names, nets)
    parts = {}
    for si, S in enumerate(out_sheets):
        for part in S["parts"]:
            if "pwr" in part: continue
            c = comps.get(part["r"], {})
            P = parts.setdefault(part["r"], {"v": c.get("value", ""), "fp": c.get("footprint", ""), "d": c.get("description", ""),
                                             "f": {k: v for k, v in c.get("fields", {}).items() if k not in ("Footprint", "Description", "Datasheet", "ALTIUM_VALUE")},
                                             "s": []})
            if si not in P["s"]: P["s"].append(si)

    for S in out_sheets: S.pop("notes", None); S.pop("paper", None)
    data = {"source": "PCB_Schematics/KiCad/Main/" + ROOT_SCH, "areas": out_sheets, "nets": net_names, "netMeta": net_meta(net_names, nets, out_sheets),
            "netNodes": {n: nodes for n, nodes in nets.items()}, "parts": parts}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {os.path.relpath(OUT, REPO)}: {os.path.getsize(OUT) / 1e6:.1f} MB, {len(out_sheets)} sheets, {len(parts)} parts, {len(net_names)} nets")

if __name__ == "__main__":
    main()
