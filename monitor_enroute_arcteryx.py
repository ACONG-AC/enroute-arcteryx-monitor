import asyncio
import json
import os
import re
import random
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================== 配置 ==================
BASE = "https://enroute.run"
COLLECTION = "https://enroute.run/collections/arcteryx"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

SNAPSHOT = Path("snapshot.json")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
NOTIFY_ON_NO_CHANGE = os.environ.get("NOTIFY_ON_NO_CHANGE", "false").lower() in ("1","true","yes","on")

REQUEST_TIMEOUT = 20000   # 单次 HTTP 超时(ms)
MAX_PAGES = 20
SCROLL_PAUSE = 700
MAX_CONCURRENCY = 8
HTTP_RETRIES = 3
TRY_VARIANT_QTY = True    # 尝试 /variants/<id>.json
# =================================================

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def cents_to_str(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "-"
    cur = (currency or "USD").upper()
    sym = "$" if cur in ("USD","CAD","AUD","NZD","SGD") else f"{cur} "
    return f"{sym}{cents/100:.2f}"

def get_handle_from_url(url: str) -> str:
    path = urlparse(url).path.split("/")
    try:
        i = path.index("products")
        return path[i+1] if len(path) > i+1 else ""
    except ValueError:
        return ""

def parse_price_to_cents(v) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, int): return v
        if isinstance(v, float): return int(round(v*100))
        s = str(v).strip().replace(",", "").replace("$", "")
        if re.match(r"^\d+(\.\d{1,2})?$", s): return int(round(float(s)*100))
        if s.isdigit(): return int(s)
    except Exception:
        return None
    return None

# ----------------- HTTP 客户端 -----------------
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": COLLECTION,
}

class HttpClient:
    def __init__(self, timeout_ms=REQUEST_TIMEOUT):
        self.timeout = aiohttp.ClientTimeout(total=timeout_ms/1000)

    async def get_text(self, session: aiohttp.ClientSession, url: str, retries=HTTP_RETRIES):
        last_err = None
        for i in range(1, retries+1):
            try:
                async with session.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout) as r:
                    if r.status == 200:
                        return await r.text()
                    elif r.status in (403,404):
                        return None
                    last_err = f"HTTP {r.status}"
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(0.4*i)
        if last_err:
            print(f"GET {url} text failed: {last_err}")
        return None

    async def get_json(self, session: aiohttp.ClientSession, url: str, retries=HTTP_RETRIES):
        last_err = None
        for i in range(1, retries+1):
            try:
                async with session.get(url, headers=DEFAULT_HEADERS|{"Accept":"application/json"}, timeout=self.timeout) as r:
                    if r.status == 200:
                        return await r.json()
                    elif r.status in (403,404):
                        return None
                    last_err = f"HTTP {r.status}"
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(0.4*i)
        if last_err:
            print(f"GET {url} json failed: {last_err}")
        return None

http = HttpClient()

# ----------------- 集合页：handles 获取（Playwright + HTTP 回退） -----------------
async def get_handles_via_playwright() -> list[str]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=USER_AGENT, viewport={"width":1400,"height":1000}, locale="en-US")

        async def _route_filter(route):
            rt = route.request.resource_type
            if rt in ("image","media","font"): return await route.abort()
            return await route.continue_()
        await ctx.route("**/*", _route_filter)

        page = await ctx.new_page()
        handles = set()

        def norm_path(href: str) -> str:
            parts = href.split("?")[0].split("/")
            if len(parts)>=3 and parts[1]=="products":
                return "/".join(parts[:3])
            return href.split("?")[0]

        async def collect():
            cards = await page.locator('a[href^="/products/"]').all()
            for a in cards:
                href = await a.get_attribute("href")
                if href and href.startswith("/products/"):
                    h = get_handle_from_url(norm_path(href))
                    if h: handles.add(h)

        try:
            await page.goto(COLLECTION, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            await page.goto(COLLECTION, wait_until="commit")

        last_h = 0
        for _ in range(20):  # 增加滚动轮数
            await collect()
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(SCROLL_PAUSE/1000)
            h = await page.evaluate("document.body.scrollHeight")
            if h == last_h: break
            last_h = h

        # 兜底分页
        for p in range(2, MAX_PAGES+1):
            try:
                resp = await page.goto(f"{COLLECTION}?page={p}", wait_until="domcontentloaded", timeout=25000)
                if not resp or resp.status != 200: break
            except PWTimeout:
                break
            before = len(handles)
            await collect()
            if len(handles) == before: break

        await browser.close()
        return sorted(handles)

async def get_handles_via_http(session: aiohttp.ClientSession) -> list[str]:
    html = await http.get_text(session, COLLECTION)
    if not html: return []
    # 抓 a 链接中的 /products/<handle>
    handles = set(re.findall(r'href="/products/([a-z0-9\-]+)"', html, flags=re.I))
    return sorted(handles)

async def get_all_product_handles() -> list[str]:
    # 先 HTTP，抓不到再用 Playwright
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT/1000)) as s:
        handles = await get_handles_via_http(s)
    if len(handles) >= 10:
        return handles
    # 回退 Playwright
    return await get_handles_via_playwright()

