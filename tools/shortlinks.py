"""List draft Shopee placeholders and fill them with verified affiliate links."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
from html.parser import HTMLParser
from http.client import HTTPException
import os
from pathlib import Path
import re
from stat import S_IMODE
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener


LEDGER = Path(__file__).resolve().parents[2] / "links.md"
AFFILIATE_ID = "an_16308020046"
PRODUCT_URL = re.compile(r"https://shopee\.tw/product/([0-9]+)/([0-9]+)")
SHORT_URL = re.compile(r"https://s\.shopee\.tw/[A-Za-z0-9]+")
NUMBER_PREFIX = re.compile(r"^\d+[.．、)]\s+")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
BUTTON_END = re.compile(r"</\s*button\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class Placeholder:
    path: Path
    line: int
    start: int
    end: int
    rank: int
    model: str
    url: str
    shop: str
    item: str

    @property
    def source(self):
        return f"{self.path}:{self.line}（{self.model}）"


class DraftParser(HTMLParser):
    def __init__(self, path, source):
        super().__init__()
        self.path = path
        self.source = source
        self.line_offsets = [0, *(m.end() for m in re.finditer("\n", source))]
        self.sections = []
        self.product_count = 0
        self.active = None
        self.placeholders = []
        self.errors = []
        self.feed(source)
        self.close()
        if self.active is not None:
            self.error(self.active[0], "佔位按鈕缺少 </button>")

    def error(self, line, message):
        self.errors.append(f"{self.path}:{line} {message}")

    def handle_starttag(self, tag, attributes):
        attrs = {key: value or "" for key, value in attributes}
        if tag == "section":
            rank = re.fullmatch(r"rank-([1-9][0-9]*)", attrs.get("id", ""))
            self.sections.append(int(rank[1]) if rank else None)

        classes = attrs.get("class", "").split()
        if tag == "a" and "btn-primary" in classes and SHORT_URL.fullmatch(attrs.get("href", "")):
            self.product_count += 1
        if tag != "button":
            return

        line, column = self.getpos()
        if self.active is not None:
            self.error(line, "佔位按鈕內含另一個 button")
            return
        if "btn-placeholder" not in classes and "data-shopee-url" not in attrs:
            return

        self.product_count += 1
        url = attrs.get("data-shopee-url", "").strip()
        model = attrs.get("data-model", "").strip()
        match = PRODUCT_URL.fullmatch(url)
        if "btn-placeholder" not in classes or "disabled" not in attrs:
            self.error(line, "佔位按鈕需有 disabled 與 class btn-placeholder")
        elif not match:
            self.error(line, "data-shopee-url 需填 https://shopee.tw/product/店家ID/商品ID")
        elif not model or re.search(r"[\r\n〔〕<>]", model):
            self.error(line, "data-model 需填實際型號")
        else:
            rank = next((number for number in reversed(self.sections) if number), self.product_count)
            start = self.line_offsets[line - 1] + column
            self.active = (line, start, rank, model, url, *match.groups())

    def handle_endtag(self, tag):
        if tag == "section" and self.sections:
            self.sections.pop()
        if tag == "button" and self.active is not None:
            line, start, rank, model, url, shop, item = self.active
            end_line, column = self.getpos()
            close = BUTTON_END.match(self.source, self.line_offsets[end_line - 1] + column)
            if close:
                self.placeholders.append(
                    Placeholder(self.path, line, start, close.end(), rank, model, url, shop, item)
                )
            else:
                self.error(line, "佔位按鈕缺少 </button>")
            self.active = None


def read_drafts(paths):
    originals = {}
    placeholders = []
    errors = []
    for input_path in paths:
        path = input_path.resolve()
        if path in originals:
            errors.append(f"{input_path}:1 同一個草稿帶入兩次")
            continue
        try:
            original = path.read_bytes()
            draft = DraftParser(path, original.decode("utf-8"))
        except (OSError, UnicodeError) as exc:
            errors.append(f"{input_path}:1 無法讀取草稿（{exc}）")
            continue
        originals[path] = original
        placeholders.extend(draft.placeholders)
        errors.extend(draft.errors)
    if errors:
        raise ValueError("\n".join(errors))
    return originals, placeholders


def list_links(placeholders):
    print("依序轉成短連結，貼回同順序")
    print("```")
    for index, item in enumerate(placeholders, 1):
        print(item.url)
        print(f"{index}｜{item.model}｜{item.url}", file=sys.stderr)
    print("```")


def read_links(path):
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    return [
        NUMBER_PREFIX.sub("", line.strip())
        for line in text.removeprefix("\ufeff").splitlines()
        if line.strip()
    ]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def head(short_link):
    request = Request(short_link, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            return response.status, response.headers.get("Location", "")
    except HTTPError as response:
        with response:
            return response.code, response.headers.get("Location", "")


def verify_links(placeholders, links):
    if len(links) != len(placeholders):
        raise ValueError(f"短連結數量不符：收到 {len(links)} 筆，佔位有 {len(placeholders)} 個")
    malformed = [
        f"{item.source} 短連結格式錯誤：{link!r}，須為 https://s.shopee.tw/英數短碼"
        for item, link in zip(placeholders, links)
        if not SHORT_URL.fullmatch(link)
    ]
    if malformed:
        raise ValueError("\n".join(malformed))

    results = {}
    for link in dict.fromkeys(links):
        try:
            results[link] = head(link)
        except (OSError, ValueError, HTTPException) as exc:
            results[link] = exc

    errors = []
    codes = []
    for item, link in zip(placeholders, links):
        result = results[link]
        if isinstance(result, Exception):
            errors.append(f"{item.source} HEAD 失敗（{result}）")
            continue
        code, location = result
        if not 300 <= code < 400:
            errors.append(f"{item.source} HEAD 回 {code}，須回 3xx")
            continue
        if not location:
            errors.append(f"{item.source} HEAD 回 {code}，缺 Location")
            continue

        location = unquote(location)
        slash_ids = rf"(?:^|[/=]){item.shop}/{item.item}(?=$|[/?&#])"
        dot_ids = rf"(?<![A-Za-z0-9])i\.{item.shop}\.{item.item}(?=$|[/?&#])"
        if not re.search(rf"{slash_ids}|{dot_ids}", location):
            errors.append(f"{item.source} Location 未含相符的店家／商品 ID {item.shop}/{item.item}")
        if not re.search(rf"(?<![A-Za-z0-9_]){AFFILIATE_ID}(?![A-Za-z0-9_])", location):
            errors.append(f"{item.source} Location 未含分潤 ID {AFFILIATE_ID}")
        codes.append(code)

    if errors:
        raise ValueError("\n".join(errors))
    return codes


def replacements(originals, placeholders, links):
    edits = {}
    for item, link in zip(placeholders, links):
        replacement = (
            f'<a class="btn-primary" href="{link}" rel="sponsored nofollow noopener" '
            'target="_blank">到蝦皮看即時價格</a>'
        )
        edits.setdefault(item.path, []).append((item.start, item.end, replacement))

    updated = {}
    for path, changes in edits.items():
        source = originals[path].decode("utf-8")
        for start, end, replacement in reversed(changes):
            source = source[:start] + replacement + source[end:]
        updated[path] = source.encode("utf-8")
    return updated


def stage_article(path, content, cleanup):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temp:
        staged = Path(temp.name)
        cleanup.callback(staged.unlink, missing_ok=True)
        temp.write(content)
        os.fchmod(temp.fileno(), S_IMODE(path.stat().st_mode))
    return staged


def write_batch(originals, updated, rows):
    # Hold the ledger lock across the article replacements and the append.
    with LEDGER.open("r+b", buffering=0) as ledger, ExitStack() as cleanup:
        fcntl.flock(ledger, fcntl.LOCK_EX)
        end = ledger.seek(0, os.SEEK_END)
        separator = b""
        if end:
            ledger.seek(-1, os.SEEK_END)
            separator = b"\n" if ledger.read(1) != b"\n" else b""
        backups = {}
        staged = {}
        for path, content in updated.items():
            backups[path] = stage_article(path, originals[path], cleanup)
            staged[path] = stage_article(path, content, cleanup)
        for path in updated:
            if path.read_bytes() != originals[path]:
                raise ValueError(f"{path}:1 驗證期間草稿已變更，請重新執行 list")

        written = []
        appending = False
        try:
            for path, temp in staged.items():
                temp.replace(path)
                written.append(path)
            appending = True
            payload = separator + rows.encode("utf-8")
            if ledger.write(payload) != len(payload):
                raise OSError(f"{LEDGER} 追加內容未完整寫入")
            os.fsync(ledger.fileno())
        except (OSError, KeyboardInterrupt):
            try:
                if appending:
                    ledger.truncate(end)
            finally:
                for path in reversed(written):
                    backups[path].replace(path)
            raise


def ledger_rows(placeholders, links, codes):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    rows = []
    for item, link, code in zip(placeholders, links, codes):
        model = item.model.replace("|", r"\|")
        rows.append(
            f"| {model} | {link} | 使用者於 {today} 提供，原文照錄。"
            f"用於 {item.path.stem} 第 {item.rank} 款。HEAD 一次回 {code}，"
            f"Location 含 {item.shop}/{item.item}，分潤 ID {AFFILIATE_ID}，相符。 |\n"
        )
    return "".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description="整理草稿中的蝦皮分潤短連結")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="依序列出待轉短連結的商品網址")
    listing.add_argument("posts", nargs="+", type=Path)
    fill = commands.add_parser("fill", help="驗證站主貼回的短連結並回填")
    fill.add_argument("posts", nargs="+", type=Path)
    fill.add_argument("--links", required=True, help="短連結檔案，或 - 代表標準輸入")
    fill.add_argument("--dry-run", action="store_true", help="只驗證，不寫檔")
    args = parser.parse_args(argv)

    try:
        originals, placeholders = read_drafts(args.posts)
        if args.command == "list":
            list_links(placeholders)
            return 0
        links = read_links(args.links)
        codes = verify_links(placeholders, links)
        if placeholders and not args.dry_run:
            write_batch(
                originals, replacements(originals, placeholders, links),
                ledger_rows(placeholders, links, codes),
            )
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中止", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"驗證通過 {len(links)} 筆（dry-run）")
    else:
        print(f"回填完成 {len(links)} 筆")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
