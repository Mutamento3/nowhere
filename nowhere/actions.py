"""Walk action registry -- Card 48.

Each if-block in walk_impl becomes an Action with should() + render().
Order = priority (荒深叙事 > 本地场景 > 收音机安静 > 节日/纪念日 > 时间轴 > 常规遭遇).
No event bus -- synchronous two-step (判断 + 渲染).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


# ── Context ─────────────────────────────────────────────────────────


@dataclass
class WalkContext:
    """All state an Action might need. Built once per walk step."""

    state: Any  # WorldState
    env: dict
    rng: random.Random
    step_result: dict
    lat: float
    lon: float
    now: datetime | None
    bearing: float | None
    semantic: str | None
    local_dt: Any  # datetime in local timezone, or None
    tz_name: str | None
    water_features: list[dict]
    is_deep_wilderness: bool
    wilderness_depth: float
    encounter_multiplier: float
    env_cached: bool
    prev_env: dict | None = None
    quiet: bool = False
    sections: list[str] = field(default_factory=list)
    # Mutable inter-action state
    # 顺序依赖: mishap_fired 由 MishapAction.resolve() 置位, 仅被紧随其后的
    # MishapEchoAction.should() 读取 —— ACTIONS 中两者必须保持相邻顺序
    mishap_fired: bool = False


# ── Protocol ────────────────────────────────────────────────────────


class Action(Protocol):
    """A walk narrative slot.

    Pipeline: should() → resolve() → render()
    - should(): gate (does this action fire?)
    - resolve(): side effects (state mutations). Called before render.
    - render(): produce narrative text. Read-only on state.

    Default resolve() is a no-op — actions that mix state mutation into
    render() still work. New actions should override resolve() for clean
    separation.
    """

    name: str

    def should(self, ctx: WalkContext) -> bool: ...
    def resolve(self, ctx: WalkContext) -> None: ...
    def render(self, ctx: WalkContext) -> str | None: ...


# ── Concrete Actions ────────────────────────────────────────────────


class RhythmAction:
    """立志节律: 这座城此刻正在发生的事 (季节门控)."""

    name = "rhythm"

    def should(self, ctx: WalkContext) -> bool:
        return ctx.local_dt is not None

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere import localcolor

        place = ctx.state.place_name
        ld = ctx.local_dt
        rhythm = localcolor.rhythm_event(
            place, ld.hour, ctx.rng, ld.month,
            recent=ctx.state.recent_scenes,
            weekday=ld.weekday(),
        )
        return rhythm


class TimeaxisAction:
    """六根时间轴(Card 46): 最多2层,优先级排序."""

    name = "timeaxis"

    def should(self, ctx: WalkContext) -> bool:
        return ctx.now is not None and not ctx.env_cached

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _compute_timeaxes

        layers = _compute_timeaxes(
            ctx.now, ctx.lat, ctx.lon,
            ctx.state.biome or "",
            ctx.env.get("sky", {}).get("phase", "day"),
            ctx.env.get("weather", {}).get("precip", "none"),
            ctx.water_features,
            ctx.state.seen_humanities,
            ctx.rng,
        )
        parts: list[str] = []
        seen = set(ctx.state.recent_scenes)
        for ta in layers:
            # 只读 recent_scenes 做去重, 不在此 append: 管线在步末会把所有
            # 长 section 统一记账, Action 侧再记一次会占双倍去重窗口
            if ta["text"] not in seen:
                parts.append(ta["text"])
                seen.add(ta["text"])
        return "\n".join(parts) if parts else None


class HumanitiesAction:
    """人文卡: 走到附近触发(非随机). Card 16: blind时禁抽."""

    name = "humanities"

    def should(self, ctx: WalkContext) -> bool:
        # Card 16: blind mode disables humanities (they contain place names)
        if getattr(ctx.state, "blind", False):
            return False
        return True

    def resolve(self, ctx: WalkContext) -> None:
        from nowhere import describe, humanities, placememory

        card = humanities.nearby_place(
            ctx.lat, ctx.lon, ctx.state.seen_humanities, ctx.rng,
        )
        if not card:
            self._text = None
            return
        ctx.state.seen_humanities.add(card["key"])
        placememory.save_seen_humanities(ctx.state.seen_humanities)
        text = describe.render("humanities", card, None, ctx.rng)
        if not text:
            self._text = None
            return
        if card.get("category") == "人物":
            name = card.get("ref", {}).get("name", "")
            text += f"\n{name}。这名字你记下了。ask 能问出更多。"
        self._text = text

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


class PersonAction:
    """卡中人遇见: walk 落在该地 5km 内 -> sight."""

    name = "person"

    def should(self, ctx: WalkContext) -> bool:
        return not ctx.state.person_encountered_this_walk

    def resolve(self, ctx: WalkContext) -> None:
        from nowhere import people as people_mod

        local_month = ctx.local_dt.month if ctx.local_dt else (ctx.now.month if ctx.now else 7)
        hit = people_mod.find_nearby_person(
            ctx.lat, ctx.lon, local_month, ctx.state.seen_people, ctx.rng,
        )
        if not hit:
            self._text = None
            return
        ctx.state.person_encountered_this_walk = True
        ctx.state.last_person = hit["data"]
        ctx.state.last_person_place = hit["place"]
        ctx.state.talk_count = 0
        ctx.state.seen_people.add(f"{hit['place']}/{hit['person']}")
        self._text = hit["sight"]

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


class MishapAction:
    """意外层(Card 28): 3% per step, 10-step cooldown."""

    name = "mishap"

    def should(self, ctx: WalkContext) -> bool:
        return True

    def resolve(self, ctx: WalkContext) -> None:
        from nowhere.server import _try_mishap

        # 置位必须在 resolve(): 紧随其后的 MishapEchoAction.should() 读取
        # 本字段, 若放在 render() 里则依赖隐式的注册表顺序 (见 WalkContext 注释)
        result = _try_mishap(ctx.env, ctx.rng)
        ctx.mishap_fired = result is not None
        self._text = result["text"] if result else None

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


class MishapEchoAction:
    """意外回声: 50% chance next step echoes last mishap (每个 mishap 仅回响一次)."""

    name = "mishap_echo"

    def should(self, ctx: WalkContext) -> bool:
        return not ctx.mishap_fired

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _try_mishap_echo

        return _try_mishap_echo(ctx.rng)


class EncounterAction:
    """File-based encounter: density-adjusted 25% chance."""

    name = "encounter"

    def should(self, ctx: WalkContext) -> bool:
        return ctx.rng.random() < 0.25 * ctx.encounter_multiplier

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere import encounters, notebook as notebook_mod

        enc = encounters.draw_encounter(
            ctx.state.biome or "", ctx.lat, ctx.lon, ctx.rng,
            place_name=ctx.state.place_name or "",
        )
        if not enc:
            return None
        # Card 43: fauna notebook hook
        try:
            fauna_name = enc.split("。")[0].split(",")[0].split("，")[0].strip()
            skip_city = False
            for cp in ("巴黎", "伦敦", "东京", "纽约", "上海", "北京", "罗马", "柏林"):
                if fauna_name.startswith(cp):
                    skip_city = True
                    break
            if fauna_name and not skip_city:
                nb_env = dict(ctx.env) if ctx.env else {}
                nb_env["_dt"] = ctx.now
                notebook_mod.record_with_env(
                    "fauna", fauna_name, ctx.state.place_name or "", nb_env, ctx.lat,
                )
        except (OSError, ValueError, TypeError):
            pass  # intentionally ignored: notebook recording is non-critical
        return enc


class MessageAction:
    """30% chance to encounter a message from another traveler."""

    name = "message"

    def should(self, ctx: WalkContext) -> bool:
        return bool(ctx.state.messages) and ctx.rng.random() < 0.3 * ctx.encounter_multiplier

    def resolve(self, ctx: WalkContext) -> None:
        msg = ctx.rng.choice(list(ctx.state.messages))
        if isinstance(msg, dict):
            msg["encountered"] = True
        self._msg = msg

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere import describe
        from nowhere.server import _strip_code_markers

        msg = getattr(self, "_msg", None)
        if msg is None:
            return None
        content = msg["content"] if isinstance(msg, dict) else msg
        content = _strip_code_markers(str(content))
        return describe.render("message", {"content": content}, None, ctx.rng)


class WildernessEventAction:
    """Deep wilderness: 10+ steps, 5% procedural flesh event."""

    name = "wilderness_event"

    def should(self, ctx: WalkContext) -> bool:
        return (
            ctx.is_deep_wilderness
            and len(ctx.state.path) >= 10
            and ctx.rng.random() < 0.05
        )

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _WILDERNESS_FLESH_EVENTS

        return ctx.rng.choice(_WILDERNESS_FLESH_EVENTS)


class RiverAction:
    """Along-river narrative: detect flow alignment."""

    name = "river"

    def should(self, ctx: WalkContext) -> bool:
        return any(f.get("type") == "river" for f in ctx.water_features)

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _compute_river_direction, _river_alignment_text

        river_dir = _compute_river_direction(ctx.water_features, ctx.lat, ctx.lon)
        return _river_alignment_text(ctx.bearing, river_dir, ctx.rng) or None


class BuriedItemAction:
    """Card 13: Buried item discovery (8% chance within 3km)."""

    name = "buried_item"

    def should(self, ctx: WalkContext) -> bool:
        return bool(ctx.state.pos)

    def resolve(self, ctx: WalkContext) -> None:
        from nowhere import placememory
        from nowhere.server import (
            _FIND_VARIANTS, _PUTBACK_VARIANTS, _sanitize_external,
        )

        nearby = placememory.buried_nearby(ctx.lat, ctx.lon, radius_km=3.0)
        if not nearby or ctx.rng.random() >= 0.08:
            self._text = None
            return
        item = ctx.rng.choice(nearby)
        find_text = ctx.rng.choice(_FIND_VARIANTS)
        note_text = ""
        if item.get("note"):
            note_text = f" 盒子里还有一行字:{_sanitize_external(item['note'])}"
        if ctx.state.souvenir is None:
            ctx.state.souvenir = {
                "name": item.get("name", "一个铁盒"),
                "from": item.get("from", "土里"),
                "desc": item.get("desc", ""),
            }
            self._text = find_text + note_text
            return
        self._text = find_text + note_text + ctx.rng.choice(_PUTBACK_VARIANTS)

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


class NightNavAction:
    """Card 14: Night navigation (30% chance at night)."""

    name = "night_nav"

    def should(self, ctx: WalkContext) -> bool:
        return ctx.env.get("sky", {}).get("phase") in ("night", "nautical")

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere import describe

        if ctx.rng.random() >= 0.3:
            return None
        now = ctx.state.now()
        month = now.month if now else 7
        return describe.render_night_nav(
            ctx.lat, ctx.env.get("sky", {}).get("moon_phase", 0),
            ctx.env["sky"].get("phase", "night"), month, ctx.rng,
        )


class WildernessNarrativeAction:
    """Deep wilderness "荒深档" rendering (Card 40)."""

    name = "wilderness_narrative"

    def should(self, ctx: WalkContext) -> bool:
        return ctx.is_deep_wilderness and not ctx.env_cached

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _WILDERNESS_FEATURES, _WILDERNESS_VARIANTS

        parts: list[str] = []
        wd = ctx.wilderness_depth
        # 本 Action 在 ACTIONS 队首执行, 此处 ctx.sections 只含管线注入的
        # pre-action 内容(body/salience/dawn chorus); LocalScene 等后续 Action
        # 产出的长段落不参与判断 —— 门控范围仅限已有叙事
        if wd > 30.0 and not any(len(s) > 10 for s in ctx.sections):
            parts.append("好久没见着人迹了。")
        # Wilderness variant (only if not too many sections)
        if len(ctx.sections) < 3:
            parts.append(ctx.rng.choice(_WILDERNESS_VARIANTS))
        # Procedural feature
        if wd > 100.0 and ctx.rng.random() < 0.3:
            parts.append(ctx.rng.choice(_WILDERNESS_FEATURES))
        return "\n".join(parts) if parts else None


class RadioQuietAction:
    """Card 39: designed quiet during radio cooldown (BotW minimalism).

    When radio_steps_since is within cooldown (1-4) but station is still in range,
    emit a quiet variant instead of complete silence.
    """

    name = "radio_quiet"

    def should(self, ctx: WalkContext) -> bool:
        station = ctx.env.get("radio")
        return station is not None and 1 <= ctx.state.radio_steps_since < 5

    def resolve(self, ctx: WalkContext) -> None:
        pass  # no side effects

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.describe import _RADIO_QUIET_VARIANTS

        return ctx.rng.choice(_RADIO_QUIET_VARIANTS)


class LocalSceneAction:
    """Local-first scene: 城市特有 > 通用 biome."""

    name = "local_scene"

    def should(self, ctx: WalkContext) -> bool:
        return not ctx.is_deep_wilderness and not ctx.env_cached

    def resolve(self, ctx: WalkContext) -> None:
        # 本 Action 把成品段落直接追加进 ctx.sections(管线渲染入口),
        # 因此没有 render 输出; 所有状态变更集中在这里, render() 保持无副作用
        from nowhere import (
            country as country_mod,
            describe,
            localcolor,
            notebook as notebook_mod,
            placememory,
        )
        from nowhere.server import _load_scene_file, _pick_fresh, _tf
        from zoneinfo import ZoneInfo

        place = ctx.state.place_name or ""
        now = ctx.now
        lat, lon = ctx.lat, ctx.lon
        sections = ctx.sections

        local_hour = None
        tz_walk = _tf.timezone_at(lat=lat, lng=lon)
        if tz_walk and now is not None:
            local_hour = now.astimezone(ZoneInfo(tz_walk)).hour
        cc = country_mod.country_code_of(lat, lon)
        had_local = False

        # 1. Localcolor card
        if place and len(sections) < 4:
            local_card = localcolor.draw(
                place, ctx.state.seen_cards, ctx.rng,
                local_hour=local_hour, country_code=cc,
                intent=getattr(ctx.state, 'intent', None),
                lat=lat, lon=lon,
                month=now.month if now else None,
            )
            if local_card:
                ctx.state.seen_cards.add(local_card["key"])
                placememory.save_seen_cards(place, ctx.state.seen_cards)
                sections.append(local_card["text"])
                had_local = True
                try:
                    if "/植被/" in local_card.get("key", ""):
                        flora = local_card["text"].split("。")[0].split(",")[0].split("，")[0].strip()
                        if flora:
                            nb_env = dict(ctx.env) if ctx.env else {}
                            nb_env["_dt"] = now
                            notebook_mod.record_with_env("flora", flora, place, nb_env, lat)
                except (OSError, ValueError, TypeError):
                    pass  # intentionally ignored: notebook recording is non-critical

        # 1b. Trace (Card 16: blind时禁抽, traces contain place-specific details)
        _blind = getattr(ctx.state, "blind", False)
        if place and not _blind and placememory.has_trace(place) and len(sections) < 4:
            trace_text = placememory.get_trace_text(place)
            if trace_text and trace_text not in set(ctx.state.recent_scenes):
                sections.append(trace_text)
                had_local = True

        # 1c. Festival hit (Card 16: blind时禁抽)
        if place and not _blind and len(sections) < 4:
            from nowhere.server import _check_festival_hit
            fest_text = _check_festival_hit(place, cc, lat, now, ctx.rng)
            if fest_text and fest_text not in set(ctx.state.recent_scenes):
                sections.append(fest_text)
                had_local = True

        # 2. Location-specific scenes
        if not had_local and place and len(sections) < 4:
            location_scenes = describe._load_location_scenes()
            if place in location_scenes:
                text = _pick_fresh(location_scenes[place], ctx.rng)
                if text:
                    sections.append(text)
                    had_local = True

        # 3. Soundscape
        if not had_local and place and len(sections) < 4:
            soundscapes = _load_scene_file("scene_soundscape")
            if place in soundscapes:
                text = _pick_fresh(soundscapes[place], ctx.rng)
                if text:
                    sections.append(text)
                    had_local = True

        # 4. Taste/smell
        if not had_local and place and len(sections) < 4:
            tastes = _load_scene_file("scene_taste")
            if place in tastes:
                text = _pick_fresh(tastes[place], ctx.rng)
                if text:
                    sections.append(text)
                    had_local = True

        # 5. Generic biome fallback
        if not had_local and len(sections) < 4:
            composed = describe._compose_walk_scene(
                ctx.step_result.get("new_surface", ctx.env.get("surface", "grass")),
                ctx.state.biome or "",
                ctx.rng,
                lat=lat, lon=lon,
                recent_scenes=ctx.state.recent_scenes,
            )
            if composed:
                sections.append(composed)

    def render(self, ctx: WalkContext) -> str | None:
        return None  # 文本已在 resolve() 中追加进 ctx.sections


# ── Post-compose Actions (append to prose, not sections) ────────────


class SouvenirAction:
    """Natural souvenir pickup: 50% on first step, 30% afterwards."""

    name = "souvenir"

    def should(self, ctx: WalkContext) -> bool:
        if ctx.quiet:
            return False
        chance = 0.5 if len(ctx.state.path) <= 1 else 0.3
        return ctx.state.souvenir is None and ctx.rng.random() < chance

    def render(self, ctx: WalkContext) -> str | None:
        from nowhere.server import _pick_souvenir

        souvenir = _pick_souvenir(ctx.lat, ctx.lon, ctx.env, ctx.rng)
        if souvenir:
            ctx.state.souvenir = souvenir
            return souvenir["desc"]
        return None


class FestivalChaseAction:
    """Card 42: Festival chase wind mention -- 10% per walk, once per journey.
    Card 16: blind时禁抽(festival names reveal location)."""

    name = "festival_chase"

    def should(self, ctx: WalkContext) -> bool:
        # Card 16: blind mode disables festival chase (names reveal location)
        if getattr(ctx.state, "blind", False):
            return False
        return (
            not ctx.state.errand_festival_mentioned_this_journey
            and ctx.rng.random() < 0.10
        )

    def resolve(self, ctx: WalkContext) -> None:
        from nowhere.server import _check_festival_chase

        text = _check_festival_chase(ctx.lat, ctx.lon, ctx.now)
        if text:
            ctx.state.errand_festival_mentioned_this_journey = True
        self._text = text

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


class CotravelerAction:
    """Cotraveler: footprints + meeting + pos refresh."""

    name = "cotraveler"

    def should(self, ctx: WalkContext) -> bool:
        from nowhere import travelers as travelers_mod

        return (
            travelers_mod.is_enabled()
            and not travelers_mod.walk_alone_active(ctx.state)
        )

    def resolve(self, ctx: WalkContext) -> None:
        import logging
        import os
        from nowhere import travelers as travelers_mod

        traveler_name = os.environ.get("NOWHERE_TRAVELER_NAME", "").strip() or "网线那头的人"
        lat, lon = ctx.lat, ctx.lon
        prose_parts: list[str] = []

        try:
            # Refresh pos every 5 steps
            if ctx.state.walk_step_counter % 5 == 0:
                travelers_mod.refresh_pos(traveler_name, lat, lon)
            # Record footprint
            travelers_mod.record_footprint(traveler_name, lat, lon, ctx.state.place_name or "")
            # Check other travelers' footprints
            from nowhere.server import _cotraveler_encounter_counts, _cotraveler_meeting_log

            fp_text = travelers_mod.check_footprints(
                traveler_name, lat, lon, ctx.rng, _cotraveler_encounter_counts,
            )
            if fp_text:
                prose_parts.append(fp_text)
            # Check meeting (full mode only)
            if not travelers_mod.is_quiet():
                my_meet, _their_meet = travelers_mod.check_meeting(
                    traveler_name, lat, lon, ctx.rng, _cotraveler_meeting_log,
                )
                if my_meet:
                    prose_parts.append(my_meet)
        except (OSError, ValueError) as exc:
            # 同游者文件损坏/读失败已按 P0-31 语义改为抛错;
            # 这里兜住, 不让非核心功能炸掉行走管线
            logging.getLogger(__name__).warning(
                "cotraveler step skipped: %s", exc,
            )
            self._text = None
            return
        self._text = "\n".join(prose_parts) if prose_parts else None

    def render(self, ctx: WalkContext) -> str | None:
        return getattr(self, "_text", None)


# ── Registries ──────────────────────────────────────────────────────

# Pre-compose: feed into sections list. Order = priority.
ACTIONS: list[Action] = [
    WildernessNarrativeAction(),  # 荒深档叙事 (gated by is_deep_wilderness)
    LocalSceneAction(),           # 城市特有 > 通用 biome
    RadioQuietAction(),           # Card 39: 冷却期设计过的安静
    RhythmAction(),               # 节日/纪念日 (先于时间轴, 非 最高优先级)
    TimeaxisAction(),             # 时间轴
    HumanitiesAction(),           # 人文卡
    PersonAction(),               # 卡中人遇见
    MishapAction(),               # 意外层
    MishapEchoAction(),           # 意外回声 (depends on mishap)
    EncounterAction(),            # 文件遭遇
    MessageAction(),              # 消息遭遇
    WildernessEventAction(),      # 荒深事件
    RiverAction(),                # 河流叙事
    BuriedItemAction(),           # 埋藏物品
    NightNavAction(),             # 夜间导航
]

# Post-compose: append to prose. Split by normalize boundary.
PRE_NORMALIZE_ACTIONS: list[Action] = [
    FestivalChaseAction(), # 节日追风
    SouvenirAction(),      # 纪念品
]
POST_NORMALIZE_ACTIONS: list[Action] = [
    CotravelerAction(),    # 同游者
]
