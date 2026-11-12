#!/usr/bin/env python3
"""KiCad PCB copper → Viewer/mynd_main.copper.json, one entry per net, so board3d.html can light up a connection.

Usage:  python3 Viewer/tools/build_board.py     (after build_schematic.py: it names the copper with the schematic's nets)

The PCB came from Altium and names its nets differently (NetC7D_1 where the schematic says Net-(C7D-Pad1)), so each
PCB net takes the name of the schematic net that most of its pads belong to (ref + pad number).
Geometry stays in board millimetres (x right, y down); the page lays it on the 3D board.
"""
import json, math, os, sys
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_schematic import parse, kids, kid, num, REPO, KDIR, Q

# the zones in the Altium import are not filled: build.sh refills them on a copy (kicad-cli pcb drc --refill-zones) and passes it here
PCB = os.environ.get("MYND_PCB") or os.path.join(KDIR, "Mynd_Main.kicad_pcb")
SCH = os.path.join(REPO, "Viewer/mynd_main.sch.json")
OUT = os.path.join(REPO, "Viewer/mynd_main.copper.json")
R = lambda v: round(v, 3)

def rot(x, y, deg):
    """KiCad angles turn counter-clockwise on screen, with y pointing down"""
    a = math.radians(deg); c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c

def arc_pts(s, m, e, n=10):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9: return [s, e]
    ux = ((x1*x1 + y1*y1) * (y2 - y3) + (x2*x2 + y2*y2) * (y3 - y1) + (x3*x3 + y3*y3) * (y1 - y2)) / d
    uy = ((x1*x1 + y1*y1) * (x3 - x2) + (x2*x2 + y2*y2) * (x1 - x3) + (x3*x3 + y3*y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (s, m, e))
    norm = lambda a: a % (2 * math.pi)
    span = norm(a3 - a1) if norm(a2 - a1) < norm(a3 - a1) else -norm(a1 - a3)
    return [(ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n)) for i in range(n + 1)]

