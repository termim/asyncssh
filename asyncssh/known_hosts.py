# Copyright (c) 2015 by Ron Frederick <ronf@timeheart.net>.
# All rights reserved.
#
# This program and the accompanying materials are made available under
# the terms of the Eclipse Public License v1.0 which accompanies this
# distribution and is available at:
#
#     http://www.eclipse.org/legal/epl-v10.html
#
# Contributors:
#     Ron Frederick - initial implementation, API, and documentation
#     Alexander Travov - proposed changes to add negated patterns, hashed
#                        entries, and support for the revoked marker

"""Parser for SSH known_hosts files"""

import binascii, hmac
from fnmatch import fnmatch
from hashlib import sha1
from ipaddress import ip_network

from .constants import *
from .public_key import *


class _WildcardPattern:
    """A host pattern with '*' and '?' wildcards"""

    def __init__(self, pattern):
        # We need to escape square brackets in host patterns if we
        # want to use Python's fnmatch.
        self._pattern = b''.join(b'[[]' if b == ord('[') else
                                 b'[]]' if b == ord(']') else
                                 bytes((b,)) for b in pattern)

    def _match(self, value):
        return fnmatch(value, self._pattern)

    def matches(self, host, addr, ip):
        return (host and self._match(host)) or (addr and self._match(addr))


class _CIDRPattern:
    """A literal IPv4/v6 address or CIDR-style subnet pattern"""

    def __init__(self, pattern):
        self._network = ip_network(pattern.decode('ascii'))

    def matches(self, host, addr, ip):
        return ip and (ip in self._network)


class HostList:
    """A plaintext host list entry in a known_hosts file"""

    def __init__(self, pattern):
        self._pos_patterns = []
        self._neg_patterns = []

        for p in pattern.split(b','):
            if p.startswith(b'!'):
                negate = True
                p = p[1:]
            else:
                negate = False

            try:
                p = _CIDRPattern(p)
            except (ValueError, UnicodeDecodeError):
                p = _WildcardPattern(p)

            if negate:
                self._neg_patterns.append(p)
            else:
                self._pos_patterns.append(p)

    def matches(self, host, addr, ip):
        pos_match = any(p.matches(host, addr, ip) for p in self._pos_patterns)
        neg_match = any(p.matches(host, addr, ip) for p in self._neg_patterns)

        return pos_match and not neg_match


class HashedHost:
    """A hashed host entry in a known_hosts file"""

    _HMAC_SHA1_MAGIC = b'1'

    def __init__(self, pattern):
        try:
            magic, salt, hosthash = pattern[1:].split(b'|')
            self._salt = binascii.a2b_base64(salt)
            self._hosthash = binascii.a2b_base64(hosthash)
        except (ValueError, binascii.Error):
            raise ValueError('Invalid known hosts hash entry: %s' %
                                 pattern.decode('ascii', errors='replace')) \
                      from None

        if magic != self._HMAC_SHA1_MAGIC:
            # Only support HMAC SHA-1 for now
            raise ValueError('Invalid known hosts hash type: %s' %
                                 magic.decode('ascii', errors='replace')) \
                      from None

    def _match(self, value):
        return hmac.new(self._salt, value, sha1).digest() == self._hosthash

    def matches(self, host, addr, ip):
        return (host and self._match(host)) or (addr and self._match(addr))


def _parse_entries(known_hosts):
    entries = []

    for line in known_hosts.splitlines():
        line = line.strip()
        if not line or line.startswith(b'#'):
            continue

        try:
            if line.startswith(b'@'):
                marker, pattern, key = line[1:].split(None, 2)
            else:
                marker = None
                pattern, key = line.split(None, 1)
        except ValueError as exc:
            raise ValueError('Invalid known hosts entry: %s' %
                                 line.decode('ascii', errors='replace')) \
                      from None

        if marker not in (None, b'cert-authority', b'revoked'):
            raise ValueError('Invalid known hosts marker: %s' %
                                 marker.decode('ascii', errors='replace')) \
                      from None

        if pattern.startswith(b'|'):
            entry = HashedHost(pattern)
        else:
            entry = HostList(pattern)

        entry.marker = marker

        try:
            entry.key = import_public_key(key)
        except KeyImportError:
            """Ignore keys in the file that we're unable to parse"""
            continue

        entries.append(entry)

    return entries

def _match_entries(entries, host, ip=None, port=DEFAULT_PORT):
    addr = str(ip) if ip else ''

    if port != DEFAULT_PORT:
        host = '[{}]:{}'.format(host, port)

        if addr:
            addr = '[{}]:{}'.format(addr, port)

    host = host.encode('utf-8')
    addr = addr.encode('utf-8')

    host_keys = []
    ca_keys = []
    revoked_keys = []

    for entry in entries:
        if entry.matches(host, addr, ip):
            if entry.marker == b'revoked':
                revoked_keys.append(entry.key)
            elif entry.marker == b'cert-authority':
                ca_keys.append(entry.key)
            else:
                host_keys.append(entry.key)

    return host_keys, ca_keys, revoked_keys

def match_known_hosts(known_hosts, host, ip, port=DEFAULT_PORT):
    """Match a host, IP address, and port against a known_hosts file

       This function looks up a host, IP address, and port in a file
       in OpenSSH ``known_hosts`` format and returns the host keys,
       CA keys, and revoked keys which match.

       If the port is not the default port and no match is found
       for it, the lookup is attempted again without a port number.

    """

    if isinstance(known_hosts, str):
        known_hosts = open(known_hosts, 'rb').read()

    entries = _parse_entries(known_hosts)

    host_keys, ca_keys, revoked_keys = _match_entries(entries, host, ip, port)

    if port != DEFAULT_PORT and not (host_keys or ca_keys):
        host_keys, ca_keys, revoked_keys = _match_entries(entries, host, ip)

    return host_keys, ca_keys, revoked_keys
