"""构建 `pokesim/data/*.json` 内置宝可梦数据(仅开发期运行,运行时不依赖网络)。

数据来源:
- 机制数据(种族值/属性/技能表/技能威力/属性相克/特性名/性格):Pokémon Showdown,
  经 `poke-env` wheel 内置的静态 JSON 提取(gen9)。
- 中文名称与技能/特性描述:PokeAPI 的 csv 数据仓库(zh-Hans)。

用法:
    .venv/bin/python tools/build_pokemon_data.py

输出到 `pokesim/static/`。脚本会缓存原始 csv 到 `.pokemon_cache/`(已 gitignore)。
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "pokesim", "static")
CACHE_DIR = os.path.join(ROOT, ".pokemon_cache")

POKEAPI_CSV = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"
POKE_ENV_WHEEL_URL = "https://files.pythonhosted.org/packages/source/p/poke-env/"

ZH_HANS = "12"

TYPE_ZH = {
    "Normal": "一般",
    "Fire": "火",
    "Water": "水",
    "Electric": "电",
    "Grass": "草",
    "Ice": "冰",
    "Fighting": "格斗",
    "Poison": "毒",
    "Ground": "地面",
    "Flying": "飞行",
    "Psychic": "超能力",
    "Bug": "虫",
    "Rock": "岩石",
    "Ghost": "幽灵",
    "Dragon": "龙",
    "Dark": "恶",
    "Steel": "钢",
    "Fairy": "妖精",
    "Stellar": "星晶",
}

STAT_ZH = {
    "hp": "HP",
    "atk": "攻击",
    "def": "防御",
    "spa": "特攻",
    "spd": "特防",
    "spe": "速度",
}

# 性格中文名由 PokeAPI 拉取,这里只做英文 identifier → Showdown 键的兜底(键相同)。


def _http(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pokesim-build/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_csv(name: str) -> list[dict]:
    """下载(带缓存)PokeAPI csv,返回 DictReader 行列表。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if not os.path.exists(path):
        print(f"  ↓ {name}")
        data = _http(POKEAPI_CSV + name)
        with open(path, "wb") as f:
            f.write(data)
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(io.StringIO(f.read())))


