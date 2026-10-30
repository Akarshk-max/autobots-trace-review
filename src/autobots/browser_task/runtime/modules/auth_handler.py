import asyncio
from typing import Dict, Optional

import httpx
from loguru import logger
from playwright.async_api import Page
from pydantic import BaseModel, Field

from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.runtime.modules.base import ModuleResult
from src.autobots.browser_task.runtime.modules.utils import log_ctx


class AuthCredentials(BaseModel):
    username: str
    password: str
    extra_fields: Optional[Dict[str, str]] = Field(default=None)


async def dismiss_post_login_popups(page: Page, job: JobEnvelope, attempts: int = 6) -> bool:
    def _ctx():
        try:
            from src.autobots.browser_task.runtime.modules.utils import log_ctx
            return log_ctx(job) if job else {}
        except Exception:
            return {}

    async def _visible(loc) -> bool:
        try:
            return await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            return False

    async def _promo_visible() -> bool:
        title = page.locator('h4:has-text("More stream data available on SoundOn")').first
        return await _visible(title)

    async def _click_locator(loc, label: str) -> bool:
        try:
            if not await _visible(loc):
                return False

            try:
                await loc.first.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            try:
                await loc.first.click(timeout=3000, force=True)
                await asyncio.sleep(0.8)
                logger.bind(ctx=_ctx()).info(
                    "blocking_overlay_dismissed kind=promo_modal action={}",
                    label,
                )
                return True
            except Exception:
                pass

            try:
                h = await loc.first.element_handle()
                if h:
                    try:
                        await page.evaluate(
                            """
                            (el) => {
                              if (!el) return false;
                              try { el.click(); return true; } catch (_) {}
                              const opts = { bubbles: true, cancelable: true, view: window };
                              try { el.dispatchEvent(new MouseEvent("mousedown", opts)); } catch (_) {}
                              try { el.dispatchEvent(new MouseEvent("mouseup", opts)); } catch (_) {}
                              try { el.dispatchEvent(new MouseEvent("click", opts)); } catch (_) {}
                              return true;
                            }
                            """,
                            h,
                        )
                        await asyncio.sleep(0.8)
                        logger.bind(ctx=_ctx()).info(
                            "blocking_overlay_dismissed kind=promo_modal action={} method=js",
                            label,
                        )
                        return True
                    finally:
                        try:
                            await h.dispose()
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            pass
        return False

    async def _dismiss_soundon_promo_modal() -> bool:
        if not await _promo_visible():
            return False

        title = page.locator('h4:has-text("More stream data available on SoundOn")').first
        header = title.locator("xpath=ancestor::div[1]").first

        candidates = [
            header.locator("svg.arco-icon-close").first,
            header.locator("svg.arco-icon.arco-icon-close").first,
            header.locator("xpath=.//*[name()='svg' and contains(@class,'arco-icon-close')]").first,
        ]

        for idx, loc in enumerate(candidates, start=1):
            if await _click_locator(loc, f"promo_close_svg_{idx}"):
                if not await _promo_visible():
                    return True

        # DOM fallback: find title, then click sibling svg directly
        try:
            clicked = await page.evaluate(
                """
                () => {
                  const title = Array.from(document.querySelectorAll("h4"))
                    .find(el => (el.innerText || "").includes("More stream data available on SoundOn"));
                  if (!title) return false;

                  const header = title.parentElement;
                  if (!header) return false;

                  const closeSvg =
                    header.querySelector("svg.arco-icon-close") ||
                    header.querySelector("svg.arco-icon.arco-icon-close") ||
                    header.querySelector("svg");

                  if (!closeSvg) return false;

                  const target = closeSvg.closest("button,[role='button'],span,div") || closeSvg;

                  try { target.click(); } catch (_) {}

                  const opts = { bubbles: true, cancelable: true, view: window };
                  try { target.dispatchEvent(new MouseEvent("mousedown", opts)); } catch (_) {}
                  try { target.dispatchEvent(new MouseEvent("mouseup", opts)); } catch (_) {}
                  try { target.dispatchEvent(new MouseEvent("click", opts)); } catch (_) {}

                  return true;
                }
                """
            )
            if clicked:
                await asyncio.sleep(0.8)
                if not await _promo_visible():
                    logger.bind(ctx=_ctx()).info(
                        "blocking_overlay_dismissed kind=promo_modal action=promo_close_dom_fallback"
                    )
                    return True
        except Exception:
            pass

        # coordinate fallback: click near top-right of the modal root
        try:
            modal_root = page.locator('div:has(h4:has-text("More stream data available on SoundOn"))').first
            if await _visible(modal_root):
                box = await modal_root.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] - 24, box["y"] + 24)
                    await asyncio.sleep(0.8)
                    if not await _promo_visible():
                        logger.bind(ctx=_ctx()).info(
                            "blocking_overlay_dismissed kind=promo_modal action=promo_close_corner_click"
                        )
                        return True
        except Exception:
            pass

        # Escape fallback
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.6)
            if not await _promo_visible():
                logger.bind(ctx=_ctx()).info(
                    "blocking_overlay_dismissed kind=promo_modal action=promo_escape"
                )
                return True
        except Exception:
            pass

        return False

    dismissed_any = False

    for attempt in range(1, attempts + 1):
        await asyncio.sleep(0.8 if attempt == 1 else 0.5)

        round_dismissed = False

        if await _dismiss_soundon_promo_modal():
            dismissed_any = True
            round_dismissed = True

        cookie_accept_candidates = [
            page.get_by_role("button", name="Accept"),
            page.locator('button:has-text("Accept")').first,
        ]
        for idx, loc in enumerate(cookie_accept_candidates, start=1):
            try:
                if await _click_locator(loc, f"cookie_accept_{idx}"):
                    dismissed_any = True
                    round_dismissed = True
                    break
            except Exception:
                pass

        if not round_dismissed and not await _promo_visible():
            break

    return dismissed_any


