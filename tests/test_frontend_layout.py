"""Layout contract: two-column shell, binding preservation, marker lane sizing.

The viewer is a browser page without a browser in CI, so the layout rules that
regressions keep breaking are checked as data:

  * every id the JS binds still exists exactly once in index.html (the two-column
    restructure must not drop a control),
  * the match column owns the canvas and the timeline while the console owns the
    setup, transport, HUD, crew and debug tools,
  * the console track is fixed/compact and the match track is fluid, so console
    scrolling or a longer debug panel cannot resize the canvas,
  * at 1440 px the match keeps at least 75 percent of the width, and narrow
    viewports stack instead of overflowing,
  * the dense event-marker lane keeps the markers inside it: the generic 36 px
    button touch target must not leak into the timeline strip.
"""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "id": None, "classes": [], "children": [], "parent": None, "text": ""}
        self.current = self.root
        self.ids = {}

    def _add(self, tag, attrs, push):
        values = dict(attrs)
        node = {
            "tag": tag,
            "id": values.get("id"),
            "classes": (values.get("class") or "").split(),
            "children": [],
            "parent": self.current,
            "text": "",
        }
        self.current["children"].append(node)
        if node["id"]:
            self.ids.setdefault(node["id"], []).append(node)
        if push and tag not in VOID_TAGS:
            self.current = node
        return node

    def handle_data(self, data):
        self.current["text"] += data

    def handle_starttag(self, tag, attrs):
        self._add(tag, attrs, True)

    def handle_startendtag(self, tag, attrs):
        self._add(tag, attrs, False)

    def handle_endtag(self, tag):
        cursor = self.current
        while cursor is not self.root:
            parent = cursor["parent"]
            if cursor["tag"] == tag:
                self.current = parent
                return
            cursor = parent


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def selector_matches(selector, target):
    """Match only single-compound selectors (tag / .class / #id / :pseudo)."""
    selector = selector.strip()
    if not selector or " " in selector or ">" in selector or "[" in selector:
        return False
    match = re.match(r"^([a-zA-Z*]+)?((?:[.#:][\w-]+)*)$", selector)
    if not match:
        return False
    tag, rest = match.group(1), match.group(2)
    if tag and tag != "*" and tag.lower() != target["tag"]:
        return False
    for token in re.findall(r"[.#:][\w-]+", rest):
        if token.startswith(".") and token[1:] not in target["classes"]:
            return False
        if token.startswith("#") and token[1:] != target.get("id"):
            return False
        if token.startswith(":") and token[1:] not in target.get("pseudo", set()):
            return False
    return True


def specificity(selector):
    selector = selector.strip()
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector)) + len(re.findall(r":[\w-]+", selector))
    types = len(re.findall(r"(?:^|[\s>])[a-zA-Z]+", selector))
    return (ids, classes, types)


