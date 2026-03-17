import re

def normalize_gabon_phone(phone: str) -> str:
    if not phone:
        return phone
    phone = re.sub(r"\D", "", str(phone).strip())

    if phone.startswith("241") and len(phone) == 11:
        return phone
    if len(phone) == 9:
        return "241" + phone
    return phone