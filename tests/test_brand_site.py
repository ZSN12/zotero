"""品牌站（web/site/）结构测试。

2026-09-03 对标增量：品牌站从 0 到 5 页（首页/产品/演示/开源/关于）。
测试钉住两件事：
  * 结构：5 页齐备 + 站内互链有效 + 服务器路由（/site/*）可达；
  * 诚实性：对外主口径数字只以「纯直读」语境出现，重建并集
    （99.7%）必须带辅助口径标注——把口径纪律钉进页面文案，
    防止品牌站退化成「拿重建数字冒充直读能力」的营销页。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "web" / "site"

PAGES = ("index.html", "product.html", "demo.html", "opensource.html", "about.html")


class BrandSiteStructureTest(unittest.TestCase):
    def test_five_pages_exist_with_shared_css(self):
        for page in PAGES:
            p = SITE / page
            self.assertTrue(p.exists(), p)
            html = p.read_text(encoding="utf-8")
            # 每页都挂共享样式 + 导航 5 链接
            self.assertIn('href="site.css"', html, f"{page} 缺 site.css")
            for nav in PAGES:
                self.assertIn(nav, html, f"{page} 导航缺 {nav}")
        self.assertTrue((SITE / "site.css").exists())

    def test_internal_links_resolve(self):
        """站内 href/src 指向的文件必须存在（防断链）。"""
        for page in PAGES:
            html = (SITE / page).read_text(encoding="utf-8")
            for m in re.finditer(r'(?:href|src)="([^"#]+)"', html):
                href = m.group(1)
                if href.startswith(("http://", "https://", "/")):
                    continue  # 外链 / 服务路由（/、/demo/…）由 server 测试覆盖
                self.assertTrue(
                    (SITE / href).exists(),
                    f"{page} 断链：{href}")

    def test_caliber_honesty_in_copy(self):
        """口径纪律落在文案上：99.7% 只能以辅助/重建语境出现。"""
        home = (SITE / "index.html").read_text(encoding="utf-8")
        # 主口径卡片：纯直读 tag + 220 TP
        self.assertIn("纯直读", home)
        self.assertIn("220", home)
        # 重建并集卡片必须带辅助口径 tag + 明示「仅内部归因」
        self.assertIn("重建并集", home)
        self.assertIn("仅内部归因", home)
        # 99.7 出现处，同卡片必须含「重建」或「辅助」字样（粗校验：
        # 数字所在行 200 字内含口径限定词）
        for m in re.finditer(r"99\.7%", home):
            ctx = home[max(0, m.start() - 200):m.end() + 200]
            self.assertTrue(
                ("重建" in ctx) or ("辅助" in ctx) or ("dual-union" in ctx),
                "99.7% 出现处 200 字内无重建/辅助口径限定词（口径纪律违例）")

    def test_server_routes_site_pages(self):
        """web/server.py 提供 /site/* 路由（镜像 /demo/ 白名单模式）。"""
        src = (REPO / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/site/")', src)
