import asyncio
import json
import os
import re
import time
import random
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

REQUEST_TIMEOUT = 90000  # 单页加载超时（毫秒）
SCROLL_PAUSE = 800       # 集合页滚动等待（毫秒）
MAX_PAGES = 20           # 集合页兜底翻页上限
# =================================================


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def cents_to_str(cents: int, currency: str | None) -> str:
    if cents is None:
        return "-"
    cur = (currency or "").upper()
    val = cents / 100.0
    sym = "$" if cur in ("USD", "", None) else f"{cur} "
    return f"{sym}{val:.2f}"


def get_handle_from_url(url: str) -> str:
    path = urlparse(url).path.split("/")
    try:
        i = path.index("products")
        handle = path[i + 1] if len(path) > i + 1 else ""
    except ValueError:
        handle = ""
    return handle


async def get_all_product_urls(page) -> list[str]:
    """
    遍历 Arc'teryx 集合页，抓取商品 URL（自动滚动 + 兜底翻页）
    并将 /products/<handle>/<variantId> 统一规范为 /products/<handle>
    """
    urls = set()

    def normalize_product_path(href: str) -> str:
        parts = href.split("?")[0].split("/")
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

    # 无限滚动加载更多
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
    return get_handle_from_url(url).replace("-", " ").strip() or "Arc'teryx"


def _parse_price_like(v) -> int | None:
    """
    尝试把多种格式的价格字段转为“分”（整数）。
    支持：整数分、字符串分、浮点美元/加币等。
    """
    if v is None:
        return None
    try:
        # 已经是分
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(round(v * 100))
        s = str(v).strip().replace(",", "")
        if s.endswith("."):
            s = s[:-1]
        # $123.45 / 123.45 → 分
        if re.match(r"^\$?\d+(\.\d{1,2})?$", s):
            return int(round(float(s.strip("$")) * 100))
        # 纯数字（可能已经是分）
        if s.isdigit():
            return int(s)
    except Exception:
        return None
    return None


async def parse_product(page, url: str) -> dict:
    """
    解析商品页：
    返回 {
      url, handle, title, currency,
      variants: [{
        key, variant_id, color, size, available,
        inventory_qty (可选), price_cents
      }]
    }
    """
    await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT)
    await page.wait_for_timeout(400)
    title = normalize_space(await _safe_get_title(page, url))
    handle = get_handle_from_url(url)
    currency = None
    variants = []

    # 读取页面 currency
    try:
        currency = await page.evaluate("""() => {
            const m = document.querySelector('meta[property="og:price:currency"], meta[itemprop="priceCurrency"]');
            if (m && m.content) return m.content.trim().toUpperCase();
            const c = (window.Shopify && Shopify.currency && Shopify.currency.active) || '';
            return (c || '').toUpperCase();
        }""")
    except Exception:
        currency = None

    # 优先从 JSON 脚本中读变体
    scripts = await page.locator('script[type="application/json"]').all()
    for s in scripts:
        txt = await s.inner_text()
        if not txt:
            continue
        if re.search(r'"variants?"\s*:', txt) or re.search(r'"options?"\s*:', txt):
            try:
                data = json.loads(txt)
                cand = []
                # 常见结构尝试
                if isinstance(data, dict):
                    if "variants" in data and isinstance(data["variants"], list):
                        cand = data["variants"]
                    else:
                        for _, v in data.items():
                            if isinstance(v, dict) and isinstance(v.get("variants"), list):
                                cand = v["variants"]; break
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and isinstance(item.get("variants"), list):
                            cand = item["variants"]; break

                for v in cand or []:
                    vid = v.get("id") or v.get("variant_id") or v.get("legacyResourceId")
                    size = v.get("option1") or v.get("size") or ""
                    color = v.get("option2") or v.get("color") or ""
                    if not color and isinstance(v.get("options"), list):
                        opts = v["options"]
                        if len(opts) >= 2:
                            color, size = opts[0], opts[1]
                        elif len(opts) == 1:
                            size = opts[0]
                    available = bool(v.get("available", v.get("is_in_stock", False)))
                    inv_qty = v.get("inventory_quantity")
                    price_cents = (
                        _parse_price_like(v.get("price")) or
                        _parse_price_like(v.get("final_price")) or
                        _parse_price_like(v.get("price_cents"))
                    )
                    variants.append({
                        "key": str(vid) if vid else f"{title}|{color}|{size}",
                        "variant_id": str(vid) if vid else "",
                        "color": normalize_space(str(color)),
                        "size": normalize_space(str(size)),
                        "available": available,
                        "inventory_qty": int(inv_qty) if isinstance(inv_qty, int) else None,
                        "price_cents": price_cents
                    })
            except Exception:
                pass

    # 回退：按钮/单选推断（没有价格与数量，只能拿到 availability）
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
                key = f"{title}|{color}|{size}"
                variants.append({
                    "key": key,
                    "variant_id": "",
                    "color": color,
                    "size": size,
                    "available": ok,
                    "inventory_qty": None,
                    "price_cents": None
                })

    return {
        "url": url,
        "handle": handle,
        "title": title,
        "currency": (currency or "").upper() or "USD",
        "variants": variants
    }


