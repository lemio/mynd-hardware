"""Annotations for the MYND main board: everything a netlist cannot say on its own.

As in Schematik (circuit_process.md, "keeping model steps honest"): this file only adds names, kinds and groups.
Connections always come from KiCad; a ref or net named here that does not exist is reported by build_schematic.py.
"""
import re

# the KiCad sheets become areas of one map, each with a block title
AREAS = {
    "Root": "Connectors",
    "U_Charger_BQ25713": "Battery charger & supplies",
    "U_Boost": "24 V boost",
    "U_MCU": "Microcontroller",
    "U_USB": "USB-C & power delivery",
    "U_AudioDet": "Audio detection",
}
# where the areas go: rows of area names, left to right (each row starts below the tallest area above it)
ROWS = [["U_USB", "U_Charger_BQ25713", "U_Boost"], ["U_MCU", "Root", "U_AudioDet"]]
GAP = 30          # mm between areas

# function names for the chips (Schematik: "name parts", step 6); the part number stays as the second line
NAMES = {
    "U1C": "Microcontroller", "U2E": "Battery charge\ncontroller", "U1B": "Boost\ncontroller", "U4E": "Buck\nregulator",
    "U3E": "Buck\nregulator", "U1E": "Current sense\namplifier", "U2B": "Schmitt\ninverter", "U1U": "USB-C PD\ncontroller",
    "U3U": "USB 2.0\nswitch", "U4U": "USB 2.0\nswitch", "U2U": "EEPROM", "U1D": "Dual\nop-amp", "U2D": "Dual\nop-amp",
    "D1C": "Dual diode", "L1U": "Common-mode\nchoke",
    # the connectors: which board each one goes to
    "P1M": "Battery\nconnector", "P2M": "Amplifier board\nconnector", "P3M": "Bluetooth board\nconnector",
    "P4M": "USB board\nconnector", "P5M": "UI board\nconnector", "P6M": "Aux jack board\nconnector",
}

# net colour groups, in Schematik's palette: warm colours carry power, cool ones carry signals.
# (colour, legend label, power?, test on the short net name, description)
GROUPS = [
    ("#111111", "ground", True, lambda n: n == "GND", "0 V: every current returns here."),
    ("#d7263d", "3.3 V and 1.5 V supplies", True, lambda n: n in {"3V3_MCU", "3V3_STB", "LDO_3V3", "3V3_ADet", "VDDA", "LDO_1V5"},
     "The low-voltage rails for the microcontroller, the standby circuits and the audio detection."),
    ("#f77f00", "USB power", True, lambda n: n in {"VBUS", "VBUS_C", "VBUS_2", "5V_CCPD"}, "Power from the USB-C plug, before and after the input switch."),
    ("#f4a261", "system power", True, lambda n: n in {"VSys", "VSys1"}, "The system rail the charger makes from USB or the battery."),
    ("#9c6644", "battery", True, lambda n: n in {"intBat", "BatVCC", "BVcc"}, "The battery pack and its connection to the charger."),
    ("#ffb703", "24 V boost output", True, lambda n: n == "24V", "The amplifier supply made by the boost converter."),
    ("#7c3aed", "control: reset, enable, interrupts", False,
     lambda n: bool(re.search(r"^~\{|RST|Rst|Ena|En_|Hold|Ctrl|WkUp|Btn|Plug_Det|Int|ACOK|PRHOT|PDN|Flt|SEL|M_DET|EN", n)),
     "Lines that switch something on or off, reset a chip or raise an interrupt."),
    ("#1e3a8a", "USB data and configuration", False, lambda n: bool(re.search(r"USB|SBU|CC1|CC2|PD_IO", n)),
     "USB data pairs, the CC lines that negotiate power, and the PD controller's pins."),
    ("#0d9488", "I²C", False, lambda n: bool(re.search(r"SCL|SDA", n)), "The two-wire buses between the microcontroller, charger, PD controller and EEPROM."),
    ("#16a34a", "audio", False, lambda n: bool(re.search(r"BCLK|DATA|LRCK|Line_", n)), "Digital audio (I²S) and the analogue line signals."),
    ("#0ea5e9", "UART and debug", False, lambda n: bool(re.search(r"UART|_RX|_TX|SWCLK|SWDIO", n)), "Serial links and the programming port."),
    ("#db2777", "sensing", False, lambda n: bool(re.search(r"ISens|ISns|PSYS|ADC|NTC|Env_Track|HW_REV|Ver|Open|PTM", n)),
     "Measured values: current, power, battery voltage and temperature, version straps."),
    ("#9aa3ad", "local connections", False, lambda n: True, "Unnamed connections between neighbouring parts."),
]

