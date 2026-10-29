"""Pure filename classification for the Offer Upload Review module."""

import re
import unicodedata


SIGNED_JOINED = frozenset({"signedoffer", "offersigned", "executedoffer"})
ACCEPTANCE_JOINED = frozenset({"acceptanceoffer", "offeracceptance", "acceptanceofoffer"})
OFFER_JOINED = frozenset({"letterofoffer", "offerletter"})


def _filename_tokens(filename):
    text = unicodedata.normalize("NFKC", str(filename or "")).casefold()
    stem = text.rsplit(".", 1)[0] if "." in text else text
    normalized = re.sub(r"[^a-z0-9]+", " ", stem).strip()
    return tuple(part for part in normalized.split() if part)


def classify_offer_filename(filename):
    tokens = _filename_tokens(filename)
    if not tokens:
        return ""
    token_set = set(tokens)
    if (
        ({"signed", "offer"} <= token_set)
        or ({"executed", "offer"} <= token_set)
        or any(token in SIGNED_JOINED for token in tokens)
    ):
        return "SignedOffer"
    if ({"acceptance", "offer"} <= token_set) or any(token in ACCEPTANCE_JOINED for token in tokens):
        return "OfferAcceptance"
    if "offer" in token_set or any(token in OFFER_JOINED or token.endswith("letterofoffer") for token in tokens):
        return "Offer"
    return ""
