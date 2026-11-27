from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

from tools import shortlinks


FIRST = "https://s.shopee.tw/Ab123"
SECOND = "https://s.shopee.tw/Zy456"
PRODUCT = "https://shopee.tw/product/123/456"
LOCATION = f"{PRODUCT}?utm_source={shortlinks.AFFILIATE_ID}"


def button(url, model):
    return (
        f'<button disabled class="btn-placeholder" data-shopee-url="{url}" '
        f'data-model="{model}">連結準備中</button>'
    )


class ShortlinksTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.article = root / "guide.html"
        self.markup = button(PRODUCT, "Model &amp; 1")
        self.original = f'<section id="rank-3">\r\n  {self.markup}\r\n</section>\r\n'
        self.article.write_bytes(self.original.encode())
        self.links = root / "short-links.txt"
        self.links.write_text(FIRST, encoding="utf-8")
        self.ledger = root / "links.md"
        self.ledger_start = "| 型號 | 短連結 | 備註 |\n|---|---|---|\n| 保留 | 舊連結 | 原紀錄 |"
        self.ledger.write_text(self.ledger_start, encoding="utf-8")
        self.enterContext(patch.object(shortlinks, "LEDGER", self.ledger))
        self.head = self.enterContext(patch.object(shortlinks, "head", autospec=True))

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = shortlinks.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def fill(self, *additional_args):
        return self.run_cli("fill", str(self.article), "--links", str(self.links), *additional_args)

    def assert_files_unchanged(self):
        self.assertEqual(self.article.read_bytes(), self.original.encode())
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), self.ledger_start)

    def assert_fill_error(self, message):
        status, _, stderr = self.fill()
        self.assertEqual(status, 1)
        self.assertIn(message, stderr)
        self.assert_files_unchanged()

    def test_list_keeps_file_and_document_order(self):
        other = self.article.with_name("another.html")
        other_urls = ["https://shopee.tw/product/789/432", "https://shopee.tw/product/123/111"]
        other.write_text(
            f'<a class="btn-primary" href="{FIRST}">已回填</a>\n'
            + "\n".join(button(url, f"Other {i}") for i, url in enumerate(other_urls, 1)),
            encoding="utf-8",
        )
        status, stdout, stderr = self.run_cli("list", str(other), str(self.article))
        self.assertEqual(status, 0)
        self.assertEqual(stdout.splitlines(), ["依序轉成短連結，貼回同順序", "```", *other_urls, PRODUCT, "```"])
        self.assertEqual(
            stderr.splitlines(),
            [f"1｜Other 1｜{other_urls[0]}", f"2｜Other 2｜{other_urls[1]}", f"3｜Model & 1｜{PRODUCT}"],
        )
        self.head.assert_not_called()
        self.assert_files_unchanged()

    def test_count_mismatch_does_not_send_head(self):
        self.links.write_text(f"{FIRST}\n{SECOND}\n", encoding="utf-8")
        self.assert_fill_error("短連結數量不符")
        self.head.assert_not_called()

    def test_invalid_short_link_does_not_send_head(self):
        for bad_link in (PRODUCT, FIRST + "?extra=1", "https://s.shopee.tw/bad-slug"):
            with self.subTest(bad_link=bad_link):
                self.links.write_text(bad_link, encoding="utf-8")
                self.assert_fill_error("短連結格式錯誤")
        self.head.assert_not_called()

    def test_wrong_product_ids(self):
        for ids in ("12/456", "123/4567", "123/654"):
            with self.subTest(ids=ids):
                self.head.return_value = (301, LOCATION.replace("123/456", ids))
                self.assert_fill_error("Location 未含相符的店家／商品 ID 123/456")
        self.assertEqual(self.head.call_args_list, [call(FIRST)] * 3)

    def test_missing_or_incomplete_affiliate_id(self):
        for tracking in ("", f"?utm_source={shortlinks.AFFILIATE_ID}9", f"?utm_source=x{shortlinks.AFFILIATE_ID}"):
            with self.subTest(tracking=tracking):
                self.head.return_value = (302, PRODUCT + tracking)
                self.assert_fill_error(f"Location 未含分潤 ID {shortlinks.AFFILIATE_ID}")

    def test_redirect_response_and_location_required(self):
        for response, message in (((200, LOCATION), "須回 3xx"), ((301, ""), "缺 Location")):
            with self.subTest(response=response):
                self.head.return_value = response
                self.assert_fill_error(message)

    def test_no_articles_written_if_any_short_link_fails(self):
        other = self.article.with_name("another.html")
        other_markup = button("https://shopee.tw/product/999/222", "Another model")
        other.write_text(other_markup, encoding="utf-8")
        self.links.write_text(f"{FIRST}\n{SECOND}\n", encoding="utf-8")
        self.head.side_effect = [(301, LOCATION), (302, LOCATION)]
        status, _, stderr = self.run_cli("fill", str(self.article), str(other), "--links", str(self.links))
        self.assertEqual(status, 1)
        self.assertIn("999/222", stderr)
        self.assertEqual(self.head.call_args_list, [call(FIRST), call(SECOND)])
        self.assert_files_unchanged()
        self.assertEqual(other.read_text(encoding="utf-8"), other_markup)

    def test_fill_from_stdin_replaces_only_the_button_and_appends_ledger(self):
        self.head.return_value = (301, LOCATION)
        with patch("sys.stdin", StringIO(f"\n1. {FIRST}\n\n")):
            status, stdout, stderr = self.run_cli("fill", str(self.article), "--links", "-")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("回填完成 1 筆", stdout)
        self.head.assert_called_once_with(FIRST)
        anchor = (
            f'<a class="btn-primary" href="{FIRST}" rel="sponsored nofollow noopener" '
            'target="_blank">到蝦皮看即時價格</a>'
        )
        self.assertEqual(self.article.read_bytes(), self.original.replace(self.markup, anchor).encode())
        self.assertRegex(
            self.ledger.read_text(encoding="utf-8").removeprefix(self.ledger_start),
            rf"^\n\| Model & 1 \| {FIRST} \| 使用者於 \d{{4}}-\d{{2}}-\d{{2}} 提供，原文照錄。"
            rf"用於 guide 第 3 款。HEAD 一次回 301，Location 含 123/456，分潤 ID {shortlinks.AFFILIATE_ID}，相符。 \|\n\Z",
        )

    def test_dry_run_accepts_supported_location_formats(self):
        for path in ("/product/123/456", "/item-name-i.123.456", "/123/456"):
            with self.subTest(path=path):
                self.head.reset_mock()
                self.head.return_value = (302, f"https://shopee.tw{path}?utm_source={shortlinks.AFFILIATE_ID}")
                status, stdout, stderr = self.fill("--dry-run")
                self.assertEqual((status, stderr), (0, ""))
                self.assertIn("驗證通過 1 筆", stdout)
                self.head.assert_called_once_with(FIRST)
                self.assert_files_unchanged()


class HeadTest(unittest.TestCase):
    def test_urllib_sends_one_head_and_returns_redirect_without_following(self):
        headers = Message()
        headers["Location"] = LOCATION
        response = addinfourl(BytesIO(), headers, FIRST, 301)
        response.msg = "Moved Permanently"
        with patch.object(HTTPSHandler, "https_open", autospec=True, return_value=response) as open_https:
            self.assertEqual(shortlinks.head(FIRST), (301, LOCATION))
        open_https.assert_called_once()
        request = open_https.call_args.args[1]
        self.assertEqual(request.full_url, FIRST)
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.timeout, 15)
        self.assertIn("Mozilla/5.0", request.get_header("User-agent"))


if __name__ == "__main__":
    unittest.main()