def short_name(net):
    if net.startswith("Net-") or net.startswith("unconnected-"): return ""
    s = net.split("/")[-1] or net
    if s.startswith("\\"): s = "~{" + s[1:] + "}"      # Altium's \NAME: active low
    return s

def connector_name(desc):
    m = re.search(r"(\d+)P", desc or "")
    n = f"{m.group(1)}-pin " if m else ""
    if "WTB" in desc: return n + "wire-to-board\nconnector"
    if "BTB" in desc: return n + "board-to-board\nconnector"
    if "HEADER" in desc: return n + "header"
    return n + "connector"

PFET = re.compile(r"\d+P\d+|3401|2301|3415")

def kind_of(ref, value, pins):
    m = re.match(r"[A-Za-z_]+", ref); pre = m.group(0) if m else ""
    names = {p[3] for p in pins}
    n = len(pins)
    if pre == "R" and n == 2: return "r"
    if pre == "C" and n == 2: return "c"
    if pre == "L" and n == 2: return "l"
    if pre == "FB" and n == 2: return "fb"
    if pre == "ESD" and n == 2: return "tvs"
    if pre == "D" and n == 2: return "d"
    if pre == "Q" and {"G", "D", "S"} <= names: return "pfet" if PFET.search(value) else "nfet"
    if pre == "TP" and n == 1: return "tp"
    return "box"

def annotate(sheets, comps, net_names, nets):
    unknown = [r for r in NAMES if r not in comps]
    if unknown: print("annotations name parts that are not in the netlist:", unknown)
    for S in sheets:
        S["title"] = AREAS.get(S["name"], S["title"])
        keep = []
        for part in S["parts"]:
            if "pwr" in part:
                # a power symbol: which way its body points from the pin, and whether it is ground
                pin = part["p"][0]
                pts = [v for g in part["g"] for v in ([g[3], g[4]] if g[0] == "C" else list(zip(g[3][0::2], g[3][1::2])))] if part["g"] else []
                pts = [p for p in pts if isinstance(p, tuple)]
                cx = sum(p[0] for p in pts) / len(pts) if pts else pin[0]
                cy = sum(p[1] for p in pts) / len(pts) if pts else pin[1] - 1
                dx, dy = cx - pin[0], cy - pin[1]
                part["dir"] = [0, -1 if dy < 0 else 1] if abs(dy) >= abs(dx) else [-1 if dx < 0 else 1, 0]
                part["gnd"] = part["pwr"] == "GND"
                part["g"] = []; part["t"] = []
                keep.append(part); continue
            if not any(p[4] >= 0 for p in part["p"]) and not part["r"].startswith("TP"):
                continue                                  # holes, marks, logos: nothing to connect
            c = comps.get(part["r"], {})
            k = kind_of(part["r"], c.get("value", ""), part["p"])
            part["k"] = k
            if k == "box":
                desc = c.get("description", "")
                part["name"] = NAMES.get(part["r"]) or (connector_name(desc) if part["r"].startswith("P") else c.get("value", ""))
                part["pn"] = c.get("value", "") if part["r"] in NAMES or part["r"].startswith("P") else ""
            else:
                part["g"] = []                              # drawn as a Schematik glyph instead
                part["t"] = [t for t in part["t"] if t[7] == "s"] if k == "box" else []
            keep.append(part)
        S["parts"] = keep
    # content bounds of each area, then the areas in rows
    for S in sheets:
        xs, ys = [], []
        for part in S["parts"]:
            if "pwr" in part:
                xs.append(part["p"][0][0]); ys.append(part["p"][0][1]); continue
            xs += [part["b"][0], part["b"][2]]; ys += [part["b"][1], part["b"][3]]
        for w in S["wires"]: xs += [w[0], w[2]]; ys += [w[1], w[3]]
        for L in S["labels"]: xs.append(L["x"]); ys.append(L["y"])
        for e in S["sheets"]: xs += [e["x"], e["x"] + e["w"]]; ys += [e["y"], e["y"] + e["h"]]
        S["bb"] = [min(xs) - 6, min(ys) - 6, max(xs) + 6, max(ys) + 6]
    by = {S["name"]: S for S in sheets}
    y = 0
    for row in ROWS:
        x, h = 0, 0
        for name in row:
            S = by[name]; bw, bh = S["bb"][2] - S["bb"][0], S["bb"][3] - S["bb"][1]
            S["ox"], S["oy"] = x - S["bb"][0], y - S["bb"][1] + 14     # room for the block title
            x += bw + GAP; h = max(h, bh + 14)
        y += h + GAP

