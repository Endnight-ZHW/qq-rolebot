"""Validate provider-side fetch targets without trusting local Fake-IP answers.

This validates URLs sent to the configured fetch provider. QQ media downloads
continue using the strict local resolver in media.public_url.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

FAKE_IP = ipaddress.ip_network('198.18.0.0/15')
RESOLVERS = ('https://1.1.1.1/dns-query', 'https://1.0.0.1/dns-query')


async def public_dns(host):
    """TLS-verified, fixed-address DNS; never contact the unverified target."""
    async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
        for resolver in RESOLVERS:
            try:
                answers = []
                for kind in ('A', 'AAAA'):
                    response = await client.get(resolver, params={'name': host, 'type': kind},
                                                headers={'accept': 'application/dns-json'})
                    response.raise_for_status()
                    if len(response.content) > 65536:
                        raise ValueError('DNS response too large')
                    data = response.json()
                    if data.get('Status') != 0 or data.get('TC') is True:
                        raise ValueError('DNS resolution failed')
                    questions = data.get('Question') or []
                    if (len(questions) != 1 or questions[0].get('name', '').rstrip('.').lower() != host
                            or questions[0].get('type') != (1 if kind == 'A' else 28)):
                        raise ValueError('DNS question mismatch')
                    answers.extend(ipaddress.ip_address(x['data']) for x in data.get('Answer', []) if x.get('type') in (1, 28))
                if answers:
                    return answers
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                continue
    raise ValueError('公网 DNS 暂时无法核实目标地址，本次未读取网页')


async def public_fetch_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password
            or parts.port not in (None,80,443)):
        raise ValueError('仅支持公网 HTTP(S) 地址')
    host = parts.hostname.lower().rstrip('.')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError('不允许读取内网地址')
        return url
    if '.' not in host or host.endswith(('.localhost','.local','.internal','.lan','.home','.test','.invalid')):
        raise ValueError('不允许读取内网地址')
    try:
        local = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM),5)
        addresses = [ipaddress.ip_address(x[4][0]) for x in local]
    except (socket.gaierror, asyncio.TimeoutError):
        addresses = []
    if addresses and all(x.is_global for x in addresses):
        return url
    # Never turn a private answer into permission. Only synthetic benchmark
    # addresses (or a failed local lookup) can be independently resolved.
    if any(not x.is_global and x not in FAKE_IP for x in addresses):
        raise ValueError('不允许读取内网地址')
    real = await public_dns(host)
    if not real or not all(x.is_global for x in real):
        raise ValueError('不允许读取内网地址')
    return url