def load_showdown() -> dict:
    """从 poke-env wheel 提取 gen9 的 pokedex / moves / learnset / typechart / natures。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    wheel = None
    for fn in os.listdir(CACHE_DIR):
        if fn.startswith("poke_env-") and fn.endswith(".whl"):
            wheel = os.path.join(CACHE_DIR, fn)
    if wheel is None:
        print("  ↓ poke-env wheel")
        # 用 PyPI JSON API 找到 wheel URL
        meta = json.loads(_http("https://pypi.org/pypi/poke-env/json"))
        url = None
        for f in meta["urls"]:
            if f["filename"].endswith(".whl"):
                url = f["url"]
                break
        if not url:
            raise RuntimeError("无法找到 poke-env wheel")
        data = _http(url)
        wheel = os.path.join(CACHE_DIR, os.path.basename(url))
        with open(wheel, "wb") as f:
            f.write(data)
    z = zipfile.ZipFile(wheel)

    def j(path: str):
        return json.loads(z.read(path))

    base = "poke_env/data/static/"
    return {
        "pokedex": j(base + "pokedex/gen9pokedex.json"),
        "moves": j(base + "moves/gen9moves.json"),
        "learnset": j(base + "learnset.json"),
        "typechart": j(base + "typechart/gen9typechart.json"),
        "natures": j(base + "natures.json"),
    }


def norm_id(name: str) -> str:
    """把 Showdown 英文名归一成 PokeAPI identifier(近似)。"""
    s = name.lower().strip()
    s = s.replace("'", "").replace(".", "").replace("%", "").replace(":", "")
    s = s.replace(" ", "-")
    return s


def build() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    sd = load_showdown()

    print("拉取 PokeAPI 本地化数据 …")
    lang_rows = fetch_csv("languages.csv")
    zh_ids = {
        r["id"]
        for r in lang_rows
        if r["identifier"] in ("zh-hans",)
    }
    assert ZH_HANS in zh_ids or any(r["identifier"] == "zh-hans" for r in lang_rows)

    species_names = {}
    for r in fetch_csv("pokemon_species_names.csv"):
        if r["local_language_id"] == ZH_HANS:
            species_names[r["pokemon_species_id"]] = {
                "name": r["name"],
                "genus": r["genus"],
            }

    move_names = {}
    for r in fetch_csv("move_names.csv"):
        if r["local_language_id"] == ZH_HANS:
            move_names[r["move_id"]] = r["name"]

    move_effects = {}
    for r in fetch_csv("move_effect_prose.csv"):
        if r["local_language_id"] == ZH_HANS:
            move_effects[r["move_effect_id"]] = (
                r.get("short_effect") or r.get("effect") or ""
            ).replace("\n", " ").strip()

    # move id → effect_id,identifier
    move_meta = {}
    for r in fetch_csv("moves.csv"):
        move_meta[r["id"]] = {
            "identifier": r["identifier"],
            "effect_id": r.get("effect_id", ""),
        }

    ability_names = {}
    for r in fetch_csv("ability_names.csv"):
        if r["local_language_id"] == ZH_HANS:
            ability_names[r["ability_id"]] = r["name"]
    ability_prose = {}
    for r in fetch_csv("ability_prose.csv"):
        if r["local_language_id"] == ZH_HANS:
            ability_prose[r["ability_id"]] = (
                r.get("short_effect") or r.get("effect") or ""
            ).replace("\n", " ").strip()
    ability_id_by_ident = {
        r["identifier"]: r["id"] for r in fetch_csv("abilities.csv")
    }

    def _flavor(csv_name: str, id_field: str) -> dict[str, str]:
        """取 zh-hans 的图鉴文字(同一实体取最新世代的文本)。"""
        best: dict[str, tuple[int, str]] = {}
        for r in fetch_csv(csv_name):
            if r.get("language_id") != ZH_HANS:
                continue
            txt = (
                (r.get("flavor_text") or "")
                .replace("\n", " ")
                .replace("\f", " ")
                .replace("\u000c", " ")
                .strip()
            )
            if not txt:
                continue
            key = r[id_field]
            vg = int(r.get("version_group_id") or 0)
            prev = best.get(key)
            if prev is None or vg >= prev[0]:
                best[key] = (vg, txt)
        return {k: v[1] for k, v in best.items()}

    move_desc = _flavor("move_flavor_text.csv", "move_id")
    ability_desc = _flavor("ability_flavor_text.csv", "ability_id")

    nature_zh = {}
    nature_id_by_ident = {r["identifier"]: r["id"] for r in fetch_csv("natures.csv")}
    for r in fetch_csv("nature_names.csv"):
        if r["local_language_id"] == ZH_HANS:
            nature_zh[r["nature_id"]] = r["name"]

    # 捕获率 / 稀有度 / 成长曲线(按全国图鉴号取自 PokeAPI pokemon_species)
    growth_ident = {r["id"]: r["identifier"] for r in fetch_csv("growth_rates.csv")}
    species_meta: dict[str, dict] = {}
    for r in fetch_csv("pokemon_species.csv"):
        species_meta[r["id"]] = {
            "captureRate": int(r.get("capture_rate") or 45),
            "growth": growth_ident.get(r.get("growth_rate_id", ""), "medium"),
            "isLegendary": r.get("is_legendary") == "1",
            "isMythical": r.get("is_mythical") == "1",
        }
    # 基础经验值(击倒时给予的经验),默认形态优先
    base_exp_by_species: dict[str, int] = {}
    default_pokemon_species: dict[str, str] = {}
    for r in fetch_csv("pokemon.csv"):
        if r.get("is_default") == "1" and r.get("species_id"):
            base_exp_by_species[r["species_id"]] = int(r.get("base_experience") or 0)
            default_pokemon_species[r["id"]] = r["species_id"]
    # 努力值产出(击败时给予)
    pstat = {"1": "hp", "2": "atk", "3": "def", "4": "spa", "5": "spd", "6": "spe"}
    ev_by_species: dict[str, dict] = {}
    for r in fetch_csv("pokemon_stats.csv"):
        eff = int(r.get("effort") or 0)
        if eff <= 0:
            continue
        sid = default_pokemon_species.get(r["pokemon_id"])
        key = pstat.get(r.get("stat_id", ""))
        if sid and key:
            ev_by_species.setdefault(sid, {})[key] = eff
    # 蛋群
    egg_group_name = {r["id"]: r["identifier"] for r in fetch_csv("egg_groups.csv")}
    egg_by_species: dict[str, list[str]] = {}
    for r in fetch_csv("pokemon_egg_groups.csv"):
        g = egg_group_name.get(r.get("egg_group_id", ""))
        if g:
            egg_by_species.setdefault(r["species_id"], []).append(g)

    # 性别比例 / 孵化周期
    for r in fetch_csv("pokemon_species.csv"):
        m = species_meta.get(r["id"])
        if m is not None:
            m["genderRate"] = int(r.get("gender_rate") or -1)
            m["hatchCounter"] = int(r.get("hatch_counter") or 0)

    # ── species ──────────────────────────────────────────────
    species = {}
    for key, v in sd["pokedex"].items():
        if "baseStats" not in v:
            continue
        num = int(v.get("num", 0) or 0)
        if num <= 0:
            continue  # 过滤 Pokémon Showdown 内置的 CAP 同人宝可梦
        base = v.get("baseSpecies")
        forme = v.get("forme")
        loc = species_names.get(str(num), {}) if num > 0 else {}
        zh = loc.get("name", "")
        if forme and zh:
            zh = f"{zh}（{forme}）"
        entry = {
            "num": num,
            "name": v.get("name", key),
            "zh": zh or v.get("name", key),
            "genus": loc.get("genus", ""),
            "types": list(v.get("types", [])),
            "baseStats": dict(v.get("baseStats", {})),
            "abilities": dict(v.get("abilities", {})),
        }
        sm = species_meta.get(str(num))
        if sm:
            entry["captureRate"] = sm["captureRate"]
            entry["growthRate"] = sm["growth"]
            entry["genderRate"] = sm.get("genderRate", -1)
            entry["hatchCounter"] = sm.get("hatchCounter", 0)
            if sm["isLegendary"]:
                entry["isLegendary"] = True
            if sm["isMythical"]:
                entry["isMythical"] = True
        be = base_exp_by_species.get(str(num))
        if be:
            entry["baseExp"] = be
        ev = ev_by_species.get(str(num))
        if ev:
            entry["evs"] = ev
        eggs = egg_by_species.get(str(num))
        if eggs:
            entry["eggGroups"] = eggs
        for f in (
            "heightm",
            "weightkg",
            "gender",
            "genderRatio",
            "tags",
            "canGigantamax",
            "requiredTeraType",
            "requiredItem",
            "requiredMove",
            "requiredAbility",
            "evoLevel",
            "evoType",
            "evoItem",
            "evoMove",
            "evoCondition",
            "battleOnly",
            "isCosmeticForme",
        ):
            if f in v:
                entry[f] = v[f]
        if base:
            entry["baseSpecies"] = base
            entry["forme"] = forme or ""
        if v.get("prevo"):
            entry["prevo"] = norm_id(v["prevo"])
        if v.get("evos"):
            entry["evos"] = [norm_id(e) for e in v["evos"]]
        species[key] = entry

    # 把 evos/prevo/baseSpecies 归一为实际 key(Showdown 对形态用连字符名,
    # 如 evos 写 "Kommo-o"/"Raichu-Alola",而 key 是 kommoo/raichualola),
    # 并剔除指不存在形态的引用(如纯外观形态 basculegion-f)。
    def _canon(x: str) -> str:
        return "".join(ch for ch in str(x).lower() if ch.isalnum())

    canon_index: dict[str, str] = {}
    for k, v in species.items():
        canon_index.setdefault(_canon(k), k)
        canon_index.setdefault(_canon(v.get("name", "")), k)
    for v in species.values():
        if v.get("evos"):
            v["evos"] = [
                e2
                for e in v["evos"]
                if (e2 := canon_index.get(_canon(e), e)) in species
            ]
        for f in ("prevo", "baseSpecies"):
            if v.get(f):
                v[f] = canon_index.get(_canon(v[f]), v[f])

    # ── moves ────────────────────────────────────────────────
    move_fields = (
        "num",
        "type",
        "category",
        "basePower",
        "accuracy",
        "pp",
        "priority",
        "target",
        "critRatio",
        "drain",
        "recoil",
        "heal",
        "multihit",
        "status",
        "volatileStatus",
        "boosts",
        "self",
        "secondary",
        "sideCondition",
        "weather",
        "terrain",
        "pseudoWeather",
        "selfSwitch",
        "selfdestruct",
        "stallingMove",
        "ignoreAbility",
        "isNonstandard",
        "isMax",
        "isZ",
        "maxMove",
        "damageCallback",
        "basePowerCallback",
        "callsMove",
    )
    moves = {}
    for key, v in sd["moves"].items():
        num = int(v.get("num", 0) or 0)
        if v.get("isNonstandard") in ("CAP", "Custom", "Future"):
            continue
        entry = {"name": v.get("name", key), "zh": ""}
        for f in move_fields:
            if f in v:
                entry[f] = v[f]
        # flags 转成名字列表
        if isinstance(v.get("flags"), dict):
            entry["flags"] = list(v["flags"].keys())
        # 本地化名与描述
        meta = move_meta.get(str(num)) if num > 0 else None
        if meta:
            zh = move_names.get(str(num))
            if zh:
                entry["zh"] = zh
            eff = move_effects.get(str(meta.get("effect_id") or ""))
            if eff:
                entry["desc"] = eff
        fd = move_desc.get(str(num))
        if fd:
            entry["desc"] = fd
        if not entry.get("zh"):
            # 按 identifier 兜底匹配
            ident = norm_id(v.get("name", key))
            for mid, m in move_meta.items():
                if m["identifier"] == ident:
                    entry["zh"] = move_names.get(mid, "") or entry["zh"]
                    eff = move_effects.get(str(m.get("effect_id") or ""))
                    if eff and not entry.get("desc"):
                        entry["desc"] = eff
                    fd2 = move_desc.get(str(mid))
                    if fd2:
                        entry["desc"] = fd2
                    break
        if not entry.get("zh"):
            entry["zh"] = entry["name"]
        moves[key] = entry

    # ── learnsets(每只取「最新可获得世代」的招式表)─────────
    # Showdown learnset 标了每个招式的可学世代(如 9M / 8L24 / 7T);
    # 只保留该宝可梦编号最大的世代,既能覆盖未进 SV 的老宝可梦,
    # 又不会把 9 个世代的重复数据全塞进来。
    learnsets = {}
    learnset_gen = {}
    for key, v in sd["learnset"].items():
        if key not in species:
            continue
        row = {}
        for move_key, codes in (v.get("learnset") or {}).items():
            if move_key not in moves:
                continue
            gens = sorted(
                {int(c[0]) for c in codes if c and c[0].isdigit()}, reverse=True
            )
            if not gens:
                continue
            g = gens[0]
            got = []
            for c in codes:
                if not c.startswith(str(g)):
                    continue
                suffix = c[1:]
                # 归一:9M→M, 9L24→L24, 9T→T, 9E→E, 9S0→S, 9R→R
                if suffix.startswith("L") and len(suffix) > 1:
                    got.append("L" + suffix[1:])
                elif suffix:
                    got.append(suffix[0])
            if got:
                row[move_key] = ",".join(_sort_codes(got))
        if row:
            learnsets[key] = row
            learnset_gen[key] = int(
                max(
                    int(c[0])
                    for arr in v.get("learnset", {}).values()
                    for c in arr
                    if c and c[0].isdigit()
                )
            )

    # ── typechart ────────────────────────────────────────────
    # Showdown damageTaken: 0=正常 1=弱点(2x) 2=抗性(0.5x) 3=免疫(0x)
    mult = {0: 1.0, 1: 2.0, 2: 0.5, 3: 0.0}
    typechart = {}
    for defender, v in sd["typechart"].items():
        dt = v.get("damageTaken") or {}
        for attacker, code in dt.items():
            if attacker.isupper():  # brn/par 等状态键跳过
                continue
            m = mult.get(int(code), 1.0)
            if m != 1.0:
                typechart.setdefault(attacker.capitalize(), {})[defender.capitalize()] = m
    # 星晶:进攻对所有属性 1x(引擎特殊处理),防御无相克
    typechart["Stellar"] = {}

    # ── natures ──────────────────────────────────────────────
    natures = {}
    for key, v in sd["natures"].items():
        inc = [k for k in ("atk", "def", "spa", "spd", "spe") if v.get(k, 1) > 1]
        dec = [k for k in ("atk", "def", "spa", "spd", "spe") if v.get(k, 1) < 1]
        natures[key] = {
            "name": key.capitalize(),
            "zh": nature_zh.get(nature_id_by_ident.get(key, ""), key.capitalize()),
            "inc": inc[0] if inc else "",
            "dec": dec[0] if dec else "",
        }

    # ── abilities(被图鉴引用到的)───────────────────────────
    ability_ids = set()
    for s in species.values():
        for an in (s.get("abilities") or {}).values():
            ability_ids.add(an)
    abilities = {}
    for name in sorted(ability_ids):
        ident = norm_id(name)
        aid = ability_id_by_ident.get(ident)
        abilities[ident] = {
            "name": name,
            "zh": ability_names.get(aid, name) if aid else name,
            "desc": (ability_desc.get(str(aid)) or ability_prose.get(aid, "")) if aid else "",
        }

    # ── 写出 ────────────────────────────────────────────────
    def dump(fn, obj):
        path = os.path.join(OUT_DIR, fn)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  → {fn}  {os.path.getsize(path) / 1024:.0f} KB")

    dump("species.json", species)
    dump("moves.json", moves)
    dump("learnsets.json", learnsets)
    dump("typechart.json", typechart)
    dump("natures.json", natures)
    dump("abilities.json", abilities)
    dump("learnset_gen.json", learnset_gen)
    meta = {
        "source": "Pokemon Showdown (gen9) via poke-env + PokeAPI zh-Hans",
        "generation": 9,
        "type_zh": TYPE_ZH,
        "stat_zh": STAT_ZH,
        "species_count": len(species),
        "move_count": len(moves),
    }
    dump("meta.json", meta)
    print(
        f"完成: species={len(species)} moves={len(moves)} "
        f"learnsets={len(learnsets)} abilities={len(abilities)}"
    )


def _sort_codes(codes: list[str]) -> list[str]:
    """排序 learnset 代码:等级招按等级、其余按类型字母。"""
    level = sorted(
        (c for c in set(codes) if c.startswith("L")), key=lambda c: int(c[1:])
    )
    other = sorted(c for c in set(codes) if not c.startswith("L"))
    return level + other


if __name__ == "__main__":
    sys.exit(build())
