"""语义版本比较：用于最低固件版本门槛判定。"""


def parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in str(v).strip().split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_version_at_least(current: str, minimum: str) -> bool:
    """current >= minimum 时返回 True。"""
    return parse_version(current) >= parse_version(minimum)
