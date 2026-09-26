"""构建 pokesim/static/locations.json —— 真实地区野外分布数据。

数据来源:
  · PokeAPI 批量 CSV(encounters/locations/location_areas/versions/...) —— 物种、等级、方法、出现率
  · Bulbapedia 的 zh langlinks —— 地点中文名(常磐市/百代森林…)

产出结构(见文件末尾注释),供 dex.location* / encounter 按地点抽取使用。
"""

from __future__ import annotations

import collections
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".pokemon_cache")
OUT = os.path.join(ROOT, "pokesim", "static", "locations.json")
POKEAPI_CSV = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"
BULBA_API = "https://bulbapedia.bulbagarden.net/w/api.php"
POKE52_API = "https://wiki.52poke.com/api.php"
UA = "Mozilla/5.0 (compatible; astrbot-plugin-life-sim data builder)"

ZH_HANS = "12"

FILES = [
    "encounters.csv",
    "locations.csv",
    "location_names.csv",
    "location_areas.csv",
    "regions.csv",
    "region_names.csv",
    "versions.csv",
    "version_names.csv",
    "version_groups.csv",
    "encounter_slots.csv",
    "encounter_methods.csv",
]

# 遭遇方法中文名
METHOD_ZH = {
    "walk": "草丛/行走",
    "grass-spots": "草丛摇动",
    "dark-grass": "深草丛",
    "cave-spots": "洞窟尘柱",
    "bridge-spots": "桥影",
    "rough-terrain": "崎岖地面",
    "yellow-flowers": "黄花丛",
    "purple-flowers": "紫花丛",
    "red-flowers": "红花丛",
    "surf": "冲浪",
    "surf-spots": "水面涟漪",
    "seaweed": "海草",
    "bubbling-spots": "冒泡点",
    "old-rod": "旧钓竿",
    "good-rod": "好钓竿",
    "super-rod": "厉害钓竿",
    "super-rod-spots": "钓点涟漪",
    "feebas-tile-fishing": "钓点",
    "rock-smash": "碎岩",
    "headbutt": "撞树",
    "headbutt-low": "撞树(低)",
    "headbutt-normal": "撞树(中)",
    "headbutt-high": "撞树(高)",
    "honey-tree": "涂蜜树",
    "berry-trees": "果树",
    "hidden-grotto": "隐藏洞穴",
    "rustling-bush-ambush": "灌木伏击",
    "trash-can-ambush": "垃圾桶伏击",
    "ceiling-ambush": "天花板伏击",
    "ground-ambush": "地面伏击",
    "sky-ambush": "空中伏击",
    "horde": "群战",
    "sos": "呼唤同伴",
    "sos-from-bubbling-spot": "冒泡点呼唤",
    "overworld": "明雷",
    "overworld-dirt": "明雷(泥地)",
    "overworld-water": "明雷(水面)",
    "overworld-flying": "明雷(空中)",
    "overworld-special": "明雷(特殊)",
    "overworld-flying-special": "明雷(空中特殊)",
    "overworld-water-special": "明雷(水面特殊)",
    "wanderer": "游荡者",
    "wanderer-water": "游荡者(水)",
    "chase-water": "追逐(水)",
    "roaming-grass": "游走(草)",
    "roaming-water": "游走(水)",
    "island-scan": "岛屿扫描",
    "dynamax-adventure": "极巨大冒险",
    "max-raid": "极巨团体战",
    "pokespot": "宝可梦据点",
    "static": "定点",
    "gift": "赠予",
    "gift-egg": "赠予蛋",
    "pokeflute": "宝可梦之笛",
    "squirt-bottle": "水壶",
    "wailmer-pail": "吼吼鲸喷壶",
    "devon-scope": "得文检测镜",
    "npc-trade": "NPC 交换",
    "snag": "抢夺",
    "snag-rematch": "抢夺再战",
}

REGION_CAP = {
    "kanto": "Kanto",
    "johto": "Johto",
    "hoenn": "Hoenn",
    "sinnoh": "Sinnoh",
    "unova": "Unova",
    "kalos": "Kalos",
    "alola": "Alola",
    "galar": "Galar",
    "hisui": "Hisui",
    "paldea": "Paldea",
}