def to_variant_key(entry: dict) -> str:
    """
    尽量使用 variant_id 作为唯一键；没有则回退 title|color|size
    """
    if entry.get("variant_id"):
        return f"vid:{entry['variant_id']}"
    return f"name:{entry.get('title','')}|{entry.get('color','')}|{entry.get('size','')}"


def build_snapshot(products: dict[str, str], variants_map: dict[str, dict]) -> dict:
    return {
        "version": 2,
        "products": products,   # handle -> title
        "variants": variants_map
    }


def read_snapshot() -> dict:
    if not SNAPSHOT.exists():
        return build_snapshot({}, {})
    try:
        data = json.loads(SNAPSHOT.read_text("utf-8"))
        # 兼容旧版（只有 variants 的纯 dict）
        if isinstance(data, dict) and "variants" not in data:
            return build_snapshot({}, data)
        if isinstance(data, dict):
            data.setdefault("products", {})
            data.setdefault("variants", {})
            return data
    except Exception:
        pass
    return build_snapshot({}, {})


def diff_events(old_snap: dict, new_snap: dict, currency: str):
    """
    生成事件：
    - NEW_PRODUCT: 新上架商品（按 handle）
    - NEW_VARIANT: 新出现的变体（按 variant key）
    - PRICE_CHANGE: 价格变动（同一变体）
    - INVENTORY_INCREASE: 库存增加（优先依据 inventory_qty；否则基于可购变体数增加；或从缺货->有货）
    """
    events = []

    old_products = old_snap.get("products", {})
    new_products = new_snap.get("products", {})
    old_vars = old_snap.get("variants", {})
    new_vars = new_snap.get("variants", {})

    # 上新：按 handle 识别
    old_handles = set(old_products.keys())
    new_handles = set(new_products.keys())
    for h in sorted(new_handles - old_handles):
        events.append({
            "type": "NEW_PRODUCT",
            "handle": h,
            "title": new_products[h],
        })

    # 逐变体对比
    for k, v in new_vars.items():
        nv = v
        ov = old_vars.get(k)

        if ov is None:
            events.append({
                "type": "NEW_VARIANT",
                "key": k,
                "title": nv.get("title"),
                "color": nv.get("color"),
                "size": nv.get("size"),
                "url": nv.get("url")
            })
            continue

        # 价格变化
        np, op = nv.get("price_cents"), ov.get("price_cents")
        if np is not None and op is not None and np != op:
            events.append({
                "type": "PRICE_CHANGE",
                "key": k,
                "title": nv.get("title"),
                "color": nv.get("color"),
                "size": nv.get("size"),
                "old_price": op,
                "new_price": np,
                "currency": currency,
                "url": nv.get("url")
            })

        # 库存增加：优先有数量时比较
        n_q, o_q = nv.get("inventory_qty"), ov.get("inventory_qty")
        if isinstance(n_q, int) and isinstance(o_q, int) and n_q > o_q:
            events.append({
                "type": "INVENTORY_INCREASE",
                "key": k,
                "title": nv.get("title"),
                "color": nv.get("color"),
                "size": nv.get("size"),
                "old_qty": o_q,
                "new_qty": n_q,
                "url": nv.get("url")
            })
        else:
            # 没有具体数量：从缺货->有货 也视为库存增加（补货）
            oa, na = ov.get("available"), nv.get("available")
            if oa is False and na is True:
                events.append({
                    "type": "INVENTORY_INCREASE",
                    "key": k,
                    "title": nv.get("title"),
                    "color": nv.get("color"),
                    "size": nv.get("size"),
                    "old_qty": None,
                    "new_qty": None,
                    "url": nv.get("url")
                })

    # 没有 per-variant 数量时，额外按“可购变体数增加”作为信号（产品维度）
    # 统计每个 handle 下可购变体数量
    def avail_count_per_handle(variants: dict[str, dict]) -> dict[str, int]:
        cnt = {}
        for v in variants.values():
            h = v.get("handle")
            if not h:
                continue
            if v.get("available") is True:
                cnt[h] = cnt.get(h, 0) + 1
        return cnt

    old_cnt = avail_count_per_handle(old_vars)
    new_cnt = avail_count_per_handle(new_vars)
    for h, nc in new_cnt.items():
        oc = old_cnt.get(h, 0)
        if nc > oc:
            events.append({
                "type": "INVENTORY_INCREASE_PRODUCT",
                "handle": h,
                "title": new_products.get(h, h),
                "old_count": oc,
                "new_count": nc,
            })

    return events


