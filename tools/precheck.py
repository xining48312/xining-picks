"""Check release HTML and optionally capture mobile previews."""

import argparse
import base64
from contextlib import contextmanager
from datetime import date, datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.parsers import expat


ROOT = Path(__file__).resolve().parents[1]
SITE = "https://xining48312.github.io/xining-picks/"
SITE_HOST = urlsplit(SITE).hostname
SITE_PREFIX = urlsplit(SITE).path
PREVIEWS = ROOT.parent / "drafts/previews/precheck-375"
SHORT_HOST = "s.shopee.tw"
AFFILIATE_REL = ["sponsored", "nofollow", "noopener"]
SHOPPING_DOMAINS = (
    "shopee.tw", "shop.ee", "shope.ee", "momo.com.tw", "momoshop.com.tw",
    "momomall.com.tw", "pchome.com.tw", "pcstore.com.tw", "books.com.tw",
    "buy.yahoo.com", "bid.yahoo.com", "mall.yahoo.com", "shopping.yahoo.com",
    "buy.yahoo.com.tw", "bid.yahoo.com.tw", "mall.yahoo.com.tw", "shopping.yahoo.com.tw",
    "shopping.udn.com", "ruten.com.tw", "etmall.com.tw", "pcone.com.tw",
    "feebee.com.tw", "biggo.com.tw", "amzn.to",
)
MARKETPLACE = re.compile(r"(?:^|\.)(?:amazon|rakuten|coupang|feebee|shopee)\.[a-z.]+$")
PLACEHOLDER = re.compile(r"待補連結|連結準備中|〔[^〕]*〕")
UPDATE_LABEL = re.compile(r"更新日[：:\s]*$")
URL_START = re.compile(r"^(?:https?://|//)", re.IGNORECASE)
MULTI_URL = re.compile(r"(?:^|[\s,])((?:https?://|//)[^\s,]+)", re.IGNORECASE)
ARTICLE_TYPES = {
    "Article", "BlogPosting", "LiveBlogPosting", "NewsArticle", "Report",
    "ScholarlyArticle", "SocialMediaPosting", "TechArticle",
}


def domain_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def site_file(url, page=""):
    """Map a project URL or a page-relative reference to the checkout."""
    parsed = urlsplit(urljoin(urljoin(SITE, page), url))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != SITE_HOST:
        return None
    path = unquote(parsed.path)
    if not path.startswith(SITE_PREFIX):
        return None
    local = path[len(SITE_PREFIX):]
    if path.endswith("/"):
        local += "index.html"
    if not (ROOT / local).resolve().is_relative_to(ROOT):
        return None
    return local


