"""Connection to a 3X-UI panel (MHSanaei/3x-ui), any version.

Relay does not pin a 3X-UI version. On first contact it detects which API the
panel offers and uses it:

* "client API" (3X-UI 3.1.0 and newer): central client records under
  /panel/api/clients/*. Verified unchanged in the parts Relay uses from 3.1.0
  through 3.11.0; newer versions only add fields.
* "classic API" (3X-UI 2.x and 3.0.x): clients live inside each inbound and are
  managed with /panel/api/inbounds/addClient, updateClient and delClient.
  These panels allow an email on one inbound only, so classic customers get
  exactly one inbound.

Detection is by capability, not by version number, so future 3X-UI releases
keep working as long as these endpoints exist. Responses are still checked for
the fields Relay needs; anything unexpected stops with "remote_schema" instead
of guessing. No mutation is retried here: even a success:false response can
have partial effects.
"""
import base64
import json
import secrets
import threading
from urllib.parse import quote
import httpx

from .domain_constants import PROTOCOLS


class RemoteError(Exception):
    def __init__(self, code="remote_unavailable"):
        self.code = code
        super().__init__(code)


CLIENT_API = 'clients'
CLASSIC_API = 'classic'
# Protocols whose clients live in settings.clients on classic panels.
CLASSIC_PROTOCOLS = {'vmess', 'vless', 'trojan', 'shadowsocks', 'hysteria', 'hysteria2'}
# The setting inside a classic inbound client that identifies it in API paths.
CLASSIC_KEY = {'trojan': 'password', 'shadowsocks': 'email', 'hysteria': 'auth', 'hysteria2': 'auth'}


def shadowsocks_password(method):
    """A password that fits the inbound's cipher (2022 ciphers need a base64 key of a fixed size)."""
    method = str(method or '')
    if method.startswith('2022-blake3-'):
        size = 16 if 'aes-128' in method else 32
        return base64.b64encode(secrets.token_bytes(size)).decode()
    return secrets.token_urlsafe(16)


