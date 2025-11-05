import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from playwright.async_api import async_playwright

# ================== 可调参数 ==================
BASE = "https://enroute.run"
COLLECTION = "https://enroute.run/collections/arcteryx"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
SNAPSHOT = Path("snapshot.json")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

REQUEST_TIMEOUT = 60000  # 单页加载超时（毫秒）
SCROLL_PAUSE = 800       # 集合页滚动等待（毫秒）
MAX_PAGES = 20           # 集合页兜底翻页上限
# =================================================


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


async def get_all_product_urls(page) -> list[str]:
    """
    遍历 Arc'teryx 集合页，抓取商品 URL（自动滚动 + 兜底翻页）
    并将 /products/<handle>/<variantId> 统一规范为 /products/<handle>
    """
    urls = set()

    def normalize_product_path(href: str) -> str:
        # 只保留 /products/<handle>
        parts = href.split("?")[0].split("/")
        # ['', 'products', '<handle>', '<maybe-variant-id>...']
        if len(parts) >= 3 and parts[1] == "products":
            return "/".join(parts[:3])
        return href.split("?")[0]

    async def collect_from_current():
        cards = await page.locator('a[href^="/products/"]').all()
        for a in cards:
            href = await a.get_attribute("href")
            if href and href.startswith("/products/"):
                norm = normalize_product_path(href)
                urls.add(urljoin(BASE, norm))

    await page.goto(COLLECTION, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT)
    await page.set_viewport_size({"width": 1400, "height": 1000})

    # 尝试无限滚动加载更多商品
    last_height = 0
    for _ in range(8):
        await collect_from_current()
        await page.mouse.wheel(0, 4000)
        await asyncio.sleep(SCROLL_PAUSE / 1000)
        height = await page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height

    # 兜底分页 ?page=2...
    for p in range(2, MAX_PAGES + 1):
        url = f"{COLLECTION}?page={p}"
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT)
        if not resp or resp.status != 200:
            break
        before = len(urls)
        await collect_from_current()
        if len(urls) == before:
            break

    return sorted(urls)


async def _safe_get_title(page, url: str) -> str:
    """
    稳健地获取标题：h1 -> og:title -> document.title -> URL handle
    """
    try:
        # 给前端更多时间完成请求
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    title = await page.evaluate(
        """() => {
        const pick = (el) => el && el.textContent ? el.textContent.trim() : '';
        const h1 = document.querySelector('h1');
        if (h1 && pick(h1)) return pick(h1);
        const og = document.querySelector('meta[property="og:title"]');
        if (og && og.content) return og.content.trim();
        return document.title || '';
    }"""
    )
    title = (title or "").strip()
    if title:
        return title

    # 最后从 URL 提取 handle
    path = urlparse(url).path.split("/")
    try:
        i = path.index("products")
        handle = path[i + 1] if len(path) > i + 1 else ""
    except ValueError:
        handle = ""
    return handle.replace("-", " ").strip() or "Arc'teryx"