async def send_discord_embeds(payload_embeds: list[dict]):
    if not DISCORD_WEBHOOK:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过通知。")
        return
    if not payload_embeds:
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"embeds": payload_embeds}, timeout=30) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print("Discord 推送失败:", resp.status, text)


async def send_text(msg: str):
    if not DISCORD_WEBHOOK:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过通知。")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=30) as resp:
            if resp.status >= 300:
                text = await resp.text()
                print("Discord 文本推送失败:", resp.status, text)


def events_to_embeds(events: list[dict], currency: str) -> list[dict]:
    embeds = []
    for e in events[:10]:  # 每批最多 10 条，避免刷屏
        t = e["type"]
        if t == "NEW_PRODUCT":
            embeds.append({
                "title": f"🆕 上新 · {e['title']}",
                "url": f"{BASE}/products/{e['handle']}",
                "fields": [
                    {"name": "商品", "value": e["title"], "inline": False},
                    {"name": "Handle", "value": e["handle"], "inline": True},
                ]
            })
        elif t == "NEW_VARIANT":
            embeds.append({
                "title": f"🆕 新变体 · {e['title']}",
                "url": e.get("url"),
                "fields": [
                    {"name": "颜色", "value": e.get("color") or "-", "inline": True},
                    {"name": "尺码", "value": e.get("size") or "-", "inline": True},
                ]
            })
        elif t == "PRICE_CHANGE":
            embeds.append({
                "title": f"💲 价格变化 · {e['title']}",
                "url": e.get("url"),
                "fields": [
                    {"name": "颜色", "value": e.get("color") or "-", "inline": True},
                    {"name": "尺码", "value": e.get("size") or "-", "inline": True},
                    {"name": "旧价", "value": cents_to_str(e.get("old_price"), currency), "inline": True},
                    {"name": "新价", "value": cents_to_str(e.get("new_price"), currency), "inline": True},
                ]
            })
        elif t == "INVENTORY_INCREASE":
            embeds.append({
                "title": f"🟢 库存增加 · {e['title']}",
                "url": e.get("url"),
                "fields": [
                    {"name": "颜色", "value": e.get("color") or "-", "inline": True},
                    {"name": "尺码", "value": e.get("size") or "-", "inline": True},
                    {"name": "变化", "value": "缺货 → 有货" if e.get("old_qty") is None else f"{e['old_qty']} → {e['new_qty']}", "inline": False},
                ]
            })
        elif t == "INVENTORY_INCREASE_PRODUCT":
            embeds.append({
                "title": f"🟢 可购变体数增加 · {e['title']}",
                "url": f"{BASE}/products/{e['handle']}",
                "fields": [
                    {"name": "可购变体数", "value": f"{e['old_count']} → {e['new_count']}", "inline": True}
                ]
            })
    return embeds


async def parse_with_retry(page, url: str, tries=3):
    for t in range(1, tries + 1):
        start = time.time()
        try:
            res = await parse_product(page, url)
            dur = int((time.time() - start) * 1000)
            print(f"  ✅ 成功: {url} ({dur} ms)")
            return res
        except Exception as e:
            dur = int((time.time() - start) * 1000)
            print(f"  ⚠️ 第 {t} 次失败 ({dur} ms): {url} -> {e}")
            if t == tries:
                raise
            await asyncio.sleep(1.5 * t)