def main():
    sch = json.load(open(SCH))
    pin_net = {(ref, pin): net for net, nodes in sch["netNodes"].items() for ref, pin, _ in nodes}
    pcb = parse(open(PCB, encoding="utf-8").read())
    pcb_names = {int(n[1]): str(n[2]) for n in kids(pcb, "net")}
    general = kid(pcb, "general")
    thickness = num(kid(general, "thickness")[1]) if general and kid(general, "thickness") else 1.6
    cu_layers = [str(l[1]) for l in (kid(pcb, "layers") or []) if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith(".Cu")]

    def net_of(node):
        """the PCB net's name (KiCad ≤ 9 refers to nets by number, KiCad 10 by name); "" for none"""
        n = kid(node, "net")
        if not n or len(n) < 2: return ""
        v = str(n[1])
        return pcb_names.get(int(v), "") if v.isdigit() and not isinstance(n[1], Q) else v

    # per PCB net: { layer: { s: [x1,y1,x2,y2,w,…], c: [x,y,r,…], p: [[x,y,…], …] } }
    geo = defaultdict(lambda: defaultdict(lambda: {"s": [], "c": [], "p": []}))
    votes = defaultdict(Counter)
    pad_pts = []

    def pad_shape(layer_list, net, shape, cx, cy, w, h, ang, rr=0.0):
        targets = []
        for L in layer_list:
            if L == "*.Cu": targets += ["F.Cu", "B.Cu"]
            elif L.endswith(".Cu"): targets.append(L)
        for L in targets:
            g = geo[net][L]
            if shape == "circle":
                g["c"] += [R(cx), R(cy), R(w / 2)]
            elif shape == "oval":
                long_, short = max(w, h), min(w, h)
                dx, dy = ((long_ - short) / 2, 0) if w >= h else (0, (long_ - short) / 2)
                dx, dy = rot(dx, dy, ang)
                g["s"] += [R(cx - dx), R(cy - dy), R(cx + dx), R(cy + dy), R(short)]
            else:     # rect, roundrect, trapezoid, custom: the pad's bounding rectangle
                pts = []
                for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    x, y = rot(sx * w / 2, sy * h / 2, ang); pts += [R(cx + x), R(cy + y)]
                g["p"].append(pts)

    footprints = []
    for fp in kids(pcb, "footprint"):
        at = kid(fp, "at"); fx, fy = num(at[1]), num(at[2]); fa = num(at[3]) if len(at) > 3 else 0
        ref = None
        for p in kids(fp, "property"):
            if p[1] == "Reference": ref = str(p[2])
        if ref is None:
            fpt = kid(fp, "fp_text")
            if fpt and fpt[1] == "reference": ref = str(fpt[2])
        fp_pads = []
        for pad in kids(fp, "pad"):
            n = net_of(pad)
            pnum, shape = str(pad[1]), str(pad[3])
            pa = kid(pad, "at"); px, py = num(pa[1]), num(pa[2]); pang = num(pa[3]) if len(pa) > 3 else 0.0
            dx, dy = rot(px, py, fa); cx, cy = fx + dx, fy + dy
            sz = kid(pad, "size"); w, h = num(sz[1]), num(sz[2])
            layers = [str(x) for x in (kid(pad, "layers") or [])[1:]]
            pad_pts.append((cx, cy))
            if not n: continue
            if ref and (ref, pnum) in pin_net: votes[n][pin_net[(ref, pnum)]] += 1
            prims = kid(pad, "primitives")
            if shape == "custom" and prims:
                for gp in kids(prims, "gr_poly"):
                    pts = []
                    for xy in kids(kid(gp, "pts"), "xy"):
                        x, y = rot(num(xy[1]), num(xy[2]), pang); pts += [R(cx + x), R(cy + y)]
                    for L in [l for l in layers if l.endswith(".Cu")]:
                        for LL in (["F.Cu", "B.Cu"] if L == "*.Cu" else [L]): geo[n][LL]["p"].append(pts)
            pad_shape(layers, n, shape, cx, cy, w, h, pang)
            fp_pads.append([R(cx), R(cy), R(w), R(h), R(pang), shape, n])
        # the footprint: where it is, which side, whether it has a 3D model, whether it is fitted, its outline (courtyard, else the pads)
        side = "B" if str((kid(fp, "layer") or ["", "F.Cu"])[1]).startswith("B") else "F"
        attr = kid(fp, "attr"); dnp = bool(attr and ("dnp" in attr[1:] or "exclude_from_bom" in attr[1:]))
        cx_, cy_ = [], []
        for g in fp:
            if not (isinstance(g, list) and g and g[0] in ("fp_line", "fp_rect", "fp_poly", "fp_circle")): continue
            L = kid(g, "layer")
            if not L or "CrtYd" not in str(L[1]): continue
            for key in ("start", "end", "center"):
                q = kid(g, key)
                if q: x, y = rot(num(q[1]), num(q[2]), fa); cx_.append(fx + x); cy_.append(fy + y)
            for xy in kids(kid(g, "pts") or [], "xy"):
                x, y = rot(num(xy[1]), num(xy[2]), fa); cx_.append(fx + x); cy_.append(fy + y)
        if not cx_:
            for q in fp_pads: cx_ += [q[0] - q[2] / 2, q[0] + q[2] / 2]; cy_ += [q[1] - q[3] / 2, q[1] + q[3] / 2]
        if ref and cx_:
            footprints.append({"r": ref, "x": R(fx), "y": R(fy), "a": R(fa), "side": side, "model": bool(kids(fp, "model")), "dnp": dnp,
                               "box": [R(min(cx_)), R(min(cy_)), R(max(cx_)), R(max(cy_))], "pads": fp_pads})

    for s in kids(pcb, "segment"):
        n = net_of(s); L = str(kid(s, "layer")[1])
        a, b, w = kid(s, "start"), kid(s, "end"), num(kid(s, "width")[1])
        geo[n][L]["s"] += [R(num(a[1])), R(num(a[2])), R(num(b[1])), R(num(b[2])), R(w)]
    for s in kids(pcb, "arc"):
        n = net_of(s); L = str(kid(s, "layer")[1]); w = num(kid(s, "width")[1])
        pts = arc_pts(*[(num(kid(s, k)[1]), num(kid(s, k)[2])) for k in ("start", "mid", "end")])
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]): geo[n][L]["s"] += [R(x1), R(y1), R(x2), R(y2), R(w)]
    vias = []
    for v in kids(pcb, "via"):
        n = net_of(v); at = kid(v, "at"); d = num(kid(v, "size")[1]); dr = num(kid(v, "drill")[1]) if kid(v, "drill") else d / 2
        vias.append([R(num(at[1])), R(num(at[2])), R(d / 2), R(dr / 2), n])
    regions = 0
    for gp in kids(pcb, "gr_poly"):
        L = kid(gp, "layer"); n = net_of(gp)
        if not L or not str(L[1]).endswith(".Cu") or not n: continue
        pts = [v for xy in kids(kid(gp, "pts"), "xy") for v in (R(num(xy[1])), R(num(xy[2])))]
        if len(pts) >= 6: geo[n][str(L[1])]["p"].append(pts); regions += 1
        w = kid(gp, "stroke"); w = kid(w, "width") if w else kid(gp, "width")
        if w and num(w[1]) > 0:                      # the outline has width too
            for i in range(0, len(pts), 2):
                j = (i + 2) % len(pts); geo[n][str(L[1])]["s"] += [pts[i], pts[i + 1], pts[j], pts[j + 1], R(num(w[1]))]
    print(f"copper regions (board polygons with a net): {regions}")
    for z in kids(pcb, "zone"):
        n = net_of(z)
        for fpoly in kids(z, "filled_polygon"):
            L = str(kid(fpoly, "layer")[1])
            pts = [v for xy in kids(kid(fpoly, "pts"), "xy") for v in (R(num(xy[1])), R(num(xy[2])))]
            if len(pts) >= 6: geo[n][L]["p"].append(pts)

    # check the pad transform: track ends should land on pad centres
    ends = set()
    for s in kids(pcb, "segment"):
        for k in ("start", "end"): p = kid(s, k); ends.add((round(num(p[1]), 2), round(num(p[2]), 2)))
    hit = sum(1 for x, y in pad_pts if (round(x, 2), round(y, 2)) in ends)
    print(f"pads with a track ending on their centre: {hit}/{len(pad_pts)}")

    names = {n: (votes[n].most_common(1)[0][0] if votes[n] else n) for n in set(geo) | {v[4] for v in vias} if n}
    unmatched = [n for n in geo if n and not votes[n]]
    print(f"PCB nets named after the schematic: {sum(1 for n in names if votes[n])}/{len(names)}; copper without a schematic name: {len(unmatched)}")

    nets = {}
    for n, layers in geo.items():
        if not n: continue
        nm = names.get(n, n)
        entry = nets.setdefault(nm, {})
        for L, g in layers.items():
            e = entry.setdefault(L, {"s": [], "c": [], "p": []})
            for k in ("s", "c", "p"): e[k] += g[k]
    # vias go through every copper layer: [x, y, pad radius, drill radius, net name]
    via_list = [[x, y, r, dr, names.get(n, n)] for x, y, r, dr, n in vias if n]
    per_layer = Counter()
    for per in nets.values():
        for L, g in per.items(): per_layer[L] += len(g["s"]) // 5 + len(g["c"]) // 3 + len(g["p"])
    print("copper items per layer:", dict(per_layer), "· vias:", len(via_list))

    for f in footprints:
        for q in f["pads"]: q[6] = names.get(q[6], q[6]) if q[6] else ""
    data = {"thickness": thickness, "layers": cu_layers, "nets": nets, "vias": via_list, "footprints": footprints}
    with open(OUT, "w") as f: json.dump(data, f, separators=(",", ":"))
    print(f"wrote {os.path.relpath(OUT, REPO)}: {os.path.getsize(OUT) / 1e6:.1f} MB, {len(nets)} nets on {', '.join(cu_layers)}")

if __name__ == "__main__":
    main()