def parse_css(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    media = []
    buffer = ""
    order = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            prelude = buffer.strip()
            buffer = ""
            if prelude.startswith("@"):
                media.append(prelude)
                index += 1
                continue
            end = text.index("}", index)
            body = text[index + 1:end]
            for selector in prelude.split(","):
                selector = selector.strip()
                if not selector:
                    continue
                rules.append({"selector": selector, "decls": parse_declarations(body),
                              "media": tuple(media), "order": order})
                order += 1
            index = end + 1
            continue
        if char == "}":
            if media:
                media.pop()
            buffer = ""
            index += 1
            continue
        buffer += char
        index += 1
    return rules


def parse_declarations(body):
    decls = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        prop, value = chunk.split(":", 1)
        prop = prop.strip().lower()
        if prop:
            decls[prop] = value.strip()
    return decls


def effective(rules, target, prop, media=None):
    best = None
    for rule in rules:
        if media is None and rule["media"]:
            continue
        if media is not None and rule["media"] != media:
            continue
        if prop not in rule["decls"] or not selector_matches(rule["selector"], target):
            continue
        key = (specificity(rule["selector"]), rule["order"])
        if best is None or key >= best[0]:
            best = (key, rule["decls"][prop])
    return best[1] if best else None


def by_selector(rules, selector, media=()):
    for rule in rules:
        if rule["selector"] == selector and rule["media"] == media:
            return rule["decls"]
    return None


def merged(rules, selector):
    """Effective declarations for an exact selector, later files winning."""
    out = {}
    for rule in rules:
        if rule["selector"] == selector and not rule["media"]:
            out.update(rule["decls"])
    return out


def px(value):
    match = re.match(r"^(-?\d+(?:\.\d+)?)px$", (value or "").strip())
    return float(match.group(1)) if match else None


def extract_ids(source):
    """Every id the frontend binds, from getElementById, $() and on() helpers."""
    found = set()
    found.update(re.findall(r"getElementById\(\s*['\"]([\w-]+)['\"]\s*\)", source))
    found.update(re.findall(r"\$\(\s*['\"]([\w-]+)['\"]\s*\)", source))
    found.update(re.findall(r"\bon\(\s*['\"]([\w-]+)['\"]", source))
    return found


class LayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dom = Dom()
        cls.dom.feed(read("index.html"))
        cls.app_css = parse_css(read("css/app.css"))
        cls.exp_css = parse_css(read("css/experience.css"))
        cls.css = cls.app_css + cls.exp_css

    def node(self, node_id):
        nodes = self.dom.ids.get(node_id)
        self.assertIsNotNone(nodes, f"index.html lost #{node_id}")
        self.assertEqual(1, len(nodes), f"#{node_id} must be unique")
        return nodes[0]

    def ancestors(self, node):
        chain = []
        cursor = node["parent"]
        while cursor is not None:
            chain.append(cursor)
            cursor = cursor["parent"]
        return chain

    def has_ancestor(self, node, node_id):
        return any(item["id"] == node_id for item in self.ancestors(node))

    def child_with_class(self, node, name):
        for child in node["children"]:
            if name in child["classes"]:
                return child
        return None

    def find_class(self, node, name):
        for child in node["children"]:
            if name in child["classes"]:
                return child
            found = self.find_class(child, name)
            if found is not None:
                return found
        return None

    # ------------------------------------------------------------- bindings
    def test_asset_urls_share_one_version_suffix(self):
        """A reload must fetch JS/CSS matching the served HTML, not a cached mix.

        The symptom this guards against: the root server serves fresh renderer.js
        but the browser keeps executing its old copy, so the page looks unchanged
        after an edit. Every local asset URL therefore carries the same `?v=`
        suffix, and the footer shows that version for diagnosis.
        """
        html = read("index.html")
        urls = re.findall(r'(?:src|href)="(/js/[\w.-]+\.js|/css/[\w.-]+\.css)(\?[^"]*)?"', html)
        self.assertTrue(urls, "the page must load the local JS and CSS assets")
        versions = {query for _, query in urls}
        self.assertEqual(1, len(versions), f"every asset needs the same suffix (got {sorted(versions)})")
        version = versions.pop()
        self.assertRegex(version, r"^\?v=[\w.-]+$", f"unexpected suffix {version!r}")
        # The visible footer text names the same version, so a stale tab (old page
        # text, or old assets under new text) can be told apart at a glance.
        token = version.split("=", 1)[1]
        footer = self.node("foot-ver")
        self.assertIn("前端", self.text_of(footer), "the footer names the frontend version")
        self.assertIn(token, self.text_of(footer), f"the footer must show {token}")

    def text_of(self, node):
        return "".join(node.get("text", ""))

    def test_every_bound_id_still_exists(self):
        js = "\n".join((WEB / "js" / name).read_text(encoding="utf-8")
                       for name in ["app.js", "panel.js", "experience.js", "guide.js"])
        missing = sorted(name for name in extract_ids(js) if name not in self.dom.ids)
        self.assertEqual([], missing, "the layout must keep every control the JS binds")
        duplicates = sorted(name for name, nodes in self.dom.ids.items() if len(nodes) > 1)
        self.assertEqual([], duplicates, "ids must stay unique")

    # ------------------------------------------------------------ two columns
    def test_shell_is_console_and_game(self):
        app = self.node("app")
        children = [child for child in app["children"] if child["tag"] != "dialog"]
        self.assertEqual(["console", "game"],
                         [child["id"] for child in children],
                         "#app must hold exactly the console and the match")

    def test_match_column_owns_canvas_and_timeline(self):
        canvas = self.node("stage-canvas")
        self.assertTrue(self.has_ancestor(canvas, "game"), "the canvas belongs to the match column")
        self.assertFalse(self.has_ancestor(canvas, "console"), "console scrolling must not own the canvas")
        self.node("map")
        game = self.node("game")
        self.assertIsNotNone(self.child_with_class(game, "timeline"), "the match column owns the timeline")
        self.assertTrue(self.has_ancestor(self.node("event-markers"), "game"),
                        "the marker lane sits in the match column")

    def test_console_owns_setup_transport_status_and_debug(self):
        console = self.node("console")
        for node_id in ["hud", "crew-list", "selection-card", "dev", "splitter", "newmatch"]:
            self.assertTrue(self.has_ancestor(self.node(node_id), "console"),
                            f"#{node_id} belongs to the console")
        for node_id in ["seed", "side", "pressure", "play", "step", "stepback", "reset", "speed"]:
            self.assertTrue(self.has_ancestor(self.node(node_id), "console"),
                            f"#{node_id} belongs to the console")
        transport = self.find_class(console, "transport")
        self.assertIsNotNone(transport, "the console owns the transport group")
        transport_ids = set()

        def collect(node):
            for child in node["children"]:
                if child["id"]:
                    transport_ids.add(child["id"])
                collect(child)

        collect(transport)
        for node_id in ["play", "step", "stepback", "reset", "speed", "mode-badge"]:
            self.assertIn(node_id, transport_ids, f"#{node_id} stays in the transport group")

    # --------------------------------------------------------------- sizing
    def test_console_track_is_compact_and_match_track_is_fluid(self):
        app = merged(self.css, ".app")
        self.assertEqual("100vh", app.get("height"), "the shell fills the viewport")
        self.assertIn("var(--console-w)", app.get("grid-template-columns", ""))
        self.assertIn("minmax(0, 1fr)", app.get("grid-template-columns", ""),
                      "the match column must be the flexible track")
        width = px(by_selector(self.exp_css, ":root").get("--console-w"))
        self.assertIsNotNone(width, "--console-w must be a pixel value")
        self.assertGreaterEqual(width, 280, "the console must stay usable")
        self.assertLessEqual(width, 320, "the console must stay compact")
        share = (1440 - width) / 1440
        self.assertGreaterEqual(share, 0.75, f"the match must keep 75 percent at 1440px (got {share:.3f})")

    def test_console_scrolls_on_its_own(self):
        console = merged(self.css, ".console")
        self.assertEqual("auto", console.get("overflow-y"))
        self.assertEqual("hidden", console.get("overflow-x"),
                         "console content must never scroll the page sideways")
        game = merged(self.css, ".game")
        self.assertIn("minmax(0, 1fr)", game.get("grid-template-rows", ""),
                      "the map row takes the leftover height")

    def test_shell_layout_has_one_source_of_truth(self):
        media_rules = [rule for rule in self.app_css if rule["media"]]
        self.assertEqual([], [f"{' '.join(rule['media'])} {rule['selector']}" for rule in media_rules],
                         "component CSS must not re-layout the shell; responsive rules live in experience.css")

    def test_narrow_viewports_stack_without_overflow(self):
        narrow = [rule for rule in self.css
                  if any("max-width:900px" in media for media in rule["media"])]
        self.assertTrue(narrow, "a narrow-viewport layout must exist")
        by_name = {(rule["selector"]): rule["decls"] for rule in narrow}
        self.assertEqual("auto", by_name.get("body", {}).get("overflow"))
        self.assertEqual("minmax(0,1fr)", by_name.get(".app", {}).get("grid-template-columns"))
        self.assertEqual("-1", by_name.get(".game", {}).get("order"),
                         "the match comes first when the columns stack")
        self.assertEqual("visible", by_name.get(".console", {}).get("overflow"))

    def test_console_dropdowns_expand_in_place(self):
        setup = by_selector(self.css, ".console .settings-menu .setup")
        self.assertIsNotNone(setup, "console dropdowns need their own placement")
        self.assertEqual("static", setup.get("position"),
                         "an overlay would be clipped by the console scroll container")

    def test_routine_round_hint_cannot_flash(self):
        decls = by_selector(self.css, '#step[aria-busy="true"]')
        self.assertIsNotNone(decls, "the routine settle hint must be defined")
        delay = decls.get("transition-delay", "")
        self.assertTrue(delay.endswith("s"), f"the hint needs a delay (got {delay!r})")
        self.assertGreaterEqual(float(delay[:-1]), 0.3,
                                "a fast round must finish before the hint is visible")

    # ------------------------------------------------------------ marker lane
    MARKER = {"tag": "button", "classes": {"event-marker"}, "pseudo": set()}
    MARKER_HOVER = {"tag": "button", "classes": {"event-marker"}, "pseudo": {"hover"}}
    MARKER_FOCUS = {"tag": "button", "classes": {"event-marker"}, "pseudo": {"focus-visible"}}

    def test_marker_buttons_fit_their_lane(self):
        self.assertEqual("0", effective(self.css, self.MARKER, "min-height"),
                         "markers must not inherit the 36px toolbar touch target")
        self.assertEqual("0", effective(self.css, self.MARKER, "padding"))
        self.assertEqual("10px", effective(self.css, self.MARKER, "height"))
        self.assertEqual("16px", effective(self.css, self.MARKER_HOVER, "height"),
                         "hover feedback stays, but inside the lane")
        outline = effective(self.css, self.MARKER_FOCUS, "outline") or ""
        self.assertRegex(outline, r"\d+px\s+solid", "keyboard focus must stay visible")
        offset = px(effective(self.css, self.MARKER_FOCUS, "outline-offset"))
        stroke = px(outline.split()[0]) if outline else None
        lane = px(effective(self.css, {"tag": "div", "classes": {"event-markers"}, "pseudo": set()}, "height"))
        self.assertIsNotNone(lane, "the marker lane needs an explicit height")
        self.assertGreaterEqual(lane, 16 + (offset or 0) + (stroke or 0),
                                "hover and focus must be contained by the lane")

    def test_marker_lane_is_a_strip_in_the_timeline(self):
        lane = None
        timeline = self.child_with_class(self.node("game"), "timeline")
        for child in timeline["children"]:
            if "event-markers" in child["classes"]:
                lane = child
        self.assertIsNotNone(lane, "the markers need their own lane inside the timeline")
        self.assertEqual([], lane["children"], "markers are created by the JS, not hard-coded")


if __name__ == "__main__":
    unittest.main()
