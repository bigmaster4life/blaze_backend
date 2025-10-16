# users/utils.py
import re

def normalize_phone_gabon(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\s+", "", raw)
    if s.startswith("+241"):
        return s
    if s.startswith("241"):
        return "+" + s
    if re.fullmatch(r"0\d{8}", s) or re.fullmatch(r"0\d{7}", s):
        return "+241" + s[1:]
    if re.fullmatch(r"\d{8,9}", s):
        return "+241" + s
    if not s.startswith("+"):
        return "+241" + s
    return s