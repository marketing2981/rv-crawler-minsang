import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import time, random, io, re, json, requests, subprocess, sys, os
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse


# ─── Playwright 지연 로딩: Streamlit Cloud 등에서 Chromium 자동 설치 ─────────

_PLAYWRIGHT_READY = None  # None=미시도, True=사용 가능, False=불가


def _ensure_playwright():
    """Playwright + Chromium이 준비되어 있는지 확인. 없으면 설치 시도."""
    global _PLAYWRIGHT_READY
    if _PLAYWRIGHT_READY is not None:
        return _PLAYWRIGHT_READY
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _PLAYWRIGHT_READY = False
        return False
    # Chromium 실행 가능한지 한번 launch 해본다
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        _PLAYWRIGHT_READY = True
        return True
    except Exception:
        # 클라우드 첫 실행: 브라우저 다운로드 시도
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=False, capture_output=True, timeout=300,
            )
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                b.close()
            _PLAYWRIGHT_READY = True
            return True
        except Exception:
            _PLAYWRIGHT_READY = False
            return False


def sync_playwright():
    """필요 시 동적으로 Playwright import — Playwright 없는 환경에서도 import 단계 실패 안 함."""
    from playwright.sync_api import sync_playwright as _sp
    return _sp()

# ─── 패턴 ────────────────────────────────────────────────────────────────────

REVIEW_ITEM_PATTERNS = [
    "[data-review-no]",  # Cafe24 + SNAP리뷰 임베드
    "li.crema-review-item", ".vreview-container .review-item",
    ".xans-product-review li", ".xans-product-review tbody tr",
    ".js_pr_review_list li",
    "div.review-item", "li[class*='review']", "div[class*='review-item']",
    "[class*='ReviewItem']", "[class*='review_item']",
    ".sdp-review__article__list", ".review-list li",
    ".AppAjaxContent__body", ".TheWidget__content", ".review-list-item",
    "div[id*='review'] li",
    "[class*='snap'] li[data-review-no]",  # SNAP리뷰 명시
]
REVIEW_TAB_PATTERNS = [
    "text=리뷰", "text=후기", "text=구매후기", "text=상품후기",
    "[class*='review'][role='tab']", "a[href*='review']",
    "button[class*='review']", ".tab-review", "#tab-review",
]
NEXT_BTN_PATTERNS = [
    "a.next", "button.btn-next", ".pagination-next",
    "a[class*='next']", "button[class*='next']",
    "text=다음", "text=>", ".btn-next", "#btnNextPage",
]
REVIEW_PAGE_HINTS = ["review", "후기", "board/product", "opinion", "comment"]


def clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ─── 자동 탐색: 홈 → 리뷰/상품 페이지 찾기 ─────────────────────────────────

def auto_discover_review_url(url, status_box):
    """홈페이지 URL이면 리뷰/상품 페이지 자동 탐색"""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path in ["/", "/index.html", "/index.php", ""]:
        if not _ensure_playwright():
            status_box.info("🏠 홈페이지 감지 — Playwright 미설치로 자동 탐색 생략, 원본 URL로 진행")
            return url
        status_box.info("🏠 홈페이지 감지 → 리뷰 페이지 자동 탐색 중...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ).new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)"
                )
            except:
                links = []
            finally:
                browser.close()

        base = f"{parsed.scheme}://{parsed.netloc}"
        # 1순위: 전용 리뷰 페이지
        for link in links:
            l = link.lower()
            if any(k in l for k in REVIEW_PAGE_HINTS) and base in link:
                if "product/detail" not in l:
                    status_box.info(f"✅ 리뷰 페이지 발견: {link}")
                    return link
        # 2순위: 상품 상세 페이지
        for link in links:
            if "product/detail" in link.lower() and base in link:
                status_box.info(f"✅ 상품 페이지 발견: {link}")
                return link
        status_box.warning("리뷰 페이지를 자동으로 찾지 못했습니다. 원본 URL로 계속합니다.")
    return url


# ─── 방법 -1: Shopline 플랫폼 (해외 자사몰) ─────────────────────────────────

