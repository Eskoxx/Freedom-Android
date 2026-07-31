from rich.style import Style
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static
from textual.reactive import reactive
from textual.message import Message
from textual.binding import Binding
from textual import events
from anime_watch.models import SearchResult, Episode, SearchResultGroup, TorrentResult
from anime_watch.providers import ANIME_SITES, MOVIE_SITES, TORRENT_SITES, CONFIGURED_PROVIDERS

C = {
    "accent": "#a78bfa",
    "text": "#e9e4f5",
    "alt": "#b9a7e6",
    "good": "#86d6a2",
    "warn": "#f0c560",
    "bad": "#ee7d92",
    "bright": "#d8b4fe",
    "rule": "#6b6577",
    "bg": "#0d0b14",
}

ICO = {
    "done": "✓",
    "error": "✗",
    "pending": "·",
    "pointer": "❯",
    "dot": "·",
    "bar": "▌",
}

SA = Style(color=C["accent"])
ST = Style(color=C["text"])
SD = Style(color=C["rule"])
SG = Style(color=C["good"])
SW = Style(color=C["warn"])
SB = Style(color=C["bad"])
SA_B = Style(color=C["accent"], bold=True)
ST_B = Style(color=C["text"], bold=True)

class LogoWidget(Static):
    def render(self):
        return Text.assemble(
            Text("\n"),
            Text("█▀▀ █▀█ █▀▀ █▀▀ █▀▄ █▀█ █▄█", SA_B),
            Text("\n"),
            Text("█▀  █▀▄ ██▄ ██▄ █▄▀ █▄█ █▀█", SA_B),
            Text("\n\n"),
            Text("A CLI anime streamer for terminal people.", SD),
        )

class RuleWidget(Static):
    def render(self):
        return Text("─" * self.size.width, style=SD)

class SidebarWidget(Widget):
    """Left rail showing configured/testing sites plus a focusable Downloads entry."""
    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "", priority=True),
    ]

    class OpenDownloads(Message):
        """Posted when the user activates the sidebar's Downloads entry."""

    def render(self):
        w = self.size.width
        lines = []

        def _render_sites(label, sites, icon, style):
            lines.append(Text("─" * w, style=SD))
            lines.append(Text(f" {label}", style=style))
            lines.append(Text("─" * w, style=SD))
            if sites:
                for site in sites:
                    lines.append(Text.assemble(
                        Text(f"  {icon} ", style=SG),
                        Text(site.name, style=ST),
                    ))
            else:
                lines.append(Text("  (none)", style=SD))
            lines.append(Text(""))

        _render_sites("Anime", ANIME_SITES, ICO['done'], SA_B)
        _render_sites("Movies", MOVIE_SITES, ICO['done'], SA_B)
        _render_sites("Torrent Movies", [s for s in TORRENT_SITES if s.slug != "nyaa"], ICO['pending'], SA_B)
        _render_sites("Torrent Anime", [s for s in TORRENT_SITES if s.slug == "nyaa"], ICO['pending'], SW)

        lines.append(Text(""))

        # ── Downloads entry ──
        lines.append(Text("─" * w, style=SD))
        label = Text(" Downloads", style=SA_B if self.has_focus else SA)
        lines.append(label)
        try:
            dls = getattr(self.app, "downloads", {})
            if dls:
                bar_w = max(3, w - 9)
                for ep_title, prog_str in list(dls.items())[:2]:
                    lines.append(Text(f"  {ep_title[:w-4]}", style=SD,
                                      no_wrap=True))
                    if "%" in prog_str and "100" not in prog_str:
                        pct_str = prog_str.split("%")[0].split()[-1]
                        try:
                            p = float(pct_str)
                            f = int(p / 100 * bar_w)
                            bar = "━" * f + "·" * (bar_w - f)
                            lines.append(Text(f"  {bar} {pct_str:>3}%",
                                              style=SG))
                        except (ValueError, IndexError):
                            lines.append(Text(f"  {prog_str[:w-4]}",
                                              style=SD))
                    else:
                        lines.append(Text(f"  {prog_str[:w-4]}", style=SD))
        except Exception:
            pass
        lines.append(Text("─" * w, style=SD))

        return Text("\n").join(lines)

    def on_click(self, event: events.Click):
        self.focus()
        self.post_message(self.OpenDownloads())

    def action_activate(self):
        """When Enter is pressed while sidebar has focus, open Downloads screen."""
        self.post_message(self.OpenDownloads())

