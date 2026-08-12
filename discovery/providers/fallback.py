"""Runtime failover between two real providers.

Every call goes to `primary` first; a ProviderError (which includes
UnsupportedCapability, so a search-less primary can still borrow the
fallback's web search) falls through to `fallback`. Anything else -- a bug,
a KeyboardInterrupt -- propagates untouched, exactly as it would without
the wrapper.

Built by get_provider() when cfg.provider_fallback names a second provider
(DISCOVERY_PROVIDER_FALLBACK); the default is "", no wrapper, no behavior
change. The point is availability, not retry policy: one extra attempt on a
different vendor, never a loop -- the fallback's own failure surfaces as the
ProviderError the pipeline already treats as a skippable scoring/mission
failure.
"""
import sys

from .base import ProviderError


class FallbackProvider:
    """Duck-typed LLMProvider pair. Not an LLMProvider subclass on purpose:
    each wrapped provider keeps tallying its own `usage` under its own
    name/model (db.record_usage drains both via `providers`), and `name`/
    `model`/`last_events` reflect whichever provider actually served the
    most recent call."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.providers = (primary, fallback)   # db.record_usage drains each
        self._active = primary

    @property
    def name(self):
        return self._active.name

    @property
    def model(self):
        return self._active.model

    @property
    def last_events(self):
        return getattr(self._active, "last_events", None)

    # Installed post-construction by Tracer.sink (see missions.web_tick's
    # `mission_provider.trace_sink = tracer.sink`) -- mirrored onto both real
    # providers so whichever one serves a call emits under its own name.
    @property
    def trace_sink(self):
        return self.primary.trace_sink

    @trace_sink.setter
    def trace_sink(self, sink):
        self.primary.trace_sink = sink
        self.fallback.trace_sink = sink

    def complete_json(self, system, prompt, schema, max_tokens=8000):
        return self._call("complete_json", system, prompt, schema, max_tokens=max_tokens)

    def search_json(self, prompt, max_searches=5, max_tokens=16000):
        return self._call("search_json", prompt, max_searches=max_searches, max_tokens=max_tokens)

    def _call(self, method, *args, **kwargs):
        self._active = self.primary
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except ProviderError as e:
            print(
                f"{self.primary.name}.{method} failed ({e}); "
                f"falling back to {self.fallback.name}",
                file=sys.stderr,
            )
            self._active = self.fallback
            return getattr(self.fallback, method)(*args, **kwargs)

    def preflight(self):
        """Reachable iff either side is. A down primary with a live fallback
        still passes the gate (that is the whole point of having one), with a
        detail string health.py can surface either way."""
        ok, detail = self.primary.preflight()
        if ok:
            return True, ""
        fb_ok, fb_detail = self.fallback.preflight()
        if fb_ok:
            return True, f"{self.primary.name} down ({detail}); {self.fallback.name} ready"
        return False, f"{self.primary.name}: {detail}; {self.fallback.name}: {fb_detail}"