def scrape_shopline(url, max_pages, status_box, progress_bar):
    """
    Shopline 기반 사이트(홍콩/대만/동남아 자사몰)의 리뷰를 공식 API로 직접 수집.
    API: /api/merchants/{merchant_id}/products/{product_id}/product_review_comments
    페이지당 limit 최대 500건.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
        "Referer": url,
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    # Shopline 플랫폼 시그니처
    if "shoplineapp" not in html and "shoplineimg" not in html:
        return None

    # product_id (24자 hex)
    m = re.search(r'data-product-id=["\']([a-f0-9]{20,30})["\']', html) \
        or re.search(r'"product_id"\s*:\s*"([a-f0-9]{20,30})"', html)
    if not m:
        return None
    product_id = m.group(1)

    # merchant_id (shoplineimg.com 경로 또는 owner_id 쿼리에서 추출)
    m = re.search(r'shoplineimg\.com/([a-f0-9]{20,30})/', html) \
        or re.search(r'owner_id=([a-f0-9]{20,30})', html) \
        or re.search(r'"merchant_id"\s*:\s*"([a-f0-9]{20,30})"', html)
    if not m:
        return None
    merchant_id = m.group(1)

    status_box.info(
        f"🎯 Shopline 직결 모드 (merchant={merchant_id[:8]}…, product={product_id[:8]}…)"
    )

    session = requests.Session()
    session.headers.update({
        "User-Agent": headers["User-Agent"],
        "Referer": url,
        "Accept": "application/json",
    })

    all_reviews = []
    page = 1
    limit = 500  # Shopline 단일 호출 최대치
    total = None

    while page <= max_pages:
        api_url = (
            f"{base}/api/merchants/{merchant_id}"
            f"/products/{product_id}/product_review_comments"
            f"?page={page}&limit={limit}&order_=desc"
        )
        try:
            resp = session.get(api_url, timeout=20)
            j = resp.json()
        except Exception as e:
            status_box.warning(f"page {page} 실패: {e}")
            break

        items = j.get("items", []) or j.get("data", []) or []
        if total is None:
            total = j.get("total", 0)
            status_box.info(f"📊 총 {total}건 감지 → 수집 시작")
        if not items:
            break

        for it in items:
            comment = it.get("comment", "")
            if isinstance(comment, dict):
                # i18n 포맷일 가능성
                comment = comment.get("en") or comment.get("zh-hant") or next(iter(comment.values()), "")
            all_reviews.append({
                "리뷰번호": it.get("_id"),
                "작성일": (it.get("commented_at") or "")[:10],
                "작성자": it.get("user_name", ""),
                "평점": it.get("score"),
                "리뷰내용": clean(str(comment)),
            })

        progress_bar.progress(min(len(all_reviews) / max(total or 1, 1), 0.99))
        status_box.info(f"📥 {len(all_reviews)}/{total} 수집...")

        if total and len(all_reviews) >= total:
            break
        if len(items) < limit:
            break
        page += 1
        time.sleep(random.uniform(0.2, 0.4))

    return all_reviews if all_reviews else None


# ─── 방법 0: Cafe24 (+ SNAP리뷰) 전용 빠른 수집 ─────────────────────────────

def _detect_cafe24_strategy(base, product_no, session):
    """
    Cafe24 자사몰에서 리뷰 ID를 어떤 경로로 가져올지 결정.
    반환: (전략명, page_url_template, id_extractor_fn, board_no) 또는 None
    """
    # 전략 A: SNAP리뷰 dp.html (data-review-no 기반)
    try:
        r = session.get(
            f"{base}/board/review/dp.html?link_product_no={product_no}&page=1",
            timeout=15,
        )
        if r.status_code == 200 and "data-review-no" in r.text:
            def extract_a(html):
                return re.findall(
                    r'data-review-no="(\d+)"\s+data-rate-point="(\d+)"', html
                )
            # board_no 자동 탐지
            board_no = _detect_board_no(base, r.text, session)
            return (
                "SNAP dp.html",
                f"{base}/board/review/dp.html?link_product_no={product_no}&page={{p}}",
                extract_a,
                board_no,
            )
    except Exception:
        pass

    # 전략 B: Cafe24 표준 board/product/list (여러 board_no 후보 시도)
    for bn in ["4", "5", "6", "7", "8", "3"]:
        try:
            r = session.get(
                f"{base}/board/product/list.html?board_no={bn}&product_no={product_no}&page=1",
                timeout=15,
            )
            if r.status_code != 200:
                continue
            ids = re.findall(rf'/article/[^/]+/{bn}/(\d+)/', r.text)
            if ids:
                bn_local = bn
                def make_extract(b):
                    pat = rf'/article/[^/]+/{b}/(\d+)/'
                    return lambda html: [(i, "") for i in re.findall(pat, html)]
                return (
                    f"Cafe24 board_no={bn_local}",
                    f"{base}/board/product/list.html?board_no={bn_local}&product_no={product_no}&page={{p}}",
                    make_extract(bn_local),
                    bn_local,
                )
        except Exception:
            continue

    return None


def _detect_board_no(base, html, session):
    """페이지 HTML에서 리뷰 게시판 board_no 추출."""
    m = re.search(r'id="board_no"[^>]*value="(\d+)"', html) \
        or re.search(r'name="board_no"[^>]*value="(\d+)"', html) \
        or re.search(r'/exec/front/Board/Get\?[^"\']*board_no=(\d+)', html)
    if m:
        return m.group(1)
    # 샘플 ID로 후보 board_no 시도
    sample = re.search(r'data-review-no="(\d+)"', html)
    if sample:
        sid = sample.group(1)
        for cand in ["4", "5", "6", "7", "8", "3"]:
            try:
                tr = session.get(
                    f"{base}/exec/front/Board/Get?no={sid}&board_no={cand}",
                    timeout=8,
                )
                tj = tr.json()
                if tj.get("failed") is False and tj.get("data", {}).get("no"):
                    return cand
            except Exception:
                continue
    return "4"


def scrape_cafe24_snap(url, max_pages, status_box, progress_bar):
    """
    Cafe24 자사몰의 리뷰를 빠르게 수집:
    1) 리뷰 ID 목록을 dp.html(SNAP리뷰) 또는 board/product/list(Cafe24 표준)에서 추출
    2) /exec/front/Board/Get?no=ID&board_no=B  → 리뷰 JSON 본문
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # product_no 추출: pretty URL 또는 쿼리스트링
    m = re.search(r"/product/[^/]+/(\d+)/", parsed.path)
    if m:
        product_no = m.group(1)
    else:
        qs = parse_qs(parsed.query)
        product_no = qs.get("product_no", [None])[0]
    if not product_no:
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": url,
    })

    strat = _detect_cafe24_strategy(base, product_no, session)
    if not strat:
        return None
    strat_name, url_tpl, extractor, board_no = strat

    status_box.info(
        f"🎯 {strat_name} 직결 모드 (product={product_no}, board={board_no})"
    )

    all_reviews = []
    seen_ids = set()

    for page_num in range(1, max_pages + 1):
        try:
            r = session.get(url_tpl.format(p=page_num), timeout=15)
            if r.status_code != 200:
                break
            page_html = r.text
        except Exception as e:
            status_box.warning(f"목록 페이지 {page_num} 실패: {e}")
            break

        id_rating = extractor(page_html)
        if not id_rating:
            break

        new_items = [(rid, rt) for rid, rt in id_rating if rid not in seen_ids]
        if not new_items:
            break

        status_box.info(
            f"📄 {page_num}페이지 — ID {len(new_items)}개, 본문 수집 중... (누적 {len(all_reviews)}건)"
        )

        for idx, (rid, rating) in enumerate(new_items):
            seen_ids.add(rid)
            try:
                gr = session.get(
                    f"{base}/exec/front/Board/Get?no={rid}&board_no={board_no}",
                    timeout=10,
                )
                gj = gr.json()
            except Exception:
                continue

            if gj.get("failed") is not False or "data" not in gj:
                continue
            d = gj["data"]
            content_html = d.get("content", "") or ""
            content_text = clean(
                BeautifulSoup(content_html, "html.parser").get_text(separator=" ")
            )
            if len(content_text) < 3:
                content_text = clean(d.get("subject", ""))
            all_reviews.append({
                "리뷰번호": d.get("no"),
                "작성일": (d.get("write_date") or "")[:10],
                "작성자": d.get("writer", ""),
                "평점": d.get("point_count") or rating,
                "제목": clean(d.get("subject", "")),
                "리뷰내용": content_text,
                "조회수": d.get("hit_count", ""),
            })

            if (idx + 1) % 10 == 0:
                progress_bar.progress(min(page_num / max_pages, 0.99))
                status_box.info(
                    f"📥 {page_num}p — {idx+1}/{len(new_items)} (전체 누적 {len(all_reviews)}건)"
                )
            time.sleep(random.uniform(0.04, 0.12))

        progress_bar.progress(min(page_num / max_pages, 0.99))
        time.sleep(random.uniform(0.15, 0.35))

    return all_reviews if all_reviews else None


