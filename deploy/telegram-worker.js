// Cloudflare Worker that forwards Relay's Telegram calls to api.telegram.org.
// Use it when Telegram is blocked on your server. Only your own bot can use it.
// Setup: docs/TELEGRAM.md, "Server in Iran or another blocked country".

const BOT_ID = '1234567890'; // the number before ':' in your bot token

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/bot' + BOT_ID + ':')) {
      return new Response('Not found', { status: 404 });
    }
    url.protocol = 'https:';
    url.hostname = 'api.telegram.org';
    url.port = '';
    return fetch(new Request(url, request));
  },
};