def fetch_csv(name: str) -> list[dict]:
    path = os.path.join(CACHE_DIR, name)
    if not os.path.exists(path):
        os.makedirs(CACHE_DIR, exist_ok=True)
        url = POKEAPI_CSV + name
        print(f"  ↓ {name}")
        with urllib.request.urlopen(url, timeout=90) as resp:
            data = resp.read()
        with open(path, "wb") as fh:
            fh.write(data)
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _http_json(url: str, tries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"    ! 请求失败: {last}")
    return {}


def _bulba_query(titles: list[str]) -> dict[str, str]:
    """批量查询 Bulbapedia 的 zh langlinks。返回 {title: zh}。

    注意:Bulbapedia 对匿名请求每次最多处理 20 个标题,多余会被静默丢弃。
    """
    if not titles:
        return {}
    params = {
        "action": "query",
        "titles": "|".join(titles),
        "prop": "langlinks",
        "lllang": "zh",
        "redirects": "1",
        "format": "json",
    }
    url = BULBA_API + "?" + urllib.parse.urlencode(params)
    payload = _http_json(url)
    out: dict[str, str] = {}
    query = payload.get("query") or {}
    norm = {n["to"]: n["from"] for n in query.get("normalized") or []}
    redir = {r["to"]: r["from"] for r in query.get("redirects") or []}
    for page in (query.get("pages") or {}).values():
        title = page.get("title", "")
        links = page.get("langlinks") or []
        if not links:
            continue
        zh = links[0]["*"]
        key = norm.get(title, title)
        key = redir.get(title, key)
        out[key] = zh
        out[title] = zh
    return out


def _bulba_search(name: str) -> list[str]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": name,
        "srlimit": 5,
        "format": "json",
    }
    url = BULBA_API + "?" + urllib.parse.urlencode(params)
    payload = _http_json(url)
    return [r["title"] for r in (payload.get("query") or {}).get("search") or []]


def _poke52_title(name: str) -> str:
    """用英文名在 52poke 搜索,取其中文标题(城镇等专有名词可靠)。"""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": name,
        "srlimit": 1,
        "format": "json",
    }
    payload = _http_json(POKE52_API + "?" + urllib.parse.urlencode(params))
    hits = (payload.get("query") or {}).get("search") or []
    return norm_zh(hits[0]["title"]) if hits else ""


def _poke52_names(entries: list[tuple[str, str]]) -> dict[str, str]:
    cache_path = os.path.join(CACHE_DIR, "poke52_zh.json")
    cache: dict[str, str] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    todo = [e for e in entries if e[0] not in cache and e[1]]
    print(f"  52poke 城镇名: 已有 {len(cache)},待查 {len(todo)}")
    for idx, (key, name) in enumerate(todo):
        cache[key] = _poke52_title(name)
        time.sleep(0.35)
        if idx % 50 == 0:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, indent=0)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=0)
    return cache


def _tokens(s: str) -> set[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in str(s))
    return {t for t in cleaned.split() if t}


def _similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta) >= 0.6


_FW = {ord("０") + i: str(i) for i in range(10)}
_FW[ord("（")] = "("
_FW[ord("）")] = ")"


def norm_zh(zh: str) -> str:
    return str(zh).translate(_FW).strip()


def fallback_zh(loc_key: str, name: str) -> str:
    m = re.search(r"route[-_ ]?(\d+)", loc_key, re.IGNORECASE) or re.search(r"Route (\d+)", name)
    if m:
        return f"{int(m.group(1))}号道路"
    return ""


