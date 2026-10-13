"""Pure URL/record validation for HYPD→Meesho. Never fetches merchant HTML.

A store-shaped URL alone is NOT affiliate proof. The caller must verify its
redirect and persist the mapping; stored records are rechecked on every use.
"""
import re
from urllib.parse import urlsplit


def hypd_parts(value):
    try:
        url = urlsplit(str(value or '').strip())
        if (url.scheme != 'https' or url.hostname not in {'hypd.store', 'www.hypd.store'}
                or url.username or url.password or url.port or url.query or url.fragment):
            return None
        match = re.fullmatch(r'/([A-Za-z0-9_-]{1,80})/afflink/([A-Za-z0-9_-]{1,256})/?', url.path)
        return (match[1].lower(), match[2]) if match else None
    except (ValueError, TypeError):
        return None


def meesho_product_id(value):
    try:
        url = urlsplit(str(value or '').strip())
        if (url.scheme not in {'http', 'https'} or url.hostname not in {'meesho.com', 'www.meesho.com', 'm.meesho.com'}
                or url.username or url.password or url.port):
            return ''
        match = re.search(r'/p/([A-Za-z0-9]{6,})/?$', url.path)
        return match[1].lower() if match else ''
    except (ValueError, TypeError):
        return ''


def meesho_key(value):
    pid = meesho_product_id(value)
    return f'PID:{urlsplit(value).hostname.lower()}:{pid}' if pid else ''


def key_matches_product(key, destination):
    pid = meesho_product_id(destination)
    match = re.fullmatch(r'PID:(?:www\.|m\.)?meesho\.com:([A-Za-z0-9]{6,})', str(key or ''), re.I)
    return bool(pid and match and match[1].lower() == pid)


def mapping_valid(afflink, destination, stored_store, key, allowed_stores):
    parts = hypd_parts(afflink)
    return bool(parts and parts[0] in allowed_stores and parts[0] == str(stored_store or '').lower()
                and key_matches_product(key, destination))