# ─── 방법 1: Crema 전용 API 직접 수집 ───────────────────────────────────────

def scrape_crema(url, status_box, progress_bar):
    """Crema 위젯 감지 시 API 토큰 추출 후 전체 수집"""
    if not _ensure_playwright():
        return None
    # (api_url 전체, 요청 헤더) — next 있는 위젯 우선
    best = None   # (url, headers) — pagy.next 있는 것
    fallback = None  # pagy.next 없어도 일단 저장

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = ctx.new_page()

        def on_resp(resp):
            nonlocal best, fallback
            try:
                if "cre.ma/api" in resp.url and "/reviews?" in resp.url and resp.status == 200:
                    body = resp.body()
                    data = json.loads(body)
                    headers = dict(resp.request.headers)
                    if data.get("pagy", {}).get("next") and best is None:
                        best = (resp.url, headers)
                    elif data.get("reviews") and fallback is None:
                        fallback = (resp.url, headers)
            except:
                pass

        page.on("response", on_resp)
        status_box.info("🌐 사이트 접속 중 (Crema 감지 모드)...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except:
            pass
        page.wait_for_timeout(5000)
        browser.close()

    captured = best or fallback
    if not captured:
        return None

    base_url, req_headers = captured
    parsed_base = urlparse(base_url)
    base_params = parse_qs(parsed_base.query, keep_blank_values=True)

    status_box.info("🎯 Crema API 토큰 확보! 전체 수집 시작...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": req_headers.get("user-agent", "Mozilla/5.0"),
        "Referer": req_headers.get("referer", url),
        "Accept": "application/json, text/plain, */*",
    })

    all_reviews = []
    page_num = 1
    total_pages = None

    while True:
        # 원본 파라미터 전체 유지, page만 교체
        params = dict(base_params)
        params["page"] = [str(page_num)]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        api_url = urlunparse(parsed_base._replace(query=new_query))

        try:
            resp = session.get(api_url, timeout=15)
            data = resp.json()
        except Exception as e:
            status_box.warning(f"API 호출 실패 (p{page_num}): {e}")
            break

        reviews = data.get("reviews", [])
        pagy = data.get("pagy", {})

        if total_pages is None:
            # series에서 가장 큰 숫자 = 마지막 페이지
            series = pagy.get("series", [])
            nums = [s for s in series if isinstance(s, int)]
            total_pages = max(nums) if nums else 1
            status_box.info(f"📊 최소 {total_pages}페이지 이상 감지 (수집 중 업데이트)")

        if not reviews:
            break

        for r in reviews:
            content = clean(r.get("filtered_message", ""))
            if len(content) > 5:
                all_reviews.append({
                    "작성일": r.get("created_at", "")[:10],
                    "작성자": r.get("user_display_name", ""),
                    "평점": r.get("score", ""),
                    "상품명": r.get("product_name", ""),
                    "옵션": ", ".join(o.get("value", "") for o in r.get("product_options", [])),
                    "리뷰내용": content,
                })

        # series에서 실시간으로 총 페이지 수 갱신
        series = pagy.get("series", [])
        nums = [s for s in series if isinstance(s, int)]
        if nums:
            total_pages = max(total_pages or 0, max(nums))

        progress_bar.progress(min(page_num / max(total_pages, 1), 0.99))
        status_box.info(f"📥 {page_num}페이지 수집 중... (현재 {len(all_reviews)}건)")

        if not pagy.get("next"):
            break
        page_num += 1
        time.sleep(random.uniform(0.3, 0.7))

    return all_reviews if all_reviews else None


