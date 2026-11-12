"""One line out of a chip per supply: pins on the same side of a chip or transistor that share a net
(VDD VDD VDD, GND GND, a FET's three S pins) become one pin, and the wires that only joined them go.

KiCad draws a comb: a stub from every pin to a bus. Here every pin but one is dropped, then
  - wire stubs that now end in nothing are pruned, one segment at a time;
  - a group of wires that touched a dropped pin and reaches no other part (a ground symbol hung on the pin) goes too;
  - a pin whose wires reach other parts on their own (a decoupling capacitor right at that pin) stays, so no part loses its drawing.
Connectivity does not change: the netlist still lists every pin, and the tooltip names them all.

Pins: [x, y, number, name, net, ex, ey, hidden] → an extra field: 0 kept as is, -1 merged away, n ≥ 2 stands for n pins.
"""
import math, re
from collections import defaultdict

Q = lambda x, y: (int(round(x * 100)), int(round(y * 100)))

def on_segment(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    if not (min(ax, bx) - 1 <= px <= max(ax, bx) + 1 and min(ay, by) - 1 <= py <= max(ay, by) + 1): return False
    return abs((bx - ax) * (py - ay) - (by - ay) * (px - ax)) <= 1.0 * max(1.0, math.hypot(bx - ax, by - ay))

def split_wires(S):
    """cut every wire where another wire, a pin or a label touches its middle, so the graph is built from end points only"""
    pts = set()
    for w in S["wires"]: pts.add(Q(w[0], w[1])); pts.add(Q(w[2], w[3]))
    for part in S["parts"]:
        for p in part["p"]: pts.add(Q(p[0], p[1]))
    for L in S["labels"]: pts.add(Q(L["x"], L["y"]))
    for e in S["sheets"]:
        for p in e["pins"]: pts.add(Q(p["x"], p["y"]))
    for j in S["junctions"]: pts.add(Q(j[0], j[1]))
    out = []
    for w in S["wires"]:
        a, b = Q(w[0], w[1]), Q(w[2], w[3])
        inner = [p for p in pts if p != a and p != b and on_segment(p, a, b)]
        if not inner: out.append(w); continue
        inner.sort(key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
        chain = [a] + inner + [b]
        for u, v in zip(chain, chain[1:]): out.append([u[0] / 100, u[1] / 100, v[0] / 100, v[1] / 100, w[4]])
    S["wires"] = out

def terminals(S, dropped):
    """point → what ends there: ("pin", ref) for part pins still drawn, ("other", …) for power symbols, labels, sheet pins"""
    t = defaultdict(list)
    for pi, part in enumerate(S["parts"]):
        if part.get("_gone"): continue
        for k, p in enumerate(part["p"]):
            if (pi, k) in dropped or (len(p) > 8 and p[8] == -1): continue
            t[Q(p[0], p[1])].append(("power", pi) if "pwr" in part else ("pin", part["r"]))
    for li, L in enumerate(S["labels"]):
        if not L.get("_gone"): t[Q(L["x"], L["y"])].append(("label", li))
    for e in S["sheets"]:
        for p in e["pins"]: t[Q(p["x"], p["y"])].append(("sheet", 0))
    for x, y in S["nc"]: t[Q(x, y)].append(("nc", 0))
    return t

def graph(wires, net):
    adj = defaultdict(list)
    for i, w in enumerate(wires):
        if w is None or w[4] != net: continue
        a, b = Q(w[0], w[1]), Q(w[2], w[3]); adj[a].append((i, b)); adj[b].append((i, a))
    return adj

def component(adj, start):
    seen, wires, stack = {start}, set(), [start]
    while stack:
        n = stack.pop()
        for i, m in adj.get(n, []):
            wires.add(i)
            if m not in seen: seen.add(m); stack.append(m)
    return seen, wires

def side(p):
    dx, dy = p[5] - p[0], p[6] - p[1]
    return (0, 1 if dy > 0 else -1) if abs(dy) > abs(dx) else (1 if dx > 0 else -1, 0)

def merge_pins(sheets):
    merged = straight = short = 0
    for S in sheets:
        split_wires(S)
        for part in S["parts"]:
            for p in part["p"]:
                if len(p) < 9: p.append(0)
        for pi, part in enumerate(S["parts"]):
            if "pwr" in part or not re.match(r"(U|Q)\d", part["r"]): continue
            groups = defaultdict(list)
            for k, p in enumerate(part["p"]):
                if p[4] >= 0 and not p[7]: groups[(p[4], side(p))].append(k)
            for (net, _), ks in groups.items():
                if len(ks) < 2: continue
                merged += apply_group(S, pi, net, ks)
        # what is left over: dangling stubs, junction dots that no longer join three things
        prune_all(S)
        S["wires"] = [w for w in S["wires"] if w is not None]
        straight += straighten_fets(S)
        short += shorten_chains(S)
        S["parts"] = [p for p in S["parts"] if not p.get("_gone")]
        S["labels"] = [L for L in S["labels"] if not L.get("_gone")]
        S["wires"] = [w for w in S["wires"] if w is not None]
        deg = defaultdict(int)
        for w in S["wires"]: deg[Q(w[0], w[1])] += 1; deg[Q(w[2], w[3])] += 1
        for k, n in terminals(S, set()).items(): deg[k] += len(n)
        S["junctions"] = [j for j in S["junctions"] if deg[Q(j[0], j[1])] >= 3]
    print(f"merged {merged} duplicate chip and transistor pins into a shared one; straightened {straight} transistor pairs; shortened {short} lines from chip and connector pins")

def apply_group(S, pi, net, ks):
    part = S["parts"][pi]
    adj = graph(S["wires"], net)
    best = None
    for keep in ks:
        others = [k for k in ks if k != keep]
        dropped = {(pi, k) for k in others}
        term = terminals(S, dropped)
        keep_pt = Q(*part["p"][keep][:2])
        keep_nodes, _ = component(adj, keep_pt)
        ok, wire_cost = [], 0
        for k in others:
            pt = Q(*part["p"][k][:2])
            nodes, wires = component(adj, pt)
            if keep_pt in nodes or keep_nodes & nodes: ok.append(k); continue
            # its own little network: fine to drop when no other part hangs on it
            if not any(kind == "pin" for n in nodes for kind, _ in term.get(n, [])): ok.append(k)
        score = (len(ok), -left_after(S, net, {(pi, k) for k in ok}), -abs(ks.index(keep) - (len(ks) - 1) / 2))
        if best is None or score > best[0]: best = (score, keep, ok)
    _, keep, ok = best
    if not ok: return 0
    names = []
    for k in [keep] + ok:
        n = part["p"][k][3]
        if n and n not in names: names.append(n)
    kp = part["p"][keep]
    kp[3] = "/".join(names) if names else kp[3]; kp[8] = len(ok) + 1
    gone_nums = set()
    for k in ok: part["p"][k][8] = -1; gone_nums.add(part["p"][k][2])
    # the dropped pins' names go; the kept one shows the shared name
    part["t"] = [t for t in part["t"] if not (len(t) > 8 and t[8] in gone_nums)]
    for t in part["t"]:
        if len(t) > 8 and t[8] == kp[2] and t[7] == "pn": t[0] = kp[3]
    # networks that only served a dropped pin: wires, ground symbols and labels on them go
    dropped = {(pi, k) for k in ok}
    term = terminals(S, dropped)
    keep_nodes, _ = component(adj, Q(*kp[:2]))
    for k in ok:
        nodes, wires = component(adj, Q(*part["p"][k][:2]))
        if nodes & keep_nodes: continue
        if any(kind == "pin" for n in nodes for kind, _ in term.get(n, [])): continue
        for i in wires: S["wires"][i] = None
        for n in nodes:
            for kind, idx in term.get(n, []):
                if kind == "power": S["parts"][idx]["_gone"] = True
                elif kind == "label": S["labels"][idx]["_gone"] = True
        adj = graph(S["wires"], net)
    return len(ok)

def left_after(S, net, dropped):
    """how much wire of this net would stay if these pins went (dangling stubs pruned): less is straighter"""
    wires = [w for w in S["wires"] if w is not None and w[4] == net]
    term = terminals(S, dropped)
    while True:
        deg = defaultdict(int)
        for w in wires: deg[Q(w[0], w[1])] += 1; deg[Q(w[2], w[3])] += 1
        keep = [w for w in wires if all(deg[e] > 1 or term.get(e) for e in (Q(w[0], w[1]), Q(w[2], w[3])))]
        if len(keep) == len(wires): break
        wires = keep
    return sum(math.hypot(w[2] - w[0], w[3] - w[1]) for w in wires)

def straighten_fets(S):
    """a transistor reads as one straight line from source to drain. When its two kept pins sit on different rows and one
    side's net only joins two transistors (the middle of a back-to-back switch), that net is redrawn straight on the row
    of the outside wires, using the pins of both transistors that lie on it."""
    fets = {}
    for pi, part in enumerate(S["parts"]):
        if "pwr" in part or not re.match(r"Q\d", part["r"]): continue
        pins = [p for p in part["p"] if p[4] >= 0]
        names = {p[3].split("/")[0] for p in pins}
        if {"D", "S"} <= names: fets[pi] = part
    done = 0
    for pi, part in fets.items():
        kept = {side: next((p for p in part["p"] if p[3].split("/")[0] == side and p[8] != -1), None) for side in "DS"}
        if not all(kept.values()): continue
        d, s_ = kept["D"], kept["S"]
        horiz = abs(d[0] - s_[0]) > abs(d[1] - s_[1]); row = (lambda p: p[1]) if horiz else (lambda p: p[0])
        if abs(row(d) - row(s_)) < 0.01: continue
        for side, other in (("D", "S"), ("S", "D")):
            net = kept[side][4]; target = row(kept[other])
            # who is on this net in this area: only pins of transistors?
            on = [(qi, q) for qi, q in enumerate(S["parts"]) for p in q["p"] if p[4] == net and p[8] != -1]
            if any("pwr" in q or not re.match(r"Q\d", q["r"]) for _, q in on): continue
            if any(L["n"] == net for L in S["labels"]): continue
            others = {qi for qi, _ in on if qi != pi}
            if len(others) != 1: continue
            oi = others.pop(); oq = S["parts"][oi]
            mine = [p for p in part["p"] if p[4] == net and abs(row(p) - target) < 0.01]
            theirs = [p for p in oq["p"] if p[4] == net and abs(row(p) - target) < 0.01]
            if not mine or not theirs: continue
            # the other transistor's far side must sit on the same row, or it would get the bump instead
            ofar = next((p for p in oq["p"] if p[4] >= 0 and p[4] != net and p[3].split("/")[0] in "DS" and p[8] != -1), None)
            if ofar is None or abs(row(ofar) - target) > 0.01: continue
            a, b = mine[0], theirs[0]
            for q, new in ((part, a), (oq, b)):
                old = next(p for p in q["p"] if p[4] == net and p[8] != -1)
                if old is not new: new[8], new[3], old[8] = old[8], old[3], -1
            S["wires"] = [w for w in S["wires"] if w is None or w[4] != net] + [[a[0], a[1], b[0], b[1], net]]
            S["junctions"] = [j for j in S["junctions"] if j[2] != net]
            done += 1
            break
    return done

U = 2.54                                    # one grid unit
TWO_PIN = re.compile(r"(R|C|L|FB|D|ESD|F)\d")

def translate(part, dx, dy):
    """move a part (pins, outline, texts) on the sheet"""
    for p in part["p"]: p[0] += dx; p[1] += dy; p[5] += dx; p[6] += dy
    b = part["b"]; part["b"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
    for g in part["g"]:
        if g[0] == "C": g[3] += dx; g[4] += dy
        else: g[3] = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(g[3])]
    for t in part["t"]: t[1] += dx; t[2] += dy

def move_label(L, dx, dy):
    L["x"] += dx; L["y"] += dy
    if "tx" in L: L["tx"] += dx; L["ty"] += dy
    if L.get("shape"): L["shape"] = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(L["shape"])]

def shorten_chains(S):
    """A pin of a chip or connector whose line runs straight to a net name, maybe through one series part
    (pin ─ R ─ name), gets a compact, regular layout: the part 2 units from the pin, the name 2 units after it;
    a bare line is 3 units long. Only the run of wire attached to the pin counts (the same name may appear elsewhere),
    and only runs that are straight with nothing else hanging on them are touched."""
    done = 0
    for pi, part in enumerate(S["parts"]):
        if "pwr" in part or not re.match(r"(U|P)\d", part["r"]): continue
        for k in range(len(part["p"])):
            if chain(S, pi, k): done += 1
    S["wires"] = [w for w in S["wires"] if w is not None]
    S["labels"] = [L for L in S["labels"] if not L.get("_gone")]
    return done

def ends_at(S):
    """point → what ends there: ("pin", part, k) incl. power symbols, ("label", i)"""
    t = defaultdict(list)
    for qi, q in enumerate(S["parts"]):
        for qk, p in enumerate(q["p"]):
            if p[4] >= 0 and p[8] != -1: t[Q(p[0], p[1])].append(("pin", qi, qk))
    for li, L in enumerate(S["labels"]): t[Q(L["x"], L["y"])].append(("label", li, 0))
    for e in S["sheets"]:
        for sp in e["pins"]: t[Q(sp["x"], sp["y"])].append(("sheet", 0, 0))
    return t

def run_from(S, start, net):
    """the wires connected to this point on this net, and the points they reach"""
    adj = defaultdict(list)
    for i, w in enumerate(S["wires"]):
        if w is None or w[4] != net: continue
        a, b = Q(w[0], w[1]), Q(w[2], w[3]); adj[a].append((i, b)); adj[b].append((i, a))
    return component(adj, start)

def chain(S, pi, k):
    part = S["parts"][pi]; p = part["p"][k]
    if p[4] < 0 or p[7] or p[8] == -1: return False
    dx, dy = p[0] - p[5], p[1] - p[6]; L0 = math.hypot(dx, dy)
    if L0 < 1e-6: return False
    dx, dy = round(dx / L0), round(dy / L0)
    if abs(dx) + abs(dy) != 1: return False
    ox, oy = p[0], p[1]
    t_of = lambda x, y: (x - ox) * dx + (y - oy) * dy
    on_ray = lambda x, y: abs((x - ox) * dy - (y - oy) * dx) < 0.01 and t_of(x, y) > -0.01
    ends = ends_at(S)
    def run(point, net, exclude):
        """a straight run along the ray: (wires, [what ends on it]) or None"""
        nodes, wires = run_from(S, Q(*point), net)
        for i in wires:
            w = S["wires"][i]
            if not (on_ray(w[0], w[1]) and on_ray(w[2], w[3])): return None
        found = []
        for n in nodes:
            for e in ends.get(n, []):
                if e[0] == "pin" and (e[1], e[2]) in exclude: continue
                found.append(e)
        return wires, found
    def pos(e):
        return (S["labels"][e[1]]["x"], S["labels"][e[1]]["y"]) if e[0] == "label" else tuple(S["parts"][e[1]]["p"][e[2]][:2])
    # what may move with the name: labels, power symbols, and test points hanging on the line
    is_name = lambda e: e[0] == "label" or (e[0] == "pin" and ("pwr" in S["parts"][e[1]] or re.match(r"TP\d", S["parts"][e[1]]["r"])))
    def one_name(found):
        """one label per run: keep the outermost (a hierarchical one if there is one), mark the rest to go"""
        lbl = [e for e in found if e[0] == "label"]
        if len(lbl) < 2: return found, []
        keep = max(lbl, key=lambda e: (S["labels"][e[1]]["kind"] == "h", t_of(S["labels"][e[1]]["x"], S["labels"][e[1]]["y"])))
        return [e for e in found if e[0] != "label" or e == keep], [e for e in lbl if e != keep]
    ra = run((ox, oy), p[4], {(pi, k)})
    if not ra or not ra[0]: return False
    wa, ea = ra
    ea, extra_a = one_name(ea)
    if any(e[0] == "sheet" for e in ea): return False
    names = [e for e in ea if is_name(e)]; others = [e for e in ea if not is_name(e)]
    moves, new_wires, drop, extra_b = [], [], set(wa), []
    if not others and names:
        ts = [t_of(*pos(e)) for e in names]
        shift = 3 * U - min(ts)
        if shift > -0.01: return False                       # only ever shorten
        moves = [(e, dx * shift, dy * shift) for e in names]
        far = max(ts) + shift
        new_wires = [[ox, oy, ox + dx * far, oy + dy * far, p[4]]]
    elif len(others) == 1 and not names and others[0][0] == "pin":
        _, qi, qk = others[0]; X = S["parts"][qi]
        if not TWO_PIN.match(X["r"]) or len(X["p"]) != 2: return False
        x1, x2 = X["p"][qk], X["p"][1 - qk]
        if not (on_ray(*x1[:2]) and on_ray(*x2[:2])): return False
        t1, t2 = t_of(*x1[:2]), t_of(*x2[:2])
        if t2 <= t1 or x2[4] < 0 or x2[4] == p[4]: return False
        rb = run(x2[:2], x2[4], {(qi, 1 - qk)})
        if not rb: return False
        wb, eb = rb
        eb, extra_b = one_name(eb)
        if not eb or any(not is_name(e) for e in eb): return False
        tb = [t_of(*pos(e)) for e in eb]
        if min(tb) < t2 - 0.01: return False
        s1 = 2 * U - t1
        if s1 > 0.01: return False
        s2 = (t2 + s1 + 2 * U) - min(tb)
        moves = [(("part", qi, 0), dx * s1, dy * s1)] + [(e, dx * s2, dy * s2) for e in eb]
        n1 = (ox + dx * 2 * U, oy + dy * 2 * U); n2 = (n1[0] + dx * (t2 - t1), n1[1] + dy * (t2 - t1))
        far = max(tb) + s2 - (t2 + s1)
        new_wires = [[ox, oy, n1[0], n1[1], p[4]], [n2[0], n2[1], n2[0] + dx * far, n2[1] + dy * far, x2[4]]]
        drop |= set(wb)
    else:
        return False
    # one name per run is enough: the others (a hierarchical and a local label saying the same) go
    for e in extra_a + extra_b: S["labels"][e[1]]["_gone"] = True
    moved = set()
    for e, mx, my in moves:
        if e[0] == "label": move_label(S["labels"][e[1]], mx, my)
        elif e[1] not in moved: translate(S["parts"][e[1]], mx, my); moved.add(e[1])
    dropped_pts = set()
    for i in drop:
        w = S["wires"][i]; dropped_pts |= {Q(w[0], w[1]), Q(w[2], w[3])}; S["wires"][i] = None
    S["junctions"] = [j for j in S["junctions"] if Q(j[0], j[1]) not in dropped_pts]
    for w in new_wires:
        if math.hypot(w[2] - w[0], w[3] - w[1]) > 0.01: S["wires"].append(w)
    return True

def prune_all(S):
    """drop wire segments that end in nothing, until none are left"""
    dropped = {(pi, k) for pi, part in enumerate(S["parts"]) for k, p in enumerate(part["p"]) if len(p) > 8 and p[8] == -1}
    while True:
        term = terminals(S, dropped)
        deg = defaultdict(int)
        for w in S["wires"]:
            if w is None: continue
            deg[Q(w[0], w[1])] += 1; deg[Q(w[2], w[3])] += 1
        gone = 0
        for i, w in enumerate(S["wires"]):
            if w is None: continue
            for e in (Q(w[0], w[1]), Q(w[2], w[3])):
                if deg[e] == 1 and not term.get(e):
                    S["wires"][i] = None; gone += 1; break
        if not gone: return
