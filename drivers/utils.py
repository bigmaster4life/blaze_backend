# drivers/utils.py
import re

def normalize_gabon_phone(raw: str) -> str:
    """
    Retourne toujours un numéro gabonais au format sans plus:
      '241' + numéro national (sans zéros initiaux)
    Accepte: '077020273', '77020273', '24177020273', '+24177020273', '00 241 77020273', etc.
    """
    if not raw:
        return ""

    # garder que les chiffres
    digits = re.sub(r"\D", "", raw)

    # enlever préfixes internationaux 00 ou 011 (au cas où)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("011"):
        digits = digits[3:]

    # enlever le + éventuel déjà retiré par re.sub ci-dessus

    cc = "241"  # Gabon
    if digits.startswith(cc):
        nat = digits[len(cc):]
    else:
        nat = digits

    # enlever les zéros en tête du national
    nat = nat.lstrip("0")

    # recompose: 241 + national
    return cc + nat if nat else cc