const crypto = require('node:crypto');

function normalizeHex(value) {
  const raw = String(value || '').trim().replace(/^0x/i, '');
  const dotted = raw.replace(/\./g, '');
  return /^[0-9a-fA-F]{64}$/.test(dotted) ? dotted : raw;
}

const apiKey = normalizeHex(process.env.COINSWITCH_API_KEY);
const secretHex = normalizeHex(process.env.COINSWITCH_SECRET_KEY);
const baseUrl = 'https://coinswitch.co';

function fail(message) {
  console.error('CoinSwitch API check: ' + message);
  process.exit(1);
}

if (!/^[0-9a-fA-F]{64}$/.test(apiKey)) fail('API key format invalid (length=' + apiKey.length + ', hex=' + /^[0-9a-fA-F]+$/.test(apiKey) + ').');
if (!/^[0-9a-fA-F]{64}$/.test(secretHex)) fail('Secret key format invalid (length=' + secretHex.length + ', hex=' + /^[0-9a-fA-F]+$/.test(secretHex) + ').');

const seed = Buffer.from(secretHex, 'hex');
const der = Buffer.concat([
  Buffer.from('302e020100300506032b657004220420', 'hex'),
  seed,
]);
const privateKey = crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' });

function signedHeaders(path) {
  const epoch = String(Date.now());
  const message = 'GET' + path + epoch;
  const signature = crypto.sign(null, Buffer.from(message, 'utf8'), privateKey).toString('hex');
  return {
    'Content-Type': 'application/json',
    'X-AUTH-APIKEY': apiKey,
    'X-AUTH-SIGNATURE': signature,
    'X-AUTH-EPOCH': epoch,
  };
}

async function getJson(path, authenticated = false) {
  const response = await fetch(baseUrl + path, {
    method: 'GET',
    headers: authenticated ? signedHeaders(path) : { 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(15000),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const safeMessage = typeof body.message === 'string' ? body.message : ('HTTP ' + response.status);
    fail('GET ' + path.split('?')[0] + ' failed (' + response.status + '): ' + safeMessage);
  }
  return body;
}

async function main() {
  // Official CoinSwitch Quickstart test 1: public server clock.
  const timeBody = await getJson('/trade/api/v2/time');
  if (!timeBody.serverTime) fail('server-time response did not contain serverTime.');
  console.log('1/4 Server time: OK.');

  // Official Quickstart test 2: authenticate the API key/signature.
  const validation = await getJson('/trade/api/v2/validate/keys', true);
  if (validation.message !== 'Valid Access') fail('API authentication response was not Valid Access.');
  console.log('2/4 API authentication: VALID ACCESS.');

  // Official Quickstart test 3: read open Spot orders. This never creates an order.
  const ordersBody = await getJson('/trade/api/v2/orders?open=true&exchanges=coinswitchx', true);
  if (!ordersBody.data || !Array.isArray(ordersBody.data.orders)) {
    fail('open Spot orders response did not contain data.orders.');
  }
  console.log('3/4 Spot open-orders read: OK (' + ordersBody.data.orders.length + ' open order(s)).');

  // Additional read-only Futures market availability check.
  const futuresPath = '/trade/api/v2/futures/instrument_info?exchange=EXCHANGE_2';
  const futuresBody = await getJson(futuresPath, true);
  if (!futuresBody.data || typeof futuresBody.data !== 'object') {
    fail('Futures v2 read-only response did not contain market data.');
  }
  const requested = ['BTC', 'ETH', 'BNB', 'ZEC'];
  const available = requested.filter((coin) => futuresBody.data[coin + 'USDT']?.status === 'TRADING');
  const unavailable = requested.filter((coin) => !available.includes(coin));
  console.log('4/4 Futures v2 READ-ONLY access: OK. No orders were placed.');
  console.log('Futures markets confirmed TRADING: ' + (available.join(', ') || 'none'));
  if (unavailable.length) console.log('Futures markets not confirmed TRADING: ' + unavailable.join(', '));
  console.log('CoinSwitch API read-only checks completed. No trades or cancellations were submitted.');
}

main().catch((error) => fail(error.message || 'request failed'));