class AuthModule(BaseModel):
    name: str = "auth"
    attach_before_goto: bool = False

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        auth_cfg = getattr(browser, "auth", None)
        if not auth_cfg or not getattr(auth_cfg, "enabled", False):
            return ModuleResult(artifacts=[])

        login_url = getattr(auth_cfg, "login_url", None)
        credentials_url = getattr(auth_cfg, "credentials_url", None)
        
        if not login_url or not credentials_url:
            logger.bind(ctx=log_ctx(job)).warning(
                "auth_config_missing login_url={} credentials_url={}",
                login_url,
                credentials_url,
            )
            return ModuleResult(artifacts=[])

        timeout_ms = int(getattr(auth_cfg, "timeout_ms", 30000))

        try:
            # Fetch credentials
            credentials = await self._fetch_credentials(credentials_url)
            
            # Navigate to login page
            logger.bind(ctx=log_ctx(job)).info(
                "auth_navigating_to_login url={}",
                login_url,
            )
            await page.goto(str(login_url), timeout=timeout_ms, wait_until="domcontentloaded")
            
            # Wait for dynamic form rendering - progressive wait with retries
            # Modern SPAs (React/Vue/Angular) need more time
            max_wait_ms = min(timeout_ms, 10000)  # Cap at 10 seconds for form detection
            check_interval = 0.5
            elapsed = 0
            
            while elapsed < max_wait_ms:
                await asyncio.sleep(check_interval)
                elapsed += check_interval * 1000
                
                # Check if form elements are present
                has_form = await self._check_form_elements_present(page)
                if has_form:
                    logger.bind(ctx=log_ctx(job)).info(
                        "auth_form_detected_after_ms ms={}",
                        elapsed,
                    )
                    break
            
            # Additional wait for any animations/transitions
            await asyncio.sleep(0.3)
            
            # Get selectors (use provided or auto-detect)
            username_selector = getattr(auth_cfg, "username_selector", None)
            password_selector = getattr(auth_cfg, "password_selector", None)
            submit_selector = getattr(auth_cfg, "submit_selector", None)
            success_indicator = getattr(auth_cfg, "success_indicator_selector", None)

            # Auto-detect selectors if not provided
            if not username_selector or not password_selector:
                detected = await self._detect_form_selectors(page, job)
                logger.bind(ctx=log_ctx(job)).info(
                    "auth_auto_detected_selectors username={} password={} submit={}",
                    detected.get("username"),
                    detected.get("password"),
                    detected.get("submit"),
                )
                username_selector = username_selector or detected.get("username")
                password_selector = password_selector or detected.get("password")
                submit_selector = submit_selector or detected.get("submit")

            if not username_selector or not password_selector:
                raise RuntimeError("auth_form_selectors_not_found")

            logger.bind(ctx=log_ctx(job)).info(
                "auth_using_selectors username={} password={} submit={}",
                username_selector,
                password_selector,
                submit_selector,
            )

            # Fill in the form
            await self._fill_login_form(
                page,
                username_selector=username_selector,
                password_selector=password_selector,
                submit_selector=submit_selector,
                credentials=credentials,
                timeout_ms=timeout_ms,
            )

            # Wait for login to complete
            await self._wait_for_login_complete(
                page,
                success_indicator=success_indicator,
                timeout_ms=timeout_ms,
            )

            logger.bind(ctx=log_ctx(job)).info(
                "auth_login_successful"
            )

            # Handle common post-login popups (2FA, cookies, welcome modals, etc.)
            await self._dismiss_post_login_popups(page, job)

            return ModuleResult(artifacts=[])

        except Exception as e:
            logger.bind(ctx=log_ctx(job)).error(
                "auth_login_failed err={}",
                str(e),
            )
            raise

    async def _dismiss_post_login_popups(self, page: Page, job: JobEnvelope) -> None:
        await dismiss_post_login_popups(page, job)

    async def _check_form_elements_present(self, page: Page) -> bool:
        """Check if any form elements are present on the page."""
        js = """
        () => {
            const hasInput = !!document.querySelector('input[type="email"], input[type="text"], input[name="email"], input[name="username"]');
            const hasPassword = !!document.querySelector('input[type="password"]');
            return hasInput || hasPassword;
        }
        """
        try:
            return await page.evaluate(js)
        except Exception:
            return False

    async def _fetch_credentials(self, credentials_url: str) -> AuthCredentials:
        """Fetch credentials from the provided URL."""
        timeout = httpx.Timeout(10.0, connect=5.0)
        limits = httpx.Limits(max_connections=5, max_keepalive_connections=3)

        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
            r = await client.get(str(credentials_url))
            r.raise_for_status()
            
            content = r.content
            if content and len(content) > 100_000:
                raise RuntimeError("credentials_file_too_large")

            data = r.json()
            return AuthCredentials.model_validate(data)

    async def _detect_form_selectors(self, page: Page, job: JobEnvelope) -> Dict[str, Optional[str]]:
        js = """
        () => {
            const result = { username: null, password: null, submit: null, debug: [] };
            
            // Helper to check if element is visible
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return el.offsetParent !== null && 
                       style.display !== 'none' && 
                       style.visibility !== 'hidden' &&
                       style.opacity !== '0';
            }
            
            // Helper to get selector for element
            function getSelector(el) {
                if (!el) return null;
                if (el.id) return '#' + el.id;
                if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                // Try to build a unique selector
                let selector = el.tagName.toLowerCase();
                if (el.type) selector += '[type="' + el.type + '"]';
                if (el.className && typeof el.className === 'string') {
                    const classes = el.className.split(' ').filter(c => c && !c.includes(':')).slice(0, 2);
                    if (classes.length > 0) selector += '.' + classes.join('.');
                }
                return selector;
            }
            
            // Find all input fields for debugging
            const allInputs = Array.from(document.querySelectorAll('input'));
            result.debug = allInputs.map(el => ({
                type: el.type,
                name: el.name,
                id: el.id,
                placeholder: el.placeholder,
                visible: isVisible(el),
                selector: getSelector(el)
            }));
            
            // Find username/email field - check ALL inputs, not just by pattern
            const userInput = allInputs.find(el => {
                if (!isVisible(el)) return false;
                const type = el.type?.toLowerCase();
                const name = (el.name || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const placeholder = (el.placeholder || '').toLowerCase();
                const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                
                return type === 'email' ||
                       name.includes('email') || id.includes('email') ||
                       name.includes('username') || id.includes('username') ||
                       name.includes('user') || id.includes('user') ||
                       name.includes('login') || id.includes('login') ||
                       placeholder.includes('email') || placeholder.includes('username') ||
                       ariaLabel.includes('email') || ariaLabel.includes('username');
            });
            
            if (userInput) {
                result.username = getSelector(userInput);
            }
            
            // If no specific user field found, look for any visible text input that's not password
            if (!result.username) {
                const textInput = allInputs.find(el => {
                    if (!isVisible(el)) return false;
                    const type = el.type?.toLowerCase();
                    return type === 'text' || type === 'email';
                });
                if (textInput) {
                    result.username = getSelector(textInput);
                }
            }
            
            // Find password field
            const passwordInput = allInputs.find(el => {
                if (!isVisible(el)) return false;
                return el.type?.toLowerCase() === 'password';
            });
            
            if (passwordInput) {
                result.password = getSelector(passwordInput);
            }
            
            // Find submit button
            const allButtons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"], [onclick]'));
            const submitBtn = allButtons.find(el => {
                if (!isVisible(el)) return false;
                const text = (el.innerText || el.value || '').toLowerCase();
                const type = el.type;
                return type === 'submit' ||
                       text.includes('sign in') || text.includes('signin') ||
                       text.includes('log in') || text.includes('login') ||
                       text.includes('submit') || text.includes('continue') ||
                       text.includes('next');
            });
            
            if (submitBtn) {
                result.submit = getSelector(submitBtn);
            }
            
            // If no submit button found, try to find any button near password field or in form
            if (!result.submit && result.password) {
                try {
                    // Find password element
                    const passwordEl = document.querySelector(result.password) || 
                                      document.querySelector('input[type="password"]');
                    if (passwordEl) {
                        // Look in same form
                        const form = passwordEl.closest('form');
                        if (form) {
                            const btn = form.querySelector('button:not([type="button"]), input[type="submit"]');
                            if (btn && isVisible(btn)) {
                                result.submit = getSelector(btn);
                            }
                        }
                        // If still no button, look for any button after password field
                        if (!result.submit) {
                            const allElements = Array.from(document.querySelectorAll('button, input[type="submit"]'));
                            const passwordIndex = allElements.findIndex(el => el === passwordEl);
                            const afterPassword = allElements.slice(passwordIndex + 1).find(el => isVisible(el));
                            if (afterPassword) {
                                result.submit = getSelector(afterPassword);
                            }
                        }
                    }
                } catch (e) {}
            }
            
            return result;
        }
        """
        
        result = await page.evaluate(js)
        
        # Log debug info
        logger.bind(ctx=log_ctx(job)).debug(
            "auth_form_detection_debug inputs={}",
            result.get("debug", []),
        )
        
        return {
            "username": result.get("username"),
            "password": result.get("password"),
            "submit": result.get("submit"),
        }

    async def _fill_login_form(
        self,
        page: Page,
        *,
        username_selector: str,
        password_selector: str,
        submit_selector: Optional[str],
        credentials: AuthCredentials,
        timeout_ms: int,
    ) -> None:
        """Fill in the login form with credentials."""
        
        # Fill username
        username_loc = page.locator(username_selector).first
        await username_loc.wait_for(state="visible", timeout=timeout_ms)
        await username_loc.fill(credentials.username)
        await asyncio.sleep(0.1)
        
        # Fill password
        password_loc = page.locator(password_selector).first
        await password_loc.wait_for(state="visible", timeout=timeout_ms)
        await password_loc.fill(credentials.password)
        await asyncio.sleep(0.1)
        
        # Submit the form
        if submit_selector:
            submit_loc = page.locator(submit_selector).first
            await submit_loc.wait_for(state="visible", timeout=timeout_ms)
            await submit_loc.click()
        else:
            # Try pressing Enter on password field
            await password_loc.press("Enter")

    async def _wait_for_login_complete(
        self,
        page: Page,
        *,
        success_indicator: Optional[str],
        timeout_ms: int,
    ) -> None:
        """
        I think these four conditions cover most cases of login completion.
        Wait for login to complete by checking for:
        1. Success indicator selector if provided
        2. Navigation to a different page
        3. Absence of login form 
        4. Sometimes it redirects to another page after successful login
        """
        start_time = asyncio.get_event_loop().time()
        timeout_s = timeout_ms / 1000
        
        initial_url = page.url
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_s:
                raise TimeoutError("auth_login_timeout")
            
            # Check for success indicator if provided
            if success_indicator:
                try:
                    loc = page.locator(success_indicator).first
                    if await loc.count() > 0:
                        if await loc.is_visible(timeout=1000):
                            return
                except Exception:
                    pass
            
            # Check if URL changed (indicates navigation after login)
            current_url = page.url
            if current_url != initial_url:
                # Give a moment for page to stabilize
                await asyncio.sleep(0.5)
                return
            
            # Check if login form is still present
            try:
                password_fields = await page.locator('input[type="password"]').count()
                if password_fields == 0:
                    # Password field disappeared, likely logged in
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                pass
            
            await asyncio.sleep(0.2)


class AuthPreLoginModule(BaseModel):
    name: str = "auth_pre_login"
    attach_before_goto: bool = True

    async def run(self, *, job: JobEnvelope, page: Page, plan: object, tracker) -> ModuleResult:
        browser = getattr(plan, "browser", None)
        if not browser:
            return ModuleResult(artifacts=[])

        auth_cfg = getattr(browser, "auth", None)
        if not auth_cfg or not getattr(auth_cfg, "enabled", False):
            return ModuleResult(artifacts=[])

        # Run the auth flow
        auth_module = AuthModule()
        return await auth_module.run(job=job, page=page, plan=plan, tracker=tracker)
