"""Name connection links by the inbound they belong to.

3X-UI returns a customer's links as one flat list (grouped by inbound, in
order). Each link is matched to the customer's inbound with the same protocol,
transport and path; when several inbounds look alike, the order decides.
"""
import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit

SCHEMES = {'vless': 'vless', 'vmess': 'vmess', 'trojan': 'trojan', 'ss': 'shadowsocks', 'hysteria2': 'hysteria',
           'hy2': 'hysteria', 'hysteria': 'hysteria', 'tuic': 'tuic', 'wireguard': 'wireguard', 'wg': 'wireguard',
           'tg': 'mtproto'}


def parse_link(uri):
    """Protocol, network, path, server and port of a share link (best effort)."""
    info = {'protocol': '', 'network': '', 'path': '', 'server': '', 'port': None, 'remark': ''}
    scheme = uri.split('://', 1)[0].lower() if '://' in uri else ''
    info['protocol'] = SCHEMES.get(scheme, scheme)
    if scheme == 'vmess':
        try:
            body = uri.split('://', 1)[1].split('#', 1)[0]
            data = json.loads(base64.b64decode(body + '=' * (-len(body) % 4)).decode('utf-8', 'replace'))
            info.update(network=str(data.get('net', '')), path=str(data.get('path', '')), server=str(data.get('add', '')),
                        remark=str(data.get('ps', '')))
            info['port'] = int(data['port']) if str(data.get('port', '')).isdigit() else None
        except (ValueError, IndexError, KeyError):
            pass
        return info
    try:
        parts = urlsplit(uri)
        query = parse_qs(parts.query)
        info['network'] = query.get('type', [''])[0]
        info['path'] = unquote(query.get('path', query.get('serviceName', ['']))[0])
        info['server'] = parts.hostname or ''
        info['port'] = parts.port
        info['remark'] = unquote(parts.fragment)
    except ValueError:
        pass
    return info


def _stream(inbound):
    try:
        meta = json.loads(inbound.get('metadata') or '{}')
    except ValueError:
        meta = {}
    stream = meta.get('streamSettings') or {}
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except ValueError:
            stream = {}
    network = stream.get('network', '') if isinstance(stream, dict) else ''
    path = ''
    if isinstance(stream, dict):
        for key in ('wsSettings', 'httpupgradeSettings', 'xhttpSettings', 'splithttpSettings'):
            if isinstance(stream.get(key), dict) and stream[key].get('path'):
                path = stream[key]['path']
        if isinstance(stream.get('grpcSettings'), dict) and stream['grpcSettings'].get('serviceName'):
            path = stream['grpcSettings']['serviceName']
    return network, path


def label_links(uris, inbounds, email):
    """[{uri, title, server, inbound_id}] with titles like "Inbound name · email"."""
    candidates = []
    for ib in inbounds:
        network, path = _stream(ib)
        candidates.append({'id': ib['id'], 'name': ib.get('name') or f"Inbound #{ib['id']}", 'protocol': ib.get('protocol', ''),
                           'network': network, 'path': path, 'port': ib.get('port')})
    result, previous, counts = [], None, {}
    for uri in uris:
        info = parse_link(uri)
        best, best_score = None, -1
        for c in candidates:
            if c['protocol'] and info['protocol'] and c['protocol'] != info['protocol']:
                continue
            score = 0
            score += 2 if info['network'] and c['network'] == info['network'] else 0
            score += 2 if info['path'] and c['path'] == info['path'] else 0
            score += 1 if info['port'] and c['port'] == info['port'] else 0
            # Links arrive grouped by inbound: on a tie, stay with the previous link's inbound.
            score += 0.5 if previous is c else 0
            if score > best_score:
                best, best_score = c, score
        if best is None and len(candidates) == 1:
            best = candidates[0]
        previous = best or previous
        name = best['name'] if best else (info['remark'] or info['protocol'].upper() or 'Config')
        counts[name] = counts.get(name, 0) + 1
        server = f"{info['server']}:{info['port']}" if info['server'] and info['port'] else info['server']
        result.append({'uri': uri, 'name': name, 'server': server, 'inbound_id': best['id'] if best else None,
                       'remark': info['remark']})
    seen = {}
    for item in result:
        number = ''
        if counts[item['name']] > 1:
            seen[item['name']] = seen.get(item['name'], 0) + 1
            number = f" ({seen[item['name']]})"
        item['title'] = f"{item['name']}{number} · {email}"
    return result
