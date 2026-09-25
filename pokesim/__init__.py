"""pokesim — 宝可梦世界观推演引擎(内置 Gen9 数据 + 战斗/培养工具)。

- `dex`    图鉴数据访问
- `engine` 单打对战引擎(属性相克 / 太晶化 / 状态 / 能力变化)
- `store`  队伍与对战状态持久化
- `tools`  PokemonMixin:暴露给 LLM 的 `poke_*` 工具
"""

from .dex import get_dex

__all__ = ["get_dex"]