# ─── 방법 2: 네트워크 인터셉트 (범용 API) ───────────────────────────────────

REVIEW_KW = ["review", "comment", "rating", "후기", "리뷰", "opinion",
             "snap", "snapfit", "snapvi", "crema"]
CONTENT_KEYS = [
    "content", "body", "text", "review_content", "comment",
    "내용", "후기", "리뷰", "review", "message", "description",
    "reviewContent", "commentContent", "filtered_message",
]


def extract_texts(data, depth=0):
    if depth > 6:
        return []
    texts = []
    if isinstance(data, list):
        for item in data:
            texts.extend(extract_texts(item, depth + 1))
    elif isinstance(data, dict):
        for key in CONTENT_KEYS:
            val = data.get(key)
            if isinstance(val, str) and len(val.strip()) > 10:
                texts.append(val.strip())
                break
        for v in data.values():
            if isinstance(v, (list, dict)):
                texts.extend(extract_texts(v, depth + 1))
    return texts


def try_paginate_api(api_url, headers, status_box):
    all_texts = []
    parsed = urlparse(api_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    page_key = next(
        (k for k in ["page", "pageNo", "page_no", "p", "offset", "currentPage"] if k in params),
        None
    )
    if not page_key:
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            texts = extract_texts(r.json())
            return [{"방법": "API", "리뷰내용": clean(t)} for t in texts if len(clean(t)) > 10]
        except:
            return []

    page_num, empty_streak = 1, 0
    while True:
        params[page_key] = [str(page_num)]
        new_query = urlencode({k: v[0] for k, v in params.items()})
        paged_url = urlunparse(parsed._replace(query=new_query))
        status_box.info(f"📡 API {page_num}페이지... ({len(all_texts)}건)")
        try:
            r = requests.get(paged_url, headers=headers, timeout=10)
            texts = [clean(t) for t in extract_texts(r.json()) if len(clean(t)) > 10]
            if not texts:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
                all_texts.extend(texts)
            page_num += 1
            time.sleep(random.uniform(0.5, 1.0))
        except:
            break
    return [{"방법": "API", "리뷰내용": t} for t in all_texts]


def scrape_via_api(url, status_box):
    if not _ensure_playwright():
        return []
    captured = []

    def on_resp(resp):
        try:
            if resp.status != 200:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            if not any(k in resp.url.lower() for k in REVIEW_KW):
                return
            body = resp.body()
            captured.append((resp.url, body, dict(resp.request.headers)))
        except:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        page.on("response", on_resp)
        status_box.info("🌐 접속 중 (범용 API 감지)...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except:
            pass
        page.wait_for_timeout(3000)
        for pattern in REVIEW_TAB_PATTERNS:
            try:
                el = page.locator(pattern).first
                if el.is_visible(timeout=1000):
                    el.click()
                    page.wait_for_timeout(2500)
                    break
            except:
                continue
        # 페이지 끝까지 스크롤하여 lazy-load 위젯(SNAP리뷰 등) 트리거
        status_box.info("📜 lazy-load 위젯 트리거 중...")
        prev_h = 0
        for _ in range(20):
            cur_h = page.evaluate("document.body.scrollHeight")
            page.evaluate(f"window.scrollTo(0, {cur_h})")
            page.wait_for_timeout(1200)
            if cur_h == prev_h:
                page.wait_for_timeout(1500)
                if page.evaluate("document.body.scrollHeight") == cur_h:
                    break
            prev_h = cur_h
        page.wait_for_timeout(2000)
        browser.close()

    if not captured:
        return []

    status_box.info(f"📡 리뷰 API {len(captured)}개 감지!")
    all_reviews = []
    for api_url, body, headers in captured:
        try:
            data = json.loads(body)
            texts = extract_texts(data)
            if texts:
                paginated = try_paginate_api(api_url, headers, status_box)
                all_reviews.extend(paginated or [{"방법": "API", "리뷰내용": clean(t)} for t in texts if len(clean(t)) > 10])
        except:
            continue
    return all_reviews


# ─── 방법 3: DOM 스크래핑 폴백 ──────────────────────────────────────────────

MORE_BTN_PATTERNS = [
    "text=리뷰 더보기", "text=더보기", "text=더 보기", "text=Load more",
    ".btn-more", "[class*='more-review']", "[class*='review-more']",
    "[class*='load-more']", "[class*='loadMore']", "a.more", "button.more",
]


def _extract_from_html(html, seen):
    """HTML에서 리뷰 항목을 추출. seen set으로 중복 제거."""
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for pattern in REVIEW_ITEM_PATTERNS:
        for item in soup.select(pattern):
            rev_no = item.get("data-review-no", "") or ""
            txt = clean(item.get_text(separator=" "))
            if len(txt) <= 10:
                continue
            key = f"no:{rev_no}" if rev_no else f"txt:{txt[:80]}"
            if key in seen:
                continue
            seen.add(key)
            row = {"리뷰내용": txt}
            if rev_no:
                row["리뷰번호"] = rev_no
            out.append(row)
    return out


def scrape_via_dom(url, max_pages, status_box, progress_bar):
    """SNAP리뷰/누적형 + 페이지네이션형 둘 다 처리."""
    if not _ensure_playwright():
        status_box.warning("⚠️ DOM 폴백은 Playwright/Chromium이 필요합니다. 이 환경에는 미설치 상태라 건너뜁니다.")
        return []
    all_reviews = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ).new_page()
        status_box.info("🌐 접속 중 (DOM 탐색)...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except:
            pass
        page.wait_for_timeout(3000)

        # 리뷰 탭 클릭
        for pattern in REVIEW_TAB_PATTERNS:
            try:
                el = page.locator(pattern).first
                if el.is_visible(timeout=1000):
                    el.click()
                    page.wait_for_timeout(2500)
                    break
            except:
                continue

        # 1단계: 페이지 끝까지 스크롤 (lazy-load 트리거)
        status_box.info("📜 페이지 스크롤 (lazy-load 트리거 중)...")
        prev_h = 0
        for _ in range(15):
            cur_h = page.evaluate("document.body.scrollHeight")
            page.evaluate(f"window.scrollTo(0, {cur_h})")
            page.wait_for_timeout(1000)
            if cur_h == prev_h:
                break
            prev_h = cur_h

        # 2단계: "더보기" 버튼 반복 클릭 + 페이지네이션
        for click_round in range(1, max_pages + 1):
            # 현재 DOM에서 추출
            try:
                all_reviews.extend(_extract_from_html(page.content(), seen))
                for frame in page.frames:
                    try:
                        all_reviews.extend(_extract_from_html(frame.content(), seen))
                    except:
                        continue
            except:
                pass

            status_box.info(f"📄 더 불러오기 ({click_round}/{max_pages})... 현재 {len(all_reviews)}건")
            progress_bar.progress(min(click_round / max_pages, 0.99))

            # "더보기" 버튼 시도 (누적형)
            clicked = False
            for sel in MORE_BTN_PATTERNS:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=800):
                        btn.scroll_into_view_if_needed(timeout=2000)
                        btn.click()
                        page.wait_for_timeout(2200)
                        clicked = True
                        break
                except:
                    continue

            # 없으면 다음 페이지 버튼 (페이지네이션형)
            if not clicked:
                for btn_sel in NEXT_BTN_PATTERNS:
                    for f in page.frames:
                        try:
                            n = f.locator(btn_sel).first
                            if n.is_visible(timeout=800):
                                n.scroll_into_view_if_needed(timeout=2000)
                                n.click()
                                page.wait_for_timeout(2200)
                                clicked = True
                                break
                        except:
                            continue
                    if clicked:
                        break

            # 그래도 없으면 한번 더 스크롤 시도 (무한 스크롤형)
            if not clicked:
                cur_h = page.evaluate("document.body.scrollHeight")
                page.evaluate(f"window.scrollTo(0, {cur_h})")
                page.wait_for_timeout(1500)
                new_h = page.evaluate("document.body.scrollHeight")
                if new_h <= cur_h:
                    # 더 이상 변화 없음 → 종료
                    break

            time.sleep(random.uniform(0.4, 0.9))

        # 최종 한 번 더 추출
        try:
            all_reviews.extend(_extract_from_html(page.content(), seen))
            for frame in page.frames:
                try:
                    all_reviews.extend(_extract_from_html(frame.content(), seen))
                except:
                    continue
        except:
            pass

        browser.close()
    return all_reviews


# ─── 통합 진입점 ─────────────────────────────────────────────────────────────

def smart_scrape(url, max_pages, status_box, progress_bar):
    # 0단계: 홈페이지면 리뷰 페이지 자동 탐색
    url = auto_discover_review_url(url, status_box)

    # 1-A단계: Shopline 플랫폼 (해외 자사몰)
    reviews = scrape_shopline(url, max_pages, status_box, progress_bar)
    if reviews:
        progress_bar.progress(1.0)
        return reviews

    # 1-B단계: Cafe24 + SNAP리뷰/표준 게시판 직결 (가장 빠르고 정확)
    reviews = scrape_cafe24_snap(url, max_pages, status_box, progress_bar)
    if reviews:
        progress_bar.progress(1.0)
        return reviews

    # 2단계: Crema 전용 처리
    reviews = scrape_crema(url, status_box, progress_bar)
    if reviews:
        progress_bar.progress(1.0)
        return reviews

    # 2단계: 범용 API 인터셉트
    reviews = scrape_via_api(url, status_box)
    if reviews:
        progress_bar.progress(1.0)
        return reviews

    # 3단계: DOM 폴백
    status_box.info("🔄 API 미감지 → DOM 방식으로 전환합니다...")
    return scrape_via_dom(url, max_pages, status_box, progress_bar)


# ─── 번역 ────────────────────────────────────────────────────────────────────

LANGS = {
    "자동 감지": "auto",
    "한국어": "ko",
    "English": "en",
    "日本語": "ja",
    "中文 (간체)": "zh-CN",
    "中文 (번체)": "zh-TW",
    "Español": "es",
    "Français": "fr",
    "Deutsch": "de",
    "Tiếng Việt": "vi",
    "Bahasa Indonesia": "id",
    "ภาษาไทย": "th",
    "Русский": "ru",
    "العربية": "ar",
}


def _quick_detect_lang(text):
    """문자셋 기반 빠른 언어 감지. Google auto-detect가 자주 실패하는 케이스 보완."""
    if not text:
        return None
    # 일본어 가나 (히라가나/가타카나)
    if re.search(r'[぀-ゟ゠-ヿ]', text):
        return 'ja'
    # 한글
    if re.search(r'[가-힣]', text):
        return 'ko'
    # 한자가 있으면 번체/간체 구분
    if re.search(r'[一-鿿]', text):
        # 번체 자주 쓰이는 글자 (간체에선 다르게 씀)
        trad_chars = '還開給謝個體會發業國學說對來這時間問題東車萬點門裡讓書'
        simp_chars = '还开给谢个体会发业国学说对来这时间问题东车万点门里让书'
        trad_count = sum(1 for c in text if c in trad_chars)
        simp_count = sum(1 for c in text if c in simp_chars)
        if trad_count > simp_count:
            return 'zh-TW'
        if simp_count > trad_count:
            return 'zh-CN'
        return 'zh-TW'  # 동률이면 번체로 (대만 시장 가정)
    # 태국어
    if re.search(r'[฀-๿]', text):
        return 'th'
    # 아랍어
    if re.search(r'[؀-ۿ]', text):
        return 'ar'
    # 키릴(러시아)
    if re.search(r'[Ѐ-ӿ]', text):
        return 'ru'
    # 베트남어 특수 발음 부호
    if re.search(r'[ăâđêôơưĂÂĐÊÔƠƯ]', text):
        return 'vi'
    return 'en'  # 그 외 ASCII는 영어로 가정


def translate_reviews(reviews, src_code, tgt_code, status_box):
    """리뷰의 '리뷰내용' 필드를 번역해서 '번역내용' 컬럼으로 추가."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        status_box.warning("⚠️ deep-translator 미설치 — pip install deep-translator")
        return reviews

    auto_mode = (src_code == "auto")
    # auto가 아니면 단일 translator 재사용
    fixed_translator = None if auto_mode else GoogleTranslator(source=src_code, target=tgt_code)
    translator_cache = {}

    def get_translator(src):
        if src in translator_cache:
            return translator_cache[src]
        t = GoogleTranslator(source=src, target=tgt_code)
        translator_cache[src] = t
        return t

    total = len(reviews)
    for i, r in enumerate(reviews):
        text = (r.get("리뷰내용") or "").strip()
        if not text:
            r["번역내용"] = ""
            continue
        sample = text[:4500]  # Google 5000자 제한
        try:
            if auto_mode:
                detected = _quick_detect_lang(sample) or "auto"
                if detected == tgt_code:
                    r["번역내용"] = sample  # 이미 같은 언어
                    continue
                result = get_translator(detected).translate(sample)
                # 결과가 입력과 같으면 (Google이 못 알아본 것) 'auto' 로 재시도
                if result.strip() == sample.strip():
                    try:
                        result = get_translator("auto").translate(sample)
                    except Exception:
                        pass
                r["번역내용"] = result
            else:
                r["번역내용"] = fixed_translator.translate(sample)
        except Exception as e:
            r["번역내용"] = f"[번역 실패: {str(e)[:60]}]"
        if (i + 1) % 20 == 0 or i + 1 == total:
            status_box.info(f"🌐 번역 {i+1}/{total}...")
        time.sleep(0.08)
    return reviews


# ─── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="스마트 리뷰 수집기", page_icon="🤖")
st.title("🤖 스마트 리뷰 크롤러")
st.caption("자사몰 URL만 붙여넣으면 리뷰를 자동으로 찾아서 수집합니다.")

url = st.text_input("🔗 URL 입력 (홈/상품/리뷰 페이지 모두 가능)")
pages = st.number_input("🔢 최대 '더보기'/페이지 클릭 횟수 (DOM 폴백 시 적용)", 1, 2000, 100)

with st.expander("🌐 번역 옵션 (선택)", expanded=False):
    do_translate = st.checkbox("리뷰 내용을 번역해서 '번역내용' 컬럼 추가", value=False)
    col_src, col_tgt = st.columns(2)
    with col_src:
        src_lang = st.selectbox("원본 언어", list(LANGS.keys()), index=0)
    with col_tgt:
        tgt_lang = st.selectbox(
            "번역 대상 언어",
            [k for k in LANGS.keys() if k != "자동 감지"],
            index=1,  # English 기본
        )
    if do_translate:
        st.caption(
            f"💡 {len(LANGS)-1}개 언어 지원. 리뷰 1건당 약 0.2초 소요 (Google 무료 엔드포인트). "
            "100건 이상이면 시간이 좀 걸리고 가끔 일시적 실패도 있을 수 있어요."
        )

if st.button("🚀 수집 시작", type="primary", use_container_width=True):
    if not url:
        st.warning("URL을 입력해주세요.")
    else:
        sb = st.empty()
        pb = st.progress(0)
        try:
            results = smart_scrape(url, pages, sb, pb)
            if results:
                # 중복 제거 먼저
                df = pd.DataFrame(results).drop_duplicates(subset=["리뷰내용"])
                results = df.to_dict("records")
                # 번역 옵션이 켜져있으면 적용
                if do_translate and results:
                    pb.progress(0.0)
                    sb.info(f"🌐 번역 시작 — {LANGS[src_lang]} → {LANGS[tgt_lang]} ({len(results)}건)")
                    results = translate_reviews(
                        results, LANGS[src_lang], LANGS[tgt_lang], sb
                    )
                    df = pd.DataFrame(results)

                # 컬럼 순서 정리: 번역내용을 리뷰내용 바로 옆에
                if "번역내용" in df.columns:
                    cols = list(df.columns)
                    cols.remove("번역내용")
                    if "리뷰내용" in cols:
                        idx = cols.index("리뷰내용") + 1
                        cols.insert(idx, "번역내용")
                    else:
                        cols.append("번역내용")
                    df = df[cols]

                pb.progress(1.0)
                sb.success(
                    f"✅ 총 {len(df)}건 수집 완료! (중복 제거 후)"
                    + (" — 표가 넓으면 좌우로 스크롤 해서 '번역내용' 컬럼 확인" if do_translate else "")
                )
                st.dataframe(df, use_container_width=True)
                out = io.BytesIO()
                with pd.ExcelWriter(out, engine="openpyxl") as w:
                    df.to_excel(w, index=False)
                st.download_button(
                    "📥 엑셀로 저장하기", out.getvalue(), "리뷰데이터.xlsx",
                    use_container_width=True,
                )
            else:
                sb.error("❌ 리뷰를 찾지 못했습니다. 상품 상세 또는 리뷰 페이지 URL을 직접 입력해보세요.")
        except Exception as e:
            st.error(f"오류: {e}")