class XUI:
    def __init__(self, config, transport=None):
        self.config = config
        self.lock = threading.RLock()
        self.logged_in = False
        self.mode = None            # CLIENT_API or CLASSIC_API once detected
        self.panel_version = ''     # as reported by the panel, when it reports one
        self._removed = {}          # classic: last settings of clients just removed, for a move
        self.client = httpx.Client(base_url=config.xui_url.rstrip('/')+'/',
                                  timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
                                  verify=True, follow_redirects=False, trust_env=False, transport=transport)
        self.client.headers['X-Requested-With'] = 'XMLHttpRequest'
        if config.xui_token:
            self.client.headers['Authorization'] = 'Bearer '+config.xui_token

    # ----- What the rest of Relay asks about the panel -------------------------

    @property
    def protocols(self):
        return CLASSIC_PROTOCOLS if self.mode == CLASSIC_API else PROTOCOLS

    @property
    def single_inbound(self):
        """Classic panels reject the same email on two inbounds."""
        return self.mode == CLASSIC_API

    @property
    def detach_first(self):
        """Moving a classic client means removing it before adding it elsewhere."""
        return self.mode == CLASSIC_API

    def info(self):
        return {'mode': self.mode or '', 'version': self.panel_version}

    def close(self):
        self.client.close()

    # ----- Transport -----------------------------------------------------------

    def _call(self, method, path, **kwargs):
        try:
            with self.client.stream(method, path, **kwargs) as response:
                if response.status_code in (401, 403):
                    self.logged_in = False
                    raise RemoteError('remote_auth')
                if response.status_code == 404:
                    raise RemoteError('remote_not_found')
                if 300 <= response.status_code < 400:
                    # Panels redirect unauthenticated requests to the login page.
                    self.logged_in = False
                    raise RemoteError('remote_auth')
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 32*1024*1024:
                        raise RemoteError('remote_schema')
            envelope = json.loads(data)
            if not isinstance(envelope, dict) or envelope.get('success') is not True:
                raise RemoteError('remote_rejected')
            return envelope.get('obj')
        except (httpx.HTTPError, ValueError):
            raise RemoteError() from None

    def _login(self):
        # Newer panels require a CSRF token for the login form; older ones have no such endpoint.
        try:
            token = self._call('GET', 'csrf-token')
        except RemoteError as exc:
            if exc.code not in ('remote_not_found', 'remote_rejected', 'remote_unavailable'):
                raise
            token = None
        if isinstance(token, str) and token:
            self.client.headers['X-CSRF-Token'] = token
        self._call('POST', 'login', data={'username': self.config.xui_username, 'password': self.config.xui_password})
        try:
            token = self._call('GET', 'csrf-token')
            if isinstance(token, str) and token:
                self.client.headers['X-CSRF-Token'] = token
        except RemoteError:
            pass
        self.logged_in = True

    def _detect(self):
        """Find out which API this panel has. Called once, and again after a reconnect."""
        try:
            status = self._call('GET', 'panel/api/server/status')
            if isinstance(status, dict) and status.get('panelVersion'):
                self.panel_version = str(status['panelVersion']).lstrip('v')
        except RemoteError as exc:
            if exc.code == 'remote_auth':
                raise
        try:
            rows = self._call('GET', 'panel/api/clients/list')
            if isinstance(rows, list):
                self.mode = CLIENT_API
                return
        except RemoteError as exc:
            if exc.code not in ('remote_not_found', 'remote_rejected'):
                raise
        rows = self._call('GET', 'panel/api/inbounds/list')
        if not isinstance(rows, list):
            raise RemoteError('remote_schema')
        self.mode = CLASSIC_API

    def call(self, method, path, **kwargs):
        with self.lock:
            if not self.config.xui_url:
                raise RemoteError('remote_unconfigured')
            if not self.config.xui_token and not self.logged_in:
                self._login()
            if not self.mode:
                self._detect()
            try:
                return self._call(method, 'panel/api/'+path, **kwargs)
            except RemoteError as exc:
                if exc.code == 'remote_auth' and not self.config.xui_token:
                    # Session expired: sign in again once and repeat a read.
                    self._login()
                    if method == 'GET':
                        return self._call(method, 'panel/api/'+path, **kwargs)
                if exc.code == 'remote_not_found':
                    # The panel was upgraded or downgraded; detect again next time.
                    self.mode = None
                raise

    def _ensure_mode(self):
        if not self.mode:
            with self.lock:
                if not self.config.xui_url:
                    raise RemoteError('remote_unconfigured')
                if not self.config.xui_token and not self.logged_in:
                    self._login()
                if not self.mode:
                    self._detect()
        return self.mode

    # ----- Reads ---------------------------------------------------------------

    def inbounds(self):
        rows = self.call('GET', 'inbounds/list')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), int) or 'protocol' not in r for r in rows):
            raise RemoteError('remote_schema')
        if len({r['id'] for r in rows}) != len(rows):
            raise RemoteError('remote_schema')
        for row in rows:
            for field in ('settings', 'streamSettings'):
                if isinstance(row.get(field), str):
                    try:
                        row[field] = json.loads(row[field]) if row[field] else {}
                    except ValueError:
                        raise RemoteError('remote_schema') from None
        return rows

    def clients(self):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_clients(self.inbounds())
        rows = self.call('GET', 'clients/list')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), int) or not isinstance(r.get('email'), str) or not isinstance(r.get('inboundIds'), list) for r in rows):
            raise RemoteError('remote_schema')
        if len({r['email'] for r in rows}) != len(rows):
            raise RemoteError('remote_schema')
        return rows

    def online(self):
        path = 'inbounds/onlines' if self._ensure_mode() == CLASSIC_API else 'clients/onlines'
        data = self.call('POST', path)
        if data is None:
            return []
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise RemoteError('remote_schema')
        return data

    def links(self, email):
        if self._ensure_mode() == CLASSIC_API:
            return []  # classic panels have no link API; the subscription link still works
        headers = {'Host': self.config.link_host} if self.config.link_host else {}
        links = self.call('GET', 'clients/links/'+quote(email, safe=''), headers=headers) or []
        if not isinstance(links,list) or not all(isinstance(x, str) for x in links):
            raise RemoteError('remote_schema')
        return links

    # ----- Writes --------------------------------------------------------------

    def create(self, payload, ids):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_create(payload, ids)
        return self.call('POST', 'clients/add', json={'client': payload, 'inboundIds': ids})

    def update(self, email, payload):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_update(email, payload)
        return self.call('POST', 'clients/update/'+quote(email, safe=''), json=payload)

    def delete(self, email):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_remove(email, None)
        return self.call('POST', 'clients/del/'+quote(email, safe=''))

    def attach(self, email, ids):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_attach(email, ids)
        return self.call('POST', 'clients/'+quote(email, safe='')+'/attach', json={'inboundIds': ids})

    def detach(self, email, ids):
        if self._ensure_mode() == CLASSIC_API:
            return self._classic_remove(email, set(ids))
        return self.call('POST', 'clients/'+quote(email, safe='')+'/detach', json={'inboundIds': ids})

    # ----- Classic API (3X-UI 2.x and 3.0.x) ----------------------------------

    @staticmethod
    def _classic_clients(inbounds):
        """Build client-API-shaped records from the clients stored inside each inbound."""
        stats, records = {}, {}
        for ib in inbounds:
            for stat in ib.get('clientStats') or []:
                if isinstance(stat, dict) and isinstance(stat.get('email'), str):
                    stats.setdefault(stat['email'], stat)
            clients = (ib.get('settings') or {}).get('clients') if isinstance(ib.get('settings'), dict) else None
            for client in clients or []:
                email = client.get('email') if isinstance(client, dict) else None
                if not isinstance(email, str) or not email:
                    continue
                record = records.get(email)
                if record is None:
                    record = dict(client)
                    record['uuid'] = client.get('id', '') if ib.get('protocol') in ('vmess', 'vless') else ''
                    record['inboundIds'] = []
                    records[email] = record
                record['inboundIds'].append(ib['id'])
        result = []
        for email, record in records.items():
            stat = stats.get(email) or {}
            # Classic panels have no client record id (the traffic row is recreated when a
            # client moves), so identity rests on email, subscription ID and UUID.
            record['id'] = None
            if stat:
                record['traffic'] = {'up': int(stat.get('up') or 0), 'down': int(stat.get('down') or 0), 'total': int(stat.get('total') or 0)}
                if (record.get('expiryTime') or 0) < 0 and (stat.get('expiryTime') or 0) > 0:
                    record['expiryTime'] = stat['expiryTime']  # first-use package already started
            result.append(record)
        return result

    def _classic_inbound(self, inbounds, iid):
        ib = next((i for i in inbounds if i['id'] == iid), None)
        if not ib:
            raise RemoteError('remote_not_found')
        if ib.get('protocol') not in CLASSIC_PROTOCOLS:
            raise RemoteError('remote_rejected')
        return ib

    @staticmethod
    def _classic_client(payload, ib, base=None):
        client = dict(base or {})
        client.update({k: v for k, v in payload.items() if v is not None})
        protocol = ib.get('protocol')
        if protocol == 'shadowsocks' and not client.get('password'):
            client['password'] = shadowsocks_password((ib.get('settings') or {}).get('method'))
        if protocol in ('trojan',) and not client.get('password'):
            client['password'] = secrets.token_urlsafe(24)
        if protocol in ('hysteria', 'hysteria2') and not client.get('auth'):
            client['auth'] = secrets.token_urlsafe(24)
        client.setdefault('flow', '')
        client.setdefault('limitIp', 0)
        client.setdefault('tgId', 0)
        return client

    def _classic_post_client(self, path, iid, client):
        return self.call('POST', path, data={'id': str(iid), 'settings': json.dumps({'clients': [client]})})

    def _classic_create(self, payload, ids):
        inbounds = self.inbounds()
        for iid in ids:
            ib = self._classic_inbound(inbounds, iid)
            self._classic_post_client('inbounds/addClient', iid, self._classic_client(payload, ib))
        return None

    def _classic_update(self, email, payload):
        inbounds = self.inbounds()
        found = False
        for ib in inbounds:
            for old in (ib.get('settings') or {}).get('clients') or []:
                if not isinstance(old, dict) or old.get('email') != email:
                    continue
                found = True
                key = CLASSIC_KEY.get(ib.get('protocol'), 'id')
                client = self._classic_client(payload, ib, base=old)
                client['email'] = email
                self._classic_post_client('inbounds/updateClient/'+quote(str(old.get(key, '')), safe=''), ib['id'], client)
        if not found:
            raise RemoteError('remote_not_found')
        return None

    def _classic_attach(self, email, ids):
        inbounds = self.inbounds()
        source = next((c for ib in inbounds for c in (ib.get('settings') or {}).get('clients') or []
                       if isinstance(c, dict) and c.get('email') == email), None) or self._removed.get(email)
        for iid in ids:
            ib = self._classic_inbound(inbounds, iid)
            self._classic_post_client('inbounds/addClient', iid, self._classic_client(source or {'email': email}, ib))
        return None

    def _classic_remove(self, email, only_ids):
        """Remove a client from all (or the given) inbounds."""
        for ib in self.inbounds():
            if only_ids is not None and ib['id'] not in only_ids:
                continue
            clients = (ib.get('settings') or {}).get('clients') or []
            target = next((c for c in clients if isinstance(c, dict) and c.get('email') == email), None)
            if not target:
                continue
            self._removed[email] = dict(target)
            if len(clients) > 1:
                key = CLASSIC_KEY.get(ib.get('protocol'), 'id')
                self.call('POST', f"inbounds/{ib['id']}/delClient/"+quote(str(target.get(key, '')), safe=''))
            else:
                # Classic panels refuse to delete an inbound's last client; save the inbound without it.
                self._classic_save_inbound(ib, [])
        return None

    def _classic_save_inbound(self, ib, clients):
        settings = dict(ib.get('settings') or {})
        settings['clients'] = clients
        form = {}
        for key, value in ib.items():
            if key in ('clientStats',) or value is None:
                continue
            if key == 'settings':
                value = settings
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif isinstance(value, bool):
                value = 'true' if value else 'false'
            form[key] = str(value)
        return self.call('POST', f"inbounds/update/{ib['id']}", data=form)
