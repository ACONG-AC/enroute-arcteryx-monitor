import asyncio
import json
import os
import re
import time
import random
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================== 可调参数 ==================
BASE = "https://enroute.run"
COLLECTION = "https://enroute.run/collections/arcteryx"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
SNAPSHOT = Path("snapshot.json")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 抓取参数
REQUEST_TIMEOUT = 20000  # 单次 HTTP 请求超时（毫秒）
MAX_PAGES = 20
SCROLL_PAUSE = 700
MAX_CONCURRENCY = 8      # 并发抓取产品 JSON 的并发度
HTTP_RETRIES = 3

# 功能开关
TRY_VARIANT_QTY = True   # 尝试 GET /variants/<id>.json 获取库存数量（若被禁会自动略过）

# =================================================

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def cents_to_str(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "-"
    cur = (currency or "USD").upper()
    sym = "$" if cur in ("USD", "CAD", "AUD", "NZD", "SGD") else f"{cur} "
    return f"{sym}{cents/100:.2f}"

def get_handle_from_url(url: str) -> str:
    path = urlparse(url).path.split("/")
    try:
        i = path.index("products")
        handle = path[i + 1] if len(path) > i + 1 else ""
    except ValueError:
        handle = ""
    return handle

async def get_all_product_handles() -> list[str]:
    """
    用 Playwright 打开集合页，仅提取 /products/<handle> 列表（去重）。
    不再逐个打开产品详情页，稳定且快速。
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1400, "height": 1000}, locale="en-US"
        )

        # 轻拦截资源，提速
        async def _route_filter(route):
            r = route.request
            rt = r.resource_type
            if rt in ("image", "media", "font"):
                return await route.abort()
            return await route.continue_()
        await ctx.route("**/*", _route_filter)

        page = await ctx.new_page()
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
                    handle = get_handle_from_url(norm)
                    if handle:
                        urls.add(handle)

        try:
            await page.goto(COLLECTION, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            await page.goto(COLLECTION, wait_until="commit")

        # 无限滚动
        last_height = 0
        for _ in range(10):
            await collect_from_current()
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(SCROLL_PAUSE/1000)
            height = await page.evaluate("document.body.scrollHeight")
            if height == last_height:
                break
            last_height = height

        # 兜底分页 ?page=2...
        for p in range(2, MAX_PAGES + 1):
            url = f"{COLLECTION}?page={p}"
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if not resp or resp.status != 200:
                    break
            except PWTimeout:
                break
            before = len(urls)
            await collect_from_current()
            if len(urls) == before:
                break

        await browser.close()
        return sorted(urls)

# ---------- HTTP 抓取（无需渲染） ----------

class HttpClient:
    def __init__(self, timeout_ms: int = REQUEST_TIMEOUT):
        self.timeout = aiohttp.ClientTimeout(total=timeout_ms/1000)

    async def get_json(self, session: aiohttp.ClientSession, url: str, retries: int = HTTP_RETRIES):
        last_err = None
        for i in range(1, retries+1):
            try:
                async with session.get(url, timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as r:
                    if r.status == 200:
                        return await r.json()
                    elif r.status in (403, 404):
                        # 明确禁止/不存在就别再试
                        return None
                    else:
                        last_err = f"HTTP {r.status}"
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(0.5 * i)
        if last_err:
            print(f"GET {url} failed after {retries} tries: {last_err}")
        return None

http = HttpClient()

async def fetch_product(handle: str, session: aiohttp.ClientSession):
    """
    直接请求 Shopify JSON：
    - /products/<handle>.js  拿到 variants（含 id / available / price）
    - 可选每个 variant 再试 /variants/<id>.json 拿 inventory_quantity
    """
    prod_url = f"{BASE}/products/{handle}.js"
    data = await http.get_json(session, prod_url)
    if not data:
        return None

    title = normalize_space(data.get("title") or handle.replace("-", " "))
    currency = "USD"  # 一些店不会给 currency，这里默认 USD（可从 theme 获取但没必要）

    variants = []
    for v in data.get("variants", []) or []:
        vid = v.get("id")
        available = bool(v.get("available", False))
        # price 可能为分或字符串金额；标准化为分
        price_cents = parse_price_to_cents(v.get("price"))
        # 选项
        color = v.get("option2") or ""
        size  = v.get("option1") or ""
        # 一些商店选项顺序不同，做个保险
        if not color and isinstance(v.get("options"), list) and len(v["options"]) >= 2:
            color, size = v["options"][0], v["options"][1]

        inv_qty = None
        if TRY_VARIANT_QTY and vid:
            # 尝试拿具体库存数量（很多店开放，少数店禁用则返回 403/404）
            vi = await http.get_json(session, f"{BASE}/variants/{vid}.json")
            if vi and isinstance(vi.get("variant"), dict):
                q = vi["variant"].get("inventory_quantity")
                if isinstance(q, int):
                    inv_qty = q

        variants.append({
            "variant_id": str(vid) if vid else "",
            "color": normalize_space(str(color)),
            "size": normalize_space(str(size)),
            "available": available,
            "price_cents": price_cents,
            "inventory_qty": inv_qty
        })

    return {
        "handle": handle,
        "title": title,
        "currency": currency,
        "variants": variants,
        "url": f"{BASE}/products/{handle}",
    }

def parse_price_to_cents(v) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, int):
            # 很多 .js 里就是分
            return v
        if isinstance(v, float):
            return int(round(v * 100))
        s = str(v).strip().replace(",", "").replace("$", "")
        if re.match(r"^\d+(\.\d{1,2})?$", s):
            return int(round(float(s) * 100))
        if s.isdigit():
            return int(s)
    except Exception:
        return None
    return None

def to_variant_key(entry: dict) -> str:
    if entry.get("variant_id"):
        return f"vid:{entry['variant_id']}"
    return f"name:{entry.get('title','')}|{entry.get('color','')}|{entry.get('size','')}"

def build_snapshot(products: dict[str, str], variants_map: dict[str, dict]) -> dict:
    return {"version": 2, "products": products, "variants": variants_map}

def read_snapshot() -> dict:
    if not SNAPSHOT.exists():
        return build_snapshot({}, {})
    try:
        data = json.loads(SNAPSHOT.read_text("utf-8"))
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
    events = []

    old_products = old_snap.get("products", {})
    new_products = new_snap.get("products", {})
    old_vars = old_snap.get("variants", {})
    new_vars = new_snap.get("variants", {})

    # 上新（按 handle）
    for h in sorted(set(new_products) - set(old_products)):
        events.append({"type": "NEW_PRODUCT", "handle": h, "title": new_products[h]})

    # 变体对比：价格、库存增加、新变体
    for k, nv in new_vars.items():
        ov = old_vars.get(k)
        if ov is None:
            events.append({
                "type": "NEW_VARIANT",
                "key": k, "title": nv.get("title"), "color": nv.get("color"),
                "size": nv.get("size"), "url": nv.get("url")
            })
            continue
        # 价格变化
        np, op = nv.get("price_cents"), ov.get("price_cents")
        if np is not None and op is not None and np != op:
            events.append({
                "type": "PRICE_CHANGE",
                "key": k, "title": nv.get("title"), "color": nv.get("color"),
                "size": nv.get("size"), "old_price": op, "new_price": np,
                "currency": currency, "url": nv.get("url")
            })
        # 库存增加
        n_q, o_q = nv.get("inventory_qty"), ov.get("inventory_qty")
        if isinstance(n_q, int) and isinstance(o_q, int) and n_q > o_q:
            events.append({
                "type": "INVENTORY_INCREASE",
                "key": k, "title": nv.get("title"),
                "color": nv.get("color"), "size": nv.get("size"),
                "old_qty": o_q, "new_qty": n_q, "url": nv.get("url")
            })
        else:
            if ov.get("available") is False and nv.get("available") is True:
                events.append({
                    "type": "INVENTORY_INCREASE",
                    "key": k, "title": nv.get("title"),
                    "color": nv.get("color"), "size": nv.get("size"),
                    "old_qty": None, "new_qty": None, "url": nv.get("url")
                })

    # 产品维度：可购变体数增加（可选保留，通常很有用）
    def avail_count_per_handle(variants: dict[str, dict]) -> dict[str, int]:
        cnt = {}
        for v in variants.values():
            h = v.get("handle")
            if h and v.get("available") is True:
                cnt[h] = cnt.get(h, 0) + 1
        return cnt

    old_cnt = avail_count_per_handle(old_vars)
    new_cnt = avail_count_per_handle(new_vars)
    for h, nc in new_cnt.items():
        oc = old_cnt.get(h, 0)
        if nc > oc:
            events.append({
                "type": "INVENTORY_INCREASE_PRODUCT",
                "handle": h, "title": new_products.get(h, h),
                "old_count": oc, "new_count": nc
            })
    return events

async def send_discord_embeds(embeds: list[dict]):
    if not DISCORD_WEBHOOK:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过通知。")
        return
    if not embeds:
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"embeds": embeds}, timeout=30) as resp:
            if resp.status >= 300:
                print("Discord 推送失败:", resp.status, await resp.text())

async def send_text(msg: str):
    if not DISCORD_WEBHOOK:
        print("WARN: 未设置 DISCORD_WEBHOOK_URL，跳过通知。")
        return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=30) as resp:
            if resp.status >= 300:
                print("Discord 文本推送失败:", resp.status, await resp.text())

def events_to_embeds(events: list[dict], currency: str) -> list[dict]:
    embeds = []
    for e in events[:12]:  # 每批 12 条以内
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

async def run_once():
    if not DISCORD_WEBHOOK:
        print("WARN: 环境变量 DISCORD_WEBHOOK_URL 为空；将无法发送 Discord 通知。")

    print("收集商品 handle ...")
    handles = await get_all_product_handles()
    print(f"共发现 {len(handles)} 个商品 handle")

    is_first_run = not SNAPSHOT.exists()
    old_snap = read_snapshot()

    # 并发抓取商品 JSON
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    products: dict[str, dict] = {}

    async def worker(handle: str, session: aiohttp.ClientSession):
        async with semaphore:
            for t in range(1, HTTP_RETRIES+1):
                try:
                    prod = await fetch_product(handle, session)
                    if prod:
                        products[handle] = prod
                    return
                except Exception as e:
                    if t == HTTP_RETRIES:
                        print(f"产品抓取失败: {handle} -> {e}")
                    await asyncio.sleep(0.6 * t)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT/1000)) as session:
        tasks = [asyncio.create_task(worker(h, session)) for h in handles]
        # 可选：进度打印
        done = 0
        for f in asyncio.as_completed(tasks):
            await f
            done += 1
            if done % 20 == 0:
                print(f"已完成 {done}/{len(tasks)}")

    # 生成新快照
    new_products: dict[str, str] = {}
    new_variants: dict[str, dict] = {}
    currency_seen = "USD"

    for handle, prod in products.items():
        title = prod["title"]
        new_products[handle] = title
        currency_seen = prod.get("currency") or currency_seen
        url = prod.get("url")
        for v in prod.get("variants", []):
            entry = {
                "handle": handle,
                "title": title,
                "color": v.get("color", ""),
                "size": v.get("size", ""),
                "available": bool(v.get("available")),
                "price_cents": v.get("price_cents"),
                "inventory_qty": v.get("inventory_qty"),
                "variant_id": v.get("variant_id"),
                "url": url,
            }
            k = to_variant_key(entry)
            new_variants[k] = entry

    new_snap = build_snapshot(new_products, new_variants)

    # 计算事件 & 写入快照
    events = diff_events(old_snap, new_snap, currency_seen)
    print(f"事件条目：{len(events)}")
    SNAPSHOT.write_text(json.dumps(new_snap, ensure_ascii=False, indent=2), "utf-8")

    # 通知逻辑
    notify_on_no_change = os.environ.get("NOTIFY_ON_NO_CHANGE", "").lower() == "true"
    if is_first_run:
        await send_text(
            f"✅ 初始化完成：已建立监控基线。\n"
            f"商品数：{len(new_products)}，变体数：{len(new_variants)}。\n"
            f"监控范围：上新 / 价格变化 / 库存增加（含从缺货→有货）。"
        )
    elif events:
        embeds = events_to_embeds(events, currency_seen)
        await send_discord_embeds(embeds)
    elif notify_on_no_change:
        await send_text("运行成功：本次无上新、无价格变化、无库存增加。")

# 支持单品调试：DEBUG_ONE_HANDLE arcteryx-mantis-2-waist-pack
if __name__ == "__main__":
    debug_handle = os.environ.get("DEBUG_ONE_HANDLE", "").strip()
    if debug_handle:
        async def _single():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT/1000)) as session:
                prod = await fetch_product(debug_handle, session)
                print(json.dumps(prod, ensure_ascii=False, indent=2))
        asyncio.run(_single())
    else:
        asyncio.run(run_once())
