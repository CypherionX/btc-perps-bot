from dataclasses import dataclass
import yaml

@dataclass
class BotConfig:
    raw: dict

    @staticmethod
    def load(path: str) -> "BotConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return BotConfig(raw=raw)

    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur
