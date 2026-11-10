#!/usr/bin/env python3
"""
HN Blackout bot: once a day, turn the Hacker News front page into a blackout
poem and post it to Bluesky and Mastodon as a 3-panel image:

    [ the front page ]  [ the same page, blacked out ]  [ the found poem ]

The poem comes from the site's own endpoint (POST /api/poem, Workers AI), so
this script needs no AI keys. Each platform is skipped when its secrets are
missing.

Env vars (GitHub Secrets):
  BSKY_HANDLE, BSKY_APP_PASSWORD                — Bluesky (app password)
  MASTODON_ACCESS_TOKEN, MASTODON_INSTANCE_URL  — Mastodon (write:statuses,
                                                  write:media scopes)
Usage:
  python bot/post_poem.py              # post today's poem
  python bot/post_poem.py --dry-run    # render bot/out/preview.png, post nothing
  python bot/post_poem.py --force      # post even if today already has a poem
  python bot/post_poem.py --force --only mastodon   # post to one platform only
"""

import argparse
import datetime as dt
import io
import json
import os
import random
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"
STATE_FILE = HERE / "posted.json"
OUT_DIR = HERE / "out"

HN = "https://hacker-news.firebaseio.com/v0"
POEM_API = os.getenv("POEM_API", "https://hn-blackout.pages.dev/api/poem")
SITE = "https://hn-blackout.pages.dev/"
BLOG = "https://mrdee.in/vibecoding/vibecoding-031-hn-blackout-bot/"
BLOG_LABEL = "How this bot works"
UA = "hn-blackout-bot (+https://github.com/mr-dinesh/hn-blackout)"

N_HEADLINES = 20
MIN_MATCHED = 6          # retry the poem if fewer words than this are found
POEM_ATTEMPTS = 3
BSKY_MAX_BYTES = 950_000  # Bluesky rejects images over ~976 KB

# Colours from the page's "newsprint" theme (blackout.html)
NEWSPRINT = "#f4ede0"
INK = "#2a2118"
INK_LIGHT = "#5a4f42"
RULE = "#e2d7c5"
MARKER = "#131110"
DARK_BG = "#0d0c0a"
DARK_BORDER = "#2c2820"
GOLD = "#c4a35a"
CREAM = "#e8d9c0"
CREAM_DIM = "#7a6e62"


# ── Fetching ──────────────────────────────────────────────────────────────────

