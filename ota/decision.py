"""判定日志：打印输入与判定依据，便于测试复现。"""

import logging
from typing import Any

logger = logging.getLogger("ota")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[OTA] %(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


def log_decision(action: str, inputs: dict[str, Any], decision: str, **extra: Any) -> None:
    parts = [f"action={action}", f"inputs={inputs}"]
    parts.extend(f"{k}={v!r}" for k, v in extra.items())
    parts.append(f"decision={decision}")
    logger.info(" | ".join(parts))
