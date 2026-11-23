const crypto = require('node:crypto');

// GitHub Secrets may contain accidental surrounding whitespace or a copied 0x prefix.
// Normalize only these harmless formatting differences; never print credential values.
function normalizeHex(value) {
  const raw = String(value || '').trim().replace(/^0x/i, '');
  // Some copied key displays may group hex characters with dots.
  // Remove dots only when the remaining value is exactly a valid 32-byte hex key.
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

async function main() {
  const epoch = String(Date.now());
  const path = '/trade/api/v2/validate/keys';
  const message = 'GET' + path + epoch;
  const seed = Buffer.from(secretHex, 'hex');
  const der = Buffer.concat([
    Buffer.from('302e020100300506032b657004220420', 'hex'),
    seed,
  ]);
  const privateKey = crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' });
  const signature = crypto.sign(null, Buffer.from(message, 'utf8'), privateKey).toString('hex');

  const response = await fetch(baseUrl + path, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-AUTH-APIKEY': apiKey,
      'X-AUTH-SIGNATURE': signature,
      'X-AUTH-EPOCH': epoch,
    },
    signal: AbortSignal.timeout(15000),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const safeMessage = typeof body.message === 'string' ? body.message : ('HTTP ' + response.status);
    fail('authentication rejected (' + response.status + '): ' + safeMessage);
  }
  if (body.message !== 'Valid Access') {
    fail('unexpected validation response; check CoinSwitch API response in Actions log.');
  }
  console.log('CoinSwitch Spot API check: VALID ACCESS.');

  // Verify Futures v2 read-only access and discover which requested markets are enabled.
  const futuresPath = '/trade/api/v2/futures/instrument_info?exchange=EXCHANGE_2';
  const futuresEpoch = String(Date.now());
  const futuresMessage = 'GET' + futuresPath + futuresEpoch;
  const futuresSignature = crypto.sign(null, Buffer.from(futuresMessage, 'utf8'), privateKey).toString('hex');
  const futuresResponse = await fetch(baseUrl + futuresPath, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-AUTH-APIKEY': apiKey,
      'X-AUTH-SIGNATURE': futuresSignature,
      'X-AUTH-EPOCH': futuresEpoch,
    },
    signal: AbortSignal.timeout(15000),
  });
  const futuresBody = await futuresResponse.json().catch(() => ({}));
  if (!futuresResponse.ok || !futuresBody.data || typeof futuresBody.data !== 'object') {
    const safeMessage = typeof futuresBody.message === 'string' ? futuresBody.message : ('HTTP ' + futuresResponse.status);
    fail('Futures v2 read-only access not confirmed (' + futuresResponse.status + '): ' + safeMessage);
  }
  const requested = ['BTC', 'ETH', 'BNB', 'ZEC'];
  const available = requested.filter((coin) => futuresBody.data[coin + 'USDT']?.status === 'TRADING');
  const unavailable = requested.filter((coin) => !available.includes(coin));
  console.log('CoinSwitch Futures v2 READ-ONLY access: VALID. No orders were placed.');
  console.log('Requested markets available: ' + (available.join(', ') || 'none confirmed'));
  if (unavailable.length) console.log('Markets not confirmed as TRADING: ' + unavailable.join(', '));
}

main().catch((error) => fail(error.message || 'request failed'));
