from dataclasses import dataclass


FREE_ONLY = "FREE_ONLY"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FreeOnlyBillingGate:
    mode: str
    environ: dict

    @classmethod
    def from_environ(cls, environ):
        values = dict(environ or {})
        return cls(str(values.get("BILLING_MODE") or FREE_ONLY).strip().upper(), values)

    def allows(self, provider, billing_type):
        if str(billing_type).lower() == "local":
            return True
        if self.mode != FREE_ONLY:
            return False
        key = f"{str(provider).upper()}_OCR_FREE_ONLY_ALLOWED"
        return str(self.environ.get(key) or "").strip().lower() in _TRUE_VALUES

    def metadata(self, provider, billing_type):
        local = str(billing_type).lower() == "local"
        return {
            "billing_mode": FREE_ONLY,
            "billing_type": "local" if local else "cloud",
            "cost": 0 if local else None,
            "allowed": self.allows(provider, billing_type),
        }