class BaseListPanel(Widget):
    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "", priority=True),
    ]
    cursor = reactive(0)
    _hover_idx = reactive(-1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._items = []

    def set_items(self, items, cursor=0):
        self._items = items
        self.cursor = min(cursor, max(0, len(items) - 1)) if items else 0
        self.refresh()

    def render(self):
        return Text("", style=SD)

    def _click_to_item(self, y: int) -> int:
        return y if 0 <= y < len(self._items) else -1

    def on_click(self, event: events.Click):
        idx = self._click_to_item(event.y)
        if idx >= 0:
            self.cursor = idx
            self.focus()
            self.post_message(self.Activated())

    def action_activate(self):
        self.post_message(self.Activated())

    def on_mouse_move(self, event: events.MouseMove):
        idx = self._click_to_item(event.y)
        if idx != self._hover_idx:
            self._hover_idx = idx

    def on_leave(self):
        if self._hover_idx != -1:
            self._hover_idx = -1

    def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        self.move_down()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        self.move_up()

    class Activated(Message):
        pass

    def move_up(self):
        if self._items and self.cursor > 0:
            self.cursor -= 1

    def move_down(self):
        if self._items and self.cursor < len(self._items) - 1:
            self.cursor += 1


class DownloadsPanel(BaseListPanel):
    def __init__(self, *, id="downloads-list"):
        super().__init__(id=id)

    def _item_rows(self, item):
        typ = item.get("type", "")
        if typ == "section_header":
            return 3
        if typ in ("torrent", "dl", "resumable"):
            return 2
        return 1

    def set_items(self, items, cursor=0):
        super().set_items(items, cursor)
        self._fix_cursor()

    def _fix_cursor(self):
        while self._items and 0 <= self.cursor < len(self._items):
            if self._items[self.cursor].get("type") == "section_header":
                self.cursor += 1
            else:
                break
        if self.cursor >= len(self._items):
            self.cursor = max(0, len(self._items) - 1)

    def move_up(self):
        if self._items and self.cursor > 0:
            self.cursor -= 1
            while self.cursor > 0 and self._items[self.cursor].get("type") == "section_header":
                self.cursor -= 1

    def move_down(self):
        if self._items and self.cursor < len(self._items) - 1:
            self.cursor += 1
            while self.cursor < len(self._items) - 1 and self._items[self.cursor].get("type") == "section_header":
                self.cursor += 1

    def _click_to_item(self, y: int) -> int:
        display_y = 0
        for i, item in enumerate(self._items):
            rows = self._item_rows(item)
            if display_y <= y < display_y + rows:
                return i if item.get("type") != "section_header" else -1
            display_y += rows
        return -1

    def on_mouse_move(self, event: events.MouseMove):
        idx = self._click_to_item(event.y)
        if idx != self._hover_idx:
            self._hover_idx = idx

    def render(self):
        w = self.size.width
        lines = []

        if not self._items:
            lines.append(Text("  (no downloads)", style=SD))
            return Text("\n").join(lines)

        real_items = [it for it in self._items if it.get("type") != "section_header"]
        if not real_items:
            lines.append(Text("  (no downloads)", style=SD))
            return Text("\n").join(lines)

        focused = self.has_focus
        for i, item in enumerate(self._items):
            if item.get("type") == "section_header":
                title = item.get("title", "")
                lines.append(Text(""))
                lines.append(Text(f"── {title} ──", style=Style(color=C["alt"], italic=True)))
                lines.append(Text(""))
                continue

            sel = i == self.cursor
            hov = i == self._hover_idx and not sel
            mark = f"{ICO['pointer']} " if sel else "  "

            title = item["title"]
            status = item.get("status", "")
            typ = item.get("type", "")

            if sel and focused:
                c = SA_B
            elif sel:
                c = ST
            elif hov:
                c = Style(color=C["bright"], bold=True)
            else:
                c = SD

            if typ == "folder":
                icon = "▸ "
                c2 = SA
            elif typ == "resumable":
                icon = ""
                c2 = Style(color=C["warn"])
            elif typ in ("dl", "torrent"):
                icon = ""
                c2 = c
            else:
                icon = ""
                c2 = c

            import re
            pct_match = re.search(r'(\d+\.?\d*)\s*%', status)

            # First line: title
            title_text = Text(f"{icon}{title[:w-4]}", style=c2 if not sel else c, no_wrap=True)
            lines.append(Text.assemble(Text(mark, style=c), title_text))

            # Second line: progress bar or status (for ongoing items)
            if typ in ("torrent", "dl", "resumable"):
                indent = "  "
                if pct_match:
                    pct = float(pct_match.group(1))
                    bar_w = max(5, w - 10)
                    filled = int(pct / 100.0 * bar_w)
                    bar = "━" * filled + "·" * (bar_w - filled)
                    pct_str = f"{pct:.0f}%"
                    progress = Text(f"{indent}{bar} {pct_str}", style=SG)
                    lines.append(progress)
                else:
                    lines.append(Text(f"{indent}{status[:w-4]}", style=SD))

        return Text("\n").join(lines)

class ResultsPanel(BaseListPanel):
    class CategoryChanged(Message):
        def __init__(self, category: str, count: int):
            super().__init__()
            self.category = category
            self.count = count

    def __init__(self, *, id="results"):
        super().__init__(id=id)
        self._all_items: list = []
        self._categories: list[str] = []
        self._categorized: dict[str, list] = {}
        self._active_category = ""
        self._hit_areas: list = []

    @staticmethod
    def _group_name(cat: str) -> str:
        import re
        m = re.match(r'^(.*) \(\d+-\d+\)$', cat)
        return m.group(1) if m else cat

    def _group_categories(self) -> list[str]:
        base = self._group_name(self._active_category)
        return [c for c in self._categories if self._group_name(c) == base]

    def set_items(self, items, cursor=0):
        self._all_items = items
        self._categories = []
        self._categorized = {}
        self._active_category = ""
        self._build_categories()
        if self._categories:
            self._active_category = self._categories[0]
        self._reindex_items()
        self.cursor = min(cursor, max(0, len(self._items) - 1)) if self._items else 0
        self.refresh()

    def _build_categories(self):
        for item in self._all_items:
            if isinstance(item, Episode) and item.category:
                cat = item.category
            else:
                cat = ""
            if cat not in self._categorized:
                self._categorized[cat] = []
                self._categories.append(cat)
            self._categorized[cat].append(item)

    def _reindex_items(self):
        if self._active_category in self._categorized:
            self._items = self._categorized[self._active_category]
        else:
            self._items = self._all_items

    @property
    def active_category(self) -> str:
        return self._active_category

    @property
    def category_count(self) -> int:
        return len(self._categories)

    def next_category(self):
        group = self._group_categories()
        if not group:
            return
        idx = group.index(self._active_category)
        idx = (idx + 1) % len(group)
        self._switch_category(group[idx])

    def prev_category(self):
        group = self._group_categories()
        if not group:
            return
        idx = group.index(self._active_category)
        idx = (idx - 1) % len(group)
        self._switch_category(group[idx])

    def switch_category(self, name: str):
        if name in self._categorized:
            self._switch_category(name)
        else:
            for c in self._categories:
                if self._group_name(c) == name:
                    self._switch_category(c)
                    return

    def _switch_category(self, name: str):
        self._active_category = name
        self._reindex_items()
        self.cursor = 0
        self.refresh()
        self.post_message(self.CategoryChanged(name, len(self._items)))

    def get_item_at_cursor(self):
        if 0 <= self.cursor < len(self._items):
            return self._items[self.cursor]
        return None

    def category_tab_count(self) -> int:
        return len(self._categories)

    def _visible_range(self, total: int, height: int, cursor: int, tab_lines: int = 1):
        avail = height - tab_lines
        if avail <= 0:
            return (0, 0)
        if total <= avail:
            return (0, total)
        half = avail // 2
        start = max(0, cursor - half)
        end = min(total, start + avail)
        if end - start < avail:
            start = max(0, end - avail)
        return (start, end)

    def _item_y(self, idx: int) -> int:
        total = len(self._items)
        if total == 0:
            return -1
        h = self.size.height
        tab_lines = 2 if self._categories and len(self._categories) > 1 else 0
        start, end = self._visible_range(total, h, self.cursor, tab_lines)
        if idx < start or idx >= end:
            return -1
        header = (1 if start > 0 else 0) + tab_lines
        return header + (idx - start)

    def render(self):
        if not self._all_items:
            return Text("", style=SD)

        w = self.size.width
        h = self.size.height
        rows = []

        tab_lines = 0
        if self._categories and len(self._categories) > 1:
            focused = self.has_focus
            group = self._group_categories()
            cat_idx = group.index(self._active_category)
            total = len(group)

            all_bases = list(dict.fromkeys(self._group_name(c) for c in self._categories))
            other_bases = [b for b in all_bases if b != self._group_name(self._active_category)]

            self._hit_areas = []
            x = 0
            parts = []
            arrow_style = SA if focused else ST

            parts.append(Text(" ", style=ST))
            x += 1

            if len(group) > 1:
                parts.append(Text("<", style=arrow_style))
                self._hit_areas.append((x, x + 1, "prev"))
            else:
                parts.append(Text(" ", style=SD))
            x += 1

            parts.append(Text(" ", style=ST))
            x += 1

            label = self._active_category if self._active_category else "All"
            page_str = f" Page {cat_idx+1}/{total}"
            parts.append(Text(label, style=SA_B if focused else SA))
            parts.append(Text(page_str, style=SD))
            x += len(label) + len(page_str)

            parts.append(Text(" ", style=ST))
            x += 1

            if len(group) > 1:
                parts.append(Text(">", style=arrow_style))
                self._hit_areas.append((x, x + 1, "next"))
            else:
                parts.append(Text(" ", style=SD))
            x += 1

            if other_bases:
                parts.append(Text("   ", style=ST))
                x += 3

                for base in other_bases:
                    tab_label = base if base else "All"
                    tab = Text(f"[{tab_label}]", style=SD)
                    parts.append(tab)
                    self._hit_areas.append((x, x + len(tab_label) + 2, base))
                    x += len(tab_label) + 2
                    parts.append(Text("  ", style=ST))
                    x += 2

            rows.append(Text.assemble(*parts))
            rows.append(Text(""))
            tab_lines = 2

        if not self._items:
            rows.append(Text("  (no episodes)", style=SD))
            return Text("\n").join(rows)

        nw = max(2, len(str(len(self._all_items))))
        start, end = self._visible_range(len(self._items), h, self.cursor, tab_lines)
        if start > 0:
            rows.append(Text(f"  … ↑ {start} above", style=SD))
        for i in range(start, end):
            item = self._items[i]
            sel = i == self.cursor
            hov = i == self._hover_idx and not sel
            mark = f"{ICO['pointer']} " if sel else "  "
            num = Text(f"{i+1:>{nw}}", style=Style(color=C["alt"], dim=True))

            if isinstance(item, SearchResult):
                if sel:
                    name_style = SA_B
                elif hov:
                    name_style = Style(color=C["bright"], bold=True)
                else:
                    name_style = ST
                alive = item.data.get("alive")
                if alive is True:
                    name_style = Style(color=C["good"])
                elif alive is False:
                    name_style = Style(color=C["bad"])
                name = Text(f"{item.title[:w-nw-14]}", style=name_style, no_wrap=True)
                if alive is True:
                    sc = Style(color=C["good"], dim=True)
                elif alive is False:
                    sc = Style(color=C["bad"], dim=True)
                elif item.site_name.lower() in CONFIGURED_PROVIDERS:
                    sc = SG
                else:
                    sc = SD
                site = Text(f" {item.site_name[:8]:>8}", style=sc)
                rows.append(Text.assemble(Text(mark), num, Text(" "), name, site))
            elif isinstance(item, SearchResultGroup):
                if sel:
                    name_style = SA_B
                elif hov:
                    name_style = Style(color=C["bright"], bold=True)
                else:
                    name_style = ST
                name = Text(f"{item.title[:w-nw-18]}", style=name_style, no_wrap=True)
                site = Text(f" [{item.providers[:16]}]", style=Style(color=C["alt"], italic=True))
                rows.append(Text.assemble(Text(mark), num, Text(" "), name, site))
            elif isinstance(item, Episode):
                if sel:
                    ep_style = SA_B
                elif hov:
                    ep_style = Style(color=C["bright"], bold=True)
                else:
                    ep_style = ST
                ep_text = f"Ep {item.number}"
                ep_num = Text(f" {ep_text}", style=SD)
                name = Text(f"{item.title[:w-nw-len(ep_text)-8]}", style=ep_style, no_wrap=True)
                rows.append(Text.assemble(Text(mark), num, Text(" "), name, ep_num))
            elif isinstance(item, TorrentResult):
                if sel:
                    t_style = Style(color=C["warn"], bold=True)
                elif hov:
                    t_style = Style(color=C["bright"], bold=True)
                else:
                    t_style = ST
                size_str = item.size_str
                name = Text(f"{item.name[:w-nw-20]}", style=t_style, no_wrap=True)
                meta = Text(f" [{item.source} S:{item.seeders} L:{item.leechers} {size_str}]", style=SD)
                rows.append(Text.assemble(Text(mark), num, Text(" "), name, meta))
        if end < len(self._items):
            remaining = len(self._items) - end
            rows.append(Text(f"  … ↓ {remaining} below", style=SD))

        return Text("\n").join(rows) if rows else Text("", style=SD)

    def _click_to_item(self, y: int) -> int:
        total = len(self._items)
        if total == 0 or y < 0:
            return -1
        h = self.size.height
        tab_lines = 2 if self._categories and len(self._categories) > 1 else 0
        if y < tab_lines:
            return -1
        start, end = self._visible_range(total, h, self.cursor, tab_lines)
        header = (1 if start > 0 else 0) + tab_lines
        idx = start + (y - header)
        return idx if 0 <= idx < total else -1

    def on_click(self, event: events.Click):
        if self._categories and len(self._categories) > 1 and event.y == 0:
            for start, end, action in self._hit_areas:
                if start <= event.x < end:
                    if action == "prev":
                        self.prev_category()
                    elif action == "next":
                        self.next_category()
                    elif isinstance(action, str):
                        self.switch_category(action)
                    self.focus()
                    return
        super().on_click(event)

    def set_cursor(self, index: int):
        if self._items:
            self.cursor = max(0, min(index, len(self._items) - 1))

class HistoryPanel(BaseListPanel):
    def __init__(self, *, id="history-list"):
        super().__init__(id=id)

    def render(self):
        from anime_watch.history import HistoryEntry
        from rich.text import Text
        w = self.size.width
        lines = []
        if not self._items:
            lines.append(Text("  (no history)", style=SD))
            return Text("\n").join(lines)
        for i, entry in enumerate(self._items):
            sel = i == self.cursor
            hov = i == self._hover_idx and not sel
            mark = f"{ICO['pointer']} " if sel else "  "
            if sel:
                row_style = SA_B
            elif hov:
                row_style = Style(color=C["bright"], bold=True)
            else:
                row_style = ST
            pct = entry.progress_pct
            label = f"{entry.display} [{entry.site_name}]"
            bar_w = max(3, w - len(label) - 12)
            filled = int(pct / 100.0 * bar_w)
            bar = "━" * filled + "·" * (bar_w - filled)
            status = SG if entry.is_finished else SW
            lines.append(Text.assemble(
                Text(mark, style=row_style),
                Text(f"{label[:w-10]}", style=row_style, no_wrap=True),
                Text(f" {bar}", style=status),
                Text(f" {pct:.0f}%", style=SD),
            ))
        return Text("\n").join(lines)


class FooterHints(Widget):
    def __init__(self, *, id="footer"):
        super().__init__(id=id)
        self._hints = []

    def set_hints(self, hints):
        self._hints = hints
        self.refresh()

    def render(self):
        parts = []
        for i, (key, desc) in enumerate(self._hints):
            if i > 0: parts.append(Text("  ", style=SD))
            parts.append(Text(f"{key}", style=Style(color=C["alt"])))
            parts.append(Text(f" {desc}", style=SD))
        return Text.assemble(*parts) if parts else Text("")