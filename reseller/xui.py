"""Adapter for MHSanaei/3x-ui v3.8.5, commit 7ef22f94c950ff09f0870e2295fa65ad5968742c.

No mutation is retried here. Even a success:false response can have partial effects.
"""
import json
import threading
from urllib.parse import quote
import httpx


class RemoteError(Exception):
    def __init__(self, code="remote_unavailable"):
        self.code = code
        super().__init__(code)


class XUI:
    def __init__(self, config, transport=None):
        self.config = config
        self.lock = threading.RLock()
        self.logged_in = False
        self.version_verified = False
        self.client = httpx.Client(base_url=config.xui_url.rstrip('/')+'/',
                                  timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
                                  verify=True, follow_redirects=False, trust_env=False, transport=transport)
        self.client.headers['X-Requested-With'] = 'XMLHttpRequest'
        if config.xui_token:
            self.client.headers['Authorization'] = 'Bearer '+config.xui_token

    def close(self):
        self.client.close()

    def _call(self, method, path, **kwargs):
        try:
            with self.client.stream(method, path, **kwargs) as response:
                if response.status_code in (401, 403):
                    self.logged_in = False
                    raise RemoteError('remote_auth')
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 16*1024*1024:
                        raise RemoteError('remote_schema')
            envelope = json.loads(data)
            if not isinstance(envelope, dict) or envelope.get('success') is not True:
                raise RemoteError('remote_rejected')
            return envelope.get('obj')
        except (httpx.HTTPError, ValueError) as exc:
            raise RemoteError() from None

    def call(self, method, path, **kwargs):
        with self.lock:
            if not self.config.xui_url:
                raise RemoteError('remote_unconfigured')
            if not self.config.xui_token and not self.logged_in:
                token = self._call('GET', 'csrf-token')
                self.client.headers['X-CSRF-Token'] = token
                self._call('POST', 'login', data={'username': self.config.xui_username, 'password': self.config.xui_password})
                self.client.headers['X-CSRF-Token'] = self._call('GET', 'csrf-token')
                self.logged_in = True
            if not self.version_verified:
                status=self._call('GET','panel/api/server/status')
                if not isinstance(status,dict) or str(status.get('panelVersion','')).lstrip('v')!='3.8.5':
                    raise RemoteError('remote_version')
                self.version_verified=True
            return self._call(method, 'panel/api/'+path, **kwargs)

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
                        row[field] = json.loads(row[field])
                    except ValueError:
                        raise RemoteError('remote_schema') from None
        return rows

    def clients(self):
        rows = self.call('GET', 'clients/list')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), int) or not isinstance(r.get('email'), str) or not isinstance(r.get('inboundIds'), list) for r in rows):
            raise RemoteError('remote_schema')
        if len({r['email'] for r in rows}) != len(rows):
            raise RemoteError('remote_schema')
        return rows

    def create(self, payload, ids):
        return self.call('POST', 'clients/add', json={'client': payload, 'inboundIds': ids})

    def update(self, email, payload):
        return self.call('POST', 'clients/update/'+quote(email, safe=''), json=payload)

    def delete(self, email):
        return self.call('POST', 'clients/del/'+quote(email, safe=''))

    def attach(self, email, ids):
        return self.call('POST', 'clients/'+quote(email, safe='')+'/attach', json={'inboundIds': ids})

    def detach(self, email, ids):
        return self.call('POST', 'clients/'+quote(email, safe='')+'/detach', json={'inboundIds': ids})

    def online(self):
        data = self.call('POST', 'clients/onlines')
        if data is None:
            return []
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise RemoteError('remote_schema')
        return data

    def links(self, email):
        headers = {'Host': self.config.link_host} if self.config.link_host else {}
        links = self.call('GET', 'clients/links/'+quote(email, safe=''), headers=headers) or []
        if not isinstance(links,list) or not all(isinstance(x, str) for x in links):
            raise RemoteError('remote_schema')
        return links
