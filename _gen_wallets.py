import json

picked = json.load(open("_picked.json"))
NAMES = ["Falcon", "Viper", "Orca", "Lynx", "Cobra", "Wolf", "Raven", "Shark", "Puma",
         "Tiger", "Jaguar", "Panther", "Eagle", "Mamba", "Kodiak", "Osprey", "Marlin",
         "Comet", "Bolt", "Hornet"]


def clean_coins(coins):
    out = []
    for c in coins:
        if c.startswith("@") or ":" in c or "/" in c:
            continue
        out.append(c)
    return out[:2] or ["crypto"]


def fmt_av(av):
    return f"${av/1e6:.1f}M" if av >= 1e6 else f"${av/1e3:.0f}k"


def cadence(f24):
    if f24 == 0:
        return "swing (multi-day holds)"
    if f24 <= 6:
        return f"~{f24}/day (hours-long holds)"
    return f"~{f24} trades/day"


print("WALLETS = [")
for i, c in enumerate(picked):
    name = NAMES[i] if i < len(NAMES) else f"T{i+1}"
    coins = "/".join(clean_coins(c["coins"]))
    blurb = f"{coins} · funded {fmt_av(c['av'])} · +{c['mr']}%/mo · {cadence(c['fills24'])}"
    print(f'    {{"name": "{name}", "addr": "{c["addr"]}",')
    print(f'     "blurb": "{blurb}"}},')
print("]")