def resolve_location_zh(entries: list[tuple[str, str, str]]) -> dict[str, str]:
    """entries: [(key, english_name, region_en)] → {key: zh}。带磁盘缓存。"""
    cache_path = os.path.join(CACHE_DIR, "bulbapedia_zh.json")
    cache: dict[str, str] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)

    todo = [e for e in entries if e[0] not in cache and e[1]]
    print(f"  Bulbapedia 中文名: 已有 {len(cache)},待查 {len(todo)}")

    def flush():
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=0)

    batch = 20  # Bulbapedia 匿名请求上限
    # 第一轮:原名
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        got = _bulba_query([c[1] for c in chunk])
        for key, name, _reg in chunk:
            if name in got:
                cache[key] = got[name]
        time.sleep(0.35)
        if i % 200 == 0:
            flush()
            print(f"    ... {i + len(chunk)}/{len(todo)}  已解析 {len(cache)}")
    flush()

    # 第二轮:地区消歧 "名称 (Region)"
    retry = [e for e in todo if e[0] not in cache and e[1]]
    print(f"  第二轮(地区消歧): {len(retry)}")
    for i in range(0, len(retry), batch):
        chunk = retry[i : i + batch]
        titles = []
        for key, name, reg in chunk:
            cap = REGION_CAP.get(reg, "")
            titles.append(f"{name} ({cap})" if cap else name)
        got = _bulba_query(titles)
        for (key, name, reg), title in zip(chunk, titles, strict=True):
            if title in got:
                cache[key] = got[title]
        time.sleep(0.35)
    flush()

    # 第三轮:搜索兜底(仅对仍未解析的,需标题相似)
    rest = [e for e in todo if e[0] not in cache and e[1]]
    print(f"  第三轮(搜索兜底): {len(rest)}")
    for idx, (key, name, _reg) in enumerate(rest):
        try:
            cands = _bulba_search(name)
        except Exception:
            cands = []
        if cands:
            got = _bulba_query(cands[:20])
            for cand in cands:
                if cand in got and _similar(name, cand):
                    cache[key] = got[cand]
                    break
        time.sleep(0.35)
        if idx % 100 == 0:
            flush()
    flush()

    return cache


