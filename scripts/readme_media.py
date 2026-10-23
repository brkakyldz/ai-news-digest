"""Render the README's two animations.

    uv sync --group docs && uv run playwright install chromium
    uv run python scripts/readme_media.py            # both
    uv run python scripts/readme_media.py hero       # the app, driven
    uv run python scripts/readme_media.py pipeline   # one press, node by node
    uv run python scripts/readme_media.py social     # GitHub's social preview card

`hero.gif` drives the real app against `data/app.db`, the same database
`screenshots.py` photographs, and composites every frame into
`docs/media/src/hero.html`. `pipeline.gif` renders `docs/media/src/pipeline.html`
by calling its `render(t)` once per frame. Both are captured frame by frame
rather than screen-recorded, so the output depends on the storyboard and not on
how fast the machine that rendered it is. ffmpeg turns the frames into a GIF:
GitHub plays a GIF inline in a README, and a video only as a click-to-play
upload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots  # the server helpers live there

ROOT = screenshots.ROOT
SRC = ROOT / "docs" / "media" / "src"
OUT = ROOT / "docs" / "media"

FPS = 10
APP_W, APP_H = 1320, 780


def ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 3 * x * x - 2 * x * x * x


class Board:
    """The hero's storyboard: every method appends frames, and a frame is the
    app screenshot it shows plus what the compositor draws over it."""

    def __init__(self, page: Any, frames: Path) -> None:
        self.page = page
        self.dir = frames
        self.frames: list[dict[str, Any]] = []
        self.img = ""
        self.cursor: tuple[float, float] | None = None
        self.caption = ""
        self.caption_at = 0
        self.shots = 0

    def snap(self) -> str:
        path = self.dir / f"app-{self.shots:04d}.png"
        self.shots += 1
        self.page.screenshot(path=str(path))
        self.img = path.as_uri()
        return self.img

    def emit(self, **extra: Any) -> None:
        since = len(self.frames) - self.caption_at
        self.frames.append(
            {
                "img": self.img,
                "cursor": self.cursor,
                "caption": self.caption,
                "captionAlpha": min(1.0, since / (0.3 * FPS)),
                **extra,
            }
        )

    def say(self, caption: str) -> None:
        if caption != self.caption:
            self.caption = caption
            self.caption_at = len(self.frames)

    def hold(self, seconds: float) -> None:
        for _ in range(round(seconds * FPS)):
            self.emit()

    def move(self, to: tuple[float, float], seconds: float) -> None:
        start = self.cursor or to
        n = max(1, round(seconds * FPS))
        for i in range(1, n + 1):
            k = ease(i / n)
            self.cursor = (start[0] + (to[0] - start[0]) * k, start[1] + (to[1] - start[1]) * k)
            self.emit()

    def settle(self) -> None:
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate("document.fonts.ready")

    def goto(self, url: str, fade: float = 0.4) -> None:
        prev = self.img
        self.page.goto(url)
        self.settle()
        self.snap()
        self.crossfade(prev, fade)

    def crossfade(self, prev: str, seconds: float) -> None:
        n = round(seconds * FPS)
        for i in range(1, n + 1):
            self.emit(prev=prev if prev and i < n else None, alpha=i / n)

    def press(self, selector: str, navigates: bool = True) -> None:
        box = self.page.locator(selector).first.bounding_box()
        assert box, f"{selector} is not on the page"
        self.move((box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.55), 0.7)
        n = round(0.35 * FPS)
        for i in range(1, n + 1):
            self.emit(down=i < n, ripple=i / n)
        prev = self.img
        if navigates:
            with self.page.expect_navigation():
                self.page.locator(selector).first.click()
            self.settle()
        else:
            self.page.locator(selector).first.click()
        self.snap()
        self.crossfade(prev, 0.4)

    def scroll(self, dy: float, seconds: float) -> None:
        col = ".app > .col"
        top = self.page.evaluate(f"document.querySelector('{col}').scrollTop")
        n = round(seconds * FPS)
        for i in range(1, n + 1):
            y = top + dy * ease(i / n)
            self.page.evaluate(f"document.querySelector('{col}').scrollTop = {y}")
            self.snap()
            self.emit()

    def type(self, selector: str, text: str) -> None:
        field = self.page.locator(selector)
        for ch in text:
            field.press_sequentially(ch)
            self.snap()
            self.emit()


def storyboard(board: Board, base: str, run: str) -> None:
    b = board
    b.page.goto(f"{base}/?lang=en&theme=dark")
    b.settle()
    b.snap()
    b.cursor = (1010, 470)
    b.say("Today's bulletin, ranked by the editor")
    b.hold(1.6)
    b.scroll(640, 2.0)
    b.hold(0.8)
    b.scroll(-640, 0.8)

    b.say("The same stories, stored in Turkish")
    b.press(".seg--lang a[hreflang=tr]")
    b.hold(2.0)
    b.say("")
    b.press(".seg--lang a[hreflang=en]")

    b.say("Light or dark, your system's by default")
    b.press(".seg--theme a:nth-child(2)")
    b.hold(1.6)

    b.say("Full-text search over every story")
    b.press("#q", navigates=False)
    b.type("#q", "agents")
    with b.page.expect_navigation():
        b.page.locator("#q").press("Enter")
    b.settle()
    prev = b.img
    b.snap()
    b.crossfade(prev, 0.4)
    b.hold(1.8)

    b.cursor = None
    b.say("Every press, node by node: time, tokens, cost")
    b.goto(f"{base}/runs/{run}?lang=en&theme=dark", fade=0.5)
    b.hold(2.8)

    b.say("Today's bulletin, ranked by the editor")
    b.goto(f"{base}/?lang=en&theme=dark", fade=0.5)


@contextmanager
def app_server() -> Iterator[str]:
    port = screenshots.free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, SCHEDULER_ENABLED="false", PHOENIX_ENABLED="false")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ainews.web.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )
    try:
        screenshots.wait_for(f"{base}/health")
        yield base
    finally:
        server.terminate()
        server.wait(timeout=10)


def encode(frames: Path, pattern: str, out: Path, width: int) -> None:
    """Frames to a looping GIF. The palette is built from the pixels that
    change, so the still photo behind the glass does not spend it."""
    graph = (
        f"scale={width}:-1:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=stats_mode=diff:max_colors=192[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(FPS if "comp" in pattern else 15),
            "-i",
            str(frames / pattern),
            "-vf",
            graph,
            "-loop",
            "0",
            str(out),
        ],
        check=True,
    )
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


def hero(work: Path) -> None:
    from playwright.sync_api import sync_playwright

    run = screenshots.latest_digest_run()
    with app_server() as base, sync_playwright() as p:
        browser = p.chromium.launch()
        app = browser.new_page(viewport={"width": APP_W, "height": APP_H}, device_scale_factor=2)
        board = Board(app, work)
        storyboard(board, base, run)

        comp = browser.new_page(viewport={"width": 1280, "height": 830})
        comp.goto((SRC / "hero.html").as_uri())
        comp.evaluate("document.fonts.ready")
        for i, frame in enumerate(board.frames):
            comp.evaluate("f => window.setFrame(f)", frame)
            comp.screenshot(path=str(work / f"comp-{i:04d}.png"))
        browser.close()
    encode(work, "comp-%04d.png", OUT / "hero.gif", 1280)


def pipeline(work: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 600})
        page.goto((SRC / "pipeline.html").as_uri())
        page.evaluate("document.fonts.ready")
        total = page.evaluate("window.DURATION")
        for i in range(round(total * 15)):
            page.evaluate(f"window.render({i / 15})")
            page.screenshot(path=str(work / f"pipe-{i:04d}.png"))
        browser.close()
    encode(work, "pipe-%04d.png", OUT / "pipeline.gif", 1280)


def social(work: Path) -> None:
    """A still, not a GIF: GitHub takes the preview as an upload under the
    repository's settings, and an unfurled link shows one frame anyway."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640})
        page.goto((SRC / "social.html").as_uri())
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(OUT / "social-preview.png"))
        browser.close()
    print(f"wrote {OUT / 'social-preview.png'}")


def main(argv: list[str]) -> int:
    wanted = argv or ["hero", "pipeline", "social"]
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH; it turns the frames into a GIF")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in wanted:
        with tempfile.TemporaryDirectory(prefix=f"readme-{name}-") as tmp:
            {"hero": hero, "pipeline": pipeline, "social": social}[name](Path(tmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
