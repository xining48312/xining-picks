import argparse
import gzip
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = "https://xining48312.github.io/xining-picks/"
HOST = "xining48312.github.io"
KEY = "a29ea906426ecc675ec02b56f60d37d5"
KEY_LOCATION = BASE + KEY + ".txt"
ENDPOINT = "https://api.indexnow.org/indexnow"
WAIT_SECONDS = 15 * 60
POLL_INTERVAL = 15
JOB_HTTP_TIMEOUT = 20

ROOT = Path(__file__).resolve().parents[2]
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
TAG_ATTRIBUTES = rb"""(?:[^"'<>]|"[^"]*"|'[^']*')*"""
NOINDEX = re.compile(
    # Skip comments before looking for a robots meta tag.
    rb"""\A(?:[^<]*<(?:!--(?:(?!-->).)*-->|(?!\!--)))*?[^<]*"""
    rb"""<meta(?=\s)"""
    rb"""(?=%s\sname\s*=\s*(["']?)robots\1(?=\s|/?>))"""
    rb"""(?=%s\scontent\s*=\s*(?:"(?:[^"]*[\s,])?noindex(?=[\s,"])"""
    rb"""|'(?:[^']*[\s,])?noindex(?=[\s,'])|noindex(?=\s|>)))"""
    rb"""%s>""" % (TAG_ATTRIBUTES, TAG_ATTRIBUTES, TAG_ATTRIBUTES),
    re.IGNORECASE | re.DOTALL,
)
NETWORK_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException, EOFError)


def git(*args, timeout=None):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, timeout=timeout
    )


def resolve_commit(revision):
    result = git("rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
    return result.stdout.decode().strip() if result.returncode == 0 else None


def blob(revision, path):
    if revision is None:
        try:
            return (ROOT / path).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return None
    result = git("show", f"{revision}:{path}")
    return result.stdout if result.returncode == 0 else None


def sitemap(revision):
    source = revision or "工作樹"
    data = blob(revision, "sitemap.xml")
    if data is None:
        raise ValueError(f"{source} 找不到 sitemap.xml")
    root = ET.fromstring(data)
    if root.tag != f"{{{NS['sm']}}}urlset":
        raise ValueError(f"{source} 的 sitemap.xml 根元素必須是 sitemap namespace 的 urlset")
    urls = {element.text.strip() for element in root.findall("sm:url/sm:loc", NS)
            if element.text}
    if not urls:
        raise ValueError(f"{source} 的 sitemap.xml 沒有 loc")
    for url in sorted(urls):
        if not url.startswith(BASE):
            raise ValueError(f"{source} 的 sitemap loc 超出網站範圍：{url!r}")
    return urls


def html_path(url):
    base_path = urllib.parse.urlsplit(BASE).path
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path[len(base_path):])
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"sitemap loc 無法對應網站檔案：{url}")
    return path + "index.html" if not path or path.endswith("/") else path


def warn(message, summary):
    print(f"警告：{message}", file=sys.stderr)
    summary.append(f"- {message}")


def candidates(before, after, summary):
    current = sitemap(after)
    previous = sitemap(before) if before else set()
    updates = {}
    deletes = set()
    for url in sorted(current | previous):
        path = html_path(url)
        content = blob(after, path)
        if url not in current:
            if content is None:
                deletes.add(url)
            elif NOINDEX.search(content):
                updates[url] = hashlib.sha256(content).digest()
            else:
                warn(f"{url} 已移出 sitemap，頁面仍可索引，未送出", summary)
        elif url not in previous or content != blob(before, path):
            if content is None or NOINDEX.search(content):
                reason = "找不到對應檔案" if content is None else "含 noindex"
                warn(f"{url} {reason}，未送出", summary)
            else:
                updates[url] = hashlib.sha256(content).digest()
    return updates, deletes


class HTTPSConnection(http.client.HTTPSConnection):
    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        # http.client normally generates Accept-Encoding: identity.
        super().putrequest(method, url, skip_host=skip_host, skip_accept_encoding=True)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(HTTPSConnection, request, context=self._context)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(HTTPSHandler(), NoRedirect())


