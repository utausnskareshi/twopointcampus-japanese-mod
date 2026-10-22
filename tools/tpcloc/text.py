"""Text helpers: Japanese detection and the markup that translations must preserve."""
import re

JP_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z']+")
UNUSED_RE = re.compile(r"unused|\(PH\)|SHOULDN.T|placeholder|^TODO|DOES .* EXIST", re.I)

# Tokens that must survive translation unchanged (order may change).
TOKEN_RE = re.compile(
    r"<sprite[^>]*>"          # inline icons
    r"|<style=[^>]*>|</style>"
    r"|<color=[^>]*>|</color>"
    r"|<size=[^>]*>|</size>"
    r"|<b>|</b>|<i>|</i>|<u>|</u>|<br>"
    r"|<link=[^>]*>|</link>"
    r"|<non_vocal_audio=[^>]*>|</non_vocal_audio>"  # subtitle timing of non-vocal audio
    r"|\{[A-Za-z0-9_.]+\}"    # named/positional parameters
    r"|\[(?:COUNT|Time)\]"    # bracket parameters (explicit: e.g. [HOLD] is translated officially)
    r"|\[i2p_[A-Za-z]+\]"     # I2 plural forms
    r"|\[i2s_[A-Za-z]+\]"     # I2 specializations
)


def has_japanese(s: str) -> bool:
    return bool(JP_RE.search(s))


def tokens(s: str) -> list[str]:
    return sorted(TOKEN_RE.findall(s))


def needs_translation(en: str, ja: str | None) -> bool:
    """True when the official Japanese value is missing/English and the English is real text.

    An official value without Japanese characters still counts as translated when it differs
    from the English and is not itself English prose (e.g. "↓", "BGM", "LV{LEVEL}", "Now Loading...").
    """
    if not en.strip() or not WORD_RE.search(en):
        return False
    if ja and has_japanese(ja):
        return False
    if not ja or not ja.strip():
        return not UNUSED_RE.search(en)
    if ja == en:
        return True
    return len(WORD_RE.findall(TOKEN_RE.sub(" ", ja))) >= 3
