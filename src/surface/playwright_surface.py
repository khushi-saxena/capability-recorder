"""
Playwright implementation of the Surface protocol.

Perception is a DOM walk that produces AX-style records, not Playwright's own
accessibility snapshot. Two reasons: the snapshot API gives no way back to an
element for acting on it, and this target has no accessible names on its inputs,
so we need the raw geometry the walker returns anyway.

Handles live for one observation. They're written onto elements as data-ax-h and
never persisted into an artifact.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Iterable

from playwright.sync_api import Frame, Page, sync_playwright

from src.surface.base import AXNode, Box, Observation

_WALKER = (Path(__file__).parent / "ax_walker.js").read_text()

EVIDENCE_DIR = Path("evidence")

_INTERACTIVE_TAGS = {"A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "LABEL"}


class PlaywrightSurface:
    def __init__(self, headless: bool = False, viewport=(1280, 900)):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._ctx = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        self._page: Page = self._ctx.new_page()
        self._vw, self._vh = viewport
        self._frames: dict[int, str] = {}  # handle -> frame_path

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def page(self) -> Page:
        """Exposed so the operator handoff can hand a human the same session."""
        return self._page

    # -- perception --------------------------------------------------------

    def _frame_paths(self) -> list[tuple[str, Frame]]:
        """Depth-first frame tree as (path, frame). Top frame is 'main'.
        Name comes off the frame element — Chromium doesn't report frame.name
        reliably for legacy <frame> tags."""
        out: list[tuple[str, Frame]] = []

        def label(child: Frame, i: int) -> str:
            for getter in (
                lambda: child.frame_element().get_attribute("name"),
                lambda: child.frame_element().get_attribute("id"),
                lambda: child.name,
            ):
                try:
                    v = getter()
                    if v:
                        return v
                except Exception:
                    pass
            return f"f{i}"

        def walk(frame: Frame, path: str) -> None:
            out.append((path, frame))
            for i, child in enumerate(frame.child_frames):
                walk(child, f"{path}/{label(child, i)}")

        walk(self._page.main_frame, "main")
        return out

    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        """Where this frame sits in the top-level viewport, in pixels."""
        if frame == self._page.main_frame:
            return 0.0, 0.0
        try:
            box = frame.frame_element().bounding_box()
        except Exception:
            return 0.0, 0.0
        if not box:
            return 0.0, 0.0
        return box["x"], box["y"]

    def observe(self) -> Observation:
        nodes: list[AXNode] = []
        self._frames = {}
        next_handle = 1

        for path, frame in self._frame_paths():
            try:
                result = frame.evaluate(_WALKER, next_handle)
            except Exception:
                continue  # frame detached mid-walk; skip it
            ox, oy = self._frame_offset(frame)
            for rec in result["nodes"]:
                b = rec["box"]
                nodes.append(
                    AXNode(
                        handle=rec["handle"],
                        role=rec["role"],
                        name=rec["name"] or "",
                        value=rec["value"],
                        enabled=rec["enabled"],
                        frame_path=path,
                        box=Box(
                            x=(ox + b["x"]) / self._vw,
                            y=(oy + b["y"]) / self._vh,
                            w=b["w"] / self._vw,
                            h=b["h"] / self._vh,
                        ),
                        table_id=f"{path}:{rec['table_id']}" if rec["table_id"] else None,
                        row=rec["row"],
                        col=rec["col"],
                    )
                )
                self._frames[rec["handle"]] = path
            next_handle = result["next"]

        return Observation(
            url=self._page.url,
            title=self._page.title(),
            nodes=tuple(nodes),
        )

    # -- actions -----------------------------------------------------------

    def _locator(self, handle: int):
        path = self._frames.get(handle)
        if path is None:
            raise KeyError(f"handle {handle} is not from the last observation")
        frame = dict(self._frame_paths()).get(path)
        if frame is None:
            raise KeyError(f"frame {path} is gone")
        return frame.locator(f'[data-ax-h="{handle}"]')

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")
        time.sleep(0.3)

    def activate(self, handle: int) -> None:
        loc = self._locator(handle)
        if loc.evaluate("el => el.tagName") not in _INTERACTIVE_TAGS:
            # table_cell resolution lands on the <td>, which in these layouts is
            # mostly padding. The operator clicked the one control inside it.
            inner = loc.locator("a, button, input[type=submit], input[type=button]")
            if inner.count() == 1:
                loc = inner.first
        loc.click()
        time.sleep(0.3)

    def fill(self, handle: int, text: str) -> None:
        loc = self._locator(handle)
        loc.fill("")
        loc.type(text)

    def select(self, handle: int, option: str) -> None:
        loc = self._locator(handle)
        try:
            loc.select_option(label=option)
        except Exception:
            loc.select_option(value=option)

    def read(self, handle: int) -> str:
        loc = self._locator(handle)
        tag = loc.evaluate("el => el.tagName")
        if tag in ("INPUT", "TEXTAREA", "SELECT"):
            return loc.input_value()
        return (loc.inner_text() or "").strip()

    def activate_at(self, x_ratio: float, y_ratio: float) -> None:
        self._page.mouse.click(x_ratio * self._vw, y_ratio * self._vh)
        time.sleep(0.3)

    # -- evidence ----------------------------------------------------------

    def screenshot(self, mask_handles: Iterable[int] = (), run_id: str = "") -> str:
        masked = list(mask_handles)
        for h in masked:
            try:
                self._locator(h).evaluate(
                    "el => { el.style.background = '#000'; el.style.color = '#000'; }"
                )
            except Exception:
                pass

        folder = EVIDENCE_DIR / (run_id or "adhoc")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"shot_{uuid.uuid4().hex[:8]}.png"
        self._page.screenshot(path=str(dest))
        return str(dest)