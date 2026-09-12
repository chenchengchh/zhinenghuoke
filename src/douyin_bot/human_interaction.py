import math
import random
from typing import Any, List, Optional, Sequence, Tuple

from loguru import logger


_KEYBOARD_NEIGHBORS = {
    "a": "sqwz",
    "b": "vghn",
    "c": "xdfv",
    "d": "serfcx",
    "e": "wsdr",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "i": "ujko",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "m": "njk",
    "n": "bhjm",
    "o": "iklp",
    "p": "ol",
    "q": "wa",
    "r": "edft",
    "s": "awedxz",
    "t": "rfgy",
    "u": "yhji",
    "v": "cfgb",
    "w": "qase",
    "x": "zsdc",
    "y": "tghu",
    "z": "asx",
    "1": "2q",
    "2": "13w",
    "3": "24e",
    "4": "35r",
    "5": "46t",
    "6": "57y",
    "7": "68u",
    "8": "79i",
    "9": "80o",
    "0": "9op",
}


class HumanInteractionHelper:
    """Shared human-like interaction primitives for Playwright pages."""

    def __init__(self, page: Any, *, seed: Optional[int] = None):
        self.page = page
        self._rng = random.Random(seed)
        self._last_pointer: Optional[Tuple[float, float]] = None

    @staticmethod
    def build_scroll_segments(total_distance: float, steps: int = 9) -> List[int]:
        steps = max(steps, 5)
        distance = max(int(total_distance), 0)
        if distance <= 0:
            return []

        weights: List[float] = []
        for index in range(steps):
            progress = index / max(steps - 1, 1)
            if progress < 0.5:
                velocity = max(progress / 0.5, 0.05)
            else:
                velocity = max((1.0 - progress) / 0.5, 0.05)
            weights.append(0.2 + velocity)

        weight_sum = sum(weights) or 1.0
        raw = [max(int(distance * (weight / weight_sum)), 1) for weight in weights]
        delta = distance - sum(raw)
        raw[-1] += delta
        return raw

    @staticmethod
    def pick_typo_char(char: str, rng: Optional[random.Random] = None) -> str:
        picker = rng or random
        lower = str(char or "").lower()
        if lower not in _KEYBOARD_NEIGHBORS:
            return char
        candidates = [candidate for candidate in _KEYBOARD_NEIGHBORS[lower] if candidate != lower]
        if not candidates:
            return char
        chosen = picker.choice(candidates)
        return chosen.upper() if str(char).isupper() else chosen

    @staticmethod
    def _bezier_point(
        start: Tuple[float, float],
        control1: Tuple[float, float],
        control2: Tuple[float, float],
        end: Tuple[float, float],
        t: float,
    ) -> Tuple[float, float]:
        inv = 1.0 - t
        x = (
            inv * inv * inv * start[0]
            + 3 * inv * inv * t * control1[0]
            + 3 * inv * t * t * control2[0]
            + t * t * t * end[0]
        )
        y = (
            inv * inv * inv * start[1]
            + 3 * inv * inv * t * control1[1]
            + 3 * inv * t * t * control2[1]
            + t * t * t * end[1]
        )
        return (x, y)

    def _wait_ms(self, minimum: int, maximum: int) -> None:
        self.page.wait_for_timeout(self._rng.randint(minimum, maximum))

    def _move_pointer_along_curve(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        *,
        steps: int = 16,
        curve_strength: float = 0.22,
    ) -> None:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = max(math.hypot(dx, dy), 1.0)
        normal_x = -dy / distance
        normal_y = dx / distance
        bend = distance * curve_strength * self._rng.uniform(0.55, 1.2)
        control1 = (
            start[0] + dx * self._rng.uniform(0.18, 0.34) + normal_x * bend,
            start[1] + dy * self._rng.uniform(0.18, 0.34) + normal_y * bend,
        )
        control2 = (
            start[0] + dx * self._rng.uniform(0.62, 0.82) - normal_x * bend * self._rng.uniform(0.4, 0.9),
            start[1] + dy * self._rng.uniform(0.62, 0.82) - normal_y * bend * self._rng.uniform(0.4, 0.9),
        )

        for index in range(1, steps + 1):
            t = index / steps
            x, y = self._bezier_point(start, control1, control2, end, t)
            self.page.mouse.move(x, y)
            if index != steps:
                self._wait_ms(6, 18)
        self._last_pointer = end

    def move_to_target(self, box: dict) -> Tuple[float, float]:
        center_x = float(box["x"]) + float(box["width"]) * self._rng.uniform(0.42, 0.58)
        center_y = float(box["y"]) + float(box["height"]) * self._rng.uniform(0.42, 0.58)
        start = self._last_pointer or (
            center_x + self._rng.uniform(-90, 90),
            max(center_y + self._rng.uniform(-120, -40), 0.0),
        )

        distance = math.hypot(center_x - start[0], center_y - start[1])
        if distance > 40:
            overshoot = min(max(distance * 0.06, 4.0), 14.0)
            direction_x = (center_x - start[0]) / max(distance, 1.0)
            direction_y = (center_y - start[1]) / max(distance, 1.0)
            overshoot_target = (
                center_x + direction_x * overshoot,
                center_y + direction_y * overshoot,
            )
            self._move_pointer_along_curve(start, overshoot_target, steps=self._rng.randint(12, 18))
            self._move_pointer_along_curve(
                overshoot_target,
                (center_x, center_y),
                steps=self._rng.randint(4, 7),
                curve_strength=0.12,
            )
        else:
            self._move_pointer_along_curve(start, (center_x, center_y), steps=self._rng.randint(8, 12))
        return (center_x, center_y)

    def click_target(self, target: Any, *, pre_hover: bool = False) -> bool:
        try:
            if hasattr(target, "scroll_into_view_if_needed"):
                target.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass

        try:
            if pre_hover and hasattr(target, "hover"):
                target.hover(timeout=1200)
        except Exception:
            pass

        try:
            box = target.bounding_box()
            if box:
                self.move_to_target(box)
                self._wait_ms(24, 90)
                self.page.mouse.down()
                self._wait_ms(28, 86)
                self.page.mouse.up()
                self._wait_ms(36, 120)
                return True
        except Exception as exc:
            logger.debug(f"拟人点击回退常规 click: {exc}")

        try:
            target.click(timeout=1500, force=True)
            return True
        except Exception as exc:
            logger.debug(f"常规点击失败: {exc}")
            return False

    def press_sequentially(
        self,
        text: str,
        *,
        base_delay: Tuple[int, int] = (45, 105),
        allow_typos: bool = True,
        typo_chance: float = 0.08,
        pause_after_chunk: bool = True,
    ) -> None:
        clean_text = str(text or "")
        for index, char in enumerate(clean_text):
            if (
                allow_typos
                and char.isascii()
                and char.isalnum()
                and self._rng.random() < typo_chance
            ):
                typo_char = self.pick_typo_char(char, self._rng)
                if typo_char != char:
                    self.page.keyboard.type(typo_char, delay=self._rng.randint(*base_delay))
                    self._wait_ms(28, 72)
                    self.page.keyboard.press("Backspace")
                    self._wait_ms(26, 68)

            self.page.keyboard.type(char, delay=self._rng.randint(*base_delay))

            if char in "，。！？,.!?;；:\n":
                self._wait_ms(140, 320)
            elif pause_after_chunk and (index + 1) % self._rng.randint(4, 8) == 0 and index + 1 < len(clean_text):
                self._wait_ms(90, 220)
            else:
                self._wait_ms(18, 70)

    def clear_and_type(self, target: Any, text: str, *, allow_typos: bool = True) -> bool:
        if not self.click_target(target):
            return False

        try:
            target.press("Control+A", timeout=1200)
        except Exception:
            self.page.keyboard.press("Control+A")
        self._wait_ms(40, 110)

        try:
            target.press("Backspace", timeout=1200)
        except Exception:
            self.page.keyboard.press("Backspace")
        self._wait_ms(60, 160)

        self.press_sequentially(text, allow_typos=allow_typos)
        return True

    def scroll_page(self, *, total_distance: float, selector: str = "") -> int:
        segments = self.build_scroll_segments(total_distance)
        scrolled = 0
        for delta in segments:
            if selector:
                moved = self.page.evaluate(
                    """([targetSelector, amount]) => {
                        const node = document.querySelector(targetSelector);
                        if (!node) return 0;
                        const before = node.scrollTop || 0;
                        node.scrollTop = before + amount;
                        node.dispatchEvent(new Event('scroll', { bubbles: true }));
                        node.dispatchEvent(new WheelEvent('wheel', { deltaY: amount, bubbles: true }));
                        return (node.scrollTop || 0) - before;
                    }""",
                    [selector, delta],
                )
            else:
                moved = self.page.evaluate(
                    """(amount) => {
                        const before = window.scrollY || window.pageYOffset || 0;
                        window.scrollBy(0, amount);
                        return (window.scrollY || window.pageYOffset || 0) - before;
                    }""",
                    delta,
                )
            scrolled += int(moved or 0)
            self._wait_ms(48, 120)
        return scrolled
