"""Small shared validation boundary. Never resolve a hostname twice before connecting."""
import ipaddress
import http.client
import json
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit


class ServiceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def body(self):
        return {"error": {"code": self.code, "message": self.message}}


PLATFORMS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "tiktok.com": "TikTok", "instagram.com": "Instagram",
    "twitter.com": "X", "x.com": "X", "reddit.com": "Reddit", "redd.it": "Reddit",
}


def public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # No mapped addresses, transition/NAT64 tunnels, scoped or special-purpose ranges.
        return (address in ipaddress.ip_network("2000::/3")
                and address not in ipaddress.ip_network("2001::/23")
                and address not in ipaddress.ip_network("2002::/16")
                and "%" not in value)
    return True


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or value.endswith("."):
        raise ServiceError("INVALID_URL", "Enter a public video URL.")
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ServiceError("INVALID_URL", "Enter a public video URL.") from None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise ServiceError("INVALID_URL", "Enter a public video URL.")
    if "." not in value or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
        raise ServiceError("INVALID_URL", "Enter a public video URL.")
    return value


def validate_url(value, extra_hosts=()):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\\x7f]", value):
        raise ServiceError("INVALID_URL", "Enter a valid HTTPS video URL.")
    try:
        parsed = urlsplit(value)
        host = hostname(parsed.hostname or "")
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
    except (ValueError, TypeError):
        raise ServiceError("INVALID_URL", "Enter a public HTTPS video URL without credentials.") from None
    platform = next((name for domain, name in PLATFORMS.items() if host == domain or host.endswith("." + domain)), None)
    if platform is None and host not in extra_hosts:
        raise ServiceError("UNSUPPORTED_SITE", "This site is not supported. Try YouTube, TikTok, Instagram, X or Reddit.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")), platform or "Video"


def public_addresses(host, port, resolver=socket.getaddrinfo):
    if port not in (80, 443) or "%" in host:
        raise ServiceError("BLOCKED_DESTINATION", "The source could not be reached safely.", 422)
    try:
        results = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ServiceError("MEDIA_UNAVAILABLE", "The video source could not be reached.", 422) from None
    if not results or any(not public_ip(item[4][0]) for item in results):
        raise ServiceError("BLOCKED_DESTINATION", "The source could not be reached safely.", 422)
    return results


def cloudflare_addresses(host, port, **_):
    """Optional resolver for hosts whose system DNS rewrites public names.

    Connect to a fixed public IP with certificate verification; do not bootstrap
    this resolver through potentially rewritten DNS. Returned IPs still pass the
    exact same all-addresses public-IP check in public_addresses.
    """
    results = []
    for kind, family in (("A", socket.AF_INET), ("AAAA", socket.AF_INET6)):
        connection = http.client.HTTPSConnection("1.1.1.1", timeout=8)
        try:
            connection.request("GET", f"/dns-query?name={quote(host, safe='')}&type={kind}", headers={"Host": "cloudflare-dns.com", "Accept": "application/dns-json"})
            response = connection.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise OSError("Public DNS resolution failed")
            data = json.loads(body)
            if data.get("Status") != 0:
                raise OSError("Public DNS resolution failed")
            for answer in data.get("Answer", []):
                if answer.get("type") == (1 if kind == "A" else 28):
                    address = str(ipaddress.ip_address(answer["data"]))
                    results.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port) if family == socket.AF_INET else (address, port, 0, 0)))
        except (ValueError, KeyError, TypeError, http.client.HTTPException):
            raise OSError("Public DNS resolution failed") from None
        finally:
            connection.close()
    return results


def connect_public(host, port, resolver=socket.getaddrinfo):
    addresses = public_addresses(host, port, resolver)
    for family, socktype, protocol, _, address in addresses:
        connection = socket.socket(family, socktype, protocol)
        connection.settimeout(20)
        try:
            connection.connect(address)  # IP returned above: no second DNS lookup.
            return connection
        except OSError:
            connection.close()
    raise ServiceError("MEDIA_UNAVAILABLE", "The video source could not be reached.", 422)
