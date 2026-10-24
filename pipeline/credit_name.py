# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-ANALYTICS
#  >>> FILE PATH:   pipeline/credit_name.py
#  z152 - company-name normalisation ("Oracle Corporation" / "ORACLE CORP" -> "ORACLE"), used to match
#  SEC filers to tickers when SEC's ticker map is unavailable.
# ============================================================
import re

SUFFIX = re.compile(r"\b(THE|INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|PLC|P L C|LLC|LP|L P|N V|NV|S A|SA|AG|SE|HOLDINGS?|GROUP|"
                    r"CLASS [A-C]|CL [A-C]|COMMON STOCK|ORDINARY SHARES|ADR|ADS|DEL|NEW)\b")


def norm(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper().replace("&", " AND "))
    s = SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()