# ----------------- 产品详情抓取：三段式回退 -----------------
async def fetch_product_via_js(handle: str, session: aiohttp.ClientSession):
    return await http.get_json(session, f"{BASE}/products/{handle}.js")

async def fetch_product_via_json(handle: str, session: aiohttp.ClientSession):
    # 少数站点提供 .json（不是通用，但可一试）
    data = await http.get_json(session, f"{BASE}/products/{handle}.json")
    if isinstance(data, dict):
        # 有的结构是 {"product": {...}}
        return data.get("product") or data
    return None

def extract_variants_from_html(html: str):
    """
    从 HTML 中提取 variants 数组（常见于主题内嵌的 JSON）。
    尝试顺序：
      1) script[type=application/ld+json] 的 Product/Offer（价格可得、变体可能缺少）
      2) 任意 <script> 文本里出现 "variants":[ {...} ] 的数组
    """
    # 2) 粗略抓取 "variants": [...] 数组（Shopify 常见）
    m = re.search(r'"variants"\s*:\s*(\[\s*\{.*?\}\s*\])', html, flags=re.S|re.I)
    if m:
        try:
            arr = json.loads(m.group(1))
            return arr
        except Exception:
            pass
    # 1) 从 ld+json 抓基本价格/可售（不一定有每个变体）
    ld_blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, flags=re.S|re.I)
    for block in ld_blocks:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        obj = None
        if isinstance(data, dict):
            obj = data
        elif isinstance(data, list) and data:
            obj = data[0]
        if not isinstance(obj, dict): continue
        # Offers 里可能有价格与可售
        offers = obj.get("offers")
        title = normalize_space(obj.get("name") or "")
        var_list = []
        if isinstance(offers, list):
            for off in offers:
                price = parse_price_to_cents(off.get("price"))
                available = str(off.get("availability","")).lower().endswith("instock")
                var_list.append({
                    "id": None, "option1": "", "option2": "",
                    "price": price, "available": available
                })
        elif isinstance(offers, dict):
            price = parse_price_to_cents(offers.get("price"))
            available = str(offers.get("availability","")).lower().endswith("instock")
            var_list.append({
                "id": None, "option1": "", "option2": "",
                "price": price, "available": available
            })
        if var_list:
            return var_list
    return None

async def fetch_product_via_html(handle: str, session: aiohttp.ClientSession):
    html = await http.get_text(session, f"{BASE}/products/{handle}")
    if not html: return None
    title = None
    m = re.search(r"<title>(.*?)</title>", html, flags=re.S|re.I)
    if m:
        title = normalize_space(re.sub(r"-\s*Enroute.*$","", m.group(1)))
    variants_arr = extract_variants_from_html(html) or []
    product = {
        "title": title or handle.replace("-", " "),
        "variants": variants_arr
    }
    return product

async def fetch_product(handle: str, session: aiohttp.ClientSession):
    """
    三段式回退：
      1) /products/<handle>.js
      2) /products/<handle>.json
      3) 抓 HTML 提取内嵌 JSON
    并尽量补齐 color/size/price/available/inventory_qty
    """
    # 1) .js
    data = await fetch_product_via_js(handle, session)
    source = "js"
    if not data:
        # 2) .json
        data = await fetch_product_via_json(handle, session)
        source = "json"
    if not data:
        # 3) HTML
        data = await fetch_product_via_html(handle, session)
        source = "html"
    if not data:
        return None

    title = normalize_space((data.get("title") if isinstance(data, dict) else None) or handle.replace("-", " "))
    variants = []
    for v in (data.get("variants") or []):
        vid = v.get("id")
        price_cents = parse_price_to_cents(v.get("price"))
        available = bool(v.get("available", False))
        # 尝试 option1/option2 / options
        color = v.get("option2") or ""
        size  = v.get("option1") or ""
        if not color and isinstance(v.get("options"), list) and len(v["options"])>=2:
            color, size = v["options"][0], v["options"][1]

        inv_qty = None
        if TRY_VARIANT_QTY and vid and source in ("js","json"):
            vi = await http.get_json(session, f"{BASE}/variants/{vid}.json")
            if vi and isinstance(vi.get("variant"), dict):
                q = vi["variant"].get("inventory_quantity")
                if isinstance(q, int): inv_qty = q

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
        "title": title or handle.replace("-", " "),
        "currency": "USD",
        "variants": variants,
        "url": f"{BASE}/products/{handle}",
    }