async def parse_product(page, url: str) -> dict:
    """
    解析商品页：
    返回 { url, title, variants: [{color, size, available}] }
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(500)  # 给前端脚本一点渲染时间
    title = normalize_space(await _safe_get_title(page, url))
    variants = []

    # 优先尝试从 JSON 脚本中读变体（常见于 Shopify 或 Headless 主题）
    scripts = await page.locator('script[type="application/json"]').all()
    for s in scripts:
        txt = await s.inner_text()
        if not txt:
            continue
        if re.search(r'"variants?"\s*:', txt) or re.search(r'"options?"\s*:', txt):
            try:
                data = json.loads(txt)
                cand = []
                if isinstance(data, dict):
                    if "variants" in data and isinstance(data["variants"], list):
                        cand = data["variants"]
                    else:
                        # 尝试从嵌套对象中找
                        for _, v in data.items():
                            if isinstance(v, dict) and "variants" in v and isinstance(v["variants"], list):
                                cand = v["variants"]
                                break
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "variants" in item and isinstance(item["variants"], list):
                            cand = item["variants"]
                            break

                for v in cand or []:
                    size = v.get("option1") or v.get("size") or ""
                    color = v.get("option2") or v.get("color") or ""
                    if not color and isinstance(v.get("options"), list):
                        opts = v["options"]
                        if len(opts) >= 2:
                            color, size = opts[0], opts[1]
                        elif len(opts) == 1:
                            size = opts[0]
                    available = bool(v.get("available", v.get("is_in_stock", False)))
                    variants.append(
                        {
                            "color": normalize_space(str(color)),
                            "size": normalize_space(str(size)),
                            "available": available,
                        }
                    )
            except Exception:
                pass

    # 回退：如果 JSON 没拿到，就根据按钮/单选状态推断
    if not variants:
        color_group = page.locator(
            '[aria-label*="Color" i], [role="radiogroup"][aria-label*="Color" i], [data-option-name="Color"]'
        )
        size_group = page.locator(
            '[aria-label*="Size"  i], [role="radiogroup"][aria-label*="Size"  i], [data-option-name="Size"]'
        )

        colors = []
        if await color_group.count() > 0:
            btns = await color_group.locator("button, [role='radio']").all()
            for b in btns:
                label = await b.get_attribute("aria-label") or await b.text_content()
                if label:
                    colors.append(normalize_space(label))
        if not colors:
            # 有些主题颜色是图片/色块
            chips = await page.locator('img[alt*="color" i], [data-swatch]').all()
            for c in chips:
                alt = await c.get_attribute("alt")
                if alt:
                    colors.append(normalize_space(alt))
        colors = colors or [""]

        sizes = []
        if await size_group.count() > 0:
            btns = await size_group.locator("button, [role='radio'], input[type=radio']").all()
            for b in btns:
                label = await b.get_attribute("aria-label") or await b.get_attribute("value") or await b.text_content()
                disabled = (await b.get_attribute("disabled")) is not None or (
                    await b.get_attribute("aria-disabled")
                ) in ("true", "True")
                if label:
                    sizes.append((normalize_space(label), not disabled))
        else:
            # 兜底匹配常见尺码文案
            guess = await page.locator("button, [role='radio']").all()
            for g in guess:
                txt = normalize_space(await g.text_content() or "")
                if txt in {"XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL"}:
                    disabled = (await g.get_attribute("disabled")) is not None or (
                        await g.get_attribute("aria-disabled")
                    ) in ("true", "True")
                    sizes.append((txt, not disabled))

        for color in colors:
            for size, ok in sizes or [("", True)]:
                variants.append({"color": color, "size": size, "available": ok})

    return {"url": url, "title": title, "variants": variants}


def to_key(item: dict) -> str:
    return f"{item.get('title','')}|{item.get('color','')}|{item.get('size','')}"


def diff_changes(old: dict, new: dict):
    """
    返回 [(key, old_avail, new_avail, product_url)]
    仅在有货/缺货状态发生变化时记录
    """
    changes = []
    for k, v in new.items():
        na = v["available"]
        oa = old.get(k, {}).get("available")
        if oa is None:
            # 第一次出现的变体不触发
            continue
        if oa != na:
            changes.append((k, oa, na, v["url"]))
    return changes


async def send_discord(changes):
    if not DISCORD_WEBHOOK:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过通知。")
        return
    embeds = []
    for k, oa, na, url in changes[:10]:  # 每批最多 10 条，避免刷屏
        title, color, size = k.split("|")
        status = "🟢 补货" if na else "🔴 售罄"
        embeds.append(
            {
                "title": f"{status} · {title}",
                "url": url,
                "fields": [
                    {"name": "颜色", "value": color or "-", "inline": True},
                    {"name": "尺码", "value": size or "-", "inline": True},
                    {
                        "name": "变更",
                        "value": f"{'缺货' if oa is False else '有货'} → {'有货' if na else '缺货'}",
                        "inline": False,
                    },
                ],
            }
        )
    payload = {"content": None, "embeds": embeds} if embeds else {"content": "无库存变更"}
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json=payload, timeout=30) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print("Discord 推送失败:", resp.status, text)


async def parse_with_retry(page, url: str, tries=3):
    """
    包装 parse_product，失败时自动重试（递增退避）
    """
    for t in range(1, tries + 1):
        try:
            return await parse_product(page, url)
        except Exception as e:
            print(f"  ⚠️ 第 {t} 次尝试失败: {url} -> {e}")
            if t == tries:
                raise
            await asyncio.sleep(1.5 * t)  # 递增退避


async def run_once():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 1000},
            locale="en-US",
        )
        page = await context.new_page()

        print("抓取品牌集合页商品链接…")
        urls = await get_all_product_urls(page)
        print(f"共发现 {len(urls)} 个商品链接")

        new_map = {}
        for i, u in enumerate(urls, 1):
            try:
                prod = await parse_with_retry(page, u, tries=3)
            except Exception as e:
                print(f"[{i}/{len(urls)}] 解析失败: {u} -> {e}")
                continue

            title = prod["title"] or "Arc'teryx"
            for v in prod["variants"] or []:
                entry = {
                    "title": title,
                    "color": v.get("color", ""),
                    "size": v.get("size", ""),
                    "available": bool(v.get("available")),
                    "url": u,
                }
                new_map[to_key(entry)] = entry

            print(f"[{i}/{len(urls)}] {title} - {len(prod['variants'])} 个变体")

            # 轻度节流，避免网站限流
            if i % 10 == 0:
                await asyncio.sleep(2)

        # 载入旧快照
        old_map = {}
        if SNAPSHOT.exists():
            try:
                old_map = json.loads(SNAPSHOT.read_text("utf-8"))
            except Exception:
                old_map = {}

        # 计算变更
        changes = diff_changes(old_map, new_map)
        print(f"变更条目：{len(changes)}")

        # 写入新快照
        SNAPSHOT.write_text(json.dumps(new_map, ensure_ascii=False, indent=2), "utf-8")

        # 通知
        if changes:
            await send_discord(changes)

        await browser.close()


# 支持单页调试：在命令行设置 DEBUG_ONE_URL="https://enroute.run/products/xxx"
if __name__ == "__main__":
    DEBUG_ONE_URL = os.environ.get("DEBUG_ONE_URL", "").strip()
    if DEBUG_ONE_URL:
        async def _single():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    user_agent=USER_AGENT, viewport={"width": 1400, "height": 1000}, locale="en-US"
                )
                p = await ctx.new_page()
                prod = await parse_with_retry(p, DEBUG_ONE_URL, tries=3)
                print(json.dumps(prod, ensure_ascii=False, indent=2))
                await browser.close()
        asyncio.run(_single())
    else:
        asyncio.run(run_once())