# ---------------------------------------------------------------------
# What each rail is for, and how power flows (traced from the netlist: see the parts named in each line)
# ---------------------------------------------------------------------
RAILS = {
    "VBUS_C": "Raw USB-C VBUS from the plug (P4M), 5–20 V as negotiated by the PD controller U1U. Goes through the input switch Q1U + Q2U.",
    "VBUS": "USB power after the input switch Q1U + Q2U, which the PD controller U1U turns on. Feeds the charger through the 20 mΩ current-sense resistors R1E/R4E.",
    "VBUS_2": "Charger input after the current-sense resistors R1E/R4E: the buck-boost charger U2E switches it with Q1E–Q4E and L1E.",
    "intBat": "The battery pack (connector P1M pins 6–8). Reaches the system rail through the 5 mΩ sense resistors R14E/R15E and the battery switch Q5E.",
    "BatVCC": "Battery voltage through 10 kΩ (R50E) to the amplifier connector P2M pin 9: a sense line, not a supply.",
    "VSys": "The system rail. The charger U2E makes it from USB (buck-boost Q1E–Q4E, L1E), or connects the battery with Q5E. Every other supply starts here.",
    "VSys1": "VSys through ferrite bead FB1E: input of the 5 V buck U4E, and out on connector P3M pin 9.",
    "3V3_STB": "Standby 3.3 V, made from VSys by the buck U3E (through diode D1E, inductor L2E). Feeds the PD controller, current-sense amp, inverter and the connectors; the microcontroller gets it through FB18C.",
    "3V3_MCU": "The microcontroller's 3.3 V: 3V3_STB through ferrite bead FB18C, with a capacitor at every VDD pin of U1C.",
    "3V3_ADet": "3.3 V for the audio-detection op-amps U1D/U2D: 3V3_STB through R20D and ferrite bead FB1D.",
    "5V_CCPD": "5 V made from VSys1 by the buck U4E (inductor L3E). Powers the PD controller's PP5V (VCONN/source) and goes to connector P3M pin 23.",
    "24V": "Amplifier supply: the boost controller U1B (Q2B, Q3B, L1B) makes it from VSys, switched in by Q1B. Leaves on connector P2M pins 1–4.",
    "LDO_3V3": "The PD controller U1U's own 3.3 V regulator output. Only powers the EEPROM U2U and the SBU switches Q4U/Q5U — not the microcontroller.",
    "LDO_1V5": "The PD controller U1U's internal 1.5 V core regulator: a decoupling capacitor only.",
    "VDDA": "The charger U2E's internal analogue supply: a decoupling capacitor only.",
    "BVcc": "The boost controller U1B's internal gate-drive supply: decoupling only.",
    "GND": "0 V: every current returns here.",
}
# the power tree in the legend: (depth, nets, parts, text)
POWER_TREE = [
    (0, ["VBUS_C"], ["P4M"], "USB-C plug P4M → VBUS_C"),
    (1, ["VBUS"], ["Q1U", "Q2U", "U1U"], "input switch Q1U + Q2U, opened by PD controller U1U → VBUS"),
    (2, ["VBUS_2"], ["R1E", "R4E"], "current sense R1E/R4E → VBUS_2"),
    (0, ["intBat"], ["P1M"], "battery connector P1M → intBat"),
    (1, [], ["Q5E", "R14E", "R15E"], "battery switch Q5E"),
    (0, ["VSys"], ["U2E", "Q1E", "Q2E", "Q3E", "Q4E", "L1E"], "charger U2E → VSys, the system rail"),
    (1, ["3V3_STB"], ["D1E", "U3E", "L2E"], "buck U3E → 3V3_STB (standby)"),
    (2, ["3V3_MCU"], ["FB18C", "U1C"], "FB18C → 3V3_MCU → microcontroller U1C"),
    (2, ["3V3_ADet"], ["R20D", "FB1D", "U1D", "U2D"], "R20D, FB1D → 3V3_ADet → audio detection"),
    (2, [], ["U1U", "U1E", "U2B", "P2M", "P3M", "P5M"], "PD controller, current sense, connectors"),
    (1, ["VSys1", "5V_CCPD"], ["FB1E", "U4E", "L3E"], "FB1E → buck U4E → 5V_CCPD (PD controller, P3M)"),
    (1, ["24V"], ["Q1B", "U1B", "L1B", "Q2B", "Q3B", "P2M"], "switch Q1B → boost U1B → 24V → amplifier P2M"),
    (0, ["LDO_3V3", "LDO_1V5"], ["U1U", "U2U"], "PD controller's own LDOs → LDO_3V3 (EEPROM U2U)"),
]