async def run_once():
    if not DISCORD_WEBHOOK:
        print("WARN: 环境变量 DISCORD_WEBHOOK_URL 为空；将无法发送 Discord 通知。")

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

        # 拦截重资源与跟踪脚本，提速稳态
        async def _route_filter(route):
            r = route.request
            rt = r.resource_type
            url = r.url
            if rt in ("image", "media", "font"):
                return await route.abort()
            blocked = ("googletagmanager.com", "google-analytics.com", "doubleclick.net",
                       "facebook.net", "hotjar.com")
            if any(b in url for b in blocked):
                return await route.abort()
            return await route.continue_()

        await context.route("**/*", _route_filter)

        page = await context.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)

        print("抓取品牌集合页商品链接…")
        urls = await get_all_product_urls(page)
        print(f"共发现 {len(urls)} 个商品链接")

        is_first_run = not SNAPSHOT.exists()

        # 载入旧快照
        old_snap = read_snapshot()

        # 解析所有商品
        new_products: dict[str, str] = {}     # handle -> title
        new_variants: dict[str, dict] = {}    # variant key -> entry
        currency_seen = None

        for i, u in enumerate(urls, 1):
            try:
                prod = await parse_with_retry(page, u, tries=3)
            except Exception as e:
                print(f"[{i}/{len(urls)}] 解析失败: {u} -> {e}")
                continue

            currency_seen = currency_seen or prod.get("currency") or "USD"
            handle = prod["handle"]
            title = prod["title"] or handle
            new_products[handle] = title

            for v in prod["variants"] or []:
                entry = {
                    "handle": handle,
                    "title": title,
                    "color": v.get("color", ""),
                    "size": v.get("size", ""),
                    "available": bool(v.get("available")),
                    "price_cents": v.get("price_cents"),
                    "inventory_qty": v.get("inventory_qty"),
                    "variant_id": v.get("variant_id"),
                    "url": u,
                }
                k = to_variant_key(entry)
                new_variants[k] = entry

            print(f"[{i}/{len(urls)}] {title} - {len(prod['variants'])} 个变体")
            if i % 8 == 0:
                await asyncio.sleep(1.2 + random.random())
            if i % 50 == 0:
                Path("new_map_partial.json").write_text(
                    json.dumps({"products": new_products, "variants": new_variants}, ensure_ascii=False, indent=2),
                    "utf-8"
                )

        # 生成新快照
        new_snap = build_snapshot(new_products, new_variants)

        # 计算事件
        events = diff_events(old_snap, new_snap, currency_seen or "USD")
        print(f"事件条目：{len(events)}")

        # 写入新快照
        SNAPSHOT.write_text(json.dumps(new_snap, ensure_ascii=False, indent=2), "utf-8")

        # 通知逻辑
        notify_on_no_change = os.environ.get("NOTIFY_ON_NO_CHANGE", "").lower() == "true"
        if is_first_run:
            await send_text(
                f"✅ 初始化完成：已建立监控基线。\n"
                f"商品数：{len(new_products)}，变体数：{len(new_variants)}。\n"
                f"后续监控：上新 / 价格变化 / 库存增加。"
            )
        elif events:
            embeds = events_to_embeds(events, currency_seen or "USD")
            await send_discord_embeds(embeds)
        elif notify_on_no_change:
            await send_text("运行成功：本次无上新、无价格变化、无库存增加。")

        await browser.close()


# 支持单页调试：在命令行设置 DEBUG_ONE_URL="https://enroute.run/products/xxx"
if __name__ == "__main__":
    DEBUG_ONE_URL = os.environ.get("DEBUG_ONE_URL", "").strip()
    if DEBUG_ONE_URL:
        async def _single():
            if not DISCORD_WEBHOOK:
                print("WARN: 环境变量 DISCORD_WEBHOOK_URL 为空；将无法发送 Discord 通知。")
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    user_agent=USER_AGENT, viewport={"width": 1400, "height": 1000}, locale="en-US"
                )
                async def _route_filter(route):
                    r = route.request
                    rt = r.resource_type
                    url = r.url
                    if rt in ("image", "media", "font"):
                        return await route.abort()
                    blocked = ("googletagmanager.com", "google-analytics.com", "doubleclick.net",
                               "facebook.net", "hotjar.com")
                    if any(b in url for b in blocked):
                        return await route.abort()
                    return await route.continue_()
                await ctx.route("**/*", _route_filter)

                p = await ctx.new_page()
                p.set_default_timeout(60000)
                p.set_default_navigation_timeout(60000)
                prod = await parse_with_retry(p, DEBUG_ONE_URL, tries=3)
                print(json.dumps(prod, ensure_ascii=False, indent=2))
                await browser.close()
        asyncio.run(_single())
    else:
        asyncio.run(run_once())
