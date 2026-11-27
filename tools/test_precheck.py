from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import precheck


POST = "posts/guide.html"
PUBLISHED = "2026-11-27"
MODIFIED = "2026-11-28"
COVER = "images/cover.jpg"
SOCIAL_IMAGE = "images/social.jpg"
OG_IMAGE = f'<meta property="og:image" content="{precheck.SITE}{SOCIAL_IMAGE}">'
GOOD_LINK = '<a href="https://s.shopee.tw/Example" rel="sponsored nofollow noopener">價格</a>'


class PrecheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(precheck, "ROOT", self.root))
        self.article = f"""<!doctype html>
<meta property="article:published_time" content="{PUBLISHED}">
<meta property="article:modified_time" content="{MODIFIED}">
{OG_IMAGE}
<script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Article",
 "datePublished": "{PUBLISHED}", "dateModified": "{MODIFIED}"}}
</script>
<figure class="article-cover"><img src="../{COVER}"></figure>
<p>更新日 <time datetime="{MODIFIED}">{MODIFIED}</time></p>
{GOOD_LINK}
"""
        self.write(POST, self.article)
        self.write("tools/precheck-allow.txt", "s.shopee.tw\nschema.org\ngov.tw\nxining48312.github.io\n")
        self.write(COVER, "cover image fixture")
        self.write(SOCIAL_IMAGE, "social image fixture")
        self.write_sitemap()

    def write(self, path, contents):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(contents, encoding="utf-8")

    def write_sitemap(self, *pages, lastmod=MODIFIED):
        pages = pages or (POST,)
        entries = "".join(
            f"  <url><loc>{precheck.SITE}{path}</loc><lastmod>{lastmod}</lastmod></url>\n"
            for path in pages
        )
        self.write("sitemap.xml", f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n')

    def change_article(self, before, after):
        self.assertIn(before, self.article)
        self.write(POST, self.article.replace(before, after, 1))

    def run_cli(self):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = precheck.main([])
        return status, output.getvalue(), errors.getvalue()

    def assert_rejected(self, *messages, path=POST):
        status, _, errors = self.run_cli()
        self.assertEqual(status, 1, errors)
        for message in messages:
            self.assertTrue(
                any(line.startswith(f"{path}:") and message in line for line in errors.splitlines()),
                errors,
            )
        for line in errors.splitlines():
            self.assertRegex(line, r"^.+:\d+ .+")

    def test_healthy_site_passes_and_unscheduled_draft_is_skipped(self):
        draft = '<meta name="robots" content="noindex"><button class="btn-placeholder">待補連結</button>'
        self.write("posts/draft.html", draft)
        self.write("posts/post-template.html", draft.removeprefix('<meta name="robots" content="noindex">'))
        self.write(POST, self.article + '<a href="https://data.gov.tw/dataset/1">資料</a>')
        status, output, errors = self.run_cli()
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("已發布文章 1 篇", output)

    def test_momo_link_is_rejected_even_if_whitelisted(self):
        for extra in ("", "momoshop.com.tw\n"):
            with self.subTest(extra=extra):
                self.write("tools/precheck-allow.txt", "s.shopee.tw\nschema.org\nxining48312.github.io\n" + extra)
                self.write(POST, self.article + '<a href="https://www.momoshop.com.tw/product/123">商品</a>')
                self.assert_rejected("黑名單購物網域 www.momoshop.com.tw")

    def test_unknown_domain_gets_allowlist_hint_on_a_sitemap_page(self):
        self.write_sitemap(POST, "about.html")
        self.write("about.html", f'{OG_IMAGE}\n<a href="https://source.example/page">來源</a>')
        self.assert_rejected("未列入白名單的網域 source.example", "加入 precheck-allow.txt", path="about.html")

    def test_shopee_product_link_is_rejected(self):
        self.change_article(GOOD_LINK, '<a href="https://shopee.tw/product/123/456">商品</a>')
        self.assert_rejected("黑名單購物網域 shopee.tw")

    def test_short_link_requires_exact_rel_tokens_in_order(self):
        for rel in ("", ' rel="nofollow sponsored noopener"', ' rel="sponsored nofollow noopener noreferrer"'):
            with self.subTest(rel=rel):
                self.change_article(GOOD_LINK, f'<a href="https://s.shopee.tw/Example"{rel}>商品</a>')
                self.assert_rejected('蝦皮短連結須使用 rel="sponsored nofollow noopener"')

    def test_published_placeholders_are_rejected(self):
        for markup, message in (
            ("<p>待補連結</p>", "殘留佔位文字 待補連結"),
            ("<p>連結準備中</p>", "殘留佔位文字 連結準備中"),
            ("<p>〔型號〕</p>", "殘留佔位文字 〔型號〕"),
            ('<button class="btn-placeholder">商品</button>', "殘留 class btn-placeholder"),
            ('<button data-shopee-url="">商品</button>', "data-shopee-url 未填"),
        ):
            with self.subTest(markup=markup):
                self.change_article(GOOD_LINK, markup)
                self.assert_rejected(message)

    def test_sitemap_post_with_noindex_is_rejected(self):
        self.write(POST, '<meta name="robots" content="nofollow, noindex">\n' + self.article)
        self.assert_rejected("已發布文章含 meta robots noindex")

    def test_og_and_json_ld_date_mismatches(self):
        for meta, field, value in (
            ("article:published_time", "datePublished", PUBLISHED),
            ("article:modified_time", "dateModified", MODIFIED),
        ):
            with self.subTest(field=field):
                self.change_article(f'"{field}": "{value}"', f'"{field}": "2026-11-26"')
                self.assert_rejected(f"日期不一致：{meta}={value}、JSON-LD {field}=2026-11-26")

    def test_visible_update_and_sitemap_lastmod_mismatches(self):
        self.change_article(f'>{MODIFIED}</time>', '>2026-11-26</time>')
        self.write_sitemap(lastmod="2026-11-25")
        self.assert_rejected("可見更新日 <time>=2026-11-26", "sitemap lastmod=2026-11-25")

    def test_missing_published_date(self):
        self.change_article(f'<meta property="article:published_time" content="{PUBLISHED}">', "")
        self.assert_rejected("缺 article:published_time")

    def test_article_missing_from_sitemap(self):
        self.write("index.html", OG_IMAGE)
        self.write_sitemap("index.html")
        self.assert_rejected("已發布文章未列入 sitemap.xml")

    def test_missing_cover_and_og_image_files(self):
        (self.root / COVER).unlink()
        (self.root / SOCIAL_IMAGE).unlink()
        self.assert_rejected("封面圖 缺少本地檔案", "og:image 缺少本地檔案")

    def test_missing_og_image_tag(self):
        self.change_article(OG_IMAGE, "")
        self.assert_rejected("缺 og:image")

    def test_invalid_json_ld(self):
        self.change_article('"@type": "Article",', '"@type": "Article"')
        self.assert_rejected("JSON-LD 無法解析")

    def test_screenshot_server_uses_os_assigned_port(self):
        @contextmanager
        def fake_server(args, **kwargs):
            self.assertEqual(args[2:7], ["-m", "http.server", "0", "--bind", "127.0.0.1"])
            reader, writer = os.pipe()
            with os.fdopen(reader, "rb") as stdout, os.fdopen(writer, "wb") as stdin:
                stdin.write(b"Serving HTTP on 127.0.0.1 port 45219 (http://127.0.0.1:45219/) ...\n")
                stdin.flush()
                yield SimpleNamespace(stdout=stdout)

        with (
            patch.object(precheck.shutil, "which", return_value="/usr/bin/google-chrome"),
            patch.object(precheck, "running_process", fake_server),
            patch.object(precheck, "capture_pages", return_value=[]) as capture,
        ):
            self.assertEqual(precheck.screenshots([POST]), [])
        chrome, _, base, paths = capture.call_args.args
        self.assertEqual((chrome, base, paths), ("/usr/bin/google-chrome", "http://127.0.0.1:45219/", [POST]))

    def test_capture_uses_full_height_and_reports_horizontal_overflow(self):
        @contextmanager
        def fake_process(args, **kwargs):
            self.assertIn("--remote-debugging-pipe", args)
            yield SimpleNamespace(stdout=None)

        def cdp(chrome, method, **params):
            if method == "Emulation.setDeviceMetricsOverride":
                self.assertEqual((params["width"], params["deviceScaleFactor"]), (375, 1))
            if method == "Page.navigate":
                self.assertEqual(params["url"], "http://127.0.0.1:45219/posts/guide.html")
            if method == "Page.captureScreenshot":
                self.assertTrue(params["captureBeyondViewport"])
                self.assertEqual(params["clip"], {"x": 0, "y": 0, "width": 375, "height": 2400, "scale": 1})
            return {
                "Target.createTarget": {"targetId": "page"},
                "Target.attachToTarget": {"sessionId": "session"},
                "Page.captureScreenshot": {"data": "cG5nLWZpeHR1cmU="},
            }.get(method, {})

        previews = self.root / "drafts/previews/precheck-375"
        with (
            patch.object(precheck, "PREVIEWS", previews),
            patch.object(precheck, "running_process", fake_process),
            patch.object(precheck.Chrome, "call", autospec=True, side_effect=cdp),
            patch.object(precheck.Chrome, "evaluate", side_effect=[True, {"width": 390, "height": 2400}]),
        ):
            errors = precheck.capture_pages("google-chrome", str(self.root), "http://127.0.0.1:45219/", [POST])
        self.assertEqual((previews / "guide.png").read_bytes(), b"png-fixture")
        self.assertEqual(len(errors), 1)
        self.assertIn("出現橫向捲動（390 / 375 px）", errors[0])


if __name__ == "__main__":
    unittest.main()