SIGNALS = [   # (pattern on the short name, purpose)
    (r"USB.*_[NP]$|USB_[A-Z]+_[NP]$", "USB 2.0 data pair"), (r"USB-CC|CC[12]$", "USB-C configuration channel: the PD controller negotiates power here"),
    (r"SBU", "USB-C sideband use line"), (r"USB_SEL", "selects which way the USB 2.0 switch routes the data pair"),
    (r"PD_SCL|PD_SDA", "I²C to the USB-C PD controller"), (r"SCL", "I²C clock"), (r"SDA", "I²C data"),
    (r"BCLK", "I²S bit clock"), (r"LRCK", "I²S word (left/right) clock"), (r"DATA", "I²S audio data"), (r"Line_", "analogue line-level audio"),
    (r"UART\d?_TX|MCU_TX", "serial transmit (UART)"), (r"UART\d?_RX|MCU_RX", "serial receive (UART)"), (r"SWCLK|SWDIO", "programming and debug port (SWD)"),
    (r"RST|Rst", "reset"), (r"Ena|En_|EN", "enable: switches a circuit on"), (r"Hold_ON", "keeps the power on after the button is released"),
    (r"WkUp", "wakes the microcontroller"), (r"P_Btn", "power button"), (r"Plug_Det", "plug detection"), (r"Int", "interrupt"),
    (r"ACOK", "charger: input power is good"), (r"PRHOT", "charger: processor-hot warning (throttle)"), (r"PDN", "amplifier power-down"),
    (r"SwFlt|TwFlt", "amplifier fault flag"), (r"Ctrl", "control line"), (r"ISens|ISns", "current sense"), (r"PSYS", "charger's system-power monitor output"),
    (r"VbatADC", "battery voltage, divided down for the ADC"), (r"NTC", "battery temperature (thermistor)"), (r"Ver|HW_REV", "hardware version strap"),
    (r"Open", "battery cell open detection"), (r"M_DET", "module detection"), (r"Env_Track", "envelope tracking: lets the boost follow the audio level"),
    (r"PTM", "charger pass-through mode"), (r"PD_IO", "PD controller general-purpose pin"),
]
PIN_ROLES = [  # anonymous nets: what the chip pin on it does
    (r"^SW\d?$", "switching node of a regulator: pulses at the switching frequency"), (r"BOOT|BTST|VBST", "bootstrap capacitor for the high-side switch"),
    (r"^V?FB$", "feedback divider: sets the output voltage"), (r"^EN", "enable"), (r"COMP", "control-loop compensation"), (r"^SS", "soft start"),
    (r"^RT|FREQ", "sets the switching frequency"), (r"ILIM", "current limit"), (r"HIDRV|LODRV|GATE|^HO$|^LO$", "gate drive for a power switch"),
    (r"^G$", "transistor gate"), (r"SR[PN]|AC[PN]|CS[PN]|IN[+-]", "current-sense input"), (r"REGN|VCC", "internal gate-drive supply"),
    (r"IBAT", "battery-current monitor"), (r"CMPIN|CMPOUT", "comparator"), (r"OUT", "output"), (r"BATPRES", "battery-present detection"),
    (r"VBUS", "input-voltage sense"), (r"SYNC|DITHER", "clock synchronisation"), (r"MODE", "mode select"), (r"PGOOD", "power good"),
    (r"UVLO", "under-voltage lock-out"), (r"BIAS", "bias supply"), (r"ADDR|ADR", "address strap"),
]

def describe_net(net, short, nodes):
    if short in RAILS: return RAILS[short]
    if short:
        for pat, d in SIGNALS:
            if re.search(pat, short): return d + (" (active low)" if short.startswith("~{") else "")
    # anonymous: say what the chip pins on it do
    fets = [(ref, re.sub(r"_\d+$", "", f or "")) for ref, _, f in nodes if re.match(r"Q\d", ref)]
    if len({r for r, _ in fets}) == 2 and all(f == "D" for _, f in fets):
        return "the middle of a back-to-back switch: two transistors joined drain to drain, so no current flows either way while they are off"
    roles = []
    for ref, pin, func in nodes:
        name = re.sub(r"_\d+$", "", func or "")
        if not re.match(r"[UQ]\d", ref) or not name: continue
        for pat, d in PIN_ROLES:
            if re.search(pat, name):
                txt = f"{d} ({ref} {name})"
                if txt not in roles: roles.append(txt)
                break
    return "; ".join(roles[:2])

def net_meta(net_names, nets, sheets):
    meta = []
    for n in net_names:
        s = short_name(n)
        g = next(i for i, G in enumerate(GROUPS) if G[3](s if s else "\x00anon"))
        if not s: g = len(GROUPS) - 1
        meta.append([s, g, describe_net(n, s, nets.get(n, []))])
    return {"groups": [[c, l, p, d] for c, l, p, _, d in GROUPS], "nets": meta, "power": POWER_TREE}
