_CHALLENGE_MARKERS = ("__jhash_", "__js_p_", "__jua_")


def is_challenge_page(html: str) -> bool:
    return any(marker in html for marker in _CHALLENGE_MARKERS)
