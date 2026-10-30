"""Headless-Chromium fallback for Cloudflare-challenged APKMirror pages.

Used by ``src/apkmirror.py`` when the direct HTTP fetch hits a Cloudflare
challenge and the Trawl browser service cannot obtain the page either.

Engine: Patchright — a patched, undetected Playwright driver (it removes
automation indicators such as ``--enable-automation`` and the
``navigator.webdriver`` / CDP fingerprints), a drop-in replacement for
Playwright: ``pip install patchright``, ``patchright install chromium``,
``from patchright.sync_api import sync_playwright``. If Patchright cannot
be imported, installed or launched on the runner, plain Playwright is
tried instead and the choice is logged plainly.

Flow (mirrors GooglePhotos-Patched ``download_apk.py``): navigate to the
URL in headless Chromium, wait for the 'Just a moment' challenge to
clear (event-driven: ``wait_for_function`` on document.title +
readyState), read the rendered HTML and hand the cookies back so the
shared HTTP session can continue with them. No fixed sleeps anywhere —
every wait ends when its condition is met.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from src import trawl

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BLOCKED_MARKERS = ("just a moment", "attention required", "verify you are human")

# Bounds only: wait_for_function / goto return as soon as their condition
# is met; these only cap how long a hung page may block the build.
_NAV_TIMEOUT_MS = 60000
_CF_CLEAR_TIMEOUT_MS = 60000


def _pip_install(package: str) -> bool:
    """Install a pip package. Returns True on success."""
    try:
        logging.info("[browser-fallback] Installing %s ...", package)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        return True
    except Exception as exc:
        logging.warning("[browser-fallback] pip install %s failed: %s", package, exc)
        return False


def _install_browser(engine: str) -> bool:
    """Install the Chromium binary for the engine. Returns True on success."""
    for args in (
        [engine, "install", "chromium", "--with-deps"],
        [engine, "install", "chromium"],
    ):
        try:
            logging.info("[browser-fallback] Running: %s", " ".join(args))
            subprocess.check_call(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
            return True
        except Exception as exc:
            logging.debug("[browser-fallback] %s failed: %s", " ".join(args), exc)
    return False


def _ensure_engine() -> tuple[str, object] | None:
    """Return (engine_name, sync_playwright) for Patchright, else Playwright.

    Patchright first; plain Playwright only if Patchright cannot be
    imported or installed. Returns None if neither is usable.
    """
    try:
        from patchright.sync_api import sync_playwright

        return "patchright", sync_playwright
    except ImportError:
        logging.info("[browser-fallback] patchright not installed; installing ...")
        if _pip_install("patchright"):
            try:
                from patchright.sync_api import sync_playwright

                return "patchright", sync_playwright
            except ImportError as exc:
                logging.warning("[browser-fallback] patchright import failed after install: %s", exc)
        else:
            logging.warning("[browser-fallback] patchright install failed; trying plain Playwright")

    try:
        from playwright.sync_api import sync_playwright

        return "playwright", sync_playwright
    except ImportError:
        logging.info("[browser-fallback] playwright not installed; installing ...")
        if _pip_install("playwright"):
            try:
                from playwright.sync_api import sync_playwright

                return "playwright", sync_playwright
            except ImportError as exc:
                logging.warning("[browser-fallback] playwright import failed after install: %s", exc)
    return None


def _launch_browser(playwright, engine: str):
    """Launch headless Chromium, installing the binary on demand if missing."""
    args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    # Chromium ignores the shell's proxy env vars; pass them explicitly so
    # the browser works in proxied environments (no-op on plain runners,
    # where no proxy var is set).
    proxy = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if engine == "playwright":
        # Patchright's driver already patches these flags; plain Playwright
        # needs the Photos-script's stealth args.
        args.append("--disable-blink-features=AutomationControlled")
    try:
        return playwright.chromium.launch(headless=True, args=args)
    except Exception as exc:
        if "executable" not in str(exc).lower() and "browser" not in str(exc).lower():
            raise
        logging.info("[browser-fallback] Chromium binary missing; installing ...")
        if not _install_browser(engine):
            raise RuntimeError(f"could not install Chromium for {engine}") from exc
        return playwright.chromium.launch(headless=True, args=args)


def _wait_cf_clear(page) -> bool:
    """Wait until the Cloudflare interstitial clears. Returns False if the
    challenge is still showing afterwards."""
    try:
        page.wait_for_function(
            "() => !document.title.includes('Just a moment') "
            "&& document.readyState === 'complete'",
            timeout=_CF_CLEAR_TIMEOUT_MS,
        )
    except Exception:
        pass
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    return "just a moment" not in title.lower()


def fetch_rendered(url: str, referer: str | None = None) -> trawl.ScrapedResponse | None:
    """Render *url* in headless Chromium and return its HTML + cookies.

    Returns a ``trawl.ScrapedResponse`` (same shape the Trawl path uses)
    or None when the page could not be obtained. Never raises.
    """
    ensured = _ensure_engine()
    if ensured is None:
        logging.warning("[browser-fallback] No usable browser engine (patchright/playwright)")
        return None
    engine, sync_playwright = ensured
    logging.info("[browser-fallback] Rendering %s via %s ...", url, engine)
    try:
        with sync_playwright() as p:
            browser = _launch_browser(p, engine)
            try:
                ctx = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    accept_downloads=False,
                    # Opt-in only, for MITM-proxied environments (e.g. a
                    # sandbox egress proxy that re-signs TLS). Off by
                    # default; CI runners validate certs normally.
                    ignore_https_errors=os.environ.get(
                        "BROWSER_FALLBACK_IGNORE_HTTPS_ERRORS"
                    )
                    == "1",
                )
                page = ctx.new_page()
                if engine == "playwright":
                    # Patchright's driver hides this already; plain
                    # Playwright needs the Photos-script's init script.
                    page.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                    )
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                if not _wait_cf_clear(page):
                    logging.warning("[browser-fallback] Cloudflare challenge did not clear for %s", url)
                    return None
                html = page.content()
                if any(m in html.lower() for m in _BLOCKED_MARKERS):
                    logging.warning("[browser-fallback] Rendered page still shows a block for %s", url)
                    return None
                cookies = {
                    c.get("name"): c.get("value")
                    for c in ctx.cookies()
                    if c.get("name") and c.get("value") is not None
                }
                logging.info("[browser-fallback] Page obtained via %s", engine)
                return trawl.ScrapedResponse(
                    url=page.url, content=html.encode(), cookies=cookies,
                    user_agent=_USER_AGENT,
                )
            finally:
                browser.close()
    except Exception as exc:
        logging.warning("[browser-fallback] %s render failed for %s: %s", engine, url, exc)
        return None