# ----------------- 快照 & Diff -----------------
def to_variant_key(entry: dict) -> str:
    if entry.get("variant_id"):
        return f"vid:{entry['variant_id']}"
    return f"name:{entry.get('title','')}|{entry.get('color','')}|{entry.get('size','')}"

def build_snapshot(products: dict[str, str], variants_map: dict[str, dict]) -> dict:
    return {"version": 2, "products": products, "variants": variants_map}

def read_snapshot() -> dict:
    if not SNAPSHOT.exists(): return build_snapshot({}, {})
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
    old_p, new_p = old_snap.get("products",{}), new_snap.get("products",{})
    old_v, new_v = old_snap.get("variants",{}), new_snap.get("variants",{})

    # NEW_PRODUCT
    for h in sorted(set(new_p) - set(old_p)):
        events.append({"type":"NEW_PRODUCT","handle":h,"title":new_p[h]})

    # 变体对比
    for k, nv in new_v.items():
        ov = old_v.get(k)
        if ov is None:
            events.append({"type":"NEW_VARIANT","key":k,"title":nv.get("title"),
                           "color":nv.get("color"),"size":nv.get("size"),"url":nv.get("url")})
            continue
        np, op = nv.get("price_cents"), ov.get("price_cents")
        if np is not None and op is not None and np != op:
            events.append({"type":"PRICE_CHANGE","key":k,"title":nv.get("title"),
                           "color":nv.get("color"),"size":nv.get("size"),
                           "old_price":op,"new_price":np,"currency":currency,"url":nv.get("url")})
        n_q, o_q = nv.get("inventory_qty"), ov.get("inventory_qty")
        if isinstance(n_q,int) and isinstance(o_q,int) and n_q>o_q:
            events.append({"type":"INVENTORY_INCREASE","key":k,"title":nv.get("title"),
                           "color":nv.get("color"),"size":nv.get("size"),
                           "old_qty":o_q,"new_qty":n_q,"url":nv.get("url")})
        else:
            if ov.get("available") is False and nv.get("available") is True:
                events.append({"type":"INVENTORY_INCREASE","key":k,"title":nv.get("title"),
                               "color":nv.get("color"),"size":nv.get("size"),
                               "old_qty":None,"new_qty":None,"url":nv.get("url")})

    # 产品维度：可购变体数增加
    def avail_count(variants: dict[str,dict]) -> dict[str,int]:
        cnt={}
        for v in variants.values():
            h=v.get("handle")
            if h and v.get("available") is True:
                cnt[h]=cnt.get(h,0)+1
        return cnt
    oc, nc = avail_count(old_v), avail_count(new_v)
    for h, val in nc.items():
        if val > oc.get(h,0):
            events.append({"type":"INVENTORY_INCREASE_PRODUCT","handle":h,"title":new_p.get(h,h),
                           "old_count":oc.get(h,0),"new_count":val})
    return events

def events_to_embeds(events: list[dict], currency: str) -> list[dict]:
    embeds=[]
    for e in events[:12]:
        t=e["type"]
        if t=="NEW_PRODUCT":
            embeds.append({"title":f"🆕 上新 · {e['title']}",
                           "url":f"{BASE}/products/{e['handle']}",
                           "fields":[{"name":"商品","value":e["title"],"inline":False},
                                     {"name":"Handle","value":e["handle"],"inline":True}]})
        elif t=="NEW_VARIANT":
            embeds.append({"title":f"🆕 新变体 · {e['title']}",
                           "url":e.get("url"),
                           "fields":[{"name":"颜色","value":e.get("color") or "-","inline":True},
                                     {"name":"尺码","value":e.get("size") or "-","inline":True}]})
        elif t=="PRICE_CHANGE":
            embeds.append({"title":f"💲 价格变化 · {e['title']}",
                           "url":e.get("url"),
                           "fields":[{"name":"颜色","value":e.get("color") or "-","inline":True},
                                     {"name":"尺码","value":e.get("size") or "-","inline":True},
                                     {"name":"旧价","value":cents_to_str(e.get("old_price"), currency),"inline":True},
                                     {"name":"新价","value":cents_to_str(e.get("new_price"), currency),"inline":True}]})
        elif t=="INVENTORY_INCREASE":
            embeds.append({"title":f"🟢 库存增加 · {e['title']}",
                           "url":e.get("url"),
                           "fields":[{"name":"颜色","value":e.get("color") or "-","inline":True},
                                     {"name":"尺码","value":e.get("size") or "-","inline":True},
                                     {"name":"变化","value":"缺货 → 有货" if e.get("old_qty") is None else f"{e['old_qty']} → {e['new_qty']}",
                                      "inline":False}]})
        elif t=="INVENTORY_INCREASE_PRODUCT":
            embeds.append({"title":f"🟢 可购变体数增加 · {e['title']}",
                           "url":f"{BASE}/products/{e['handle']}",
                           "fields":[{"name":"可购变体数","value":f"{e['old_count']} → {e['new_count']}",
                                      "inline":True}]})
    return embeds