def read_sitemap():
    entries = []
    entry = None
    field = None
    parser = expat.ParserCreate(namespace_separator=" ")

    def start(name, attrs):
        nonlocal entry, field
        name = name.rsplit(" ", 1)[-1]
        if name == "url":
            entry = {"line": parser.CurrentLineNumber}
        elif entry is not None and name in {"loc", "lastmod"}:
            field = name
            entry[field] = (parser.CurrentLineNumber, "")

    def characters(text):
        if field is not None:
            line, value = entry[field]
            entry[field] = (line, value + text)

    def end(name):
        nonlocal entry, field
        name = name.rsplit(" ", 1)[-1]
        if name == "url":
            entries.append(entry)
            entry = None
        elif name == field:
            field = None

    parser.StartElementHandler = start
    parser.CharacterDataHandler = characters
    parser.EndElementHandler = end
    parser.Parse((ROOT / "sitemap.xml").read_bytes(), True)
    return entries


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.meta = {}
        self.noindex = []
        self.cover_images = []
        self.update_times = []
        self.placeholders = []
        self.json_blocks = []
        self.figures = []
        self.script_line = None
        self.script_data = []
        self.update_time = None
        self.last_text = ""
        self.feed(source)
        self.close()
        self.placeholders.extend(
            (source.count("\n", 0, m.start()) + 1, f"殘留佔位文字 {m.group()}")
            for m in PLACEHOLDER.finditer(source)
        )

    def handle_starttag(self, tag, attributes):
        attrs = {key: value or "" for key, value in attributes}
        line = self.getpos()[0]
        classes = attrs.get("class", "").split()
        for key, value in attrs.items():
            value = value.strip()
            if key in {"srcset", "imagesrcset", "ping"}:
                self.links.extend((line, url, None) for url in MULTI_URL.findall(value))
            elif key != "xmlns" and URL_START.match(value):
                rel = attrs.get("rel", "").split() if tag == "a" and key == "href" else None
                self.links.append((line, value, rel))
        if tag == "meta":
            name = attrs.get("name", "").strip().lower()
            content = attrs.get("content", "").strip()
            if name == "robots" and "noindex" in re.split(r"[\s,]+", content.lower()):
                self.noindex.append(line)
            if attrs.get("http-equiv", "").lower() == "refresh":
                if match := re.search(r";\s*url\s*=\s*(.+)", content, re.IGNORECASE):
                    self.links.append((line, match[1].strip("'\" "), None))
            prop = attrs.get("property", "").strip().lower()
            if prop:
                self.meta.setdefault(prop, []).append((line, content))
        if tag == "figure":
            self.figures.append("article-cover" in classes)
        if tag == "img" and any(self.figures):
            self.cover_images.append((line, attrs.get("src", "").strip()))
        if "btn-placeholder" in classes:
            self.placeholders.append((line, "殘留 class btn-placeholder"))
        if "data-shopee-url" in attrs:
            value = attrs["data-shopee-url"].strip()
            if not value or "<" in value or PLACEHOLDER.search(value):
                self.placeholders.append((line, "data-shopee-url 未填"))
        if tag == "time" and UPDATE_LABEL.search(self.last_text):
            self.update_time = (line, attrs.get("datetime"), [])
        if tag == "script" and attrs.get("type", "").strip().lower() == "application/ld+json":
            self.script_line = line
            self.script_data = []

    def handle_data(self, data):
        if self.script_line is not None:
            self.script_data.append((self.getpos()[0], data))
        else:
            if data.strip():
                self.last_text = data.strip()
            if self.update_time is not None:
                self.update_time[2].append(data)

    def handle_endtag(self, tag):
        if tag == "figure" and self.figures:
            self.figures.pop()
        if tag == "time" and self.update_time is not None:
            line, attr, text = self.update_time
            self.update_times.append((line, attr, "".join(text).strip()))
            self.update_time = None
        if tag == "script" and self.script_line is not None:
            line = self.script_data[0][0] if self.script_data else self.script_line
            data = "".join(part for _, part in self.script_data)
            self.json_blocks.append((line, data))
            self.script_line = None


def json_nodes(value):
    yield value
    children = value.values() if isinstance(value, dict) else value
    if isinstance(value, (dict, list)):
        for child in children:
            yield from json_nodes(child)


def json_constant(value):
    raise ValueError(f"JSON 不允許 {value}")


def check_json(page, report):
    dates = {"datePublished": [], "dateModified": []}
    if page.script_line is not None:
        report(page.script_line, "JSON-LD 缺少 </script>")
    for line, source in page.json_blocks:
        try:
            data = json.loads(source, parse_constant=json_constant)
        except json.JSONDecodeError as exc:
            report(line + exc.lineno - 1, f"JSON-LD 無法解析（{exc.msg}）")
            continue
        except ValueError as exc:
            report(line, f"JSON-LD 無法解析（{exc}）")
            continue
        for node in json_nodes(data):
            if isinstance(node, str) and URL_START.match(node):
                token = json.dumps(node, ensure_ascii=False)
                offset = source.find(token)
                page.links.append((line + source.count("\n", 0, max(0, offset)), node, None))
            if not isinstance(node, dict):
                continue
            kinds = node.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            is_article = isinstance(kinds, list) and any(
                isinstance(kind, str) and kind.rsplit("/", 1)[-1] in ARTICLE_TYPES
                for kind in kinds
            )
            if is_article or ("@type" not in node and dates.keys() & node.keys()):
                for name, fields in dates.items():
                    if name in node:
                        match = re.search(rf'"{name}"\s*:', source)
                        field_line = line + source.count("\n", 0, match.start()) if match else line
                        fields.append((field_line, node[name]))
    return dates


