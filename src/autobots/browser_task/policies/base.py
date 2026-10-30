from abc import ABC, abstractmethod
from typing import Dict, List

from src.autobots.browser_task.compiler.models import Budgets, ContextHints, NetworkRules, StepPlan
from src.autobots.browser_task.intent.models import ScrapeIntentRequest

_JS_HEAVY_TOTAL_MS = 1_800_000
_JS_HEAVY_NAV_MS   = 18_500

_STEALTH_TOTAL_MS  = 1_800_000
_STEALTH_NAV_MS    = 18_000

class PolicyPack(ABC):
    policy_id: str

    @abstractmethod
    def build_steps(self, intent: ScrapeIntentRequest) -> List[StepPlan]: ...

    def budgets(self, intent: ScrapeIntentRequest) -> Budgets:
        return Budgets()

    def network(self, intent: ScrapeIntentRequest) -> NetworkRules:
        return NetworkRules()

    def context(self, intent: ScrapeIntentRequest) -> ContextHints:
        return ContextHints()


class DefaultPolicy(PolicyPack):
    policy_id = "default"

    def build_steps(self, intent) -> List[StepPlan]:
        w = intent.navigation.wait
        ready = (w.ready.value if w else "auto")
        selectors_any = w.selectors_any if w else []
        selectors_all = w.selectors_all if w else []

        # Deterministic ladder:
        # 1) fast http
        # 2) rendered
        return [
            StepPlan(mode="http", nav_timeout_ms=5000, ready="auto"),
            StepPlan(
                mode="rendered",
                nav_timeout_ms=9000,
                ready=ready,
                selectors_any=selectors_any,
                selectors_all=selectors_all,
            ),
        ]


class JsHeavyPolicy(PolicyPack):
    policy_id = "js-heavy"

    def budgets(self, intent: ScrapeIntentRequest) -> Budgets:
        b = super().budgets(intent)
        b.max_total_ms = max(b.max_total_ms, _JS_HEAVY_TOTAL_MS)
        b.max_nav_ms = max(b.max_nav_ms, _JS_HEAVY_NAV_MS)
        b.max_requests = max(b.max_requests, 180)
        return b

    def network(self, intent: ScrapeIntentRequest) -> NetworkRules:
        n = super().network(intent)
        # SPAs often need more assets; don't block images by default (still block media/fonts)
        n.block_resource_types = ["font", "media"]
        return n

    def build_steps(self, intent: ScrapeIntentRequest) -> List[StepPlan]:
        w = intent.navigation.wait
        ready = (w.ready.value if w else "auto")
        selectors_any = w.selectors_any if w else []
        selectors_all = w.selectors_all if w else []

        return [
            StepPlan(mode="rendered", nav_timeout_ms=18000, ready=ready, selectors_any=selectors_any, selectors_all=selectors_all),
            StepPlan(mode="rendered", nav_timeout_ms=24000, ready="networkidle"),
        ]


class StealthLitePolicy(PolicyPack):
    policy_id = "stealth-lite"

    def budgets(self, intent: ScrapeIntentRequest) -> Budgets:
        b = super().budgets(intent)
        b.max_total_ms = max(b.max_total_ms, _STEALTH_TOTAL_MS)
        b.max_nav_ms = max(b.max_nav_ms, _STEALTH_NAV_MS)
        b.max_requests = max(b.max_requests, 180)
        return b

    def network(self, intent: ScrapeIntentRequest) -> NetworkRules:
        n = super().network(intent)
        # Keep blocks modest; some bot walls require full CSS/layout signals.
        n.block_resource_types = ["media"]  # allow images/fonts
        return n

    def context(self, intent: ScrapeIntentRequest) -> ContextHints:
        # Safe, common-ish values. You can tune per region.
        return ContextHints(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.6167.85 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    def build_steps(self, intent: ScrapeIntentRequest) -> List[StepPlan]:
        w = intent.navigation.wait
        ready = (w.ready.value if w else "auto")
        selectors_any = w.selectors_any if w else []
        selectors_all = w.selectors_all if w else []

        return [
            StepPlan(mode="rendered", nav_timeout_ms=20000, ready=ready, selectors_any=selectors_any, selectors_all=selectors_all),
            StepPlan(mode="rendered", nav_timeout_ms=25000, ready="networkidle"),
        ]


class PolicyRegistry:
    _reg: Dict[str, PolicyPack] = {
        "default": DefaultPolicy(),
        "balanced": DefaultPolicy(),  # alias
        "js-heavy": JsHeavyPolicy(),
        "stealth-lite": StealthLitePolicy(),
    }

    @classmethod
    def get(cls, policy_id: str) -> PolicyPack:
        pid = (policy_id or "default").strip()
        return cls._reg.get(pid, cls._reg["default"])