async def send_discord_embeds(embeds: list[dict]):
    if not DISCORD_WEBHOOK: return
    if not embeds: return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"embeds": embeds}, timeout=30) as resp:
            body = await resp.text()
            print(f"Discord embeds status={resp.status}, len={len(embeds)}")
            if resp.status >= 300:
                print("Discord 推送失败:", resp.status, body)

async def send_text(msg: str):
    if not DISCORD_WEBHOOK: return
    async with aiohttp.ClientSession() as session:
        async with session.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=30) as resp:
            body = await resp.text()
            print(f"Discord text status={resp.status}")
            if resp.status >= 300:
                print("Discord 文本推送失败:", resp.status, body)

# ----------------- 主流程 -----------------
async def run_once():
    print("收集商品 handle ...")
    handles = await get_all_product_handles()
    print(f"共发现 {len(handles)} 个商品 handle")

    is_first_run = not SNAPSHOT.exists()
    old_snap = read_snapshot()

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    products: dict[str, dict] = {}

    async def worker(handle: str, session: aiohttp.ClientSession):
        async with semaphore:
            for t in range(1, HTTP_RETRIES+1):
                try:
                    prod = await fetch_product(handle, session)
                    if prod: products[handle] = prod
                    return
                except Exception as e:
                    if t == HTTP_RETRIES: print(f"产品抓取失败: {handle} -> {e}")
                    await asyncio.sleep(0.6*t + random.random()*0.3)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT/1000)) as session:
        tasks = [asyncio.create_task(worker(h, session)) for h in handles]
        done = 0
        for f in asyncio.as_completed(tasks):
            await f
            done += 1
            if done % 20 == 0:
                print(f"已完成 {done}/{len(tasks)}")

    # 统计成功条目
    succ_products = len(products)
    succ_variants = sum(len(p.get("variants") or []) for p in products.values())
    print(f"成功解析：商品 {succ_products} 个，变体 {succ_variants} 条")

    # 构建新快照
    new_products: dict[str,str] = {}
    new_variants: dict[str,dict] = {}
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
                "color": v.get("color",""),
                "size": v.get("size",""),
                "available": bool(v.get("available")),
                "price_cents": v.get("price_cents"),
                "inventory_qty": v.get("inventory_qty"),
                "variant_id": v.get("variant_id"),
                "url": url,
            }
            k = to_variant_key(entry)
            new_variants[k] = entry

    new_snap = build_snapshot(new_products, new_variants)

    events = diff_events(old_snap, new_snap, currency_seen)
    print(f"事件条目：{len(events)}")
    SNAPSHOT.write_text(json.dumps(new_snap, ensure_ascii=False, indent=2), "utf-8")

    # 按你的要求：不发送“初始化”也不发送“无变更”
    if events:
        embeds = events_to_embeds(events, currency_seen)
        await send_discord_embeds(embeds)
    elif NOTIFY_ON_NO_CHANGE:
        await send_text("运行成功：本次无上新、无价格变化、无库存增加。")

# 支持单品调试：DEBUG_ONE_HANDLE=arcteryx-mantis-2-waist-pack
if __name__ == "__main__":
    dbg = os.environ.get("DEBUG_ONE_HANDLE","").strip()
    if dbg:
        async def _single():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT/1000)) as s:
                prod = await fetch_product(dbg, s)
                print(json.dumps(prod, ensure_ascii=False, indent=2))
        asyncio.run(_single())
    else:
        asyncio.run(run_once())