def http_json(url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_titles(n=N_HEADLINES):
    ids = http_json(f"{HN}/topstories.json")[:n]
    with ThreadPoolExecutor(max_workers=10) as pool:
        items = list(pool.map(lambda i: http_json(f"{HN}/item/{i}.json"), ids))
    return [it["title"].strip() for it in items if it and it.get("title")]


def capture_hn(n=N_HEADLINES):
    """Screenshot the live HN front page down to story n with headless Chromium.
    Returns (PIL image, titles) — titles read from the same page, so the
    screenshot and the poem always use identical headlines."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv("CHROME_PATH") or None)
        try:
            # Under 750px HN switches to its mobile layout: larger text, wrapped
            # titles, and a tall shape that fills the panel (~0.64 wide:high).
            page = browser.new_page(viewport={"width": 620, "height": 2400}, device_scale_factor=2)
            page.goto("https://news.ycombinator.com/", wait_until="domcontentloaded", timeout=30_000)
            info = page.evaluate("""(n) => {
                const rows = [...document.querySelectorAll('tr.athing')].slice(0, n);
                const titles = rows.map(r => r.querySelector('.titleline > a')?.textContent.trim() || '');
                const last = rows[rows.length - 1];
                const sub = last?.nextElementSibling || last;
                const main = document.querySelector('#hnmain').getBoundingClientRect();
                return { titles, x: main.left, w: main.width,
                         bottom: sub.getBoundingClientRect().bottom + window.scrollY + 3 };
            }""", n)
            titles = [t for t in info["titles"] if t]
            if len(titles) < n // 2:
                raise RuntimeError(f"only {len(titles)} stories found on the page")
            png = page.screenshot(clip={"x": info["x"], "y": 0, "width": info["w"], "height": info["bottom"]})
        finally:
            browser.close()
    return Image.open(io.BytesIO(png)).convert("RGB"), titles


# ── Tokenise + match (same rules as blackout.html) ────────────────────────────

_TRAIL = re.compile(r"[.,;:!?()\[\]'\"‘’“”–—]+$")


def strip(s):
    return _TRAIL.sub("", s)


def tokenize(titles):
    """[(title_idx, word_idx, raw), ...]"""
    return [(ti, wi, raw)
            for ti, t in enumerate(titles)
            for wi, raw in enumerate(t.split())]


def match_poem(tokens, poem_words):
    """Find each poem word in the headlines, each token used at most once.
    Returns (selected token keys, poem words that were actually found)."""
    used, found = set(), []
    tests = (
        lambda t, w: t == w,
        lambda t, w: t.lower() == w.lower(),
        lambda t, w: strip(t) == strip(w),
        lambda t, w: strip(t).lower() == strip(w).lower(),
    )
    for word in poem_words:
        for test in tests:
            hit = next((tok for tok in tokens
                        if (tok[0], tok[1]) not in used and test(tok[2], word)), None)
            if hit:
                used.add((hit[0], hit[1]))
                found.append(hit[2])
                break
    return used, found


def tidy(word):
    """Poem-panel display only: drop dangling ':' ',' ';' left over from headlines."""
    return word.rstrip(":;,") or word


def get_poem(titles, tokens):
    last = None
    for _ in range(POEM_ATTEMPTS):
        try:
            res = http_json(POEM_API, {"titles": titles}, timeout=90)
        except Exception as e:  # noqa: BLE001 — retry on any network/API error
            last = f"poem API: {e}"
            continue
        selected, found = match_poem(tokens, res.get("poem") or [])
        if len(found) >= MIN_MATCHED:
            return (res.get("title") or "untitled").strip(), found, selected, res.get("model")
        last = f"only {len(found)} of {len(res.get('poem') or [])} poem words found in the headlines"
    raise SystemExit(f"Could not get a usable poem: {last}")


# ── Rendering ─────────────────────────────────────────────────────────────────

def font(name, size, weight=None):
    f = ImageFont.truetype(str(FONTS / name), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except (OSError, AttributeError):
            pass  # not a variable font / no FreeType variation support
    return f


def layout_headlines(draw, titles, f, x0, y0, max_w, line_h, gap):
    """Word-wrap every headline. Returns (positioned words, rule y's, bottom y)."""
    space = draw.textlength(" ", font=f)
    words, rules, y = [], [], y0
    for ti, title in enumerate(titles):
        x = x0
        for wi, raw in enumerate(title.split()):
            w = draw.textlength(raw, font=f)
            if x > x0 and x + w > x0 + max_w:
                x, y = x0, y + line_h
            words.append({"key": (ti, wi), "text": raw, "x": x, "y": y, "w": w})
            x += w + space
        y += line_h
        rules.append(y + gap / 2 - line_h * 0.12)
        y += gap
    return words, rules, y


def render(titles, selected, poem_title, poem_words, date, shot=None):
    W, H = 2400, 1400
    PANEL_W, PAD, TOP = 760, 30, 150
    img = Image.new("RGB", (W, H), DARK_BG)
    d = ImageDraw.Draw(img)

    # Masthead across the full width
    d.text((W / 2, 62), "HN BLACKOUT", font=font("PlayfairDisplay.ttf", 64, 900),
           fill=CREAM, anchor="mm")
    d.text((W / 2, 112), f"Words found. Meaning made. The rest, erased.  ·  {date:%-d %B %Y}",
           font=font("PlayfairDisplay-Italic.ttf", 24), fill=CREAM_DIM, anchor="mm")

    panels = [(PAD + i * (PANEL_W + PAD), TOP) for i in range(3)]
    panel_h = H - TOP - PAD
    inner_x, inner_w = 36, PANEL_W - 72
    label_f = font("PlayfairDisplay.ttf", 20, 700)

    # Pick the largest headline size that fits the panel height
    for size in range(30, 13, -1):
        f = font("IMFellEnglish-Regular.ttf", size)
        line_h, gap = size * 1.32, size * 0.55
        words, rules, bottom = layout_headlines(d, titles, f, inner_x, 0, inner_w, line_h, gap)
        if bottom <= panel_h - 100:
            break

    rng = random.Random(date.toordinal())  # stable marker wobble per day
    labels = ["THE FRONT PAGE", "BLACKED OUT"]
    for idx in (0, 1):
        px, py = panels[idx]
        if idx == 0 and shot is not None:
            # The real front page, scaled to fit the panel
            d.rectangle([px, py, px + PANEL_W, py + panel_h], fill="#f6f6ef")
            d.text((px + inner_x, py + 30), "THE FRONT PAGE", font=label_f, fill=INK_LIGHT)
            d.line([px + inner_x, py + 62, px + PANEL_W - inner_x, py + 62], fill=INK, width=2)
            box_w, box_h = PANEL_W - 24, panel_h - 86 - 12
            scale = min(box_w / shot.width, box_h / shot.height)
            im = shot.resize((round(shot.width * scale), round(shot.height * scale)), Image.LANCZOS)
            img.paste(im, (px + (PANEL_W - im.width) // 2, py + 86))
            continue
        d.rectangle([px, py, px + PANEL_W, py + panel_h], fill=NEWSPRINT)
        d.text((px + inner_x, py + 30), labels[idx], font=label_f, fill=INK_LIGHT)
        d.line([px + inner_x, py + 62, px + PANEL_W - inner_x, py + 62], fill=INK, width=2)
        oy = py + 86
        for ry in rules[:-1]:
            d.line([px + inner_x, oy + ry, px + PANEL_W - inner_x, oy + ry], fill=RULE, width=1)
        for w in words:
            x, y = px + w["x"], oy + w["y"]
            keep = idx == 0 or w["key"] in selected
            if keep:
                d.text((x, y), w["text"], font=f, fill=INK)
            else:
                # A marker stroke: slightly ragged, slightly wider than the word
                top = y + line_h * 0.04 + rng.uniform(-1.5, 1.5)
                bot = y + line_h * 0.86 + rng.uniform(-1.5, 1.5)
                d.rounded_rectangle([x - 2 + rng.uniform(-1, 0.5), top,
                                     x + w["w"] + 2 + rng.uniform(-0.5, 1.5), bot],
                                    radius=4, fill=MARKER)

    # Poem panel
    px, py = panels[2]
    d.rectangle([px, py, px + PANEL_W, py + panel_h], fill="#161411", outline=DARK_BORDER, width=2)
    d.text((px + inner_x, py + 30), "FOUND POEM", font=label_f, fill=CREAM_DIM)
    d.line([px + inner_x, py + 62, px + PANEL_W - inner_x, py + 62], fill=DARK_BORDER, width=2)

    title_f = font("PlayfairDisplay-Italic.ttf", 40)
    d.text((px + inner_x, py + 92), f"“{poem_title}”", font=title_f, fill=GOLD)

    avail_top, avail_bot = py + 170, py + panel_h - 90
    n = len(poem_words)
    size = int(min(54, (avail_bot - avail_top) / (n * 1.3)))
    pf = font("PlayfairDisplay-Italic.ttf", size)
    while size > 18 and max(d.textlength(w, font=pf) for w in poem_words) > inner_w:
        size -= 2
        pf = font("PlayfairDisplay-Italic.ttf", size)
    for i, w in enumerate(poem_words):
        d.text((px + inner_x, avail_top + i * size * 1.3), w, font=pf, fill=CREAM)

    foot = font("PlayfairDisplay-Italic.ttf", 22)
    d.text((px + inner_x, py + panel_h - 60), "— Found in today's Hacker News front page",
           font=foot, fill=CREAM_DIM)
    d.text((px + PANEL_W - inner_x, py + panel_h - 30), "hn-blackout.pages.dev",
           font=font("PlayfairDisplay.ttf", 18), fill=CREAM_DIM, anchor="rs")
    return img


def encode(img):
    """PNG if it fits Bluesky's limit (crisper text), else JPEG."""
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if buf.tell() <= BSKY_MAX_BYTES:
        return buf.getvalue(), "image/png"
    for q in (90, 85, 80, 70):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= BSKY_MAX_BYTES:
            return buf.getvalue(), "image/jpeg"
    raise SystemExit("Image too large even as JPEG q70")


# ── Post text ─────────────────────────────────────────────────────────────────

TAGS = ["BlackoutPoetry", "HackerNews", "poetry"]


def post_text(poem_title, poem_words):
    body = " ".join(poem_words)
    head = f"“{poem_title}”\n\n"
    tail = "\n\nFound in today's Hacker News front page."
    # Bluesky allows 300 graphemes; leave room for the link line and tags
    # (Bluesky link facets count only their display text, not the URL)
    links = len(SITE.removeprefix("https://").rstrip("/")) + 3 + len(BLOG_LABEL) + 1
    room = 300 - len(head) - len(tail) - links - sum(len(t) + 2 for t in TAGS)
    if len(body) > room:
        body = body[: room - 1].rsplit(" ", 1)[0] + "…"
    return head + body + tail


def alt_text(titles, poem_title, poem_words, screenshot=False):
    left = ("a screenshot of the Hacker News front page, top {n} stories" if screenshot
            else "the top {n} Hacker News headlines").format(n=len(titles))
    alt = (f"Three panels. Left: {left}. "
           f"Middle: the same headlines with every word blacked out except the poem. "
           f"Right: the found poem, titled “{poem_title}”: {' / '.join(poem_words)}. "
           f"Headlines: " + " · ".join(titles))
    return alt if len(alt) <= 1400 else alt[:1399] + "…"


# ── Posting ───────────────────────────────────────────────────────────────────

def post_bluesky(text, image, mime, alt, size):
    from atproto import Client, client_utils, models

    client = Client()
    # A leading "@" makes Bluesky treat the handle as an email address
    handle = os.environ["BSKY_HANDLE"].strip().lstrip("@")
    client.login(handle, os.environ["BSKY_APP_PASSWORD"].strip())
    tb = (client_utils.TextBuilder().text(text + "\n")
          .link(SITE.removeprefix("https://").rstrip("/"), SITE)
          .text(" · ").link(BLOG_LABEL, BLOG))
    for t in TAGS:
        tb.text(" ").tag(f"#{t}", t)
    res = client.send_image(
        text=tb, image=image, image_alt=alt,
        image_aspect_ratio=models.AppBskyEmbedDefs.AspectRatio(width=size[0], height=size[1]),
    )
    return res.uri


def post_mastodon(text, image, mime, alt):
    from mastodon import Mastodon

    m = Mastodon(access_token=os.environ["MASTODON_ACCESS_TOKEN"],
                 api_base_url=os.getenv("MASTODON_INSTANCE_URL") or "https://mastodon.social",
                 user_agent=UA)
    media = m.media_post(io.BytesIO(image), mime_type=mime, description=alt, synchronous=True)
    status = m.status_post(text + "\n" + SITE + "\n" + f"{BLOG_LABEL}: {BLOG}" + "\n\n" + " ".join(f"#{t}" for t in TAGS),
                           media_ids=[media], visibility="public")
    return status["url"]


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_state(entries):
    STATE_FILE.write_text(json.dumps(entries[-365:], indent=2, ensure_ascii=False) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="render bot/out/preview.* and print the post; post nothing")
    ap.add_argument("--force", action="store_true", help="post even if today already has a poem")
    ap.add_argument("--only", choices=["bluesky", "mastodon"],
                    help="post to this platform only (e.g. to add one that was missed today)")
    args = ap.parse_args()

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()  # IST
    state = load_state()
    if not args.dry_run and not args.force and any(e.get("date") == today.isoformat() for e in state):
        print(f"Already posted for {today}; use --force to post again.")
        return

    targets = []
    if os.getenv("BSKY_HANDLE") and os.getenv("BSKY_APP_PASSWORD"):
        targets.append("bluesky")
    if os.getenv("MASTODON_ACCESS_TOKEN"):
        targets.append("mastodon")
    if args.only:
        if args.only not in targets and not args.dry_run:
            raise SystemExit(f"--only {args.only}: that platform's secrets are not set.")
        targets = [args.only]
    if not targets and not args.dry_run:
        raise SystemExit("No accounts configured: set BSKY_HANDLE + BSKY_APP_PASSWORD and/or MASTODON_ACCESS_TOKEN.")

    try:
        shot, titles = capture_hn()
    except Exception as e:  # noqa: BLE001 — fall back to the API and a drawn panel
        print(f"Screenshot failed ({e}); using the HN API and a drawn front page.", file=sys.stderr)
        shot, titles = None, fetch_titles()
    tokens = tokenize(titles)
    poem_title, found, selected, model = get_poem(titles, tokens)
    poem_words = [tidy(w) for w in found]
    print(f"{len(titles)} headlines · “{poem_title}” · {len(poem_words)} words · {model}")

    img = render(titles, selected, poem_title, poem_words, today, shot)
    image, mime = encode(img)
    text = post_text(poem_title, poem_words)
    alt = alt_text(titles, poem_title, poem_words, screenshot=shot is not None)

    if args.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / ("preview.png" if mime == "image/png" else "preview.jpg")
        out.write_bytes(image)
        print(f"\n{text}\n{SITE} · {BLOG_LABEL} → {BLOG}\n\nalt ({len(alt)} chars): {alt}\n\nimage: {out} ({len(image) // 1024} KB)")
        return

    entry = {"date": today.isoformat(), "title": poem_title, "poem": poem_words,
             "model": model, "headlines": titles}
    errors = []
    for target in targets:
        try:
            if target == "bluesky":
                entry["bluesky"] = post_bluesky(text, image, mime, alt, img.size)
            else:
                entry["mastodon"] = post_mastodon(text, image, mime, alt)
            print(f"{target}: posted {entry[target]}")
        except Exception as e:  # noqa: BLE001 — report and carry on to the other platform
            errors.append(f"{target}: {e}")
            print(f"{target} error: {e}", file=sys.stderr)

    if len(errors) == len(targets):
        raise SystemExit("All platforms failed: " + "; ".join(errors))
    state.append(entry)
    save_state(state)


if __name__ == "__main__":
    main()