def check_link(url, rel, allowed, report, line):
    if not URL_START.match(url):
        return
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        host = ""
    if not host:
        report(line, f"外連網址格式錯誤：{url}")
    elif host != SHORT_HOST and (
        MARKETPLACE.search(host) or any(domain_matches(host, item) for item in SHOPPING_DOMAINS)
    ):
        report(line, f"黑名單購物網域 {host}：{url}")
    elif not any(domain_matches(host, item) for item in allowed):
        report(line, f"未列入白名單的網域 {host}。若為非購物資訊來源，加入 precheck-allow.txt")
    elif host == SHORT_HOST and rel is not None and rel != AFFILIATE_REL:
        report(line, '蝦皮短連結須使用 rel="sponsored nofollow noopener"')


def calendar_date(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    chinese = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    try:
        if chinese:
            return date(*(int(part) for part in chinese.groups()))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", value):
            return datetime.fromisoformat(value).date()
    except ValueError:
        pass
    return None


def check_dates(fields, fallback_line, report):
    valid = []
    for name, values in fields:
        if not values:
            report(fallback_line, f"缺 {name}")
        for line, value in values:
            day = calendar_date(value)
            if day is None:
                report(line, f"{name} 日期缺漏或格式錯誤：{value!r}")
            else:
                valid.append((line, name, day))
    if len({day for _, _, day in valid}) > 1:
        details = "、".join(f"{name}={day}" for _, name, day in valid)
        report(valid[0][0], f"日期不一致：{details}")


def check_image(src, path, line, name, report):
    try:
        local = site_file(src, path) if src else None
        exists = local is not None and (ROOT / local).is_file()
    except (OSError, ValueError):
        exists = False
    if not exists:
        report(line, f"{name} 缺少本地檔案：{src or '未填路徑'}")


def check_page(path, page, published, sitemap_entry, allowed, errors):
    def report(line, reason):
        errors.append(f"{path}:{line} {reason}")

    dates = check_json(page, report)
    for line, url, rel in page.links:
        check_link(url, rel, allowed, report, line)

    images = page.meta.get("og:image", [])
    if not images:
        report(1, "缺 og:image")
    for line, src in images:
        check_image(src, path, line, "og:image", report)
    if not published:
        return

    for line in page.noindex:
        report(line, "已發布文章含 meta robots noindex")
    for line, reason in page.placeholders:
        report(line, reason)
    if not page.cover_images:
        report(1, "缺文章頂部封面圖（.article-cover img）")
    for line, src in page.cover_images:
        check_image(src, path, line, "封面圖", report)

    published_time = page.meta.get("article:published_time", [])
    modified_time = page.meta.get("article:modified_time", [])
    check_dates(
        [
            ("article:published_time", published_time),
            ("JSON-LD datePublished", dates["datePublished"]),
        ],
        1,
        report,
    )
    lastmod = []
    if sitemap_entry is None:
        report(1, "已發布文章未列入 sitemap.xml")
    else:
        line, value = sitemap_entry.get("lastmod", (sitemap_entry["line"], ""))
        if value.strip():
            if calendar_date(value) is None:
                errors.append(f"sitemap.xml:{line} {path} 的 lastmod 日期格式錯誤：{value.strip()!r}")
            else:
                lastmod.append((modified_time[0][0] if modified_time else 1, value))
        else:
            errors.append(f"sitemap.xml:{line} {path} 缺 lastmod")

    updates = [(line, visible) for line, _, visible in page.update_times]
    datetime_attrs = [(line, attr) for line, attr, _ in page.update_times if attr is not None]
    modified_fields = [
        ("article:modified_time", modified_time),
        ("JSON-LD dateModified", dates["dateModified"]),
        ("可見更新日 <time>", updates),
    ]
    if datetime_attrs:
        modified_fields.append(("<time> datetime", datetime_attrs))
    if lastmod:
        modified_fields.append(("sitemap lastmod", lastmod))
    check_dates(modified_fields, 1, report)


def check_release():
    errors = []
    sitemap = {}
    for entry in read_sitemap():
        line, loc = entry.get("loc", (entry["line"], ""))
        try:
            path = site_file(loc.strip()) if URL_START.match(loc.strip()) else None
        except (OSError, ValueError):
            path = None
        if path is None:
            errors.append(f"sitemap.xml:{line} loc 必須指向站內頁面：{loc.strip()!r}")
        else:
            sitemap[path] = entry

    allowed = {
        line.strip().lower().rstrip(".")
        for line in (ROOT / "tools/precheck-allow.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    posts = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob("posts/*.html")
        if path.name != "post-template.html"
    }
    published = []
    for path in sorted(sitemap.keys() | posts):
        try:
            page = Page((ROOT / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            if path in sitemap:
                line = sitemap[path].get("loc", (sitemap[path]["line"], ""))[0]
                errors.append(f"sitemap.xml:{line} 無法讀取 {path}（{exc}）")
            else:
                errors.append(f"{path}:1 無法讀取頁面（{exc}）")
            continue

        is_post = Path(path).parent == Path("posts") and path != "posts/post-template.html"
        is_published = is_post and (path in sitemap or not page.noindex)
        if is_published:
            published.append(path)
        if is_published or path in sitemap:
            check_page(path, page, is_published, sitemap.get(path), allowed, errors)
    return published, errors


@contextmanager
def running_process(args, **kwargs):
    with subprocess.Popen(args, **kwargs) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


class MessagePipe:
    def __init__(self, stream, separator):
        self.stream = stream
        self.separator = separator
        self.buffer = bytearray()

    def read(self, deadline):
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, selectors.EVENT_READ)
            while (end := self.buffer.find(self.separator)) < 0:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("等待子程序回應逾時")
                chunk = os.read(self.stream.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("子程序提前結束")
                self.buffer.extend(chunk)
            message = bytes(self.buffer[:end])
            del self.buffer[:end + len(self.separator)]
            return message


class Chrome:
    def __init__(self, process):
        self.process = process
        self.messages = MessagePipe(process.stdout, b"\x00")
        self.last_id = 0
        self.session = None

    def call(self, method, **params):
        self.last_id += 1
        request = {"id": self.last_id, "method": method, "params": params}
        if self.session is not None:
            request["sessionId"] = self.session
        self.process.stdin.write(json.dumps(request).encode() + b"\x00")
        self.process.stdin.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            message = json.loads(self.messages.read(deadline))
            if message.get("id") != self.last_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}：{message['error']['message']}")
            return message["result"]
        raise TimeoutError(f"{method} 回應逾時")

    def evaluate(self, expression, await_promise=False):
        response = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=await_promise
        )
        if "exceptionDetails" in response:
            raise RuntimeError("Chrome 無法讀取頁面的繪製狀態")
        return response["result"].get("value")

    def capture(self, url, dest):
        navigation = self.call("Page.navigate", url=url)
        if navigation.get("errorText"):
            raise RuntimeError(f"無法載入頁面：{navigation['errorText']}")
        ready = f"location.href === {json.dumps(url)} && document.readyState === 'complete'"
        deadline = time.monotonic() + 20
        while not self.evaluate(ready):
            if time.monotonic() >= deadline:
                raise TimeoutError("等待頁面載入逾時")
            time.sleep(0.1)

        sizes = self.evaluate(
            """(async () => {
                const images = Array.from(document.images);
                for (const image of images) image.loading = 'eager';
                await Promise.allSettled([document.fonts.ready, ...images.map(i => i.decode())]);
                await new Promise(resolve => requestAnimationFrame(resolve));
                const root = document.documentElement;
                return {width: root.scrollWidth, height: root.scrollHeight};
            })()""",
            await_promise=True,
        )
        result = self.call(
            "Page.captureScreenshot", format="png", captureBeyondViewport=True,
            clip={"x": 0, "y": 0, "width": 375, "height": sizes["height"], "scale": 1},
        )
        dest.write_bytes(base64.b64decode(result["data"], validate=True))
        return sizes["width"]


def screenshots(paths):
    if not paths:
        return []
    chrome_command = shutil.which("google-chrome")
    if not chrome_command:
        return ["tools/precheck.py:1 找不到 google-chrome，無法截圖"]

    errors = []
    server_args = [
        sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1",
        "--directory", str(ROOT),
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="precheck-chrome-") as profile:
            with (Path(profile) / "server.log").open("w+b") as server_log:
                try:
                    with running_process(
                        server_args, stdout=subprocess.PIPE, stderr=server_log
                    ) as server:
                        announcement = MessagePipe(server.stdout, b"\n").read(time.monotonic() + 10)
                        match = re.search(rb"port ([0-9]+)", announcement)
                        if not match:
                            raise RuntimeError("無法識別 http.server 連接埠")
                        base = f"http://127.0.0.1:{int(match[1])}/"
                        errors.extend(capture_pages(chrome_command, profile, base, paths))
                except (OSError, RuntimeError) as exc:
                    server_log.seek(0)
                    lines = server_log.read().decode(errors="replace").splitlines()
                    detail = f"{exc}；{lines[-1]}" if lines else str(exc)
                    errors.append(f"tools/precheck.py:1 本機截圖服務失敗（{detail}）")
    except OSError as exc:
        errors.append(f"tools/precheck.py:1 無法準備截圖環境（{exc}）")
    return errors


def capture_pages(command, profile, base, paths):
    # Chrome uses descriptors 3 and 4 for null-delimited CDP messages.
    args = [
        "/bin/sh", "-c", 'exec 3<&0 4>&1; exec "$@" </dev/null >&2', "chrome",
        command, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--remote-debugging-pipe", f"--user-data-dir={profile}", "about:blank",
    ]
    errors = []
    with (Path(profile) / "chrome.log").open("w+b") as chrome_log:
        try:
            with running_process(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=chrome_log
            ) as process:
                chrome = Chrome(process)
                target = chrome.call("Target.createTarget", url="about:blank")
                attached = chrome.call("Target.attachToTarget", targetId=target["targetId"], flatten=True)
                chrome.session = attached["sessionId"]
                chrome.call("Page.enable")
                chrome.call(
                    "Emulation.setDeviceMetricsOverride", width=375, height=800,
                    deviceScaleFactor=1, mobile=True,
                )
                PREVIEWS.mkdir(parents=True, exist_ok=True)
                for path in paths:
                    dest = PREVIEWS / f"{Path(path).stem}.png"
                    try:
                        width = chrome.capture(urljoin(base, quote(path)), dest)
                    except (OSError, RuntimeError, ValueError) as exc:
                        errors.append(f"{path}:1 截圖失敗（{exc}）")
                        continue
                    if width > 375:
                        errors.append(f"{path}:1 出現橫向捲動（{width} / 375 px），截圖 {dest}")
                    else:
                        print(f"{path}:1 無橫向捲動（{width} / 375 px），截圖 {dest}")
        except (OSError, RuntimeError, ValueError) as exc:
            chrome_log.seek(0)
            lines = chrome_log.read().decode(errors="replace").splitlines()
            detail = f"{exc}；{lines[-1]}" if lines else str(exc)
            errors.append(f"tools/precheck.py:1 Chrome 截圖服務失敗（{detail}）")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description="發布前檢查")
    parser.add_argument("--screenshots", action="store_true", help="截取 375px 文章全頁預覽")
    args = parser.parse_args(argv)
    try:
        published, errors = check_release()
    except expat.ExpatError as exc:
        published, errors = [], [f"sitemap.xml:{exc.lineno} XML 無法解析（{exc}）"]
    except (OSError, UnicodeError) as exc:
        published, errors = [], [f"{getattr(exc, 'filename', None) or 'tools/precheck.py'}:1 {exc}"]

    if args.screenshots:
        errors.extend(screenshots(published))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"發布前檢查通過（已發布文章 {len(published)} 篇）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