def main():
    print("读取 PokeAPI 遭遇数据 …")
    d = {f: fetch_csv(f) for f in FILES}

    vgs = {r["id"]: r for r in d["version_groups.csv"]}
    versions = {r["id"]: r for r in d["versions.csv"]}
    slots = {r["id"]: r for r in d["encounter_slots.csv"]}
    methods = {r["id"]: r["identifier"] for r in d["encounter_methods.csv"]}
    areas = {r["id"]: r for r in d["location_areas.csv"]}
    locs = {r["id"]: r for r in d["locations.csv"]}
    regions = {r["id"]: r["identifier"] for r in d["regions.csv"]}
    region_zh = {}
    for r in d["region_names.csv"]:
        if r["local_language_id"] == ZH_HANS:
            region_zh[r["region_id"]] = r["name"]
        elif r["local_language_id"] == "4":
            region_zh.setdefault(r["region_id"], r["name"])
    loc_name_en, loc_name_zh = {}, {}
    for r in d["location_names.csv"]:
        if r["local_language_id"] == "9":
            loc_name_en[r["location_id"]] = r["name"]
        elif r["local_language_id"] in (ZH_HANS, "4"):
            loc_name_zh.setdefault(r["location_id"], r["name"])
    version_zh = collections.defaultdict(list)
    for r in d["version_names.csv"]:
        if r["local_language_id"] == ZH_HANS:
            version_zh[r["version_id"]].append(r["name"])

    # 物种: PokeAPI pokemon_id → Showdown key(按全国图鉴号)
    with open(OUT.replace("locations", "species"), encoding="utf-8") as fh:
        species = json.load(fh)
    num_to_key: dict[int, str] = {}
    for k, e in species.items():
        num = int(e.get("num", 0) or 0)
        if num > 0 and not e.get("forme"):
            num_to_key.setdefault(num, k)
    for k, e in species.items():
        num = int(e.get("num", 0) or 0)
        if num > 0:
            num_to_key.setdefault(num, k)

    # 聚合 encounters
    agg: dict[tuple[str, str, str], dict[tuple[str, str], list[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: [999, 0, 0])
    )
    loc_has_enc: set[str] = set()
    for r in d["encounters.csv"]:
        v = versions.get(r["version_id"])
        slot = slots.get(r["encounter_slot_id"])
        if not v or not slot:
            continue
        vg = vgs.get(v["version_group_id"])
        area = areas.get(r["location_area_id"])
        if not vg or not area:
            continue
        loc_id = area["location_id"]
        loc = locs.get(loc_id)
        if not loc:
            continue
        reg = regions.get(loc["region_id"], "")
        loc_key = loc["identifier"]
        loc_has_enc.add(loc_id)
        method = methods.get(slot["encounter_method_id"], "unknown")
        num = int(r["pokemon_id"])
        skey = num_to_key.get(num)
        if not skey:
            continue
        lo, hi = int(r["min_level"]), int(r["max_level"])
        chance = int(slot["rarity"] or 0)
        cell = agg[(reg, loc_key, vg["identifier"])][(method, skey)]
        cell[0] = min(cell[0], lo)
        cell[1] = max(cell[1], hi)
        cell[2] = min(100, cell[2] + chance)

    # 中文名:优先 Bulbapedia(权威),PokeAPI 作为回退
    entries: list[tuple[str, str, str]] = []
    for loc_id, loc in locs.items():
        if loc_id not in loc_has_enc:
            continue
        en = loc_name_en.get(loc_id, "")
        if not en:
            continue
        entries.append((loc["identifier"], en, regions.get(loc["region_id"], "")))
    bulba = resolve_location_zh(entries)
    town_entries = [
        (k, n) for (k, n, _r) in entries if k.endswith(("-city", "-town", "-island"))
    ]
    poke52 = _poke52_names(town_entries)

    out_regions = {
        ident: {"name": ident.capitalize(), "zh": region_zh.get(rid, ident)}
        for rid, ident in regions.items()
        if ident
    }
    out_vgs = {}
    for vg in vgs.values():
        vids = [vid for vid, v in versions.items() if v["version_group_id"] == vg["id"]]
        labels: list[str] = []
        for vid in vids:
            labels.extend(version_zh.get(vid, []))
        out_vgs[vg["identifier"]] = {
            "gen": int(vg["generation_id"]),
            "order": int(vg["order"]),
            "label": "/".join(dict.fromkeys(labels)) or vg["identifier"],
        }

    out_locations: dict[str, dict] = {}
    for (reg, loc_key, vgname), pools in agg.items():
        loc_id = next((lid for lid, l in locs.items() if l["identifier"] == loc_key), None)
        candidates: list[str] = []
        if poke52.get(loc_key):
            candidates.append(poke52[loc_key])
        if bulba.get(loc_key):
            candidates.append(bulba[loc_key])
        if loc_id and loc_name_zh.get(str(loc_id)):
            candidates.append(loc_name_zh[str(loc_id)])
        candidates = list(dict.fromkeys(norm_zh(c) for c in candidates if c))
        zh = candidates[0] if candidates else fallback_zh(loc_key, loc_name_en.get(str(loc_id), loc_key) if loc_id else loc_key)
        aliases = [c for c in candidates[1:] if c != zh]
        entry = out_locations.setdefault(
            loc_key,
            {
                "name": loc_name_en.get(str(loc_id), loc_key) if loc_id else loc_key,
                "zh": zh,
                "aliases": aliases,
                "region": reg,
                "pools": {},
            },
        )
        if zh and not entry["zh"]:
            entry["zh"] = zh
        for a in aliases:
            if a not in entry["aliases"] and a != entry["zh"]:
                entry["aliases"].append(a)
        entry["pools"][vgname] = [
            [skey, lo, hi, method, chance]
            for (method, skey), (lo, hi, chance) in sorted(pools.items(), key=lambda x: -x[1][2])
        ]

    used_methods: dict[str, str] = {}
    for pools in agg.values():
        for method, _skey in pools:
            used_methods[method] = METHOD_ZH.get(method, method)
    meta = {
        "source": "PokeAPI encounters.csv + Bulbapedia zh langlinks",
        "count": len(out_locations),
        "versions": len(out_vgs),
    }
    out = {
        "meta": meta,
        "regions": out_regions,
        "versionGroups": out_vgs,
        "methods": used_methods,
        "locations": out_locations,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    resolved = sum(1 for v in out_locations.values() if v["zh"])
    print(f"完成: locations={len(out_locations)} 中文名={resolved} 方法={len(used_methods)}")
    print(f"  → {OUT}  {os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