def http(request, deadline):
    timeout = min(JOB_HTTP_TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError("已達全域截止時間")
    try:
        response = OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.getcode()
        headers = response.headers
        body = response.read()
        if headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return status, headers, body


def refresh_hashes(after, pending, hashes, deadline, summary):
    if not after:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    try:
        fetched = git("fetch", "origin", "main", timeout=min(JOB_HTTP_TIMEOUT, remaining))
    except subprocess.TimeoutExpired:
        warn("抓取 origin/main 逾時，沿用已有的檔案雜湊", summary)
        return
    if fetched.returncode:
        warn("無法抓取 origin/main，沿用已有的檔案雜湊", summary)
        return
    if not pending:
        return
    # FETCH_HEAD is the branch tip fetched by this command.
    revisions = git("rev-list", f"{after}..FETCH_HEAD")
    if revisions.returncode:
        warn("無法讀取後續 main 版本，沿用已有的檔案雜湊", summary)
        return
    for revision in revisions.stdout.decode().splitlines():
        for url in sorted(pending):
            if time.monotonic() >= deadline:
                return
            content = blob(revision, html_path(url))
            if content is not None:
                hashes[url].add(hashlib.sha256(content).digest())


def wait_for_pages(after, updates, deletes, started, summary):
    deadline = started + WAIT_SECONDS
    hashes = {url: {digest} for url, digest in updates.items()}
    pending = set(updates) | deletes
    confirmed = set()
    key_ready = False
    refresh_hashes(after, set(updates), hashes, deadline, summary)
    refresh_at = time.monotonic() + 60

    while time.monotonic() < deadline:
        round_start = time.monotonic()
        if round_start >= refresh_at and pending & updates.keys():
            refresh_hashes(after, pending & updates.keys(), hashes, deadline, summary)
            refresh_at = time.monotonic() + 60
        if not key_ready:
            try:
                status, _, body = http(KEY_LOCATION, deadline)
                key_ready = status == 200 and body.strip() == KEY.encode()
            except NETWORK_ERRORS:
                pass
        for url in sorted(pending):
            if time.monotonic() >= deadline:
                break
            try:
                status, _, body = http(url, deadline)
            except NETWORK_ERRORS:
                continue
            if url in deletes:
                ready = status in (404, 410)
            else:
                ready = status == 200 and hashlib.sha256(body).digest() in hashes[url]
            if ready:
                pending.remove(url)
                confirmed.add(url)
        if key_ready and not pending:
            break
        time.sleep(max(0, min(round_start + POLL_INTERVAL, deadline) - time.monotonic()))

    for url in sorted(pending):
        warn(f"{url} 等待上線逾時，未送出", summary)
    if not key_ready:
        warn("等待金鑰檔上線逾時", summary)
        for url in sorted(confirmed):
            summary.append(f"- {url} 已確認上線，金鑰尚未確認，未送出")
        return set(), True
    return confirmed, bool(pending)


def retry_after(headers):
    value = headers.get("Retry-After", "").strip()
    if value.isdecimal():
        return int(value)
    if value:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    return 60


def submit(urls, deadline, summary):
    for url in urls:
        if not url.startswith(BASE) or urllib.parse.urlsplit(url).hostname != HOST:
            raise ValueError(f"提交網址超出網站範圍：{url}")

    success = True
    for start in range(0, len(urls), 10_000):
        batch = urls[start:start + 10_000]
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps({"host": HOST, "key": KEY, "keyLocation": KEY_LOCATION,
                             "urlList": batch}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        result = submit_batch(request, deadline, summary)
        for url in batch:
            summary.append(f"- {url}：{result}")
        success = success and result.startswith(("200 ", "202 "))
    return success


def submit_batch(request, deadline, summary):
    connection_delays = iter((10, 30, 60))
    retried_429 = False
    while time.monotonic() < deadline:
        try:
            status, headers, body = http(request, deadline)
        except NETWORK_ERRORS as error:
            delay = next(connection_delays, None)
            warn(f"IndexNow 連線錯誤，重試可能重複送達：{error}", summary)
            if delay is None or delay + JOB_HTTP_TIMEOUT > deadline - time.monotonic():
                return "連線錯誤，提交結果未確認"
        else:
            if status == 200:
                return "200 已提交"
            if status == 202:
                return "202 已接收、金鑰待驗證。若連續多次收到 202，金鑰可能一直未通過驗證"
            delay = retry_after(headers) if status == 429 else 0
            if (status != 429 or retried_429
                    or delay + JOB_HTTP_TIMEOUT > deadline - time.monotonic()):
                detail = body.decode("utf-8", errors="replace").replace(KEY, "[金鑰已隱去]")
                warn(f"IndexNow HTTP {status}：{detail}", summary)
                return f"HTTP {status}，提交失敗"
            retried_429 = True
            warn(f"IndexNow HTTP 429，等待 {delay:g} 秒後重試一次", summary)
        time.sleep(delay)
    return "提交期限已到，未送出"


def run(args, summary):
    if (ROOT / f"{KEY}.txt").read_text(encoding="utf-8").strip() != KEY:
        raise ValueError("repo 根目錄的金鑰檔內容不符")
    head = resolve_commit(args.after) if args.after is not None else None
    if args.after is not None and head is None:
        raise ValueError("--after 無法解析為 commit")
    before = None
    if not args.all:
        if args.after is None or args.before is None:
            raise ValueError("差異模式需要 --before 和 --after")
        if re.fullmatch("0{40}", args.before):
            warn("--before 為 40 個 0，改用全送模式", summary)
        else:
            before = resolve_commit(args.before)
            if before is None:
                warn("--before 無法解析為 commit，改用全送模式", summary)
    if before is None:
        summary.append("- 全送模式無法補送基準未知且已移出 sitemap 的網址。")

    updates, deletes = candidates(before, head, summary)
    summary.append("\n### 候選網址")
    for url in sorted(updates.keys() | deletes):
        label = "DELETE " if url in deletes else ""
        print(label + url)
        summary.append(f"- {label}{url}")
    if not updates and not deletes:
        print("沒有需要通知的網址")
        summary.append("- 沒有需要通知的網址")
        return 0
    if args.dry_run:
        summary.append("- Dry run，候選網址皆未送出。")
        return 0

    started = time.monotonic()
    confirmed, timed_out = wait_for_pages(head or resolve_commit("HEAD"), updates, deletes,
                                          started, summary)
    summary.append("\n### 提交結果")
    # Keep time for POST retries after the 15-minute deployment deadline.
    success = submit(sorted(confirmed), started + 24 * 60, summary)
    return int(timed_out or not success)


def main():
    parser = argparse.ArgumentParser(description="通知 IndexNow 網站有更新的網址")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    summary = ["## IndexNow"]
    try:
        return run(parser.parse_args(), summary)
    except (ET.ParseError, OSError, ValueError) as error:
        message = str(error).replace(KEY, "[金鑰已隱去]")
        print(f"錯誤：{message}", file=sys.stderr)
        summary.append(f"- 錯誤：{message}")
        return 1
    finally:
        destination = os.environ.get("GITHUB_STEP_SUMMARY")
        if destination:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(("\n".join(summary) + "\n").replace(KEY, "[金鑰已隱去]"))


if __name__ == "__main__":
    sys.exit(main())
